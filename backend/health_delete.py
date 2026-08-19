"""Canonical health-data deletion scope."""

from __future__ import annotations


HEALTH_USER_TABLES = (
    "health_metrics",
    "health_sync_runs",
    "health_connections",
)


def deletion_statements(user_id: int) -> tuple[tuple[str, tuple[int]], ...]:
    if user_id <= 0:
        raise ValueError("deletion requires a positive user_id")
    return tuple((f"DELETE FROM {table} WHERE user_id = ?", (user_id,)) for table in HEALTH_USER_TABLES)
