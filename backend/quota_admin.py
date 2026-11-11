"""Audited administrator actions for per-account quotas.

An administrator is an authenticated account whose sign-in email appears in
``QUOTA_ADMIN_EMAILS``; with the variable empty nobody is an administrator
(fail closed). The acting account always comes from the signed session and is
re-checked against ``users`` here; the affected account is a validated path
identifier, never a request-body owner claim.

Every state change commits in the same transaction as its ``quota_audit_log``
record, so a change cannot exist without its audit trail, and the table rejects
updates. See ``docs/ACCOUNT_QUOTAS.md``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from database import get_user_by_id
from quotas import (
    ABUSE_DENIAL_THRESHOLD,
    ABUSE_WINDOW_SECONDS,
    Entitlement,
    QuotaKind,
    QuotaUnavailableError,
    connection,
    entitlement_for,
    write_transaction,
)

ADMIN_EMAILS_ENV = "QUOTA_ADMIN_EMAILS"

MIN_REASON_LENGTH = 3
MAX_REASON_LENGTH = 500
MAX_OVERRIDE_PERCENT = 1000
MIN_OVERRIDE_SECONDS = 60
MAX_OVERRIDE_SECONDS = 90 * 24 * 60 * 60
MAX_AUDIT_PAGE = 200
MAX_QUEUE_LENGTH = 100

# decision -> entitlement applied to the reviewed account (None keeps it).
ABUSE_DECISIONS: Mapping[str, Entitlement | None] = {
    "dismiss": None,
    "restrict": Entitlement.RESTRICTED,
    "restore": Entitlement.STANDARD,
}


class AdminError(Exception):
    """Base class for administrator-action failures."""


class AdminAuthorizationError(AdminError, PermissionError):
    """The acting account is not a quota administrator."""


class AccountNotFoundError(AdminError, LookupError):
    """The affected account does not exist."""


class AdminValidationError(AdminError, ValueError):
    """A request value is not a valid administrative action."""


def configured_admin_emails(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    raw = (
        os.environ.get("QUOTA_ADMIN_EMAILS", "")
        if environ is None
        else environ.get(ADMIN_EMAILS_ENV, "")
    )
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


def require_admin(
    actor_user_id: str,
    db_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Raise unless *actor_user_id* is an account listed in ``QUOTA_ADMIN_EMAILS``."""

    allowed = configured_admin_emails(environ)
    if not allowed or not isinstance(actor_user_id, str) or not actor_user_id.strip():
        raise AdminAuthorizationError("administrator access required")
    try:
        user = get_user_by_id(actor_user_id.strip(), db_path)
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("account lookup is unavailable") from exc
    if user is None or user["email"].strip().lower() not in allowed:
        raise AdminAuthorizationError("administrator access required")


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _clean_reason(reason: object) -> str:
    if not isinstance(reason, str):
        raise AdminValidationError("A reason is required.")
    cleaned = " ".join(reason.split())
    if not cleaned.isprintable() or not MIN_REASON_LENGTH <= len(cleaned) <= MAX_REASON_LENGTH:
        raise AdminValidationError(
            f"The reason must be {MIN_REASON_LENGTH}-{MAX_REASON_LENGTH} printable characters."
        )
    return cleaned


def _parse_entitlement(value: object) -> Entitlement:
    if isinstance(value, str):
        with suppress(ValueError):
            return Entitlement(value)
    names = ", ".join(item.value for item in Entitlement)
    raise AdminValidationError(f"Unknown entitlement. Use one of: {names}.")


def _parse_kind(value: object) -> QuotaKind:
    if isinstance(value, str):
        with suppress(ValueError):
            return QuotaKind(value)
    names = ", ".join(item.value for item in QuotaKind)
    raise AdminValidationError(f"Unknown quota. Use one of: {names}.")


def _bounded_int(value: object, low: int, high: int, what: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise AdminValidationError(f"{what} must be a whole number from {low} to {high}.")
    return value


def _require_account(conn: sqlite3.Connection, user_id: object) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise AccountNotFoundError("No such account.")
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise AccountNotFoundError("No such account.")
    return user_id


def _audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    target: str,
    action: str,
    reason: str,
    detail: Mapping[str, Any],
    now: float,
) -> int:
    cursor = conn.execute(
        "INSERT INTO quota_audit_log "
        "(user_id, actor_user_id, action, detail_json, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            target,
            actor,
            action,
            json.dumps(detail, sort_keys=True, separators=(",", ":")),
            reason,
            _iso(now),
        ),
    )
    audit_id = cursor.lastrowid
    if audit_id is None:
        raise QuotaUnavailableError("audit record was not written")
    return audit_id


def _write_profile(
    conn: sqlite3.Connection,
    target: str,
    entitlement: Entitlement,
    actor: str,
    now: float,
) -> None:
    conn.execute(
        "INSERT INTO account_quota_profiles (user_id, entitlement, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET entitlement = excluded.entitlement, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (target, entitlement.value, _iso(now), actor),
    )


def _override_state(
    conn: sqlite3.Connection, target: str, kind: QuotaKind
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT limit_percent, expires_at FROM quota_overrides "
        "WHERE user_id = ? AND kind = ?",
        (target, kind.value),
    ).fetchone()
    if row is None:
        return None
    return {
        "limit_percent": int(row[0]),
        "expires_at": None if row[1] is None else _iso(row[1]),
    }


def set_entitlement(
    actor_user_id: str,
    target_user_id: str,
    entitlement: str,
    reason: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Move one account to another entitlement (audited)."""

    require_admin(actor_user_id, db_path, environ=environ)
    new = _parse_entitlement(entitlement)
    cleaned = _clean_reason(reason)
    moment = _now(now)
    try:
        with write_transaction(db_path) as conn:
            target = _require_account(conn, target_user_id)
            previous = entitlement_for(conn, target)
            _write_profile(conn, target, new, actor_user_id, moment)
            audit_id = _audit(
                conn,
                actor=actor_user_id,
                target=target,
                action="entitlement_set",
                reason=cleaned,
                detail={"previous": previous.value, "current": new.value},
                now=moment,
            )
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return {"audit_id": audit_id, "entitlement": new.value, "previous": previous.value}


def set_override(
    actor_user_id: str,
    target_user_id: str,
    kind: str,
    limit_percent: int,
    expires_in_seconds: int | None,
    reason: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Scale one account's limits for one quota; ``0`` switches the quota off.

    The override replaces any earlier one for the same quota and expires after
    *expires_in_seconds* (never, when omitted).
    """

    require_admin(actor_user_id, db_path, environ=environ)
    quota = _parse_kind(kind)
    percent = _bounded_int(limit_percent, 0, MAX_OVERRIDE_PERCENT, "limit_percent")
    lifetime = (
        None
        if expires_in_seconds is None
        else _bounded_int(
            expires_in_seconds, MIN_OVERRIDE_SECONDS, MAX_OVERRIDE_SECONDS, "expires_in_seconds"
        )
    )
    cleaned = _clean_reason(reason)
    moment = _now(now)
    expires_at = None if lifetime is None else moment + lifetime
    try:
        with write_transaction(db_path) as conn:
            target = _require_account(conn, target_user_id)
            previous = _override_state(conn, target, quota)
            conn.execute(
                "INSERT INTO quota_overrides "
                "(user_id, kind, limit_percent, expires_at, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, kind) DO UPDATE SET "
                "limit_percent = excluded.limit_percent, expires_at = excluded.expires_at, "
                "created_by = excluded.created_by, created_at = excluded.created_at",
                (target, quota.value, percent, expires_at, actor_user_id, _iso(moment)),
            )
            current = _override_state(conn, target, quota)
            audit_id = _audit(
                conn,
                actor=actor_user_id,
                target=target,
                action="override_set",
                reason=cleaned,
                detail={"kind": quota.value, "previous": previous, "current": current},
                now=moment,
            )
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return {"audit_id": audit_id, "kind": quota.value, "override": current}


def clear_override(
    actor_user_id: str,
    target_user_id: str,
    kind: str,
    reason: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove one account's override for one quota (audited)."""

    require_admin(actor_user_id, db_path, environ=environ)
    quota = _parse_kind(kind)
    cleaned = _clean_reason(reason)
    moment = _now(now)
    try:
        with write_transaction(db_path) as conn:
            target = _require_account(conn, target_user_id)
            previous = _override_state(conn, target, quota)
            if previous is None:
                raise AdminValidationError("No override is set for that quota.")
            conn.execute(
                "DELETE FROM quota_overrides WHERE user_id = ? AND kind = ?",
                (target, quota.value),
            )
            audit_id = _audit(
                conn,
                actor=actor_user_id,
                target=target,
                action="override_clear",
                reason=cleaned,
                detail={"kind": quota.value, "previous": previous, "current": None},
                now=moment,
            )
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return {"audit_id": audit_id, "kind": quota.value, "override": None}


def review_abuse(
    actor_user_id: str,
    target_user_id: str,
    decision: str,
    reason: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record an abuse-review decision for one account (audited).

    ``dismiss`` keeps the entitlement, ``restrict`` applies the restricted one
    and ``restore`` returns to the standard one. The denial tallies that
    prompted the review are summarised in the audit record and then cleared, so
    the account re-enters the queue only after fresh evidence accumulates.
    """

    require_admin(actor_user_id, db_path, environ=environ)
    if decision not in ABUSE_DECISIONS:
        raise AdminValidationError(
            f"Unknown decision. Use one of: {', '.join(ABUSE_DECISIONS)}."
        )
    cleaned = _clean_reason(reason)
    moment = _now(now)
    try:
        with write_transaction(db_path) as conn:
            target = _require_account(conn, target_user_id)
            previous = entitlement_for(conn, target)
            evidence = {
                str(kind): int(total)
                for kind, total in conn.execute(
                    "SELECT kind, SUM(denials) FROM quota_denials "
                    "WHERE user_id = ? AND last_denied_at > ? GROUP BY kind",
                    (target, moment - ABUSE_WINDOW_SECONDS),
                )
            }
            current = ABUSE_DECISIONS[decision] or previous
            if current != previous:
                _write_profile(conn, target, current, actor_user_id, moment)
            conn.execute("DELETE FROM quota_denials WHERE user_id = ?", (target,))
            audit_id = _audit(
                conn,
                actor=actor_user_id,
                target=target,
                action="abuse_review",
                reason=cleaned,
                detail={
                    "decision": decision,
                    "previous": previous.value,
                    "current": current.value,
                    "evidence": {
                        "window_seconds": ABUSE_WINDOW_SECONDS,
                        "denials_by_kind": evidence,
                    },
                },
                now=moment,
            )
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return {
        "audit_id": audit_id,
        "decision": decision,
        "entitlement": current.value,
        "previous": previous.value,
    }


def abuse_overview(
    actor_user_id: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return recent volume per quota and the accounts awaiting abuse review."""

    require_admin(actor_user_id, db_path, environ=environ)
    moment = _now(now)
    since = moment - ABUSE_WINDOW_SECONDS
    try:
        with connection(db_path) as conn:
            usage: dict[str, dict[str, int]] = {
                kind.value: {"allowed": 0, "denied": 0} for kind in QuotaKind
            }
            for kind, total in conn.execute(
                "SELECT kind, COUNT(*) FROM quota_usage WHERE occurred_at > ? GROUP BY kind",
                (since,),
            ):
                if kind in usage:
                    usage[kind]["allowed"] = int(total)
            for kind, total in conn.execute(
                "SELECT kind, SUM(denials) FROM quota_denials "
                "WHERE last_denied_at > ? GROUP BY kind",
                (since,),
            ):
                if kind in usage:
                    usage[kind]["denied"] = int(total)

            flagged = conn.execute(
                "SELECT d.user_id, u.email, SUM(d.denials), MAX(d.last_denied_at) "
                "FROM quota_denials d JOIN users u ON u.id = d.user_id "
                "WHERE d.last_denied_at > ? GROUP BY d.user_id "
                "HAVING SUM(d.denials) >= ? "
                "ORDER BY SUM(d.denials) DESC, MAX(d.last_denied_at) DESC LIMIT ?",
                (since, ABUSE_DENIAL_THRESHOLD, MAX_QUEUE_LENGTH),
            ).fetchall()
            by_kind: dict[str, dict[str, int]] = {}
            for user_id, kind, total in conn.execute(
                "SELECT user_id, kind, SUM(denials) FROM quota_denials "
                "WHERE last_denied_at > ? GROUP BY user_id, kind",
                (since,),
            ):
                by_kind.setdefault(user_id, {})[kind] = int(total)
            queue = [
                {
                    "account_id": user_id,
                    "email": email,
                    "entitlement": entitlement_for(conn, user_id).value,
                    "denials": int(total),
                    "denials_by_kind": by_kind.get(user_id, {}),
                    "last_denied_at": _iso(last),
                }
                for user_id, email, total, last in flagged
            ]
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return {
        "window_seconds": ABUSE_WINDOW_SECONDS,
        "review_threshold": ABUSE_DENIAL_THRESHOLD,
        "usage": usage,
        "queue": queue,
    }


def list_audit(
    actor_user_id: str,
    db_path: str,
    *,
    account_id: str | None = None,
    limit: int = 50,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return audit records, newest first, optionally for one affected account."""

    require_admin(actor_user_id, db_path, environ=environ)
    page = max(1, min(limit, MAX_AUDIT_PAGE))
    sql = (
        "SELECT id, user_id, actor_user_id, action, reason, detail_json, created_at "
        "FROM quota_audit_log "
    )
    params: tuple[Any, ...] = (page,)
    if account_id is not None:
        sql += "WHERE user_id = ? "
        params = (account_id, page)
    sql += "ORDER BY id DESC LIMIT ?"
    try:
        with connection(db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise QuotaUnavailableError("quota administration is unavailable") from exc
    return [
        {
            "id": row[0],
            "account_id": row[1],
            "actor_account_id": row[2],
            "action": row[3],
            "reason": row[4],
            "detail": json.loads(row[5]),
            "created_at": row[6],
        }
        for row in rows
    ]
