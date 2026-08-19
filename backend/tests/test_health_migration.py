import sqlite3

from health_migration import migrate_health_schema


def test_health_schema_migration_is_additive_and_idempotent():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE body_metrics (id INTEGER PRIMARY KEY, weight REAL)")
    connection.execute("INSERT INTO body_metrics(weight) VALUES (80.0)")
    migrate_health_schema(connection)
    migrate_health_schema(connection)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"body_metrics", "health_connections", "health_metrics", "health_sync_runs"} <= tables
    assert connection.execute("SELECT weight FROM body_metrics").fetchone()[0] == 80.0
