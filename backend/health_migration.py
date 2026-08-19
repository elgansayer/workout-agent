"""Apply the additive canonical health schema to an existing SQLite connection."""

from __future__ import annotations

import sqlite3

from health_schema import HEALTH_SCHEMA_SQL


def migrate_health_schema(connection: sqlite3.Connection) -> None:
    """Apply only additive CREATE TABLE/INDEX statements.

    Existing body_metrics data is deliberately left intact. A later migration
    can backfill canonical rows once existing ownership semantics are verified.
    """
    connection.executescript(HEALTH_SCHEMA_SQL)
    connection.commit()
