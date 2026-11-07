"""Unit tests for the tenant ownership context and database ownership contract."""

from __future__ import annotations

import copy
import dataclasses
import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import database
import pytest
from ownership import (
    _MINT_TOKEN,
    CASCADE_ON_DELETE,
    LEGACY_TENANT_EMAIL,
    USER_SCOPED_TABLE_NAMES,
    USER_SCOPED_TABLES,
    USERS_TABLE_SQL,
    AuthenticatedOwner,
    InvalidOwnerContextError,
    OwnershipConstraintError,
    UnauthenticatedOwnerError,
    UnknownOwnerError,
    UserScopedTable,
    audit_ownership_constraints,
    enforce_ownership_constraints,
    find_unregistered_owner_tables,
    install_owner_immutability_triggers,
    owner_for_email,
    owner_for_legacy_tenant,
    owner_for_user,
    owner_from_session,
    owner_id,
    owner_immutability_trigger_sql,
    registered_table,
)


def _db(tmp_path: Path, name: str = "ownership.db") -> str:
    return str(tmp_path / name)


# ---------------------------------------------------------------------------
# AuthenticatedOwner: a verified context that cannot be forged from an id
# ---------------------------------------------------------------------------


def test_owner_cannot_be_constructed_from_a_plain_identifier() -> None:
    with pytest.raises(TypeError, match="ownership.py factories"):
        AuthenticatedOwner("victim-id")
    with pytest.raises(TypeError, match="ownership.py factories"):
        AuthenticatedOwner("victim-id", object())  # a guessed token is not the token


def test_minted_owner_requires_a_non_empty_identifier() -> None:
    for blank in ("", "   ", None, 7):
        with pytest.raises(ValueError, match="non-empty user id"):
            AuthenticatedOwner(blank, _MINT_TOKEN)  # type: ignore[arg-type]


def test_owner_is_immutable_and_cannot_be_retargeted() -> None:
    owner = AuthenticatedOwner("user-a", _MINT_TOKEN)

    with pytest.raises(AttributeError, match="immutable"):
        owner.user_id = "user-b"  # type: ignore[misc]
    with pytest.raises(AttributeError, match="immutable"):
        owner._user_id = "user-b"
    with pytest.raises(AttributeError, match="immutable"):
        del owner._user_id
    with pytest.raises(AttributeError):
        owner.extra = "value"  # type: ignore[attr-defined]  # slotted: no new state
    assert owner.user_id == "user-a"


def test_owner_cannot_be_copied_onto_another_identity() -> None:
    owner = AuthenticatedOwner("user-a", _MINT_TOKEN)

    # dataclasses.replace() used to carry the private mint token across and
    # forge an owner for any identifier.
    with pytest.raises(TypeError):
        dataclasses.replace(owner, user_id="user-b")  # type: ignore[type-var]
    assert not dataclasses.is_dataclass(owner)

    assert copy.copy(owner) is owner
    assert copy.deepcopy(owner) is owner


def test_owner_cannot_be_subclassed_or_serialised() -> None:
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Forged(AuthenticatedOwner):  # type: ignore[misc]
            pass

    owner = AuthenticatedOwner("user-a", _MINT_TOKEN)
    with pytest.raises(TypeError, match="cannot be serialised"):
        pickle.dumps(owner)


def test_owner_equality_hash_and_repr_are_identity_based() -> None:
    first = AuthenticatedOwner("user-a", _MINT_TOKEN)
    same = AuthenticatedOwner("user-a", _MINT_TOKEN)
    other = AuthenticatedOwner("user-b", _MINT_TOKEN)

    assert first == same
    assert hash(first) == hash(same)
    assert first != other
    assert len({first, same, other}) == 2
    # A bare identifier never compares equal to a verified context.
    assert first != "user-a"
    assert "user-a" in repr(first)


@pytest.mark.parametrize(
    "look_alike",
    [
        "user-a",
        None,
        7,
        object(),
        SimpleNamespace(user_id="user-a"),
        MagicMock(user_id="user-a"),
        {"user_id": "user-a"},
    ],
    ids=["str", "none", "int", "object", "namespace", "mock", "dict"],
)
def test_owner_id_rejects_anything_that_is_not_an_authenticated_owner(
    look_alike: Any,
) -> None:
    with pytest.raises(InvalidOwnerContextError, match="authenticated owner"):
        owner_id(look_alike)
    # The failure is also a TypeError so a stray caller cannot swallow it as a
    # lookup miss.
    assert issubclass(InvalidOwnerContextError, TypeError)


def test_owner_id_returns_the_verified_identifier() -> None:
    assert owner_id(AuthenticatedOwner("user-a", _MINT_TOKEN)) == "user-a"


# ---------------------------------------------------------------------------
# Owner factories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session",
    [
        None,
        "not-a-mapping",
        {},
        {"user": "athlete@example.test"},
        {"user_id": "user-a"},
        {"user": "athlete@example.test", "user_id": ""},
        {"user": "athlete@example.test", "user_id": "   "},
        {"user": "athlete@example.test", "user_id": 12},
        {"user": "", "user_id": "user-a"},
        {"user": "   ", "user_id": "user-a"},
        {"user": {}, "user_id": "user-a"},
        {"user": ["athlete@example.test"], "user_id": "user-a"},
    ],
)
def test_owner_from_session_fails_closed_without_a_complete_identity(
    session: Any,
) -> None:
    with pytest.raises(UnauthenticatedOwnerError):
        owner_from_session(session)


def test_owner_from_session_mints_from_a_complete_signed_identity() -> None:
    owner = owner_from_session({"user": "athlete@example.test", "user_id": " user-a "})
    assert owner.user_id == "user-a"
    assert owner_from_session(
        {"user": {"email": "athlete@example.test"}, "user_id": "user-a"}
    ) == owner
    assert owner_from_session({"user": {"id": "profile-1"}, "user_id": "user-a"}) == owner


def test_owner_for_user_only_resolves_stored_users(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    stored = database.get_or_create_user("a@example.test", "A", db)

    assert owner_for_user(db, stored["id"]).user_id == stored["id"]
    assert owner_for_user(db, f"  {stored['id']}  ").user_id == stored["id"]
    for unknown in ("not-a-user", "", "   ", None, 5):
        with pytest.raises(UnknownOwnerError):
            owner_for_user(db, unknown)  # type: ignore[arg-type]


def test_owner_for_user_reports_unreadable_databases_as_unknown_owner(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()  # a database without a users table
    with pytest.raises(UnknownOwnerError, match="Could not verify"):
        owner_for_user(str(empty), "user-a")
    with pytest.raises(UnknownOwnerError, match="Could not verify"):
        owner_for_email(str(empty), "a@example.test")


def test_owner_for_email_and_legacy_tenant_resolve_through_users(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    stored = database.get_or_create_user("a@example.test", "A", db)

    assert owner_for_email(db, " a@example.test ").user_id == stored["id"]
    for unknown in ("missing@example.test", "", "  ", None):
        with pytest.raises(UnknownOwnerError):
            owner_for_email(db, unknown)  # type: ignore[arg-type]

    legacy = owner_for_legacy_tenant(db)
    assert legacy.user_id == database.get_legacy_user_id(db)
    assert legacy.user_id != stored["id"]


def test_legacy_tenant_is_unavailable_until_the_schema_is_initialised(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(USERS_TABLE_SQL)
    with pytest.raises(UnknownOwnerError, match="legacy|owner email"):
        owner_for_legacy_tenant(db)
    assert LEGACY_TENANT_EMAIL == "legacy@local"


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_entries_are_unique_and_well_formed() -> None:
    names = [table.name for table in USER_SCOPED_TABLES]
    assert len(names) == len(set(names))
    assert set(names) == set(USER_SCOPED_TABLE_NAMES)
    for table in USER_SCOPED_TABLES:
        assert table.owner_column == "user_id"
        assert table.require_not_null and table.require_owner_index
        assert table.require_immutable_owner
        assert table.reference
        assert table.immutability_trigger == f"trg_{table.name}_user_id_immutable"


def test_only_the_staged_health_tables_defer_the_owner_foreign_key() -> None:
    deferred = {t.name for t in USER_SCOPED_TABLES if not t.require_foreign_key}
    assert deferred == {"health_connections", "health_metrics", "health_sync_runs"}
    cascading = {t.name for t in USER_SCOPED_TABLES if t.on_delete == CASCADE_ON_DELETE}
    assert cascading == {"csrf_tokens"}


def test_registered_table_fails_closed_for_unknown_names() -> None:
    assert registered_table("notifications").name == "notifications"
    with pytest.raises(OwnershipConstraintError, match="not a registered"):
        registered_table("rogue_table")


# ---------------------------------------------------------------------------
# Audit: one test per breach of the contract
# ---------------------------------------------------------------------------

_WIDGETS = UserScopedTable("widgets", reference="synthetic widgets")


def _conn_with_users() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(USERS_TABLE_SQL)
    return conn


def _compliant_widgets(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id), label TEXT)"
    )
    conn.execute("CREATE INDEX idx_widgets_user ON widgets (user_id, id)")
    install_owner_immutability_triggers(conn, tables=(_WIDGETS,))


def test_audit_accepts_a_table_that_meets_the_whole_contract() -> None:
    conn = _conn_with_users()
    _compliant_widgets(conn)
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,)) == ()
    enforce_ownership_constraints(conn, tables=(_WIDGETS,))


def test_audit_ignores_tables_that_do_not_exist_yet() -> None:
    conn = _conn_with_users()
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,)) == ()


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        (
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT)",
            "missing owner column user_id",
        ),
        (
            (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                "user_id TEXT REFERENCES users(id), label TEXT)"
            ),
            "allows null owners",
        ),
        (
            (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                "user_id TEXT NOT NULL, label TEXT)"
            ),
            "does not reference users(id)",
        ),
        (
            (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                "user_id TEXT NOT NULL REFERENCES other_table(id), label TEXT)"
            ),
            "does not reference users(id)",
        ),
        (
            (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "label TEXT)"
            ),
            "ON DELETE CASCADE but the contract requires ON DELETE NO ACTION",
        ),
        (
            (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
                "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE SET NULL, "
                "label TEXT)"
            ),
            "ON DELETE SET NULL but the contract requires ON DELETE NO ACTION",
        ),
    ],
    ids=[
        "no-owner-column",
        "nullable-owner",
        "no-foreign-key",
        "foreign-key-to-wrong-table",
        "silent-cascade",
        "set-null",
    ],
)
def test_audit_reports_each_schema_breach(ddl: str, expected: str) -> None:
    conn = _conn_with_users()
    conn.execute(ddl)
    conn.execute("CREATE TABLE other_table (id TEXT PRIMARY KEY)")
    if "user_id" in ddl:
        conn.execute("CREATE INDEX idx_widgets_user ON widgets (user_id, id)")
        install_owner_immutability_triggers(conn, tables=(_WIDGETS,))

    violations = audit_ownership_constraints(conn, tables=(_WIDGETS,))

    assert violations, "the breach was not detected"
    assert any(expected in str(violation) for violation in violations), violations
    assert all(violation.table == "widgets" for violation in violations)
    with pytest.raises(OwnershipConstraintError, match="widgets"):
        enforce_ownership_constraints(conn, tables=(_WIDGETS,))


def test_audit_requires_a_cascade_for_tables_whose_contract_says_so() -> None:
    nonce_table = UserScopedTable("nonces", on_delete=CASCADE_ON_DELETE)
    conn = _conn_with_users()
    conn.execute(
        "CREATE TABLE nonces (id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id))"
    )
    conn.execute("CREATE INDEX idx_nonces_user ON nonces (user_id)")
    install_owner_immutability_triggers(conn, tables=(nonce_table,))

    violations = audit_ownership_constraints(conn, tables=(nonce_table,))
    assert [str(v) for v in violations] == [
        (
            "nonces: owner foreign key uses ON DELETE NO ACTION but the contract "
            "requires ON DELETE CASCADE"
        )
    ]


def test_audit_requires_an_index_that_leads_with_the_owner() -> None:
    conn = _conn_with_users()
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id), label TEXT)"
    )
    install_owner_immutability_triggers(conn, tables=(_WIDGETS,))
    assert [str(v) for v in audit_ownership_constraints(conn, tables=(_WIDGETS,))] == [
        "widgets: no index leads with user_id"
    ]

    # An index on the owner that is not the leading column is not an access
    # path for owner-filtered queries.
    conn.execute("CREATE INDEX idx_widgets_label_user ON widgets (label, user_id)")
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,))

    # A composite primary key that starts with the owner satisfies the contract.
    conn.execute("CREATE INDEX idx_widgets_user ON widgets (user_id, label)")
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,)) == ()


def test_audit_requires_the_owner_immutability_trigger() -> None:
    conn = _conn_with_users()
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id), label TEXT)"
    )
    conn.execute("CREATE INDEX idx_widgets_user ON widgets (user_id, id)")
    assert [str(v) for v in audit_ownership_constraints(conn, tables=(_WIDGETS,))] == [
        (
            "widgets: owner column user_id is not protected by trigger "
            "trg_widgets_user_id_immutable"
        )
    ]

    # A trigger with the right name that does not actually abort is not enough.
    conn.execute(
        "CREATE TRIGGER trg_widgets_user_id_immutable "
        "BEFORE UPDATE OF user_id ON widgets FOR EACH ROW "
        "WHEN NEW.user_id IS NOT OLD.user_id BEGIN SELECT 1; END"
    )
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,))
    conn.execute("DROP TRIGGER trg_widgets_user_id_immutable")

    # A trigger that only guards a different column does not protect the owner.
    conn.execute(
        "CREATE TRIGGER trg_widgets_user_id_immutable "
        "BEFORE UPDATE OF label ON widgets FOR EACH ROW "
        "WHEN NEW.label IS NOT OLD.label BEGIN SELECT RAISE(ABORT, 'x'); END"
    )
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,))
    conn.execute("DROP TRIGGER trg_widgets_user_id_immutable")

    install_owner_immutability_triggers(conn, tables=(_WIDGETS,))
    assert audit_ownership_constraints(conn, tables=(_WIDGETS,)) == ()


def test_contract_flags_allow_a_documented_exception_to_be_audited() -> None:
    relaxed = UserScopedTable(
        "widgets",
        require_foreign_key=False,
        require_owner_index=False,
        require_immutable_owner=False,
    )
    conn = _conn_with_users()
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)"
    )
    assert audit_ownership_constraints(conn, tables=(relaxed,)) == ()
    # The non-null owner is never optional.
    conn.execute("DROP TABLE widgets")
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, user_id INTEGER)")
    assert [str(v) for v in audit_ownership_constraints(conn, tables=(relaxed,))] == [
        "widgets: owner column user_id allows null owners"
    ]


# ---------------------------------------------------------------------------
# Trigger behaviour
# ---------------------------------------------------------------------------


def test_trigger_blocks_reassigning_a_record_to_another_owner() -> None:
    conn = _conn_with_users()
    for user in ("user-a", "user-b"):
        conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, 'now')",
            (user, f"{user}@example.test"),
        )
    _compliant_widgets(conn)
    conn.execute("INSERT INTO widgets (id, user_id, label) VALUES (1, 'user-a', 'one')")

    with pytest.raises(sqlite3.IntegrityError, match="owner is immutable: widgets.user_id"):
        conn.execute("UPDATE widgets SET user_id = 'user-b' WHERE id = 1")
    assert conn.execute("SELECT user_id FROM widgets WHERE id = 1").fetchone() == ("user-a",)

    # Restating the same owner and changing other columns stays allowed, so
    # upserts that rewrite a row for its own tenant keep working.
    conn.execute("UPDATE widgets SET user_id = 'user-a', label = 'two' WHERE id = 1")
    conn.execute("UPDATE widgets SET label = 'three' WHERE id = 1")
    assert conn.execute("SELECT label FROM widgets WHERE id = 1").fetchone() == ("three",)


def test_trigger_also_blocks_reassignment_through_an_upsert() -> None:
    conn = _conn_with_users()
    for user in ("user-a", "user-b"):
        conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, 'now')",
            (user, f"{user}@example.test"),
        )
    conn.execute(
        "CREATE TABLE widgets (id INTEGER PRIMARY KEY, "
        "user_id TEXT NOT NULL REFERENCES users(id), label TEXT)"
    )
    conn.execute("CREATE INDEX idx_widgets_user ON widgets (user_id, id)")
    install_owner_immutability_triggers(conn, tables=(_WIDGETS,))
    conn.execute("INSERT INTO widgets (id, user_id, label) VALUES (1, 'user-a', 'one')")

    with pytest.raises(sqlite3.IntegrityError, match="owner is immutable"):
        conn.execute(
            "INSERT INTO widgets (id, user_id, label) VALUES (1, 'user-b', 'stolen') "
            "ON CONFLICT(id) DO UPDATE SET user_id = excluded.user_id, "
            "label = excluded.label"
        )
    assert conn.execute("SELECT user_id, label FROM widgets").fetchall() == [
        ("user-a", "one")
    ]


def test_install_triggers_is_idempotent_and_skips_missing_tables_and_columns() -> None:
    conn = _conn_with_users()
    _compliant_widgets(conn)
    before = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()

    ghost = UserScopedTable("ghost_table")
    no_owner = UserScopedTable("plain")
    conn.execute("CREATE TABLE plain (id INTEGER PRIMARY KEY)")
    install_owner_immutability_triggers(conn, tables=(_WIDGETS, ghost, no_owner))
    install_owner_immutability_triggers(conn, tables=(_WIDGETS,))

    after = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'").fetchall()
    assert before == after


def test_trigger_ddl_never_interpolates_unsafe_identifiers() -> None:
    for unsafe in ("widgets; DROP TABLE users", "a b", "", "1abc", 'x"y'):
        with pytest.raises(OwnershipConstraintError, match="Unsafe SQL identifier"):
            owner_immutability_trigger_sql(UserScopedTable(unsafe))
    with pytest.raises(OwnershipConstraintError, match="Unsafe SQL identifier"):
        owner_immutability_trigger_sql(
            UserScopedTable("widgets", owner_column="user_id; --")
        )

    # A quoted, oddly named table that does exist fails the audit closed rather
    # than being queried through string interpolation.
    conn = _conn_with_users()
    conn.execute('CREATE TABLE "odd name" (id INTEGER PRIMARY KEY, user_id TEXT)')
    with pytest.raises(OwnershipConstraintError, match="Unsafe SQL identifier"):
        audit_ownership_constraints(conn, tables=(UserScopedTable("odd name"),))


# ---------------------------------------------------------------------------
# Registry versus the real schema
# ---------------------------------------------------------------------------


def test_fresh_database_satisfies_the_whole_contract(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        assert audit_ownership_constraints(conn) == ()
        assert find_unregistered_owner_tables(conn) == ()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    # Nothing registered is dead: every non-staged registration is a real table.
    staged = {name for name in USER_SCOPED_TABLE_NAMES if name.startswith("health_")}
    assert USER_SCOPED_TABLE_NAMES - staged <= tables


def test_a_table_with_an_owner_column_must_be_registered(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE rogue_notes (id INTEGER PRIMARY KEY, user_id TEXT)")
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, label TEXT)")
        assert find_unregistered_owner_tables(conn) == ("rogue_notes",)


def test_init_db_installs_triggers_on_every_registered_table(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
    expected = {
        table.immutability_trigger
        for table in USER_SCOPED_TABLES
        if not table.name.startswith("health_")
    }
    assert expected <= triggers


def test_restart_repairs_dropped_indexes_and_triggers(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER trg_notifications_user_id_immutable")
        conn.execute("DROP INDEX idx_notifications_user_created")
        conn.execute("DROP INDEX idx_notifications_user_unread")
        assert len(audit_ownership_constraints(conn)) == 2

    # Indexes and triggers are idempotent DDL, so a restart restores the
    # contract instead of running with an unprotected table.
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        assert audit_ownership_constraints(conn) == ()


def test_startup_fails_closed_when_drift_cannot_be_repaired(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE notifications")
        # A nullable owner is not something idempotent DDL can safely repair.
        conn.execute(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id TEXT REFERENCES users(id), type TEXT NOT NULL, "
            "title TEXT NOT NULL, message TEXT NOT NULL, link TEXT, "
            "is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
        )

    with pytest.raises(OwnershipConstraintError, match="notifications.*allows null owners"):
        database.init_db(db)


# ---------------------------------------------------------------------------
# Legacy upgrade paths
# ---------------------------------------------------------------------------


def _legacy_workout_database(db: str) -> tuple[str, str]:
    """Create a pre-constraint database holding two tenants and an unowned row."""

    with sqlite3.connect(db) as conn:
        conn.execute(USERS_TABLE_SQL)
        for user in ("user-a", "user-b"):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, 'now')",
                (user, f"{user}@example.test"),
            )
        conn.execute(
            "CREATE TABLE workout_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "date TEXT NOT NULL, hevy_payload TEXT NOT NULL, user_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO workout_history (date, hevy_payload, user_id) VALUES (?, ?, ?)",
            [
                ("2026-01-01", "{}", "user-a"),
                ("2026-01-02", "{}", "user-b"),
                ("2026-01-03", "{}", None),
            ],
        )
    return "user-a", "user-b"


def test_upgrade_enforces_the_contract_without_losing_or_reassigning_rows(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _legacy_workout_database(db)

    database.init_db(db)
    database.init_db(db)  # idempotent

    with sqlite3.connect(db) as conn:
        assert audit_ownership_constraints(conn) == ()
        rows = conn.execute(
            "SELECT date, user_id FROM workout_history ORDER BY date"
        ).fetchall()
        legacy = conn.execute(
            "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
        ).fetchone()[0]
    assert rows == [
        ("2026-01-01", "user-a"),
        ("2026-01-02", "user-b"),
        ("2026-01-03", legacy),  # only the unowned row goes to the legacy tenant
    ]


def test_upgrade_refuses_to_reassign_rows_owned_by_a_missing_user(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _legacy_workout_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload, user_id) "
            "VALUES ('2026-01-04', '{}', 'deleted-user')"
        )

    with pytest.raises(OwnershipConstraintError, match="does not exist"):
        database.init_db(db)

    # The failed upgrade rolled back: nothing was rebuilt, dropped or reassigned.
    with sqlite3.connect(db) as conn:
        columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(workout_history)")}
        owners = sorted(
            row[0]
            for row in conn.execute("SELECT user_id FROM workout_history")
            if row[0] is not None
        )
    assert columns["user_id"] == 0  # still the original, nullable column
    assert owners == ["deleted-user", "user-a", "user-b"]


def test_upgrade_refuses_to_drop_columns_the_contract_does_not_know(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _legacy_workout_database(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE workout_history ADD COLUMN operator_note TEXT")

    with pytest.raises(OwnershipConstraintError, match="operator_note"):
        database.init_db(db)

    with sqlite3.connect(db) as conn:
        names = {row[1] for row in conn.execute("PRAGMA table_info(workout_history)")}
        assert "operator_note" in names
        assert conn.execute("SELECT COUNT(*) FROM workout_history").fetchone() == (3,)


def test_upgrade_rebuilds_a_table_whose_delete_policy_drifted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    owner = owner_for_legacy_tenant(db)
    database.save_workout({"id": "w-1"}, db, "2026-01-01", owner=owner)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE workout_history RENAME TO workout_history_old")
        conn.execute(
            "CREATE TABLE workout_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "date TEXT NOT NULL, hevy_payload TEXT NOT NULL, "
            "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE)"
        )
        conn.execute("INSERT INTO workout_history SELECT * FROM workout_history_old")
        conn.execute("DROP TABLE workout_history_old")
        assert audit_ownership_constraints(conn)  # the cascade is a violation

    database.init_db(db)

    with sqlite3.connect(db) as conn:
        assert audit_ownership_constraints(conn) == ()
        assert conn.execute("SELECT COUNT(*) FROM workout_history").fetchone() == (1,)


def test_upgrade_purges_orphaned_csrf_nonces_instead_of_blocking_startup(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(USERS_TABLE_SQL)
        # The earlier token store had no owner foreign key, so a nonce for an
        # unknown user could exist.
        conn.execute(
            "CREATE TABLE csrf_tokens (token_hash TEXT PRIMARY KEY, "
            "session_hash TEXT NOT NULL, user_id TEXT NOT NULL, "
            "expires_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO csrf_tokens VALUES ('hash', 'session', 'ghost-user', 4102444800)"
        )

    database.init_db(db)

    with sqlite3.connect(db) as conn:
        assert audit_ownership_constraints(conn) == ()
        assert conn.execute("SELECT COUNT(*) FROM csrf_tokens").fetchone() == (0,)


def test_connect_enforces_foreign_keys_on_every_repository_connection(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    database.init_db(db)
    with database._connect(db) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO notifications "
                "(user_id, type, title, message, is_read, created_at) "
                "VALUES ('no-such-user', 't', 't', 'm', 0, 'now')"
            )
