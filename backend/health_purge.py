"""Provider-scoped purge of imported health data and derived aggregates.

Purge is intentionally narrower than account deletion. It removes only:

* canonical health records imported by one connector/provider,
* the sync-run metadata for that connector,
* the stored connection row (so a later reconnect starts clean and is
  idempotent),
* cached/derived insights and aggregates for the owning user.

Rows that belong to other tenants or to other providers are never selected.
Manually entered records (workouts, check-ins, notes, preferences) are not
touched by this module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from database import DEFAULT_DB_PATH, _connect


@dataclass(frozen=True, slots=True)
class PurgeResult:
    provider: str
    connection_id: str | None = None
    imported_metrics_deleted: int = 0
    sync_runs_deleted: int = 0
    connections_deleted: int = 0
    legacy_metrics_deleted: int = 0
    derived_insights_deleted: int = 0
    derived_correlations_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        return (
            self.imported_metrics_deleted
            + self.sync_runs_deleted
            + self.connections_deleted
            + self.legacy_metrics_deleted
            + self.derived_insights_deleted
            + self.derived_correlations_deleted
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "imported_metrics_deleted": self.imported_metrics_deleted,
            "sync_runs_deleted": self.sync_runs_deleted,
            "connections_deleted": self.connections_deleted,
            "legacy_metrics_deleted": self.legacy_metrics_deleted,
            "derived_insights_deleted": self.derived_insights_deleted,
            "derived_correlations_deleted": self.derived_correlations_deleted,
            "total_deleted": self.total_deleted,
        }


_SENSITIVE_DERIVED_TABLES = (
    ("dashboard_insights", "derived_insights_deleted"),
    ("deep_correlations", "derived_correlations_deleted"),
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _validate_scope(user_id: int | str, provider: str, connection_id: str | None) -> None:
    if isinstance(user_id, int) and user_id <= 0:
        raise ValueError("purge requires a positive user_id")
    if not str(user_id).strip():
        raise ValueError("purge requires a user_id")
    if not provider.strip():
        raise ValueError("purge requires a provider")
    if connection_id is not None and not connection_id.strip():
        raise ValueError("connection_id must be non-empty when supplied")


def _delete_provider_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    user_id: int | str,
    provider: str,
    connection_id: str | None,
) -> int:
    if not _table_exists(connection, table):
        return 0
    columns = _columns(connection, table)
    if not {"user_id", "provider"}.issubset(columns):
        return 0
    sql = f"DELETE FROM {table} WHERE user_id = ? AND provider = ?"
    params: list[Any] = [user_id, provider]
    if connection_id is not None and "connection_id" in columns:
        sql += " AND connection_id = ?"
        params.append(connection_id)
    cursor = connection.execute(sql, tuple(params))
    return max(cursor.rowcount, 0)


def _delete_connection_rows(
    connection: sqlite3.Connection,
    *,
    user_id: int | str,
    provider: str,
    connection_id: str | None,
) -> int:
    if not _table_exists(connection, "health_connections"):
        return 0
    sql = "DELETE FROM health_connections WHERE user_id = ? AND provider = ?"
    params: list[Any] = [user_id, provider]
    if connection_id is not None:
        sql += " AND id = ?"
        params.append(connection_id)
    cursor = connection.execute(sql, tuple(params))
    return max(cursor.rowcount, 0)


def _delete_legacy_metrics(
    connection: sqlite3.Connection,
    *,
    user_id: int | str,
    provider: str,
) -> int:
    """Remove legacy imported body metrics that carry this provider's source.

    Older deployments may predate the ``source`` column. In that case we skip
    rather than delete rows whose provenance is unknown, so a purge can never
    destroy user-entered data by guesswork.
    """

    if not _table_exists(connection, "body_metrics"):
        return 0
    columns = _columns(connection, "body_metrics")
    if not {"user_id", "source"}.issubset(columns):
        return 0
    cursor = connection.execute(
        "DELETE FROM body_metrics WHERE user_id = ? AND source = ?",
        (user_id, provider),
    )
    return max(cursor.rowcount, 0)


def _delete_derived(
    connection: sqlite3.Connection,
    *,
    user_id: int | str,
) -> tuple[int, int]:
    """Invalidate cached insights that may have been built from purged data."""

    deleted: dict[str, int] = {}
    for table, result_field in _SENSITIVE_DERIVED_TABLES:
        if not _table_exists(connection, table):
            deleted[result_field] = 0
            continue
        if "user_id" not in _columns(connection, table):
            deleted[result_field] = 0
            continue
        cursor = connection.execute(
            f"DELETE FROM {table} WHERE user_id = ?",
            (user_id,),
        )
        deleted[result_field] = max(cursor.rowcount, 0)
    return deleted["derived_insights_deleted"], deleted["derived_correlations_deleted"]


def purge_imported_data(
    connection: sqlite3.Connection,
    *,
    user_id: int | str,
    provider: str,
    connection_id: str | None = None,
    invalidate_derived: bool = True,
) -> PurgeResult:
    """Delete one provider's imported health data for one authenticated user.

    The caller owns the transaction. Repeating the call is safe: DELETE
    statements simply report zero affected rows once the data is gone.
    """

    _validate_scope(user_id, provider, connection_id)

    imported = _delete_provider_rows(
        connection,
        "health_metrics",
        user_id=user_id,
        provider=provider,
        connection_id=connection_id,
    )
    sync_runs = _delete_provider_rows(
        connection,
        "health_sync_runs",
        user_id=user_id,
        provider=provider,
        connection_id=connection_id,
    )
    connections = _delete_connection_rows(
        connection,
        user_id=user_id,
        provider=provider,
        connection_id=connection_id,
    )
    legacy = _delete_legacy_metrics(connection, user_id=user_id, provider=provider)
    if invalidate_derived:
        insights, correlations = _delete_derived(connection, user_id=user_id)
    else:
        insights = correlations = 0

    return PurgeResult(
        provider=provider,
        connection_id=connection_id,
        imported_metrics_deleted=imported,
        sync_runs_deleted=sync_runs,
        connections_deleted=connections,
        legacy_metrics_deleted=legacy,
        derived_insights_deleted=insights,
        derived_correlations_deleted=correlations,
    )


def purge_imported_data_at(
    db_path: str = DEFAULT_DB_PATH,
    *,
    user_id: int | str,
    provider: str,
    connection_id: str | None = None,
    invalidate_derived: bool = True,
) -> PurgeResult:
    """Open the configured database and purge one provider's imported data."""

    with _connect(db_path) as connection:
        return purge_imported_data(
            connection,
            user_id=user_id,
            provider=provider,
            connection_id=connection_id,
            invalidate_derived=invalidate_derived,
        )
