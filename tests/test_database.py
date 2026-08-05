"""Tests for the SQLite persistence layer using a temporary database."""

from __future__ import annotations

from database import (
    advance_day,
    get_body_metrics,
    get_current_day,
    get_daily_logs,
    get_exercise_volumes,
    get_personal_records,
    get_progress_history,
    get_recent_bests,
    get_recent_hevy_logs,
    get_session_volumes,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_progress,
    save_workout,
)
from hevy_parser import ExerciseSummary, WorkoutSummary


def _db(tmp_path) -> str:
    return str(tmp_path / "test.db")


def test_init_seeds_day_one(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    assert get_current_day(db) == 1


def test_init_is_idempotent(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    advance_day(db)
    init_db(db)  # must not reset the stored day
    assert get_current_day(db) == 2


def test_advance_increments(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    assert advance_day(db) == 2
    assert advance_day(db) == 3
    assert get_current_day(db) == 3


def test_advance_wraps_at_six(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    days = [advance_day(db) for _ in range(6)]
    assert days == [2, 3, 4, 5, 6, 1]


def test_save_workout_ignores_none(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_workout(None, db)  # should not raise


def test_save_progress_and_get_recent_bests(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    summary = WorkoutSummary(
        title="Legs & Abs",
        date="2026-06-17",
        duration_seconds=3600,
        total_volume_kg=7920.0,
        exercises=[
            ExerciseSummary("Leg Press", 120.0, 12, 3, True),
            ExerciseSummary("Leg Extensions", 60.0, 15, 4, True),
        ],
    )
    save_progress(summary, db)

    bests = get_recent_bests(db)
    assert bests["Leg Press"]["top_weight_kg"] == 120.0
    assert bests["Leg Press"]["top_reps"] == 12
    assert bests["Leg Extensions"]["sets"] == 4


def test_get_recent_bests_returns_latest_per_exercise(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary(
            "S1", "2026-06-10", duration_seconds=3600, total_volume_kg=3000.0,
            exercises=[ExerciseSummary("Leg Press", 100.0, 10, 3)],
        ),
        db,
    )
    save_progress(
        WorkoutSummary(
            "S2", "2026-06-17", duration_seconds=3600, total_volume_kg=3960.0,
            exercises=[ExerciseSummary("Leg Press", 110.0, 12, 3)],
        ),
        db,
    )
    bests = get_recent_bests(db)
    assert bests["Leg Press"]["top_weight_kg"] == 110.0
    assert bests["Leg Press"]["top_reps"] == 12


def test_save_progress_ignores_none(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(None, db)
    assert get_recent_bests(db) == {}


def test_daily_log_roundtrip_and_dedupes_by_date(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_daily_log("2026-06-17", 1, "Back, Deadlifts & Chest", "high", "plan A", "life A", db)
    # A re-run on the same day replaces the earlier entry.
    save_daily_log("2026-06-17", 1, "Back, Deadlifts & Chest", "high", "plan B", "life B", db)
    save_daily_log("2026-06-18", 2, "Shoulders & Arms", "low", "plan C", "life C", db)

    logs = get_daily_logs(db_path=db)
    assert len(logs) == 2
    assert logs[0]["date"] == "2026-06-18"  # most recent first
    assert logs[1]["plan"] == "plan B"
    assert logs[1]["carb_tier"] == "high"


def test_body_metrics_roundtrip_and_dedupes_by_date(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_body_metrics({"weight_kg": 82.0, "body_fat_pct": 15.0}, "2026-06-17", db)
    save_body_metrics({"weight_kg": 81.5, "body_fat_pct": 14.6}, "2026-06-17", db)
    save_body_metrics({"weight_kg": 81.0, "body_fat_pct": 14.2}, "2026-06-18", db)

    readings = get_body_metrics(db_path=db)
    assert len(readings) == 2  # one per date
    assert readings[0]["date"] == "2026-06-17"  # oldest first
    assert readings[0]["weight_kg"] == 81.5  # latest reading for the day wins
    assert readings[-1]["body_fat_pct"] == 14.2


def test_save_body_metrics_ignores_none(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_body_metrics(None, "2026-06-17", db)
    assert get_body_metrics(db_path=db) == []


def test_get_session_volumes_aggregates_by_date(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary(
            "S1",
            "2026-06-10",
            duration_seconds=3600,
            total_volume_kg=2000.0,
            exercises=[
                ExerciseSummary("Deadlift", 100.0, 5, 4),  # 100*5*4 = 2000
                ExerciseSummary("Pull-Ups", None, 8, 4),  # bodyweight -> 0
            ],
        ),
        db,
    )
    volumes = get_session_volumes(db)
    assert len(volumes) == 1
    assert volumes[0]["date"] == "2026-06-10"
    assert volumes[0]["volume"] == 2000.0
    assert volumes[0]["exercises"] == 2


def test_get_personal_records_uses_best_epley_1rm(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary("S1", "2026-06-10", 3600, 3000, [ExerciseSummary("Deadlift", 100.0, 5, 4)]),
        db,
    )
    save_progress(
        WorkoutSummary("S2", "2026-06-17", 3600, 3000, [ExerciseSummary("Deadlift", 120.0, 3, 5)]),
        db,
    )
    prs = get_personal_records(db)
    assert len(prs) == 1
    pr = prs[0]
    assert pr["exercise"] == "Deadlift"
    # 120 * (1 + 3/30) = 132 beats 100 * (1 + 5/30) = 116.67
    assert round(pr["e1rm"], 1) == 132.0
    assert pr["weight_kg"] == 120.0


def test_get_personal_records_empty_without_data(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    assert get_personal_records(db) == []


def test_get_exercise_volumes_sums_per_exercise(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary(
            "S1",
            "2026-06-10",
            duration_seconds=3600,
            total_volume_kg=3000.0,
            exercises=[
                ExerciseSummary("Leg Press", 100.0, 10, 3),  # 3000
                ExerciseSummary("Pull-Ups", None, 8, 4),  # 0 (bodyweight)
            ],
        ),
        db,
    )
    save_progress(
        WorkoutSummary(
            "S2", "2026-06-17", duration_seconds=3600, total_volume_kg=3300.0,
            exercises=[ExerciseSummary("Leg Press", 110.0, 10, 3)],
        ),  # 3300
        db,
    )
    volumes = {row["exercise"]: row for row in get_exercise_volumes(db)}
    assert volumes["Leg Press"]["volume"] == 6300.0
    assert volumes["Leg Press"]["sessions"] == 2
    assert volumes["Pull-Ups"]["volume"] == 0.0


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: workout_history user_id scoping
# ---------------------------------------------------------------------------


def test_workout_history_migration_adds_user_id_column(tmp_path):
    """Running init_db on a pre-migration DB backfills user_id via a legacy user."""
    db = _db(tmp_path)
    # Simulate a pre-migration DB by creating workout_history without user_id
    import sqlite3
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workout_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            hevy_payload TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO workout_history (date, hevy_payload) VALUES (?, ?)",
        ("2026-08-01", '{"test": true}'),
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {
            row[1]
            for row in conn2.execute("PRAGMA table_info(workout_history)").fetchall()
        }
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM workout_history WHERE date = '2026-08-01'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled to the legacy user


def test_workout_history_user_isolation(tmp_path):
    """Two users writing to workout_history do not see each other's rows."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_workout({"user": "a", "exercise": "Squat"}, db, user_id=user_a)
    save_workout({"user": "b", "exercise": "Bench"}, db, user_id=user_b)

    logs_a = get_recent_hevy_logs(limit=10, db_path=db, user_id=user_a)
    logs_b = get_recent_hevy_logs(limit=10, db_path=db, user_id=user_b)

    assert len(logs_a) == 1
    assert logs_a[0]["user"] == "a"
    assert len(logs_b) == 1
    assert logs_b[0]["user"] == "b"


def test_workout_history_user_isolation_same_payload(tmp_path):
    """Scoped reads only return the correct user's data."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    for i in range(3):
        save_workout({"count": i}, db, user_id=user_a)
        save_workout({"count": i + 100}, db, user_id=user_b)

    logs_a = get_recent_hevy_logs(limit=20, db_path=db, user_id=user_a)
    logs_b = get_recent_hevy_logs(limit=20, db_path=db, user_id=user_b)

    assert len(logs_a) == 3
    assert {w["count"] for w in logs_a} == {0, 1, 2}
    assert len(logs_b) == 3
    assert {w["count"] for w in logs_b} == {100, 101, 102}


def test_workout_history_null_user_id_backward_compat(tmp_path):
    """Calling save_workout/get_recent_hevy_logs without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_workout({"exercise": "Deadlift"}, db)
    logs = get_recent_hevy_logs(db_path=db)

    assert len(logs) == 1
    assert logs[0]["exercise"] == "Deadlift"


def test_init_db_migration_idempotent(tmp_path):
    """Running init_db twice on the same migrated DB does not crash."""
    db = _db(tmp_path)
    init_db(db)
    init_db(db)  # must not raise

    import sqlite3
    with sqlite3.connect(db, timeout=10) as conn:
        row = conn.execute("PRAGMA table_info('workout_history')").fetchall()
        # user_id column still exists
        col_names = {r[1] for r in row}
        assert "user_id" in col_names


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: exercise_progress user_id scoping
# ---------------------------------------------------------------------------


def _exercise_summary(name: str, weight: float, reps: int, sets: int = 3) -> WorkoutSummary:
    return WorkoutSummary(
        f"S-{name}", "2026-08-01", duration_seconds=3600, total_volume_kg=3000.0,
        exercises=[ExerciseSummary(name, weight, reps, sets)],
    )


def test_exercise_progress_migration_adds_user_id_column(tmp_path):
    """Running init_db on a pre-migration DB adds user_id and backfills legacy."""
    db = _db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exercise_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            exercise_name TEXT NOT NULL,
            top_weight_kg REAL,
            top_reps INTEGER,
            sets INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO exercise_progress (date, exercise_name, top_weight_kg, top_reps, sets) "
        "VALUES ('2026-08-01', 'Squat', 100.0, 10, 3)"
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(exercise_progress)").fetchall()}
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM exercise_progress WHERE exercise_name = 'Squat'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled


def test_exercise_progress_user_isolation(tmp_path):
    """Two users writing exercise_progress do not see each other's rows."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_progress(_exercise_summary("Squat", 100.0, 10), db, user_id=user_a)
    save_progress(_exercise_summary("Squat", 120.0, 8), db, user_id=user_b)

    bests_a = get_recent_bests(db, user_id=user_a)
    bests_b = get_recent_bests(db, user_id=user_b)

    assert bests_a["Squat"]["top_weight_kg"] == 100.0
    assert bests_b["Squat"]["top_weight_kg"] == 120.0


def test_progress_history_user_isolation(tmp_path):
    """get_progress_history scopes by user_id."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_progress(_exercise_summary("Bench Press", 80.0, 10), db, user_id=user_a)
    save_progress(_exercise_summary("Bench Press", 90.0, 8), db, user_id=user_b)

    history_a = get_progress_history(db_path=db, user_id=user_a)
    history_b = get_progress_history(db_path=db, user_id=user_b)

    assert "Bench Press" in history_a
    assert history_a["Bench Press"][0]["top_weight_kg"] == 80.0
    assert history_b["Bench Press"][0]["top_weight_kg"] == 90.0


def test_session_volumes_user_isolation(tmp_path):
    """get_session_volumes scopes by user_id."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_progress(_exercise_summary("Deadlift", 100.0, 5, 4), db, user_id=user_a)
    save_progress(_exercise_summary("Deadlift", 140.0, 3, 5), db, user_id=user_b)

    vols_a = get_session_volumes(db, user_id=user_a)
    vols_b = get_session_volumes(db, user_id=user_b)

    assert vols_a[0]["volume"] == 2000.0  # 100*5*4
    assert vols_b[0]["volume"] == 2100.0  # 140*3*5


def test_exercise_volumes_user_isolation(tmp_path):
    """get_exercise_volumes scopes by user_id."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_progress(_exercise_summary("Leg Press", 100.0, 10, 3), db, user_id=user_a)
    save_progress(_exercise_summary("Leg Press", 200.0, 8, 3), db, user_id=user_b)

    vols_a = {r["exercise"]: r for r in get_exercise_volumes(db, user_id=user_a)}
    vols_b = {r["exercise"]: r for r in get_exercise_volumes(db, user_id=user_b)}

    assert vols_a["Leg Press"]["volume"] == 3000.0
    assert vols_b["Leg Press"]["volume"] == 4800.0


def test_personal_records_user_isolation(tmp_path):
    """get_personal_records scopes by user_id."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_progress(_exercise_summary("Squat", 100.0, 5), db, user_id=user_a)
    save_progress(_exercise_summary("Squat", 150.0, 3), db, user_id=user_b)

    prs_a = get_personal_records(db, user_id=user_a)
    prs_b = get_personal_records(db, user_id=user_b)

    assert prs_a[0]["weight_kg"] == 100.0
    assert prs_b[0]["weight_kg"] == 150.0


def test_exercise_progress_null_user_id_backward_compat(tmp_path):
    """Calling save_progress without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_progress(_exercise_summary("Curls", 20.0, 12), db)
    bests = get_recent_bests(db)
    assert bests["Curls"]["top_weight_kg"] == 20.0


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: body_metrics user_id scoping
# ---------------------------------------------------------------------------


def test_body_metrics_migration_adds_user_id_column(tmp_path):
    """Running init_db on a pre-migration DB adds user_id and backfills legacy."""
    db = _db(tmp_path)
    import sqlite3
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS body_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            weight_kg REAL,
            body_fat_pct REAL,
            muscle_pct REAL,
            resting_hr INTEGER,
            hrv REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO body_metrics (date, weight_kg) VALUES ('2026-08-01', 82.0)"
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(body_metrics)").fetchall()}
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM body_metrics WHERE date = '2026-08-01'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled


def test_body_metrics_user_isolation(tmp_path):
    """Two users writing body_metrics do not see each other's rows."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_body_metrics({"weight_kg": 80.0}, "2026-08-01", db, user_id=user_a)
    save_body_metrics({"weight_kg": 90.0}, "2026-08-01", db, user_id=user_b)

    metrics_a = get_body_metrics(db_path=db, user_id=user_a)
    metrics_b = get_body_metrics(db_path=db, user_id=user_b)

    assert len(metrics_a) == 1
    assert metrics_a[0]["weight_kg"] == 80.0
    assert len(metrics_b) == 1
    assert metrics_b[0]["weight_kg"] == 90.0


def test_body_metrics_same_date_different_users_preserved(tmp_path):
    """Dedup only within the same user_id."""
    db = _db(tmp_path)
    init_db(db)

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_body_metrics({"weight_kg": 80.0}, "2026-08-01", db, user_id=user_a)
    save_body_metrics({"weight_kg": 90.0}, "2026-08-01", db, user_id=user_b)
    # A second reading from user A on the same day should replace theirs
    save_body_metrics({"weight_kg": 80.5}, "2026-08-01", db, user_id=user_a)

    metrics_a = get_body_metrics(db_path=db, user_id=user_a)
    metrics_b = get_body_metrics(db_path=db, user_id=user_b)

    assert len(metrics_a) == 1
    assert metrics_a[0]["weight_kg"] == 80.5
    assert len(metrics_b) == 1
    assert metrics_b[0]["weight_kg"] == 90.0


def test_body_metrics_null_user_id_backward_compat(tmp_path):
    """Calling save_body_metrics/get_body_metrics without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_body_metrics({"weight_kg": 75.0}, "2026-08-01", db)
    readings = get_body_metrics(db_path=db)
    assert len(readings) == 1
    assert readings[0]["weight_kg"] == 75.0

