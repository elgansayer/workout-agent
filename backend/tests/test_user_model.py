"""Schema contract of the production user model.

Covers the migration that gives every account an immutable id, a lifecycle
status, timestamps and canonical identity rows, and the persistence-level
guarantee that every user-owned record has an owner. All data is synthetic.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from accounts import AccountStatus
from database import (
    ACCOUNT_STATUSES,
    TERMINAL_ACCOUNT_STATUSES,
    USER_OWNED_TABLES,
    get_all_users,
    get_legacy_user_id,
    get_or_create_user,
    init_db,
)
from legacy_ownership import NOT_TRANSFERRED_TABLES, OWNED_TABLES
from tenancy import LEGACY_TENANT_EMAIL, TenantRequiredError
from test_database import _create_pre_multi_tenant_schema, _user_id_is_indexed_first

_NOW = "2030-01-01T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path: Path) -> str:
    return str(tmp_path / "user-model.db")


@pytest.fixture()
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run with the production tenancy policy (no implicit tenant)."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")


def _add_user(conn: sqlite3.Connection, email: str, *, user_id: str | None = None) -> str:
    uid = user_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, email, created_at, status_changed_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, email, _NOW, _NOW, _NOW),
    )
    return uid


def _add_identity(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    provider: str = "google",
    subject: str = "subject-1",
    email_verified: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO user_identities "
        "(user_id, provider, subject, email, email_verified, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, provider, subject, "person@example.com", email_verified, _NOW),
    )


def _owned_row(conn: sqlite3.Connection, table: str, user_id: Any) -> None:
    """Insert one syntactically valid row into *table* owned by *user_id*."""
    names: list[str] = []
    values: list[Any] = []
    for _cid, name, column_type, _notnull, _default, pk in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        if name == "user_id":
            names.append(name)
            values.append(user_id)
        elif pk and column_type.upper() == "INTEGER" and name == "id":
            continue  # rowid alias
        elif column_type.upper() == "INTEGER":
            names.append(name)
            values.append(1)
        elif column_type.upper() == "REAL":
            names.append(name)
            values.append(1.0)
        else:
            names.append(name)
            values.append("synthetic")
    placeholders = ", ".join("?" for _ in names)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})", values
    )


# ---------------------------------------------------------------------------
# Account lifecycle columns and vocabulary
# ---------------------------------------------------------------------------


def test_users_table_carries_identity_status_and_timestamps(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
    assert {
        "id",
        "email",
        "created_at",
        "status",
        "status_changed_at",
        "updated_at",
        "last_login_at",
    } <= set(columns)
    assert columns["status"][3] == 1  # NOT NULL
    assert columns["status"][4] == "'active'"  # default
    assert columns["created_at"][3] == 1


def test_status_vocabulary_is_one_definition(db: str) -> None:
    init_db(db)
    assert {status.value for status in AccountStatus} == set(ACCOUNT_STATUSES)
    assert set(TERMINAL_ACCOUNT_STATUSES) < set(ACCOUNT_STATUSES)
    with sqlite3.connect(db) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()[0]
        for status in ACCOUNT_STATUSES:
            assert f"'{status}'" in ddl
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(
                "INSERT INTO users (id, email, created_at, status) "
                "VALUES ('u', 'u@example.com', ?, 'banished')",
                (_NOW,),
            )


def test_new_accounts_default_to_active_with_consistent_timestamps(db: str) -> None:
    init_db(db)
    user = get_or_create_user("Person@Example.com", "Person", db)
    assert user["status"] == "active"
    assert user["email"] == "person@example.com"
    assert user["created_at"] == user["status_changed_at"] == user["updated_at"]
    assert user["last_login_at"] is None
    assert uuid.UUID(user["id"]).version == 4


def test_provisioning_returns_the_same_account_for_any_email_case(
    db: str, production: None
) -> None:
    init_db(db)
    first = get_or_create_user("Mixed@Example.com", None, db)
    second = get_or_create_user("mixed@EXAMPLE.com", None, db)
    assert first["id"] == second["id"]
    assert len(get_all_users(db, include_inactive=True)) == 1


def test_the_reserved_placeholder_address_cannot_be_provisioned(db: str) -> None:
    init_db(db)
    with pytest.raises(ValueError, match="reserved|holding"):
        get_or_create_user(LEGACY_TENANT_EMAIL, None, db)
    with pytest.raises(ValueError):
        get_or_create_user("   ", None, db)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_user_id_is_immutable(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "immutable@example.com")
        with pytest.raises(sqlite3.IntegrityError, match="users.id is immutable"):
            conn.execute("UPDATE users SET id = 'replacement' WHERE id = ?", (uid,))
        assert conn.execute("SELECT count(*) FROM users WHERE id = ?", (uid,)).fetchone() == (1,)
        # Rewriting the same value is not a change.
        conn.execute("UPDATE users SET id = id WHERE id = ?", (uid,))


def test_created_at_is_immutable(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "created@example.com")
        with pytest.raises(sqlite3.IntegrityError, match="created_at is immutable"):
            conn.execute(
                "UPDATE users SET created_at = '1999-01-01' WHERE id = ?", (uid,)
            )
        stored = conn.execute(
            "SELECT created_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
        assert stored == (_NOW,)


@pytest.mark.parametrize("terminal", TERMINAL_ACCOUNT_STATUSES)
def test_terminal_status_can_never_change(db: str, terminal: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, f"{terminal}@example.com")
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (terminal, uid))
        with pytest.raises(sqlite3.IntegrityError, match="cannot change status"):
            conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (uid,))
        # Re-stating the terminal status is harmless.
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (terminal, uid))
        assert conn.execute(
            "SELECT status FROM users WHERE id = ?", (uid,)
        ).fetchone() == (terminal,)


@pytest.mark.parametrize("leaving", ["active", "suspended", "unclaimed"])
def test_non_terminal_statuses_may_transition(db: str, leaving: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, f"{leaving}@example.com")
        conn.execute("UPDATE users SET status = ? WHERE id = ?", (leaving, uid))
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (uid,))
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (uid,))


# ---------------------------------------------------------------------------
# Canonical identities
# ---------------------------------------------------------------------------


def test_one_external_identity_binds_to_exactly_one_account(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        first = _add_user(conn, "first@example.com")
        second = _add_user(conn, "second@example.com")
        _add_identity(conn, first, subject="shared-subject")
        with pytest.raises(sqlite3.IntegrityError):
            _add_identity(conn, second, subject="shared-subject")
        # Another provider may reuse the same opaque subject string.
        _add_identity(conn, second, provider="other", subject="shared-subject")


def test_an_account_has_at_most_one_identity_per_provider(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "single@example.com")
        _add_identity(conn, uid, subject="one")
        with pytest.raises(sqlite3.IntegrityError):
            _add_identity(conn, uid, subject="two")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("user_id", "another-account"),
        ("provider", "other"),
        ("subject", "rewritten"),
        ("created_at", "1999-01-01"),
    ],
)
def test_identity_columns_are_immutable(db: str, column: str, value: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "bound@example.com")
        _add_identity(conn, uid)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE user_identities SET {column} = ?", (value,))
        # The mutable attributes still update.
        conn.execute(
            "UPDATE user_identities SET email = 'new@example.com', "
            "email_verified = 1, last_login_at = ?",
            (_NOW,),
        )


def test_identity_must_reference_an_existing_account(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn, pytest.raises(
        sqlite3.IntegrityError, match="existing user"
    ):
        _add_identity(conn, "no-such-account")


@pytest.mark.parametrize(
    ("provider", "subject", "verified"),
    [("", "s", 1), ("Google", "s", 1), ("google", "", 1), ("google", "s", 2)],
)
def test_identity_values_are_validated(
    db: str, provider: str, subject: str, verified: int
) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "validated@example.com")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _add_identity(
                conn,
                uid,
                provider=provider,
                subject=subject,
                email_verified=verified,
            )


def test_the_legacy_placeholder_can_never_gain_an_identity(db: str) -> None:
    init_db(db)  # local mode: the placeholder exists and is active
    with sqlite3.connect(db) as conn:
        placeholder = conn.execute(
            "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="holding"):
            _add_identity(conn, placeholder)


def test_email_uniqueness_is_case_insensitive(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        _add_user(conn, "Case@Example.com")
        with pytest.raises(sqlite3.IntegrityError):
            _add_user(conn, "case@example.COM")


# ---------------------------------------------------------------------------
# Upgrading databases that predate the user model
# ---------------------------------------------------------------------------

_PRE_LIFECYCLE_USERS = """
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    timezone TEXT DEFAULT 'UTC',
    units TEXT DEFAULT 'metric'
)
"""


def test_upgrade_gives_existing_accounts_a_status_and_honest_timestamps(
    db: str, production: None
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(_PRE_LIFECYCLE_USERS)
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) "
            "VALUES ('legacy-account-1', 'Older@Example.com', 'Older', ?)",
            ("2029-05-06T07:08:09+00:00",),
        )

    init_db(db)
    init_db(db)  # idempotent

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT id, email, created_at, status, status_changed_at, updated_at, "
            "last_login_at FROM users WHERE id = 'legacy-account-1'"
        ).fetchone()
    assert row == (
        "legacy-account-1",
        "Older@Example.com",  # stored value is preserved; lookups are canonical
        "2029-05-06T07:08:09+00:00",
        "active",
        "2029-05-06T07:08:09+00:00",
        "2029-05-06T07:08:09+00:00",
        None,
    )


def test_upgrade_never_rewrites_timestamps_on_later_starts(db: str, production: None) -> None:
    init_db(db)
    user = get_or_create_user("stable@example.com", None, db)
    init_db(db)
    init_db(db)
    after = get_all_users(db, include_inactive=True)
    assert [u["id"] for u in after] == [user["id"]]
    assert after[0]["updated_at"] == user["updated_at"]
    assert after[0]["status_changed_at"] == user["status_changed_at"]


def test_case_colliding_emails_do_not_block_startup_and_are_reported(
    db: str, production: None, caplog: pytest.LogCaptureFixture
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(_PRE_LIFECYCLE_USERS)
        for uid, email in (("a", "Dup@Example.com"), ("b", "dup@example.com")):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
                (uid, email, _NOW),
            )

    with caplog.at_level(logging.ERROR, logger="database"):
        init_db(db)

    assert "differ only by letter case" in caplog.text
    assert "dup@example.com" not in caplog.text.lower()
    with sqlite3.connect(db) as conn:
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert "idx_users_email_lower" not in indexes


# ---------------------------------------------------------------------------
# Every user-owned record has an owner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", USER_OWNED_TABLES)
@pytest.mark.parametrize("owner", [None, ""])
def test_an_ownerless_insert_is_rejected_on_every_owned_table(
    db: str, table: str, owner: str | None
) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn, pytest.raises(
        sqlite3.IntegrityError, match="user_id is required"
    ):
        _owned_row(conn, table, owner)


@pytest.mark.parametrize("table", USER_OWNED_TABLES)
def test_an_owned_row_can_never_be_orphaned_by_an_update(db: str, table: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        uid = _add_user(conn, "owner@example.com")
        _owned_row(conn, table, uid)
        for orphan in (None, ""):
            with pytest.raises(sqlite3.IntegrityError, match="user_id is required"):
                conn.execute(f"UPDATE {table} SET user_id = ?", (orphan,))
        assert conn.execute(
            f"SELECT count(*) FROM {table} WHERE user_id = ?", (uid,)
        ).fetchone() == (1,)


def test_owner_guards_are_reinstalled_by_every_start(db: str) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '%owner_required%'"
        ).fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
    init_db(db)
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '%owner_required%'"
        ).fetchone()[0]
    assert count == 2 * len(USER_OWNED_TABLES)


def test_ownership_registries_cannot_drift_from_the_schema(db: str) -> None:
    init_db(db)
    assert set(USER_OWNED_TABLES) == {table.name for table in OWNED_TABLES}
    with sqlite3.connect(db) as conn:
        with_user_id = {
            row[0]
            for row in conn.execute(
                "SELECT m.name FROM sqlite_master m "
                "JOIN pragma_table_info(m.name) c "
                "WHERE m.type = 'table' AND c.name = 'user_id'"
            ).fetchall()
        }
        assert with_user_id == set(USER_OWNED_TABLES) | {"user_identities"}
        assert "user_identities" in NOT_TRANSFERRED_TABLES
        for table in sorted(with_user_id):
            assert _user_id_is_indexed_first(conn, table), table


# ---------------------------------------------------------------------------
# No shared fallback tenant in production
# ---------------------------------------------------------------------------


def test_a_fresh_production_database_has_no_accounts_and_no_seed_rows(
    db: str, production: None
) -> None:
    init_db(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone() == (0,)
        for table in USER_OWNED_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    with pytest.raises(TenantRequiredError):
        get_legacy_user_id(db)


def test_local_mode_has_one_active_tenant_and_seed_rows(db: str) -> None:
    init_db(db)
    init_db(db)
    users = get_all_users(db, include_inactive=True)
    assert [u["email"] for u in users] == [LEGACY_TENANT_EMAIL]
    assert users[0]["status"] == "active"
    assert get_legacy_user_id(db) == users[0]["id"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM programme_state").fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM hevy_meta WHERE key = 'programme_start_date'"
        ).fetchone() == (1,)


def test_production_upgrade_holds_pre_account_rows_under_an_unclaimed_placeholder(
    db: str, production: None, caplog: pytest.LogCaptureFixture
) -> None:
    _create_pre_multi_tenant_schema(db)

    with caplog.at_level(logging.WARNING, logger="database"):
        init_db(db)

    users = get_all_users(db, include_inactive=True)
    assert [(u["email"], u["status"]) for u in users] == [
        (LEGACY_TENANT_EMAIL, "unclaimed")
    ]
    assert get_all_users(db) == []  # nobody can authenticate or be scheduled
    with sqlite3.connect(db) as conn:
        for table in (
            "workout_history",
            "exercise_progress",
            "body_metrics",
            "daily_log",
            "check_ins",
            "chat_messages",
            "programme_state",
            "hevy_meta",
            "hevy_routines",
            "reasoning_logs",
            "dashboard_insights",
            "deep_correlations",
        ):
            owners = conn.execute(f"SELECT DISTINCT user_id FROM {table}").fetchall()
            assert owners == [(users[0]["id"],)], table
    # Visible, actionable, and free of identifiers.
    assert "manage_accounts.py plan-legacy-assignment" in caplog.text
    assert users[0]["id"] not in caplog.text
    with pytest.raises(TenantRequiredError):
        get_legacy_user_id(db)


def test_production_start_demotes_an_active_placeholder_from_an_older_release(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_db(db)  # local mode: the placeholder is active and holds the seed rows
    placeholder = get_legacy_user_id(db)
    real = get_or_create_user("real@example.com", None, db)

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    init_db(db)

    by_email = {u["email"]: u for u in get_all_users(db, include_inactive=True)}
    assert by_email[LEGACY_TENANT_EMAIL]["id"] == placeholder
    assert by_email[LEGACY_TENANT_EMAIL]["status"] == "unclaimed"
    assert by_email[LEGACY_TENANT_EMAIL]["status_changed_at"] >= (
        by_email[LEGACY_TENANT_EMAIL]["created_at"]
    )
    assert by_email["real@example.com"]["status"] == "active"
    assert [u["id"] for u in get_all_users(db)] == [real["id"]]
    with sqlite3.connect(db) as conn:  # data untouched, still the placeholder's
        assert conn.execute(
            "SELECT count(*) FROM programme_state WHERE user_id = ?", (placeholder,)
        ).fetchone() == (1,)
