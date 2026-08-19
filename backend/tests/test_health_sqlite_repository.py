import sqlite3
from datetime import datetime, timezone

import pytest

from health_migration import migrate_health_schema
from health_models import HealthMetric, MetricType, SourceProvenance
from health_sqlite_repository import SQLiteHealthRepository


def setup_repo():
    connection = sqlite3.connect(":memory:")
    migrate_health_schema(connection)
    return connection, SQLiteHealthRepository(connection)


def metric(user=1):
    return HealthMetric(user, MetricType.WEIGHT_KG, 80, "kg", datetime(2026, 8, 19, tzinfo=timezone.utc), SourceProvenance("withings", "c1", "r1"))


def test_sqlite_repository_is_idempotent_by_user_and_fingerprint():
    connection, repo = setup_repo()
    assert repo.save_metrics(1, [metric()]) == 1
    assert repo.save_metrics(1, [metric()]) == 0
    assert connection.execute("SELECT user_id, provider FROM health_metrics").fetchall() == [(1, "withings")]


def test_sqlite_repository_rejects_cross_user_write():
    _, repo = setup_repo()
    with pytest.raises(ValueError, match="another user"):
        repo.save_metrics(1, [metric(2)])
