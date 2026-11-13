"""The central role/permission model and its schema contract.

Synthetic accounts only. Ordinary users must hold no administrative permission,
and the matrix must stay in agreement with the ``users.role`` CHECK constraint.
"""

from __future__ import annotations

import sqlite3

import pytest
from accounts import AccountError, AccountRole, set_account_role
from database import (
    ACCOUNT_ROLES,
    get_account_state,
    get_all_users,
    get_or_create_user,
    get_user_by_id,
    init_db,
)
from permissions import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDeniedError,
    authorize,
    permissions_for_user,
    role_has_permission,
    role_matrix,
)
from scripts import manage_accounts


def _user(db: str, email: str, role: AccountRole) -> str:
    user = get_or_create_user(email, db_path=db)
    set_account_role(user["id"], role, db)
    return user["id"]


def test_role_vocabulary_and_user_role_are_one_definition(tmp_path) -> None:
    db = str(tmp_path / "roles.db")
    init_db(db)
    assert {role.value for role in AccountRole} == set(ACCOUNT_ROLES)
    with sqlite3.connect(db) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()[0]
        for role in ACCOUNT_ROLES:
            assert f"'{role}'" in ddl
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO users (id, email, created_at, role) "
                "VALUES ('u', 'u@example.com', 'now', 'superuser')"
            )


def test_accounts_default_to_the_non_administrative_role(tmp_path) -> None:
    db = str(tmp_path / "roles.db")
    init_db(db)
    user = get_or_create_user("person@example.com", db_path=db)
    assert user["role"] == AccountRole.USER.value
    assert permissions_for_user(user) == frozenset()


def test_the_role_permission_matrix_is_exact() -> None:
    assert role_matrix() == {
        "owner": (
            "account.management",
            "configuration.management",
            "connector.diagnostics",
            "data.deletion",
            "data.export",
            "support.access",
        ),
        "admin": (
            "account.management",
            "connector.diagnostics",
            "data.deletion",
            "data.export",
            "support.access",
        ),
        "support": ("connector.diagnostics", "support.access"),
        "user": (),
    }
    assert set(ROLE_PERMISSIONS) == set(AccountRole)


@pytest.mark.parametrize(
    ("role", "permission", "expected"),
    [
        (AccountRole.OWNER, Permission.CONFIGURATION, True),
        (AccountRole.ADMIN, Permission.CONFIGURATION, False),
        (AccountRole.ADMIN, Permission.ACCOUNT_MANAGEMENT, True),
        (AccountRole.SUPPORT, Permission.SUPPORT_ACCESS, True),
        (AccountRole.SUPPORT, Permission.DATA_EXPORT, False),
        (AccountRole.USER, Permission.SUPPORT_ACCESS, False),
        (AccountRole.USER, Permission.ACCOUNT_MANAGEMENT, False),
        ("banished", Permission.ACCOUNT_MANAGEMENT, False),
        (AccountRole.OWNER, "unknown.permission", False),
    ],
)
def test_permission_checks_deny_unknown_values(
    role: object, permission: object, expected: bool
) -> None:
    assert role_has_permission(role, permission) is expected


def test_authorize_resolves_the_current_role_from_the_database(tmp_path) -> None:
    db = str(tmp_path / "roles.db")
    init_db(db)
    owner_id = _user(db, "owner@example.com", AccountRole.OWNER)
    user_id = _user(db, "user@example.com", AccountRole.USER)

    assert authorize(owner_id, Permission.CONFIGURATION, db) is AccountRole.OWNER
    with pytest.raises(PermissionDeniedError, match="support.access"):
        authorize(user_id, Permission.SUPPORT_ACCESS, db)

    # A role change takes effect immediately and ends earlier sessions.
    before = get_account_state(user_id, db)
    set_account_role(user_id, AccountRole.SUPPORT, db)
    after = get_account_state(user_id, db)
    assert before is not None and after is not None
    assert after["session_epoch"] == before["session_epoch"] + 1
    assert authorize(user_id, Permission.SUPPORT_ACCESS, db) is AccountRole.SUPPORT


def test_the_legacy_placeholder_and_terminal_accounts_cannot_be_promoted(
    tmp_path,
) -> None:
    from tenancy import LEGACY_TENANT_EMAIL

    db = str(tmp_path / "roles.db")
    init_db(db)
    placeholder = next(
        user
        for user in get_all_users(db, include_inactive=True)
        if user["email"] == LEGACY_TENANT_EMAIL
    )
    with pytest.raises(AccountError, match="legacy placeholder"):
        set_account_role(placeholder["id"], AccountRole.OWNER, db)

    user = get_or_create_user("person@example.com", db_path=db)
    from database import set_account_status

    set_account_status(user["id"], "deleted", db_path=db)
    with pytest.raises(AccountError, match="deleted"):
        set_account_role(user["id"], AccountRole.ADMIN, db)


def test_the_operator_cli_bootstraps_a_role_and_lists_it(tmp_path, capsys) -> None:
    db = str(tmp_path / "roles.db")
    init_db(db)
    user = get_or_create_user("owner@example.com", db_path=db)
    assert user["role"] == "user"

    code = manage_accounts.main(
        ["--database", db, "set-role", "--email", "owner@example.com", "--role", "owner"]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert get_user_by_id(user["id"], db)["role"] == "owner"
    assert "all earlier sessions are revoked" in out

    assert manage_accounts.main(["--database", db, "list"]) == 0
    listing = capsys.readouterr().out
    assert "owner@example.com" in listing
    assert listing.split("owner@example.com", 1)[1].split()[0] == user["id"]
    assert listing.rstrip().endswith("owner")


def test_the_operator_cli_refuses_an_unknown_role(tmp_path, capsys) -> None:
    db = str(tmp_path / "roles.db")
    init_db(db)
    get_or_create_user("person@example.com", db_path=db)

    with pytest.raises(SystemExit) as excinfo:
        manage_accounts.main(
            [
                "--database",
                db,
                "set-role",
                "--email",
                "person@example.com",
                "--role",
                "root",
            ]
        )

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
