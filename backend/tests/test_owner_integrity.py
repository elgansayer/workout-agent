"""Database-level ownership integrity for every user-scoped table.

Application filters are not enough: a repository bug, a migration or an ad-hoc
statement must not be able to leave a record ownerless, give it to an account
that does not exist, hand it to another account, or point it at another
account's row. These tests prove each guarantee for *every* table that carries a
``user_id``, with the connection opened exactly as the runtime opens it
(``PRAGMA foreign_keys`` off), so they hold without relying on foreign keys.
All data is synthetic.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from database import (
    ACCOUNT_BOUND_TABLES,
    APPEND_ONLY_OWNED_TABLES,
    USER_OWNED_TABLES,
    init_db,
    owned_references,
)
from legacy_ownership import NOT_TRANSFERRED_TABLES
from owned_rows import insert_owned_row
from test_database import _user_id_is_indexed_first

_NOW = "2030-01-01T00:00:00+00:00"
OWNER_SCOPED_TABLES = (*USER_OWNED_TABLES, *ACCOUNT_BOUND_TABLES)

# Tables that carry ``user_id`` and are intentionally outside the guarded set,
# with the reason. A new user-scoped table must be added to one of the two
# registries or listed here, so none can be forgotten.
EXEMPT_TABLES = {
    "user_identities": "has its own existence and immutability triggers",
    "security_audit_events": "append-only audit trail; events may have no account",
    "account_deletion_requests": "deletion ledger must outlive the account",
    "account_tombstones": "deletion ledger must outlive the account",
}


#: Owner-scoped tables that declare no ``REFERENCES users(id)``: the staged health
#: platform types ``user_id`` as INTEGER while ``users.id`` is TEXT, and SQLite
#: cannot add a foreign key to an existing table. Their owner is enforced by the
#: existence trigger instead (asserted below for every table).
NO_DECLARED_OWNER_FK = {"health_connections", "health_metrics", "health_sync_runs"}


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "owner-integrity.db")
    init_db(path)
    return path


def _user(conn: sqlite3.Connection, name: str) -> str:
    uid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users (id, email, created_at, status_changed_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, f"{name}@example.com", _NOW, _NOW, _NOW),
    )
    return uid


def _triggers(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        )
    }


def _is_runtime_connection(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA foreign_keys").fetchone() == (0,)


# ---------------------------------------------------------------------------
# Every user-scoped table: owner relationship, index and constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", OWNER_SCOPED_TABLES)
def test_every_owner_scoped_table_has_an_indexed_enforced_owner(
    db: str, table: str
) -> None:
    with sqlite3.connect(db) as conn:
        assert _is_runtime_connection(conn)
        columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        assert "user_id" in columns, table

        # Non-null: a NOT NULL column, or the owner-required guard where the
        # column was added by ALTER TABLE (SQLite cannot add NOT NULL later).
        triggers = _triggers(conn, table)
        assert columns["user_id"][3] == 1 or {
            f"trg_{table}_owner_required_insert",
            f"trg_{table}_owner_required_update",
        } <= triggers, table

        # A declared relationship to the account, or the documented exemption.
        owners = {
            (row[2], row[3])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert ("users", "user_id") in owners or table in NO_DECLARED_OWNER_FK, table

        # Indexed access path with the owner first.
        assert _user_id_is_indexed_first(conn, table), table

        # Integrity constraints inside the database. Append-only tables never
        # accept an UPDATE, so only the insert-time existence guard applies.
        assert f"trg_{table}_owner_exists_insert" in triggers, table
        if table in APPEND_ONLY_OWNED_TABLES:
            assert any(name.endswith("append_only") for name in triggers), table
        else:
            assert {
                f"trg_{table}_owner_exists_update",
                f"trg_{table}_user_immutable",
            } <= triggers, table


def test_every_table_with_an_owner_column_is_classified(db: str) -> None:
    with sqlite3.connect(db) as conn:
        with_user_id = {
            row[0]
            for row in conn.execute(
                "SELECT m.name FROM sqlite_master m "
                "JOIN pragma_table_info(m.name) c "
                "WHERE m.type = 'table' AND c.name = 'user_id'"
            )
        }
    classified = set(OWNER_SCOPED_TABLES) | set(EXEMPT_TABLES)
    assert with_user_id == classified, sorted(with_user_id ^ classified)
    assert set(ACCOUNT_BOUND_TABLES) <= set(NOT_TRANSFERRED_TABLES)
    assert set(ACCOUNT_BOUND_TABLES).isdisjoint(USER_OWNED_TABLES)


@pytest.mark.parametrize("table", OWNER_SCOPED_TABLES)
@pytest.mark.parametrize("owner", [None, ""])
def test_an_ownerless_write_is_rejected(db: str, table: str, owner: str | None) -> None:
    with sqlite3.connect(db) as conn:
        uid = _user(conn, "owner")
        row = insert_owned_row(conn, table, uid)
        # NOT NULL columns refuse NULL themselves; every table refuses '' and
        # every table refuses NULL (by column or by guard).
        with pytest.raises(sqlite3.IntegrityError):
            insert_owned_row(conn, table, owner)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {table} SET user_id = ? WHERE rowid = ?", (owner, row))


# ---------------------------------------------------------------------------
# Owner exists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", OWNER_SCOPED_TABLES)
def test_a_row_cannot_name_an_owner_that_does_not_exist(db: str, table: str) -> None:
    with sqlite3.connect(db) as conn:
        before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        with pytest.raises(
            sqlite3.IntegrityError, match="must reference an existing user"
        ):
            insert_owned_row(conn, table, str(uuid.uuid4()))
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == before


# ---------------------------------------------------------------------------
# Owner is immutable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", OWNER_SCOPED_TABLES)
def test_an_owned_row_cannot_be_reassigned_to_another_account(
    db: str, table: str
) -> None:
    with sqlite3.connect(db) as conn:
        alice, bob = _user(conn, "alice"), _user(conn, "bob")
        row = insert_owned_row(conn, table, alice)
        refusal = "append-only" if table in APPEND_ONLY_OWNED_TABLES else "is immutable"
        with pytest.raises(sqlite3.IntegrityError, match=refusal):
            conn.execute(f"UPDATE {table} SET user_id = ? WHERE rowid = ?", (bob, row))
        assert conn.execute(
            f"SELECT user_id FROM {table} WHERE rowid = ?", (row,)
        ).fetchone() == (alice,)


def test_an_unowned_row_can_still_be_given_its_first_owner(db: str) -> None:
    """The pre-account backfill assigns rows that have no owner yet."""
    adoptable = 0
    with sqlite3.connect(db) as conn:
        owner = _user(conn, "owner")
        for table in USER_OWNED_TABLES:
            nullable = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if any(col[1] == "user_id" and col[3] == 0 for col in nullable):
                # Reproduce a legacy row: lift only the owner-required guards.
                for guard in ("insert", "update"):
                    conn.execute(f"DROP TRIGGER trg_{table}_owner_required_{guard}")
                conn.execute(f"DROP TRIGGER trg_{table}_owner_exists_insert")
                row = insert_owned_row(conn, table, None)
                conn.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (owner,)
                )
                assert conn.execute(
                    f"SELECT user_id FROM {table} WHERE rowid = ?", (row,)
                ).fetchone() == (owner,)
                adoptable += 1
    assert adoptable >= 8  # the legacy tables whose owner column is nullable


# ---------------------------------------------------------------------------
# References stay inside the tenant
# ---------------------------------------------------------------------------


def _edges(db: str) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(db) as conn:
        return owned_references(conn.cursor())


def test_the_owner_reference_map_covers_the_programme_domain(db: str) -> None:
    edges = set(_edges(db))
    assert ("programme_revisions", "programme_id", "programmes", "id") in edges
    assert ("programme_routines", "revision_id", "programme_revisions", "id") in edges
    assert ("adaptation_decisions", "from_revision_id", "programme_revisions", "id") in edges
    # Not declared as foreign keys, but still references into another record.
    assert ("programmes", "current_revision_id", "programme_revisions", "id") in edges
    assert ("routine_workout_matches", "programme_id", "programmes", "id") in edges
    for child, _column, parent, _parent_column in edges:
        assert child in USER_OWNED_TABLES and parent in USER_OWNED_TABLES


def _edge_ids(edge: tuple[str, str, str, str]) -> str:
    return f"{edge[0]}.{edge[1]}"


@pytest.fixture()
def edges(db: str) -> list[tuple[str, str, str, str]]:
    return _edges(db)


def test_every_reference_rejects_another_tenants_row(
    db: str, edges: list[tuple[str, str, str, str]]
) -> None:
    assert len(edges) >= 15
    failures: list[str] = []
    with sqlite3.connect(db) as conn:
        alice, bob = _user(conn, "alice"), _user(conn, "bob")
        for child, column, parent, parent_column in edges:
            alice_parent = insert_owned_row(conn, parent, alice)
            alice_key = conn.execute(
                f"SELECT {parent_column} FROM {parent} WHERE rowid = ?", (alice_parent,)
            ).fetchone()[0]
            bob_parent = insert_owned_row(conn, parent, bob)
            bob_key = conn.execute(
                f"SELECT {parent_column} FROM {parent} WHERE rowid = ?", (bob_parent,)
            ).fetchone()[0]

            # Writing bob's row so it points at alice's parent is refused.
            try:
                insert_owned_row(conn, child, bob, **{column: alice_key})
            except sqlite3.IntegrityError as exc:
                if f"{child}.{column} must reference a row owned" not in str(exc):
                    failures.append(f"{child}.{column}: wrong error on insert: {exc}")
            else:
                failures.append(f"{child}.{column}: cross-tenant insert was accepted")

            # Pointing it at his own parent works ...
            own = insert_owned_row(conn, child, bob, **{column: bob_key})
            # ... and an existing row cannot be re-pointed at alice's parent.
            try:
                conn.execute(
                    f"UPDATE {child} SET {column} = ? WHERE rowid = ?", (alice_key, own)
                )
            except sqlite3.IntegrityError as exc:
                if f"{child}.{column} must reference a row owned" not in str(exc):
                    failures.append(f"{child}.{column}: wrong error on update: {exc}")
            else:
                failures.append(f"{child}.{column}: cross-tenant update was accepted")
    assert failures == []


def test_a_same_tenant_reference_and_an_unset_reference_are_accepted(db: str) -> None:
    with sqlite3.connect(db) as conn:
        alice = _user(conn, "alice")
        programme = insert_owned_row(conn, "programmes", alice)
        revision = insert_owned_row(
            conn,
            "programme_revisions",
            alice,
            programme_id=conn.execute(
                "SELECT id FROM programmes WHERE rowid = ?", (programme,)
            ).fetchone()[0],
        )
        revision_id = conn.execute(
            "SELECT id FROM programme_revisions WHERE rowid = ?", (revision,)
        ).fetchone()[0]
        # A nullable reference may stay unset, and a programme may point at its
        # own revision.
        conn.execute(
            "UPDATE programmes SET current_revision_id = ? WHERE rowid = ?",
            (revision_id, programme),
        )
        conn.execute(
            "UPDATE programmes SET current_revision_id = NULL WHERE rowid = ?",
            (programme,),
        )


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def test_integrity_guards_are_reinstalled_by_every_start(db: str) -> None:
    def integrity_guards() -> set[str]:
        with sqlite3.connect(db) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND ("
                    "name LIKE '%owner_exists%' OR name LIKE '%user_immutable' "
                    "OR name LIKE '%same_owner%')"
                )
            }

    installed = integrity_guards()
    append_only = len(APPEND_ONLY_OWNED_TABLES)
    assert len(installed) >= 3 * (len(OWNER_SCOPED_TABLES) - append_only) + append_only
    with sqlite3.connect(db) as conn:
        for name in installed:
            conn.execute(f'DROP TRIGGER "{name}"')
    assert integrity_guards() == set()
    init_db(db)
    assert integrity_guards() == installed
    init_db(db)
    assert integrity_guards() == installed
