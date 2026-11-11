"""Hardened, revocable browser sessions for the Workout Agent web application.

The Google login produces a signed Starlette session cookie.  A signed cookie on
its own is only a bearer token: it cannot be revoked, it never goes idle, and a
copy lifts as much access as the original until its signature ages out.  This
module adds the missing server-side half without changing what route code sees
(``request.session`` is still the same mapping):

* **Registry.**  Every authenticated cookie carries a random ``sid``.  The
  ``user_sessions`` table (see :func:`database.ensure_user_sessions_schema`)
  stores only the SHA-256 digest of that identifier, the owning ``user_id`` and
  the lifetime/revocation timestamps.  A cookie is honoured only while the
  registry says its session is active *and* owned by the user the cookie claims.
* **Bounded lifetime.**  An absolute limit (from login, never extended) and an
  idle limit (from last use) are enforced on the server against the registry, so
  they hold even when a client keeps a cookie past its ``Max-Age``.
* **Rotation.**  Every login mints a brand-new identifier, revokes the one the
  browser held and discards all pre-login session data (OAuth state included), so
  an identifier planted before login is useless afterwards.
* **Revocation.**  Logout revokes the current session, logout-all revokes every
  session of the authenticated user, and removing an e-mail from the allow-list
  revokes its sessions on their next request.
* **Cookie attributes.**  ``HttpOnly``; ``Secure`` (always in production);
  ``SameSite=Lax`` (the Google callback is a cross-site top-level navigation that
  must carry the OAuth state, so ``Strict`` would break sign-in); ``Path=/`` (the
  application owns its whole origin and every SPA route is authenticated
  server-side); no ``Domain`` attribute, i.e. host-only, so the cookie is never
  shared with sibling subdomains.  When ``Secure`` the cookie uses the
  ``__Host-`` prefix, which makes browsers enforce all of the above.

Everything fails closed: unknown, revoked, expired, mismatched or legacy
(``sid``-less) sessions are emptied before any authentication boundary can see
them, and an unavailable registry yields HTTP 503 rather than access.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeGuard

from database import ensure_user_sessions_schema
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Secret
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from webapp.runtime_security import validate_web_runtime

logger = logging.getLogger(__name__)

SESSION_ID_KEY = "sid"
SESSION_COOKIE_BASENAME = "session"
HOST_COOKIE_PREFIX = "__Host-"

# Defaults keep the previous 14-day ceiling (the old cookie's Max-Age) and add an
# idle limit; neither environment variable can raise the ceiling above 90 days.
DEFAULT_IDLE_TIMEOUT_SECONDS = 3 * 24 * 60 * 60
DEFAULT_ABSOLUTE_TIMEOUT_SECONDS = 14 * 24 * 60 * 60
MINIMUM_TIMEOUT_SECONDS = 60
MAXIMUM_TIMEOUT_SECONDS = 90 * 24 * 60 * 60
# "Last seen" is written at most this often, so idle expiry is accurate to about
# a minute without a database write on every request.  (A short idle limit
# shortens this, see ``SessionLifetimePolicy.touch_interval``.)
LAST_SEEN_RESOLUTION_SECONDS = 60
# Dead rows are kept briefly for incident review, then purged.
DEAD_SESSION_RETENTION_SECONDS = 7 * 24 * 60 * 60

REVOKE_LOGOUT = "logout"
REVOKE_LOGOUT_ALL = "logout_all"
REVOKE_REAUTHENTICATED = "reauthenticated"
REVOKE_ACCESS_REMOVED = "access_removed"

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_MAX_USER_ID_LENGTH = 256
_IDENTITY_KEYS = ("user", "user_id", SESSION_ID_KEY)


class SessionSecurityConfigurationError(RuntimeError):
    """Raised at startup when session limits are malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    """Attributes of the ``Set-Cookie`` header for the session cookie."""

    name: str
    secure: bool
    same_site: Literal["lax", "strict", "none"]
    path: str
    domain: str | None
    max_age: int


@dataclass(frozen=True, slots=True)
class SessionLifetimePolicy:
    """Server-enforced lifetime limits, all in seconds."""

    idle_timeout: int
    absolute_timeout: int
    last_seen_resolution: int = LAST_SEEN_RESOLUTION_SECONDS
    dead_session_retention: int = DEAD_SESSION_RETENTION_SECONDS

    @property
    def touch_interval(self) -> int:
        """How stale ``last_seen_at`` may get before a request refreshes it.

        Normally ``last_seen_resolution``, but never more than a quarter of the
        idle limit.  The stored timestamp lags real activity by up to one
        interval, so with a short idle limit a coarse interval would sign out a
        user who is genuinely active (a 60 second limit and a request every 40
        seconds, for example).
        """

        return max(1, min(self.last_seen_resolution, self.idle_timeout // 4))


@dataclass(frozen=True, slots=True)
class SessionSecurityConfig:
    """Validated cookie and lifetime policy for the web application."""

    cookie: SessionCookiePolicy
    lifetime: SessionLifetimePolicy


def _read_seconds(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()):
        raise SessionSecurityConfigurationError(
            f"{name} must be a whole number of seconds."
        )
    value = int(raw)
    if not MINIMUM_TIMEOUT_SECONDS <= value <= MAXIMUM_TIMEOUT_SECONDS:
        raise SessionSecurityConfigurationError(
            f"{name} must be between {MINIMUM_TIMEOUT_SECONDS} and "
            f"{MAXIMUM_TIMEOUT_SECONDS} seconds."
        )
    return value


def load_session_security_config(
    environ: Mapping[str, str] | None = None,
) -> SessionSecurityConfig:
    """Build the cookie and lifetime policy from the process environment.

    ``Secure`` is derived rather than configurable: it is always on in
    production, and elsewhere follows the scheme of ``WEB_PUBLIC_URL`` so local
    ``http://localhost`` development keeps working.  There is deliberately no
    knob for ``SameSite``, ``Path`` or ``Domain``; see the module docstring.
    """

    source = os.environ if environ is None else environ
    runtime = validate_web_runtime(source)
    public_url = source.get("WEB_PUBLIC_URL", "").strip().lower()
    secure = runtime.production or public_url.startswith("https://")

    idle = _read_seconds(
        source, "SESSION_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS
    )
    absolute = _read_seconds(
        source, "SESSION_ABSOLUTE_TIMEOUT_SECONDS", DEFAULT_ABSOLUTE_TIMEOUT_SECONDS
    )
    if idle > absolute:
        raise SessionSecurityConfigurationError(
            "SESSION_IDLE_TIMEOUT_SECONDS must not exceed "
            "SESSION_ABSOLUTE_TIMEOUT_SECONDS."
        )

    name = SESSION_COOKIE_BASENAME
    if secure:
        name = f"{HOST_COOKIE_PREFIX}{name}"
    return SessionSecurityConfig(
        cookie=SessionCookiePolicy(
            name=name,
            secure=secure,
            same_site="lax",
            path="/",
            domain=None,
            max_age=absolute,
        ),
        lifetime=SessionLifetimePolicy(idle_timeout=idle, absolute_timeout=absolute),
    )


class SessionState(StrEnum):
    """Why a presented session was, or was not, honoured."""

    ACTIVE = "active"
    UNKNOWN = "unknown"
    OWNER_MISMATCH = "owner_mismatch"
    REVOKED = "revoked"
    ABSOLUTE_EXPIRED = "absolute_expired"
    IDLE_EXPIRED = "idle_expired"


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _is_session_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _SESSION_ID_PATTERN.fullmatch(value) is not None


def _is_user_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= _MAX_USER_ID_LENGTH


class SessionStore:
    """Tenant-scoped SQLite registry of browser sessions.

    Every statement is parameterised and every mutation is keyed by both the
    session digest and its owner, so knowing someone else's identifier is not
    enough to revoke (or read) their session.  ``clock`` is injectable so
    lifetime behaviour can be tested deterministically.
    """

    def __init__(
        self,
        db_path: str,
        lifetime: SessionLifetimePolicy,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout: float = 10.0,
    ) -> None:
        self.db_path = db_path
        self.lifetime = lifetime
        self.clock = clock
        self.busy_timeout = busy_timeout

    def _now(self) -> int:
        return int(self.clock())

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path, timeout=self.busy_timeout, isolation_level=None
        )
        try:
            ensure_user_sessions_schema(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self, conn: sqlite3.Connection) -> Iterator[None]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            # SQLite may already have rolled back (e.g. on a full disk); the
            # original error is the one worth surfacing.
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def create(
        self,
        user_id: str,
        *,
        device: str | None = None,
        location: str | None = None,
    ) -> str:
        """Register a new session for ``user_id`` and return its raw identifier.

        The raw identifier is returned exactly once, to be placed in the signed
        cookie; only its digest is persisted. ``device`` and ``location`` are
        coarse display metadata for the account session list.
        """

        if not _is_user_id(user_id):
            raise ValueError("A session must belong to a user.")
        now = self._now()
        session_id = secrets.token_urlsafe(32)
        with self._connection() as conn, self._write_transaction(conn):
            self._purge(conn, now)
            conn.execute(
                "INSERT INTO user_sessions "
                "(session_token_hash, user_id, created_at, last_seen_at, "
                "device, location) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_digest(session_id), user_id, now, now, device, location),
            )
        return session_id

    def check(
        self,
        session_id: object,
        user_id: object,
        *,
        touch: bool = True,
    ) -> SessionState:
        """Return whether ``session_id`` is an active session owned by ``user_id``.

        Anything malformed is simply ``UNKNOWN``.  When the session is active
        and ``touch`` is set, its last-seen time is refreshed (at most once per
        ``SessionLifetimePolicy.touch_interval``) on a best-effort basis.
        """

        if not _is_session_id(session_id) or not _is_user_id(user_id):
            return SessionState.UNKNOWN
        now = self._now()
        digest = _digest(session_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT user_id, created_at, last_seen_at, revoked_at "
                "FROM user_sessions WHERE session_token_hash = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return SessionState.UNKNOWN
            owner, created_at, last_seen_at, revoked_at = row
            if owner != user_id:
                return SessionState.OWNER_MISMATCH
            if revoked_at is not None:
                return SessionState.REVOKED
            if now - created_at >= self.lifetime.absolute_timeout:
                return SessionState.ABSOLUTE_EXPIRED
            if now - last_seen_at >= self.lifetime.idle_timeout:
                return SessionState.IDLE_EXPIRED
            if touch and now - last_seen_at >= self.lifetime.touch_interval:
                try:
                    conn.execute(
                        "UPDATE user_sessions SET last_seen_at = ? "
                        "WHERE session_token_hash = ? AND user_id = ? "
                        "AND revoked_at IS NULL AND last_seen_at < ?",
                        (now, digest, owner, now),
                    )
                except sqlite3.OperationalError:
                    # A busy writer must not turn a valid request into an error;
                    # the next request will refresh the timestamp.
                    logger.debug("Skipped session last-seen refresh: database busy")
        return SessionState.ACTIVE

    def is_active_payload(self, payload: Mapping[str, Any]) -> bool:
        """Registry check for a decoded session payload, without refreshing it."""

        state = self.check(
            payload.get(SESSION_ID_KEY), payload.get("user_id"), touch=False
        )
        return state is SessionState.ACTIVE

    def revoke(self, session_id: object, user_id: object, *, reason: str) -> bool:
        """Revoke one session owned by ``user_id``; ``True`` if it was live."""

        if not _is_session_id(session_id) or not _is_user_id(user_id):
            return False
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE user_sessions SET revoked_at = ?, revoked_reason = ? "
                "WHERE session_token_hash = ? AND user_id = ? AND revoked_at IS NULL",
                (self._now(), reason, _digest(session_id), user_id),
            )
            return cursor.rowcount == 1

    def revoke_all(self, user_id: object, *, reason: str) -> int:
        """Revoke every session of ``user_id``; return how many were still active.

        Rows that already aged out are revoked too, so loosening a timeout later
        can never bring a signed-out device back to life.
        """

        if not _is_user_id(user_id):
            return 0
        now = self._now()
        with self._connection() as conn, self._write_transaction(conn):
            active = conn.execute(
                "SELECT COUNT(*) FROM user_sessions "
                "WHERE user_id = ? AND revoked_at IS NULL "
                "AND created_at > ? AND last_seen_at > ?",
                (
                    user_id,
                    now - self.lifetime.absolute_timeout,
                    now - self.lifetime.idle_timeout,
                ),
            ).fetchone()[0]
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ?, revoked_reason = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, reason, user_id),
            )
        return int(active)

    def _purge(self, conn: sqlite3.Connection, now: int) -> None:
        retention = self.lifetime.dead_session_retention
        conn.execute(
            "DELETE FROM user_sessions "
            "WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) "
            "OR created_at <= ? OR last_seen_at <= ?",
            (
                now - retention,
                now - self.lifetime.absolute_timeout - retention,
                now - self.lifetime.idle_timeout - retention,
            ),
        )


def _claims_identity(session: Mapping[str, Any]) -> bool:
    return any(key in session for key in _IDENTITY_KEYS)


class SessionRegistryGuard:
    """Empty any session the registry does not honour before the app sees it.

    This sits immediately inside the session middleware, which has already
    decoded the signed cookie into ``scope['session']``.  Because it runs before
    the authentication boundary, the mutation guard and every route, those all
    observe a revoked, expired, forged-owner or legacy session as logged out,
    and the session middleware answers with a cookie-deleting ``Set-Cookie``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: SessionStore,
        identity_allowed: Callable[[object], bool] | None = None,
    ) -> None:
        self.app = app
        self.store = store
        self.identity_allowed = identity_allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if isinstance(session, MutableMapping) and _claims_identity(session):
            try:
                accepted = await run_in_threadpool(self._accepts, dict(session))
            except (sqlite3.Error, OSError):
                logger.exception("Session registry unavailable; refusing the request")
                await self._unavailable(scope, receive, send)
                return
            if not accepted:
                session.clear()
        await self.app(scope, receive, send)

    def _accepts(self, claimed: Mapping[str, Any]) -> bool:
        session_id = claimed.get(SESSION_ID_KEY)
        user_id = claimed.get("user_id")
        state = self.store.check(session_id, user_id)
        if state is SessionState.OWNER_MISMATCH:
            logger.warning(
                "Rejected a session cookie whose user does not own its session"
            )
        if state is not SessionState.ACTIVE:
            return False
        if self.identity_allowed is not None and not self.identity_allowed(
            claimed.get("user")
        ):
            self.store.revoke(session_id, user_id, reason=REVOKE_ACCESS_REMOVED)
            return False
        return True

    @staticmethod
    async def _unavailable(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013})
            return
        response = JSONResponse(
            {"detail": "Session service unavailable"},
            status_code=503,
            headers={"Retry-After": "5"},
        )
        await response(scope, receive, send)


class HardenedSessionMiddleware(SessionMiddleware):
    """``SessionMiddleware`` with the hardened cookie policy and a live registry.

    It is a drop-in subclass so the repository's stack hooks (authentication
    boundary, mutation guard) and every ``request.session`` consumer keep
    working unchanged.
    """

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str | Secret,
        *,
        config: SessionSecurityConfig,
        store: SessionStore,
        identity_allowed: Callable[[object], bool] | None = None,
    ) -> None:
        super().__init__(
            SessionRegistryGuard(app, store=store, identity_allowed=identity_allowed),
            secret_key=secret_key,
            session_cookie=config.cookie.name,
            max_age=config.cookie.max_age,
            path=config.cookie.path,
            same_site=config.cookie.same_site,
            https_only=config.cookie.secure,
            domain=config.cookie.domain,
        )
        self.config = config


class SessionLifecycle:
    """Login, logout and logout-all operations over ``request.session``."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def establish(
        self,
        session: MutableMapping[str, Any],
        *,
        user_id: str,
        email: str,
        device: str | None = None,
        location: str | None = None,
    ) -> None:
        """Sign ``user_id`` in on a brand-new session identifier.

        This is the rotation point for every authentication or identity change.
        The new identifier is registered first so a storage failure leaves the
        previous state untouched; then the identifier this browser held is
        revoked (covering re-login and account switching) and *all* previous
        session data, including leftover OAuth state, is discarded.
        """

        new_session_id = self.store.create(user_id, device=device, location=location)
        self._revoke_current(session, REVOKE_REAUTHENTICATED)
        session.clear()
        session.update(
            {"user": email, "user_id": user_id, SESSION_ID_KEY: new_session_id}
        )

    def end(self, session: MutableMapping[str, Any]) -> bool:
        """Log out this browser: revoke its session and empty the cookie."""

        revoked = self._revoke_current(session, REVOKE_LOGOUT)
        session.clear()
        return revoked

    def end_all(self, session: MutableMapping[str, Any], user_id: str) -> int:
        """Log ``user_id`` out of every device, including this browser."""

        revoked = self.store.revoke_all(user_id, reason=REVOKE_LOGOUT_ALL)
        session.clear()
        return revoked

    def _revoke_current(self, session: Mapping[str, Any], reason: str) -> bool:
        return self.store.revoke(
            session.get(SESSION_ID_KEY), session.get("user_id"), reason=reason
        )


__all__ = [
    "DEFAULT_ABSOLUTE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "HOST_COOKIE_PREFIX",
    "SESSION_ID_KEY",
    "HardenedSessionMiddleware",
    "SessionCookiePolicy",
    "SessionLifecycle",
    "SessionLifetimePolicy",
    "SessionRegistryGuard",
    "SessionSecurityConfig",
    "SessionSecurityConfigurationError",
    "SessionState",
    "SessionStore",
    "load_session_security_config",
]
