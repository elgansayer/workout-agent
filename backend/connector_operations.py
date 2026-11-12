"""Tenant-scoped, persisted state of a connector's live operations.

Account readiness has to say whether Hevy is being connected, is mid-sync, or
failed its last sync.  None of that can be reconstructed from imported rows, so
the web routes that connect and sync Hevy record it here, in the tenant-scoped
``hevy_meta`` key/value store:

* an *operation lease* while a connect or sync is running.  The lease expires
  after :data:`OPERATION_LEASE`, so a crashed worker can never leave an account
  "syncing" forever; an expired lease is reported as stalled instead.  The lease
  is also a per-tenant lock: a second sync cannot start while one is running,
  which keeps two history rebuilds from interleaving.
* the *outcome of the last sync*: succeeded or failed, when, how many records,
  and a fixed failure code.  Provider error text is never stored.

Every read and write is scoped to the owning ``user_id``; nothing here can see
another tenant's state.  Credentials are never stored or returned.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from database import (
    DEFAULT_DB_PATH,
    get_meta,
    require_tenant,
    set_meta,
    transaction,
)

logger = logging.getLogger(__name__)

#: How long a connect or sync may hold the lease before it is reported as stalled.
OPERATION_LEASE = timedelta(minutes=15)

DEFAULT_PROVIDER = "hevy"

#: Failure code recorded when a sync raised something unexpected.
UNEXPECTED_ERROR = "unexpected_error"

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")
_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")


class OperationPhase(StrEnum):
    """A live connector operation."""

    CONNECTING = "connecting"
    SYNCING = "syncing"


class SyncOutcome(StrEnum):
    """How the most recent sync ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationBusyError(RuntimeError):
    """Another connect or sync already holds this tenant's lease."""

    def __init__(self, phase: OperationPhase) -> None:
        super().__init__(f"A {phase.value} operation is already in progress.")
        self.phase = phase


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    """What a tenant's connector is doing now and how it last ended."""

    provider: str = DEFAULT_PROVIDER
    operation: OperationPhase | None = None
    operation_started_at: datetime | None = None
    operation_stalled: bool = False
    last_sync_outcome: SyncOutcome | None = None
    last_sync_at: datetime | None = None
    last_sync_code: str | None = None
    last_sync_records: int | None = None

    @property
    def last_sync_succeeded(self) -> bool:
        return self.last_sync_outcome is SyncOutcome.SUCCEEDED

    @property
    def last_sync_failed(self) -> bool:
        return self.last_sync_outcome is SyncOutcome.FAILED

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the browser; contains no credential or provider text."""

        last_sync: dict[str, Any] | None = None
        if self.last_sync_outcome is not None:
            last_sync = {
                "outcome": self.last_sync_outcome.value,
                "at": self.last_sync_at.isoformat() if self.last_sync_at else None,
                "code": self.last_sync_code,
                "records": self.last_sync_records,
            }
        return {
            "provider": self.provider,
            "operation": self.operation.value if self.operation else None,
            "operation_started_at": (
                self.operation_started_at.isoformat()
                if self.operation_started_at
                else None
            ),
            "operation_stalled": self.operation_stalled,
            "last_sync": last_sync,
        }


@dataclass(slots=True)
class OperationTracker:
    """Collects the outcome of a tracked sync so it can be persisted."""

    outcome: SyncOutcome | None = None
    code: str | None = None
    records: int | None = None

    def succeeded(self, *, records: int | None = None) -> None:
        self.outcome = SyncOutcome.SUCCEEDED
        self.code = None
        self.records = records

    def failed(self, code: str) -> None:
        self.outcome = SyncOutcome.FAILED
        self.code = code
        self.records = None


def _utc(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(tz=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _provider(provider: str) -> str:
    cleaned = provider.strip().lower()
    if not _PROVIDER_PATTERN.fullmatch(cleaned):
        raise ValueError("Unknown connector provider.")
    return cleaned


def _operation_key(provider: str) -> str:
    return f"connector:{_provider(provider)}:operation"


def _last_sync_key(provider: str) -> str:
    return f"connector:{_provider(provider)}:last_sync"


def _parse_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _parse_operation(raw: str | None) -> tuple[OperationPhase, datetime] | None:
    """Return the recorded operation, or ``None`` when it is absent or malformed."""

    payload = _load_json(raw)
    if payload is None:
        return None
    try:
        phase = OperationPhase(str(payload.get("phase")))
    except ValueError:
        return None
    started_at = _parse_moment(payload.get("started_at"))
    if started_at is None:
        return None
    return phase, started_at


def _safe_code(value: Any) -> str | None:
    if isinstance(value, str) and _CODE_PATTERN.fullmatch(value):
        return value
    return None


def _safe_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def begin_operation(
    user_id: str,
    phase: OperationPhase,
    *,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> str:
    """Take this tenant's connector lease, or raise :class:`OperationBusyError`.

    Returns the lease token to hand to :func:`finish_operation`.  The check and
    the write happen in one ``BEGIN IMMEDIATE`` transaction so two workers
    cannot both acquire the lease.
    """

    uid = require_tenant(user_id, db_path)
    moment = _utc(now)
    key = _operation_key(provider)
    token = moment.isoformat()
    payload = json.dumps({"phase": phase.value, "started_at": token})
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM hevy_meta WHERE user_id = ? AND key = ?",
            (uid, key),
        ).fetchone()
        live = _parse_operation(row[0] if row else None)
        if live is not None and moment - live[1] < OPERATION_LEASE:
            raise OperationBusyError(live[0])
        conn.execute(
            """
            INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (uid, key, payload),
        )
    return token


def finish_operation(
    user_id: str,
    token: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Release the lease taken with *token*.

    A lease that expired and was re-taken by a newer operation is left alone, so
    a slow worker cannot clear someone else's marker.
    """

    uid = require_tenant(user_id, db_path)
    key = _operation_key(provider)
    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM hevy_meta WHERE user_id = ? AND key = ?",
            (uid, key),
        ).fetchone()
        payload = _load_json(row[0] if row else None)
        if payload is None or payload.get("started_at") != token:
            return
        conn.execute(
            "DELETE FROM hevy_meta WHERE user_id = ? AND key = ?",
            (uid, key),
        )


def record_sync_outcome(
    user_id: str,
    outcome: SyncOutcome,
    *,
    code: str | None = None,
    records: int | None = None,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> None:
    """Persist how this tenant's latest sync ended."""

    payload: dict[str, Any] = {
        "outcome": outcome.value,
        "at": _utc(now).isoformat(),
    }
    if outcome is SyncOutcome.FAILED:
        payload["code"] = _safe_code(code) or "sync_failed"
    else:
        count = _safe_count(records)
        if count is not None:
            payload["records"] = count
    set_meta(
        _last_sync_key(provider),
        json.dumps(payload),
        db_path,
        user_id=user_id,
    )


def clear_sync_outcome(
    user_id: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Forget the last sync outcome, e.g. when the credential was replaced."""

    uid = require_tenant(user_id, db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "DELETE FROM hevy_meta WHERE user_id = ? AND key = ?",
            (uid, _last_sync_key(provider)),
        )


def read_connector_status(
    user_id: str,
    *,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> ConnectorStatus:
    """Read this tenant's connector state.  Malformed records read as absent."""

    moment = _utc(now)
    provider_id = _provider(provider)
    operation = _parse_operation(
        get_meta(_operation_key(provider_id), db_path, user_id=user_id)
    )
    live_phase: OperationPhase | None = None
    started_at: datetime | None = None
    stalled = False
    if operation is not None:
        started_at = operation[1]
        if moment - started_at < OPERATION_LEASE:
            live_phase = operation[0]
        else:
            stalled = True

    last = _load_json(get_meta(_last_sync_key(provider_id), db_path, user_id=user_id))
    outcome: SyncOutcome | None = None
    last_at: datetime | None = None
    code: str | None = None
    records: int | None = None
    if last is not None:
        try:
            outcome = SyncOutcome(str(last.get("outcome")))
        except ValueError:
            outcome = None
        if outcome is not None:
            last_at = _parse_moment(last.get("at"))
            code = _safe_code(last.get("code"))
            records = _safe_count(last.get("records"))

    return ConnectorStatus(
        provider=provider_id,
        operation=live_phase,
        operation_started_at=started_at,
        operation_stalled=stalled,
        last_sync_outcome=outcome,
        last_sync_at=last_at,
        last_sync_code=code,
        last_sync_records=records,
    )


@contextmanager
def tracked_operation(
    user_id: str,
    phase: OperationPhase,
    *,
    provider: str = DEFAULT_PROVIDER,
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> Iterator[OperationTracker]:
    """Hold the lease for the duration of a connect or sync.

    A sync records its outcome when the block ends: whatever the caller marked
    on the tracker, or :data:`UNEXPECTED_ERROR` when the block raised before it
    marked anything.  A block that ends without marking an outcome records
    nothing, which is how a connect behaves.  Raises
    :class:`OperationBusyError` when the lease is taken.
    """

    token = begin_operation(
        user_id, phase, provider=provider, db_path=db_path, now=now
    )
    tracker = OperationTracker()
    try:
        yield tracker
    except BaseException:
        if tracker.outcome is None:
            tracker.failed(UNEXPECTED_ERROR)
        raise
    finally:
        try:
            if phase is OperationPhase.SYNCING and tracker.outcome is not None:
                record_sync_outcome(
                    user_id,
                    tracker.outcome,
                    code=tracker.code,
                    records=tracker.records,
                    provider=provider,
                    db_path=db_path,
                )
            finish_operation(user_id, token, provider=provider, db_path=db_path)
        except sqlite3.Error:
            # The lease expires on its own, so a bookkeeping failure must never
            # mask the sync's own result or error.
            logger.warning("Could not release the %s operation lease.", provider)


__all__ = [
    "DEFAULT_PROVIDER",
    "OPERATION_LEASE",
    "UNEXPECTED_ERROR",
    "ConnectorStatus",
    "OperationBusyError",
    "OperationPhase",
    "OperationTracker",
    "SyncOutcome",
    "begin_operation",
    "clear_sync_outcome",
    "finish_operation",
    "read_connector_status",
    "record_sync_outcome",
    "tracked_operation",
]
