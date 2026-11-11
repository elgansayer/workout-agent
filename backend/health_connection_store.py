"""Tenant-scoped reads for canonical health connection rows."""

from __future__ import annotations

import sqlite3
from typing import Any

from database import DEFAULT_DB_PATH, _connect

_COLUMNS = (
    "id",
    "provider",
    "state",
    "granted_capabilities",
    "cursor",
    "last_sync_at",
    "created_at",
    "updated_at",
)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {name: row[index] for index, name in enumerate(_COLUMNS)}


def list_connections(user_id: str | int, db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return only the authenticated tenant's connections."""

    if not str(user_id).strip():
        raise ValueError("listing health connections requires a user_id")
    with _connect(db_path) as connection:
        if not _table_exists(connection, "health_connections"):
            return []
        rows = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM health_connections "
            "WHERE user_id = ? ORDER BY provider, id",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_connection(
    user_id: str | int,
    connection_id: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return one connection only when it belongs to the authenticated tenant."""

    if not str(user_id).strip():
        raise ValueError("reading a health connection requires a user_id")
    if not connection_id.strip():
        raise ValueError("connection_id is required")
    with _connect(db_path) as connection:
        if not _table_exists(connection, "health_connections"):
            return None
        row = connection.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM health_connections "
            "WHERE user_id = ? AND id = ?",
            (user_id, connection_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None
