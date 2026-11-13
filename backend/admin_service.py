"""Permission-enforced administrative services.

These functions are the only supported way for one account to act on another
tenant. Each call resolves the actor's role from the database, checks the
central matrix in :mod:`permissions`, and only then reads or changes the target.
No caller may pass an actor id that it did not obtain from the authenticated
session.

Privilege hierarchy: an account may only change or erase a target whose role is
strictly lower (owner > admin > support > user). Role changes additionally
require the owner-only ``configuration.management`` permission and may target an
equal role, but removing the last active owner is refused so a deployment cannot
lock itself out.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from accounts import (
    OPERATOR_STATUSES,
    AccountError,
    AccountNotFoundError,
    AccountRole,
    AccountStatus,
)
from accounts import (
    set_account_role as _set_account_role,
)
from accounts import (
    set_account_status as _set_account_status,
)
from database import (
    ACCOUNT_STATUS_DELETED,
    DEFAULT_DB_PATH,
    USER_OWNED_TABLES,
    UserRow,
    get_active_programme,
    get_all_users,
    get_user_by_id,
    get_user_preferences,
    transaction,
)
from database import (
    set_account_status as _tombstone_account,
)
from permissions import (
    ROLE_RANK,
    Permission,
    PermissionDeniedError,
    authorize,
)
from webapp.health import authenticated_diagnostics


class RoleHierarchyError(PermissionDeniedError):
    """The actor's role may not act on the target's role."""

    def __init__(self) -> None:
        super().__init__(Permission.ACCOUNT_MANAGEMENT, None)


class LastOwnerError(AccountError):
    """Refused because it would leave the deployment without an active owner."""


class AdminValidationError(ValueError):
    """A request value is not a valid administrative action."""


def _target_user(target_user_id: str, db_path: str) -> UserRow:
    if not isinstance(target_user_id, str) or not target_user_id.strip():
        raise AccountNotFoundError("No such account.")
    user = get_user_by_id(target_user_id, db_path)
    if user is None:
        raise AccountNotFoundError("No such account.")
    return user


def _target_role(user: UserRow) -> AccountRole:
    try:
        return AccountRole(user["role"])
    except ValueError as exc:
        raise PermissionDeniedError(Permission.ACCOUNT_MANAGEMENT, None) from exc


def _require_strictly_outranks(actor: AccountRole, target: AccountRole) -> None:
    if ROLE_RANK[actor] <= ROLE_RANK[target]:
        raise RoleHierarchyError()


def _active_owner_count(db_path: str) -> int:
    return sum(
        1
        for user in get_all_users(
            db_path, include_inactive=True, include_closing=True
        )
        if user["role"] == AccountRole.OWNER.value
        and user["status"] == AccountStatus.ACTIVE.value
    )


def _record_counts(db_path: str, user_id: str) -> dict[str, int]:
    """Count a tenant's rows in the registered owner-scoped tables only.

    Table names come from the code constant ``USER_OWNED_TABLES``, never from a
    request. Tables absent from an older database are omitted rather than
    treated as zero, so an export cannot imply data that does not exist.
    """

    with transaction(db_path) as conn:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        for table in USER_OWNED_TABLES:
            if table not in existing:
                continue
            row = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id = ?', (user_id,)
            ).fetchone()
            counts[table] = int(row[0]) if row is not None else 0
    return counts


def _account_payload(user: UserRow) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "status": user["status"],
        "role": user["role"],
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
    }


def list_accounts(
    actor_user_id: str, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """List accounts for an actor with ``account.management``."""

    authorize(actor_user_id, Permission.ACCOUNT_MANAGEMENT, db_path)
    return [
        _account_payload(user)
        for user in get_all_users(
            db_path, include_inactive=True, include_closing=True
        )
    ]


def set_account_status(
    actor_user_id: str,
    target_user_id: str,
    status: AccountStatus | str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Suspend or reactivate a lower-ranked account."""

    actor = authorize(actor_user_id, Permission.ACCOUNT_MANAGEMENT, db_path)
    try:
        requested = AccountStatus(status)
    except ValueError as exc:
        raise AdminValidationError("Unknown account status.") from exc
    if requested not in OPERATOR_STATUSES:
        raise AdminValidationError("Only active and suspended are operator statuses.")
    target = _target_user(target_user_id, db_path)
    _require_strictly_outranks(actor, _target_role(target))
    updated = _set_account_status(target["id"], requested, db_path)
    return _account_payload(updated)


def set_account_role(
    actor_user_id: str,
    target_user_id: str,
    role: AccountRole | str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Change a role. Owner-only, and never removes the last active owner."""

    actor = authorize(actor_user_id, Permission.CONFIGURATION, db_path)
    try:
        requested = AccountRole(role)
    except ValueError as exc:
        raise AdminValidationError("Unknown account role.") from exc
    target = _target_user(target_user_id, db_path)
    target_role = _target_role(target)
    if ROLE_RANK[actor] < ROLE_RANK[target_role]:
        raise RoleHierarchyError()
    if (
        target_role is AccountRole.OWNER
        and requested is not AccountRole.OWNER
        and target["status"] == AccountStatus.ACTIVE.value
        and _active_owner_count(db_path) <= 1
    ):
        raise LastOwnerError("The last active owner cannot be demoted.")
    updated = _set_account_role(target["id"], requested, db_path)
    return _account_payload(updated)


def support_summary(
    actor_user_id: str,
    target_user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return non-secret operational context for support staff."""

    authorize(actor_user_id, Permission.SUPPORT_ACCESS, db_path)
    target = _target_user(target_user_id, db_path)
    return {
        "account": _account_payload(target),
        "record_counts": _record_counts(db_path, target["id"]),
    }


def connector_diagnostics(
    actor_user_id: str,
    target_user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[int, dict[str, Any]]:
    """Return the same aggregate diagnostics the target may see for itself."""

    authorize(actor_user_id, Permission.CONNECTOR_DIAGNOSTICS, db_path)
    target = _target_user(target_user_id, db_path)
    return authenticated_diagnostics(db_path, target["id"])


def export_account(
    actor_user_id: str,
    target_user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return a JSON-safe export envelope for a target tenant.

    Credential material, provider subjects, sessions and audit trails are
    deliberately excluded; the envelope holds the account record, preferences,
    the active programme and per-table row counts.
    """

    authorize(actor_user_id, Permission.DATA_EXPORT, db_path)
    target = _target_user(target_user_id, db_path)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "account": _account_payload(target),
        "preferences": get_user_preferences(target["id"], db_path),
        "active_programme": get_active_programme(target["id"], db_path=db_path),
        "record_counts": _record_counts(db_path, target["id"]),
    }


def delete_account(
    actor_user_id: str,
    target_user_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Permanently tombstone a lower-ranked account and revoke its sessions.

    This is the access-deletion action. Erasing retained data remains the
    account-deletion workflow's responsibility; this call never reports that
    the target's data was erased.
    """

    actor = authorize(actor_user_id, Permission.DATA_DELETION, db_path)
    target = _target_user(target_user_id, db_path)
    _require_strictly_outranks(actor, _target_role(target))
    _tombstone_account(target["id"], ACCOUNT_STATUS_DELETED, db_path=db_path)
    updated = get_user_by_id(target["id"], db_path)
    if updated is None:  # pragma: no cover - target existed at the start
        raise AccountNotFoundError("No such account.")
    return _account_payload(updated)


def configuration_summary(
    actor_user_id: str, db_path: str = DEFAULT_DB_PATH
) -> dict[str, Any]:
    """Return non-secret deployment configuration to an owner.

    Configuration is environment-owned and cannot be mutated at runtime, so this
    is deliberately read-only: it exposes whether controls are configured, never
    their values.
    """

    authorize(actor_user_id, Permission.CONFIGURATION, db_path)
    allowed = os.environ.get("ALLOWED_EMAILS", "").strip()
    invite_only = os.environ.get("INVITE_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    anonymous = os.environ.get("ALLOW_ANONYMOUS_WEB", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return {
        "app_env": os.environ.get("APP_ENV", "").strip(),
        "allowlist_configured": bool(allowed),
        "allowlist_entries": len([item for item in allowed.split(",") if item.strip()]),
        "invite_only": invite_only,
        "anonymous_web": anonymous,
    }


__all__ = [
    "AdminValidationError",
    "LastOwnerError",
    "RoleHierarchyError",
    "configuration_summary",
    "connector_diagnostics",
    "delete_account",
    "export_account",
    "list_accounts",
    "set_account_role",
    "set_account_status",
    "support_summary",
]
