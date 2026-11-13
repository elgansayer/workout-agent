"""Server-side single use for OAuth state (issue #764).

``connectors.oauth`` proves that a callback belongs to the account, session and
connector that started it and has not expired.  It cannot prove the state is
still unspent: the session cookie is client-held, so an older copy of it, or a
second request racing the first, would still look unused.  This module holds
the one piece of server state that closes the gap, an atomic ledger of states
that were already consumed, and applies it to the web sign-in flow, whose state
Authlib otherwise keeps only in the session cookie.

The ledger stores ``(claim_hash, expires_at)`` and nothing else.  The hash
digests the connector, tenant and state (see :func:`connectors.oauth.claim_key`),
so entries are tenant-scoped without holding an identifier, the state itself
never reaches the database, there is no read or list path, and a row disappears
on its own once the state it guards could no longer verify.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import StarletteIntegration
from connectors.oauth import (
    CLOCK_SKEW_SECONDS,
    STATE_TTL_SECONDS,
    StateLedger,
    claim_key,
)
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 10_000


class SQLiteStateLedger:
    """Atomic ledger of consumed OAuth state in the application database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS oauth_state_claims ("
                "claim_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_oauth_state_claims_expiry "
                "ON oauth_state_claims (expires_at)"
            )
        except BaseException:
            conn.close()
            raise
        return conn

    def claim(self, key: str, *, expires_at: int, now: int) -> bool:
        """Record *key*; return ``True`` for exactly one caller per live key.

        The delete, insert and commit run in one ``BEGIN IMMEDIATE``
        transaction, so concurrent callers (threads or processes) serialise on
        the primary key and only one sees its row inserted.  A claim that is
        already expired is refused: it could not outlive the state it guards,
        so recording it would re-open the replay window.
        """

        if not key or expires_at <= now:
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM oauth_state_claims WHERE expires_at <= ?", (now,))
            cursor = conn.execute(
                "INSERT OR IGNORE INTO oauth_state_claims (claim_hash, expires_at) "
                "VALUES (?, ?)",
                (key, expires_at),
            )
            claimed = cursor.rowcount == 1
            conn.execute("COMMIT")
            return claimed
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def single_use_integration(
    ledger: StateLedger, *, clock: Callable[[], float] = time.time
) -> type[StarletteIntegration]:
    """Return an Authlib integration whose sign-in state is short-lived and single use.

    Authlib keeps the web sign-in state (nonce, PKCE verifier, redirect URI) in
    the session cookie and tags it with an ``exp`` it never compares to the
    clock.  The returned integration

    * refuses and discards a state past the short OAuth TTL, and
    * claims the state in the server-side *ledger* before handing it to Authlib,
      so a second callback is rejected even when it presents an older copy of
      the cookie that still contains the state.

    The claim happens only for a state the caller's own session already holds,
    so anonymous requests cannot fill the ledger with arbitrary values.  Any
    failure, including an unavailable ledger, returns ``None``, which makes
    Authlib reject the callback (fail closed).
    """

    class SingleUseStarletteIntegration(StarletteIntegration):
        expires_in = STATE_TTL_SECONDS

        async def get_state_data(  # type: ignore[override]
            self, session: dict[str, Any] | None, state: str
        ) -> dict[str, Any] | None:
            key = f"_state_{self.name}_{state}"
            entry = session.get(key) if session is not None else None
            if session is None or not isinstance(entry, dict):
                return None
            now = clock()
            try:
                expires_at = float(entry["exp"])
            except (KeyError, TypeError, ValueError):
                expires_at = 0.0
            if expires_at <= now:
                session.pop(key, None)
                return None
            data = await super().get_state_data(session, state)
            if data is None:
                return None
            try:
                claimed = await run_in_threadpool(
                    ledger.claim,
                    claim_key(f"signin:{self.name}", None, state),
                    expires_at=int(expires_at) + CLOCK_SKEW_SECONDS,
                    now=int(now),
                )
            except (sqlite3.Error, OSError):
                logger.exception("OAuth state ledger unavailable; refusing sign-in.")
                return None
            return data if claimed else None

    return SingleUseStarletteIntegration


__all__ = ["SQLiteStateLedger", "single_use_integration"]
