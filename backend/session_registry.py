"""Tenant-scoped registry for browser sessions.

The signed Starlette cookie keeps the user identity and an opaque session id.
This module persists only a hash of that id so a database leak cannot be used to
forge a session cookie. Every lookup is scoped by ``user_id``; revocation and
listing therefore cannot cross account boundaries.

It shares the hardened ``user_sessions`` registry owned by
``webapp.session_security.SessionStore`` (issue #752) rather than creating a
second store, and adds the safe display metadata required for device/session
management (issue #782).

Device labels and location approximations are deliberately coarse. Raw user
agents, full IP addresses, and session credentials are never persisted or
returned to clients.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Any

from database import DEFAULT_DB_PATH, _connect

SESSION_COOKIE_KEY = "sid"
SESSION_MAX_AGE_SECONDS = 14 * 24 * 60 * 60
_TOUCH_INTERVAL_SECONDS = 60
_MAX_FIELD_LENGTH = 160
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PRIVATE_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)
_DEVICE_PATTERNS = (
    ("Edg", "Edge"),
    ("OPR", "Opera"),
    ("Chrome", "Chrome"),
    ("Firefox", "Firefox"),
    ("Safari", "Safari"),
)
_OS_PATTERNS = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


class SessionRegistryError(RuntimeError):
    """Raised when a session cannot be registered safely."""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _epoch(moment: datetime | None) -> int:
    return int((moment or _utcnow()).timestamp())


def _isoformat(epoch: Any) -> str:
    if not isinstance(epoch, (int, float)):
        return ""
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .isoformat(timespec="seconds")
    )


def _clean(value: Any) -> str:
    text = _CONTROL_RE.sub("", str(value or "")).strip()
    return text[:_MAX_FIELD_LENGTH]


def hash_session_id(session_id: str) -> str:
    """Return the at-rest representation of an opaque session id."""

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def describe_device(user_agent: str) -> str:
    """Return a bounded, human-readable device label from a user agent."""

    user_agent = _clean(user_agent)
    if not user_agent:
        return "Unknown device"

    browser = next(
        (label for token, label in _DEVICE_PATTERNS if token in user_agent),
        "Browser",
    )
    operating_system = next(
        (label for token, label in _OS_PATTERNS if token in user_agent),
        "Unknown OS",
    )
    return _clean(f"{browser} on {operating_system}")


def approximate_location(client_ip: str, country_code: str = "") -> str:
    """Return a coarse location that never exposes the full client address."""

    country = _clean(country_code).upper()
    country_label = (
        f"Country {country}"
        if _COUNTRY_RE.fullmatch(country) and country not in {"XX", "T1"}
        else ""
    )

    candidate = _clean(client_ip)
    try:
        address = ip_address(candidate)
    except ValueError:
        return country_label or "Unknown location"

    if address.is_unspecified or any(
        address.version == network.version and address in network
        for network in _PRIVATE_NETWORKS
    ):
        return "Local network"

    prefix = 24 if address.version == 4 else 48
    network = ip_network(f"{address}/{prefix}", strict=False)
    network_label = f"Approx. network {network.network_address}/{prefix}"
    return _clean(f"{country_label} · {network_label}" if country_label else network_label)


def _metadata(
    user_agent: str,
    client_ip: str,
    country_code: str,
) -> tuple[str, str]:
    return (
        describe_device(user_agent),
        approximate_location(client_ip, country_code),
    )


def _insert_session(
    conn: sqlite3.Connection,
    user_id: str,
    session_id: str,
    device: str,
    location: str,
    moment: datetime,
) -> dict[str, Any]:
    epoch = _epoch(moment)
    cursor = conn.execute(
        "INSERT INTO user_sessions "
        "(session_token_hash, user_id, created_at, last_seen_at, revoked_at, "
        "revoked_reason, device, location) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
        (
            hash_session_id(session_id),
            user_id,
            epoch,
            epoch,
            device,
            location,
        ),
    )
    return {
        "id": int(cursor.lastrowid or 0),
        "sid": session_id,
        "device": device,
        "location": location,
        "created_at": _isoformat(epoch),
        "last_used_at": _isoformat(epoch),
    }


def create_session(
    user_id: str,
    *,
    user_agent: str = "",
    client_ip: str = "",
    country_code: str = "",
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Register a new active session and return its opaque credential."""

    if not user_id or not user_id.strip():
        raise SessionRegistryError("A user id is required to create a session")

    device, location = _metadata(user_agent, client_ip, country_code)
    session_id = secrets.token_urlsafe(32)
    with _connect(db_path) as conn:
        return _insert_session(
            conn,
            user_id.strip(),
            session_id,
            device,
            location,
            now or _utcnow(),
        )


def _session_row(
    conn: sqlite3.Connection,
    session_id: str,
) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT rowid, user_id, created_at, last_seen_at, revoked_at "
        "FROM user_sessions WHERE session_token_hash = ?",
        (hash_session_id(session_id),),
    ).fetchone()


def _is_expired(created_at: Any, now: datetime) -> bool:
    if not isinstance(created_at, (int, float)):
        return True
    return now - datetime.fromtimestamp(float(created_at), tz=timezone.utc) > timedelta(
        seconds=SESSION_MAX_AGE_SECONDS
    )


def _touch(
    conn: sqlite3.Connection,
    session_id: str,
    last_seen_at: Any,
    now: datetime,
) -> None:
    if not isinstance(last_seen_at, (int, float)):
        return
    if now.timestamp() - float(last_seen_at) < _TOUCH_INTERVAL_SECONDS:
        return
    conn.execute(
        "UPDATE user_sessions SET last_seen_at = ? WHERE session_token_hash = ?",
        (_epoch(now), hash_session_id(session_id)),
    )


def validate_session(
    user_id: str,
    session_id: str | None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether the session is active and still owned by *user_id*."""

    if not user_id or not isinstance(session_id, str) or not session_id:
        return False

    moment = now or _utcnow()
    with _connect(db_path) as conn:
        row = _session_row(conn, session_id)
        if row is None:
            return False
        _row_id, row_user_id, created_at, last_seen_at, revoked_at = row
        if revoked_at is not None or row_user_id != user_id:
            return False
        if _is_expired(created_at, moment):
            return False
        _touch(conn, session_id, last_seen_at, moment)
    return True


def legacy_session_adoptable(
    user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """Return whether a pre-registry cookie (no ``sid``) may be adopted.

    Legacy cookies are stateless and cannot be individually revoked. Once the
    user has revoked any session (logout, single revoke, or "sign out other
    sessions"), every unregistered legacy cookie must fail closed; otherwise a
    copied legacy cookie could be replayed and silently re-adopted.
    """

    if not user_id:
        return False
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM user_sessions "
            "WHERE user_id = ? AND revoked_at IS NOT NULL LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is None


def current_session_row_id(
    user_id: str,
    session_id: str | None,
    db_path: str = DEFAULT_DB_PATH,
) -> int | None:
    """Return the non-sensitive row id of the caller's current session."""

    if not user_id or not isinstance(session_id, str) or not session_id:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT rowid FROM user_sessions "
            "WHERE session_token_hash = ? AND user_id = ? AND revoked_at IS NULL",
            (hash_session_id(session_id), user_id),
        ).fetchone()
    return int(row[0]) if row else None


def list_user_sessions(
    user_id: str,
    current_session_id: str | None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """List only *user_id*'s active sessions with safe display metadata."""

    if not user_id:
        return []

    moment = now or _utcnow()
    current_hash = (
        hash_session_id(current_session_id) if current_session_id else None
    )
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid, session_token_hash, device, location, created_at, "
            "last_seen_at "
            "FROM user_sessions "
            "WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY last_seen_at DESC, rowid DESC",
            (user_id,),
        ).fetchall()

    sessions = []
    for row_id, session_hash, device, location, created_at, last_seen_at in rows:
        if _is_expired(created_at, moment):
            continue
        sessions.append(
            {
                "id": int(row_id),
                "device": device or "Unknown device",
                "location": location or "Unknown location",
                "created_at": _isoformat(created_at),
                "last_used_at": _isoformat(last_seen_at),
                "current": session_hash == current_hash,
            }
        )
    return sessions


def revoke_session(
    user_id: str,
    session_row_id: int,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke one session owned by *user_id*; never another account's row."""

    if not user_id:
        return False
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id, revoked_at FROM user_sessions WHERE rowid = ?",
            (session_row_id,),
        ).fetchone()
        if row is None or row[0] != user_id:
            return False
        if row[1] is None:
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? "
                "WHERE rowid = ? AND user_id = ? AND revoked_at IS NULL",
                (_epoch(now), session_row_id, user_id),
            )
    return True


def revoke_current_session(
    user_id: str,
    session_id: str | None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> bool:
    """Revoke the caller's current session, for example on logout."""

    if not user_id or not isinstance(session_id, str) or not session_id:
        return False
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE user_sessions SET revoked_at = ? "
            "WHERE session_token_hash = ? AND user_id = ? AND revoked_at IS NULL",
            (_epoch(now), hash_session_id(session_id), user_id),
        )
    return cursor.rowcount > 0


def revoke_other_sessions(
    user_id: str,
    current_session_id: str | None,
    db_path: str = DEFAULT_DB_PATH,
    *,
    now: datetime | None = None,
) -> int:
    """Revoke every active session for *user_id* except the current one."""

    if not user_id:
        return 0
    current_hash = (
        hash_session_id(current_session_id) if current_session_id else ""
    )
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE user_sessions SET revoked_at = ? "
            "WHERE user_id = ? AND session_token_hash <> ? AND revoked_at IS NULL",
            (_epoch(now), user_id, current_hash),
        )
    return int(cursor.rowcount)


def rotate_session(
    user_id: str,
    current_session_id: str | None,
    *,
    user_agent: str = "",
    client_ip: str = "",
    country_code: str = "",
    db_path: str = DEFAULT_DB_PATH,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Revoke the current credential and issue a fresh session id.

    Security-sensitive session changes call this so a previously observed
    cookie cannot be replayed after the change.
    """

    if not user_id or not isinstance(current_session_id, str) or not current_session_id:
        return None

    moment = now or _utcnow()
    device, location = _metadata(user_agent, client_ip, country_code)
    new_session_id = secrets.token_urlsafe(32)

    with _connect(db_path) as conn:
        row = _session_row(conn, current_session_id)
        if row is None:
            return None
        _row_id, row_user_id, created_at, _last_seen_at, revoked_at = row
        if row_user_id != user_id or revoked_at is not None:
            return None
        if _is_expired(created_at, moment):
            return None
        conn.execute(
            "UPDATE user_sessions SET revoked_at = ? "
            "WHERE session_token_hash = ? AND user_id = ?",
            (_epoch(moment), hash_session_id(current_session_id), user_id),
        )
        return _insert_session(
            conn,
            user_id,
            new_session_id,
            device,
            location,
            moment,
        )


def request_metadata(headers: Mapping[str, str], client_ip: str) -> tuple[str, str, str]:
    """Extract the safe subset of request metadata used for display."""

    user_agent = headers.get("user-agent", "")
    country_code = headers.get("cf-ipcountry", "")
    return user_agent, client_ip, country_code


__all__ = [
    "SESSION_COOKIE_KEY",
    "SESSION_MAX_AGE_SECONDS",
    "SessionRegistryError",
    "approximate_location",
    "create_session",
    "current_session_row_id",
    "describe_device",
    "hash_session_id",
    "legacy_session_adoptable",
    "list_user_sessions",
    "request_metadata",
    "revoke_current_session",
    "revoke_other_sessions",
    "revoke_session",
    "rotate_session",
    "validate_session",
]
