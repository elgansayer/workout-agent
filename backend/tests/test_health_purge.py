import sqlite3

import pytest
from health_purge import purge_imported_data
from health_schema import HEALTH_SCHEMA_SQL


def _context_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE dashboard_insights (
            user_id TEXT NOT NULL PRIMARY KEY,
            date TEXT NOT NULL,
            insight_json TEXT NOT NULL
        );
        CREATE TABLE deep_correlations (
            user_id TEXT NOT NULL PRIMARY KEY,
            date TEXT NOT NULL,
            insight_markdown TEXT NOT NULL
        );
        CREATE TABLE body_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            weight_kg REAL,
            user_id TEXT,
            source TEXT NOT NULL DEFAULT 'legacy'
        );
        CREATE TABLE workout_history (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            title TEXT
        );
        CREATE TABLE daily_log (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            focus TEXT
        );
        """
    )


def _seed(connection: sqlite3.Connection) -> None:
    connection.executescript(HEALTH_SCHEMA_SQL)
    _context_tables(connection)

    connection.executemany(
        "INSERT INTO health_metrics "
        "(user_id, metric_type, value, unit, observed_at, provider, connection_id, fingerprint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "weight_kg", 80.0, "kg", "2026-08-01T00:00:00+00:00", "withings", "c1", "u1-a"),
            (1, "weight_kg", 81.0, "kg", "2026-08-02T00:00:00+00:00", "withings", "c1", "u1-b"),
            (1, "sleep_duration_min", 420.0, "min", "2026-08-02T00:00:00+00:00", "oura", "c2", "u1-c"),
            (2, "weight_kg", 90.0, "kg", "2026-08-01T00:00:00+00:00", "withings", "c3", "u2-a"),
        ],
    )
    connection.executemany(
        "INSERT INTO health_sync_runs "
        "(user_id, provider, connection_id, state, started_at) VALUES (?, ?, ?, ?, ?)",
        [
            (1, "withings", "c1", "finished", "2026-08-01T00:00:00+00:00"),
            (1, "oura", "c2", "finished", "2026-08-01T00:00:00+00:00"),
            (2, "withings", "c3", "finished", "2026-08-01T00:00:00+00:00"),
        ],
    )
    connection.executemany(
        "INSERT INTO health_connections "
        "(id, user_id, provider, state, granted_capabilities) VALUES (?, ?, ?, ?, ?)",
        [
            ("c1", 1, "withings", "connected", "[]"),
            ("c2", 1, "oura", "connected", "[]"),
            ("c3", 2, "withings", "connected", "[]"),
        ],
    )
    connection.executemany(
        "INSERT INTO dashboard_insights (user_id, date, insight_json) VALUES (?, ?, ?)",
        [(1, "2026-08-02", "{\"weight\": 81}"), (2, "2026-08-02", "{\"weight\": 90}")],
    )
    connection.executemany(
        "INSERT INTO deep_correlations (user_id, date, insight_markdown) VALUES (?, ?, ?)",
        [(1, "2026-08-02", "correlation"), (2, "2026-08-02", "correlation")],
    )
    connection.executemany(
        "INSERT INTO body_metrics (date, weight_kg, user_id, source) VALUES (?, ?, ?, ?)",
        [
            ("2026-08-01", 80.0, 1, "withings"),
            ("2026-08-01", 79.5, 1, "legacy"),
            ("2026-08-01", 90.0, 2, "withings"),
        ],
    )
    connection.executemany(
        "INSERT INTO workout_history (id, user_id, title) VALUES (?, ?, ?)",
        [(1, 1, "User entered workout"), (2, 2, "Other user workout")],
    )
    connection.executemany(
        "INSERT INTO daily_log (id, user_id, focus) VALUES (?, ?, ?)",
        [(1, 1, "User entered day"), (2, 2, "Other user day")],
    )
    connection.commit()


def test_purge_removes_only_the_selected_tenant_and_provider():
    connection = sqlite3.connect(":memory:")
    _seed(connection)

    result = purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")

    assert result.imported_metrics_deleted == 2
    assert result.sync_runs_deleted == 1
    assert result.connections_deleted == 1
    assert result.legacy_metrics_deleted == 1
    assert result.derived_insights_deleted == 1
    assert result.derived_correlations_deleted == 1
    assert result.total_deleted == 7

    assert connection.execute(
        "SELECT provider FROM health_metrics WHERE user_id = 1 ORDER BY provider"
    ).fetchall() == [("oura",)]
    assert connection.execute(
        "SELECT user_id, provider FROM health_metrics WHERE user_id = 2"
    ).fetchall() == [(2, "withings")]
    assert connection.execute(
        "SELECT source FROM body_metrics WHERE user_id = 1 ORDER BY source"
    ).fetchall() == [("legacy",)]
    assert connection.execute("SELECT COUNT(*) FROM dashboard_insights WHERE user_id = 1").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM dashboard_insights WHERE user_id = 2").fetchone()[0] == 1


def test_purge_never_deletes_unrelated_user_entered_records():
    connection = sqlite3.connect(":memory:")
    _seed(connection)

    purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")

    assert connection.execute(
        "SELECT id, user_id, title FROM workout_history ORDER BY id"
    ).fetchall() == [(1, 1, "User entered workout"), (2, 2, "Other user workout")]
    assert connection.execute(
        "SELECT id, user_id, focus FROM daily_log ORDER BY id"
    ).fetchall() == [(1, 1, "User entered day"), (2, 2, "Other user day")]


def test_repeated_purge_is_idempotent():
    connection = sqlite3.connect(":memory:")
    _seed(connection)

    first = purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")
    second = purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")

    assert first.total_deleted > 0
    assert second.total_deleted == 0


def test_reconnection_after_purge_is_idempotent():
    connection = sqlite3.connect(":memory:")
    _seed(connection)

    purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")

    # A reconnect writes a fresh connection/credential reference, not the data.
    connection.execute(
        "INSERT INTO health_connections "
        "(id, user_id, provider, state, granted_capabilities) VALUES (?, ?, ?, ?, ?)",
        ("c1", 1, "withings", "pending", "[]"),
    )
    connection.commit()

    purged = purge_imported_data(connection, user_id=1, provider="withings", connection_id="c1")
    assert purged.connections_deleted == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM health_connections WHERE user_id = 1 AND provider = 'withings'"
    ).fetchone()[0] == 0


def test_purge_can_skip_derived_invalidation_when_explicitly_requested():
    connection = sqlite3.connect(":memory:")
    _seed(connection)

    result = purge_imported_data(
        connection,
        user_id=1,
        provider="withings",
        connection_id="c1",
        invalidate_derived=False,
    )

    assert result.derived_insights_deleted == 0
    assert result.derived_correlations_deleted == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM dashboard_insights WHERE user_id = 1"
    ).fetchone()[0] == 1


def test_purge_requires_a_scoped_identity():
    connection = sqlite3.connect(":memory:")
    _seed(connection)
    with pytest.raises(ValueError, match="user_id"):
        purge_imported_data(connection, user_id=0, provider="withings")
    with pytest.raises(ValueError, match="provider"):
        purge_imported_data(connection, user_id=1, provider=" ")
    with pytest.raises(ValueError, match="connection_id"):
        purge_imported_data(connection, user_id=1, provider="withings", connection_id=" ")
