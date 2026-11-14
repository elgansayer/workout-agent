"""Build one valid synthetic row for any owner-scoped table.

Shared by the schema tests so they can insert real parent/child chains now that
the database refuses a reference that points into another tenant. Every value is
synthetic; nothing here resembles health, workout or account data.
"""

from __future__ import annotations

import itertools
import sqlite3
from typing import Any

from database import _connect, owned_references

_NOW = "2030-01-01T00:00:00+00:00"
_UNIQUE_SUFFIX = itertools.count(1)

# Columns whose synthetic default would violate a CHECK constraint.
_TABLE_VALUES: dict[str, dict[str, Any]] = {
    "account_action_tokens": {"purpose": "recovery"},
    "record_provenance": {"kind": "imported", "supersedes_id": None, "held_by_id": None},
    "user_api_keys": {"status": "active"},
}


def insert_owned_row(
    conn: sqlite3.Connection,
    table: str,
    user_id: Any,
    *,
    _chain: tuple[str, ...] = (),
    **values: Any,
) -> int:
    """Insert one row into *table* owned by *user_id* and return its ``rowid``.

    Rows a ``NOT NULL`` reference points at are created first, owned by the same
    user, so the row satisfies the same-tenant reference guards. Nullable
    references stay unset unless the caller supplies them, which also keeps the
    programme/revision cycle from recursing.
    """

    references = {
        column: (parent, parent_column)
        for child, column, parent, parent_column in owned_references(conn.cursor())
        if child == table
    }
    required = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if row[3]
    }
    given = {**_TABLE_VALUES.get(table, {}), **values}
    for column, (parent, parent_column) in references.items():
        if column in given:
            continue
        if column not in required:
            given[column] = None
            continue
        if parent == table or parent in _chain:
            raise AssertionError(f"required reference cycle at {table}.{column}")
        parent_rowid = insert_owned_row(
            conn, parent, user_id, _chain=(*_chain, table)
        )
        given[column] = conn.execute(
            f"SELECT {parent_column} FROM {parent} WHERE rowid = ?", (parent_rowid,)
        ).fetchone()[0]

    names: list[str] = []
    params: list[Any] = []
    for _cid, name, column_type, _notnull, _default, pk in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        kind = column_type.upper()
        if name == "user_id":
            value: Any = user_id
        elif name in given:
            value = given[name]
        elif pk and kind == "INTEGER" and name == "id":
            continue  # rowid alias
        elif kind == "INTEGER":
            value = 1
        elif kind == "REAL":
            value = 1.0
        else:
            # Unique per call so a second row never collides on a UNIQUE key.
            value = f"synthetic-{next(_UNIQUE_SUFFIX)}"
        names.append(name)
        params.append(value)
    placeholders = ", ".join("?" for _ in names)
    cursor = conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})", params
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def ensure_users(db_path: str, *user_ids: str) -> None:
    """Create an account for each id so rows may name it as their owner.

    The database refuses a row whose owner is not a real account, so a test
    that wants the stable ids ``"user-a"`` and ``"user-b"`` registers them first.
    """

    with _connect(db_path) as conn:
        for user_id in user_ids:
            conn.execute(
                "INSERT OR IGNORE INTO users "
                "(id, email, created_at, status_changed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    f"{user_id}@example.test",
                    _NOW,
                    _NOW,
                    _NOW,
                ),
            )
