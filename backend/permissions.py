"""Central role/permission model for administrative capabilities.

Ownership of tenant data is checked separately, from the authenticated account
id. This module answers the other question: may this account perform an
*administrative* action such as managing another account, reading another
tenant's support summary, exporting data, deleting an account, or changing
deployment configuration?

Every route, service and background action that crosses that boundary must call
:func:`authorize` (directly or through ``admin_service``). UI visibility is not
a control; a hidden control is still denied server-side.

The matrix is intentionally small and explicit:

===========================  =====  =====  =======  ====
Permission                   owner  admin  support  user
===========================  =====  =====  =======  ====
``account.management``        yes    yes     no      no
``support.access``            yes    yes     yes     no
``connector.diagnostics``     yes    yes     yes     no
``data.export``               yes    yes     no      no
``data.deletion``             yes    yes     no      no
``configuration.management``  yes    no      no      no
===========================  =====  =====  =======  ====

``user`` keeps full ownership of its own tenant data; the matrix only grants
administrative capabilities that cross the tenant boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from accounts import AccountRole, require_active_user
from database import ACCOUNT_ROLE_RANK, DEFAULT_DB_PATH, UserRow


class Permission(StrEnum):
    """A deployable administrative capability."""

    ACCOUNT_MANAGEMENT = "account.management"
    SUPPORT_ACCESS = "support.access"
    CONNECTOR_DIAGNOSTICS = "connector.diagnostics"
    DATA_EXPORT = "data.export"
    DATA_DELETION = "data.deletion"
    CONFIGURATION = "configuration.management"


#: The complete role-permission matrix. Changing this table changes authorisation
#: for every caller, which is why it is the single definition rather than a
#: collection of route-local checks.
ROLE_PERMISSIONS: Mapping[AccountRole, frozenset[Permission]] = MappingProxyType(
    {
        AccountRole.OWNER: frozenset(Permission),
        AccountRole.ADMIN: frozenset(
            {
                Permission.ACCOUNT_MANAGEMENT,
                Permission.SUPPORT_ACCESS,
                Permission.CONNECTOR_DIAGNOSTICS,
                Permission.DATA_EXPORT,
                Permission.DATA_DELETION,
            }
        ),
        AccountRole.SUPPORT: frozenset(
            {
                Permission.SUPPORT_ACCESS,
                Permission.CONNECTOR_DIAGNOSTICS,
            }
        ),
        AccountRole.USER: frozenset(),
    }
)

#: Role precedence used by destructive/administrative services. Compare with
#: ``database.ACCOUNT_ROLE_RANK`` so the schema vocabulary and this model agree.
ROLE_RANK: Mapping[AccountRole, int] = MappingProxyType(
    {role: ACCOUNT_ROLE_RANK[role.value] for role in AccountRole}
)


class PermissionDeniedError(RuntimeError):
    """The authenticated account does not hold *permission*.

    The message deliberately carries only the closed-vocabulary permission and
    role names, never an account id, address or request content.
    """

    def __init__(
        self, permission: Permission | str, role: AccountRole | None
    ) -> None:
        self.permission = permission
        self.role = role
        role_name = role.value if role is not None else "unknown"
        permission_name = (
            permission.value
            if isinstance(permission, Permission)
            else str(permission)
        )
        super().__init__(
            f"Role {role_name!r} lacks permission {permission_name!r}."
        )


def role_permissions(role: AccountRole) -> frozenset[Permission]:
    """Return the permissions granted to *role* (never a mutable view)."""

    return ROLE_PERMISSIONS[role]


def role_has_permission(role: AccountRole | str, permission: Permission | str) -> bool:
    """Return whether *role* holds *permission*.

    Unknown role or permission values deny rather than raise, so a corrupt row or
    a typo can never become an accidental grant.
    """

    try:
        resolved_role = AccountRole(role)
        resolved_permission = Permission(permission)
    except ValueError:
        return False
    return resolved_permission in ROLE_PERMISSIONS[resolved_role]


def permissions_for_user(user: UserRow) -> frozenset[Permission]:
    """Return the permissions of an already-loaded account row."""

    try:
        role = AccountRole(user["role"])
    except (KeyError, ValueError):
        return frozenset()
    return ROLE_PERMISSIONS[role]


def authorize(
    user_id: str,
    permission: Permission | str,
    db_path: str = DEFAULT_DB_PATH,
) -> AccountRole:
    """Require an active account holding *permission*; return its role.

    The account state is re-read from the database on every call, so a role
    change or suspension takes effect immediately. Raises
    :class:`accounts.AccountError` when the account is missing/inactive and
    :class:`PermissionDeniedError` when the role lacks the permission.
    """

    try:
        resolved_permission = Permission(permission)
    except ValueError as exc:
        raise PermissionDeniedError(str(permission), None) from exc
    user = require_active_user(user_id, db_path)
    try:
        role = AccountRole(user["role"])
    except ValueError as exc:
        raise PermissionDeniedError(resolved_permission, None) from exc
    if resolved_permission not in ROLE_PERMISSIONS[role]:
        raise PermissionDeniedError(resolved_permission, role)
    return role


def role_matrix() -> dict[str, tuple[str, ...]]:
    """Return a stable, JSON-safe rendering of the matrix for docs and tests."""

    return {
        role.value: tuple(
            permission.value
            for permission in sorted(ROLE_PERMISSIONS[role], key=lambda p: p.value)
        )
        for role in AccountRole
    }


__all__ = [
    "ROLE_PERMISSIONS",
    "ROLE_RANK",
    "Permission",
    "PermissionDeniedError",
    "authorize",
    "permissions_for_user",
    "role_has_permission",
    "role_matrix",
    "role_permissions",
]
