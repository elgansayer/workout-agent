"""Tests for the SQLite persistence layer using a temporary database."""

from __future__ import annotations

from pathlib import Path

from database import (
    advance_day,
    delete_routine_record,
    get_body_metrics,
    get_current_day,
    get_daily_logs,
    get_exercise_volumes,
    get_meta,
    get_or_create_user,
    get_personal_records,
    get_programme_start_date,
    get_progress_history,
    get_recent_bests,
    get_recent_hevy_logs,
    get_routine_record,
    get_session_volumes,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_progress,
    save_routine_record,
    save_workout,
    set_meta,
)
from hevy_parser import ExerciseSummary, WorkoutSummary


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


def test_init_seeds_day_one(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    assert get_current_day(db) == 1


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    advance_day(db)
    init_db(db)  # must not reset the stored day
    assert get_current_day(db) == 2


def test_advance_increments(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    assert advance_day(db) == 2
    assert advance_day(db) == 3
    assert get_current_day(db) == 3


def test_advance_wraps_at_six(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    days = [advance_day(db) for _ in range(6)]
    assert days == [2, 3, 4, 5, 6, 1]


def test_save_workout_ignores_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_workout(None, db)  # should not raise


def test_save_progress_and_get_recent_bests(tmp_path: Path) -> None:
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


def test_get_recent_bests_returns_latest_per_exercise(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary(
            "S1",
            "2026-06-10",
            duration_seconds=3600,
            total_volume_kg=3000.0,
            exercises=[ExerciseSummary("Leg Press", 100.0, 10, 3)],
        ),
        db,
    )
    save_progress(
        WorkoutSummary(
            "S2",
            "2026-06-17",
            duration_seconds=3600,
            total_volume_kg=3960.0,
            exercises=[ExerciseSummary("Leg Press", 110.0, 12, 3)],
        ),
        db,
    )
    bests = get_recent_bests(db)
    assert bests["Leg Press"]["top_weight_kg"] == 110.0
    assert bests["Leg Press"]["top_reps"] == 12


def test_save_progress_ignores_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_progress(None, db)
    assert get_recent_bests(db) == {}


def test_daily_log_roundtrip_and_dedupes_by_date(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_daily_log(
        "2026-06-17", 1, "Back, Deadlifts & Chest", "high", "plan A", "life A", db
    )
    # A re-run on the same day replaces the earlier entry.
    save_daily_log(
        "2026-06-17", 1, "Back, Deadlifts & Chest", "high", "plan B", "life B", db
    )
    save_daily_log("2026-06-18", 2, "Shoulders & Arms", "low", "plan C", "life C", db)

    logs = get_daily_logs(db_path=db)
    assert len(logs) == 2
    assert logs[0]["date"] == "2026-06-18"  # most recent first
    assert logs[1]["plan"] == "plan B"
    assert logs[1]["carb_tier"] == "high"


def test_body_metrics_roundtrip_and_dedupes_by_date(tmp_path: Path) -> None:
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


def test_save_body_metrics_ignores_none(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_body_metrics(None, "2026-06-17", db)
    assert get_body_metrics(db_path=db) == []


def test_get_session_volumes_aggregates_by_date(tmp_path: Path) -> None:
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


def test_get_personal_records_uses_best_epley_1rm(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary(
            "S1", "2026-06-10", 3600, 3000, [ExerciseSummary("Deadlift", 100.0, 5, 4)]
        ),
        db,
    )
    save_progress(
        WorkoutSummary(
            "S2", "2026-06-17", 3600, 3000, [ExerciseSummary("Deadlift", 120.0, 3, 5)]
        ),
        db,
    )
    prs = get_personal_records(db)
    assert len(prs) == 1
    pr = prs[0]
    assert pr["exercise"] == "Deadlift"
    # 120 * (1 + 3/30) = 132 beats 100 * (1 + 5/30) = 116.67
    assert round(pr["e1rm"], 1) == 132.0
    assert pr["weight_kg"] == 120.0


def test_get_personal_records_empty_without_data(tmp_path: Path) -> None:
    db = _db(tmp_path)
    init_db(db)
    assert get_personal_records(db) == []


def test_get_exercise_volumes_sums_per_exercise(tmp_path: Path) -> None:
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
            "S2",
            "2026-06-17",
            duration_seconds=3600,
            total_volume_kg=3300.0,
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


def test_workout_history_migration_adds_user_id_column(tmp_path: Path) -> None:
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
        """,
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
            "SELECT user_id FROM workout_history WHERE date = '2026-08-01'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled to the legacy user


def test_workout_history_user_isolation(tmp_path: Path) -> None:
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


def test_workout_history_user_isolation_same_payload(tmp_path: Path) -> None:
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


def test_workout_history_null_user_id_backward_compat(tmp_path: Path) -> None:
    """Calling save_workout/get_recent_hevy_logs without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_workout({"exercise": "Deadlift"}, db)
    logs = get_recent_hevy_logs(db_path=db)

    assert len(logs) == 1
    assert logs[0]["exercise"] == "Deadlift"


def test_init_db_migration_idempotent(tmp_path: Path) -> None:
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


def _exercise_summary(
    name: str, weight: float, reps: int, sets: int = 3
) -> WorkoutSummary:
    return WorkoutSummary(
        f"S-{name}",
        "2026-08-01",
        duration_seconds=3600,
        total_volume_kg=3000.0,
        exercises=[ExerciseSummary(name, weight, reps, sets)],
    )


def test_exercise_progress_migration_adds_user_id_column(tmp_path: Path) -> None:
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
        """,
    )
    conn.execute(
        "INSERT INTO exercise_progress (date, exercise_name, top_weight_kg, top_reps, sets) "
        "VALUES ('2026-08-01', 'Squat', 100.0, 10, 3)",
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {
            row[1]
            for row in conn2.execute("PRAGMA table_info(exercise_progress)").fetchall()
        }
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM exercise_progress WHERE exercise_name = 'Squat'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled


def test_exercise_progress_user_isolation(tmp_path: Path) -> None:
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


def test_progress_history_user_isolation(tmp_path: Path) -> None:
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


def test_session_volumes_user_isolation(tmp_path: Path) -> None:
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


def test_exercise_volumes_user_isolation(tmp_path: Path) -> None:
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


def test_personal_records_user_isolation(tmp_path: Path) -> None:
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


def test_exercise_progress_null_user_id_backward_compat(tmp_path: Path) -> None:
    """Calling save_progress without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_progress(_exercise_summary("Curls", 20.0, 12), db)
    bests = get_recent_bests(db)
    assert bests["Curls"]["top_weight_kg"] == 20.0


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: body_metrics user_id scoping
# ---------------------------------------------------------------------------


def test_body_metrics_migration_adds_user_id_column(tmp_path: Path) -> None:
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
        """,
    )
    conn.execute(
        "INSERT INTO body_metrics (date, weight_kg) VALUES ('2026-08-01', 82.0)",
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {
            row[1]
            for row in conn2.execute("PRAGMA table_info(body_metrics)").fetchall()
        }
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM body_metrics WHERE date = '2026-08-01'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled


def test_body_metrics_user_isolation(tmp_path: Path) -> None:
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


def test_body_metrics_same_date_different_users_preserved(tmp_path: Path) -> None:
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


def test_body_metrics_null_user_id_backward_compat(tmp_path: Path) -> None:
    """Calling save_body_metrics/get_body_metrics without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    save_body_metrics({"weight_kg": 75.0}, "2026-08-01", db)
    readings = get_body_metrics(db_path=db)
    assert len(readings) == 1
    assert readings[0]["weight_kg"] == 75.0


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: chat_messages
# ---------------------------------------------------------------------------


def test_chat_messages_migration_adds_user_id_column(tmp_path: Path) -> None:
    """Running init_db on a pre-migration DB with chat_messages adds user_id."""
    db = _db(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "INSERT INTO chat_messages (role, content, created_at) VALUES (?, ?, ?)",
        ("user", "hello", "2026-08-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    init_db(db)

    with sqlite3.connect(db, timeout=10) as conn2:
        cols = {
            row[1]
            for row in conn2.execute("PRAGMA table_info(chat_messages)").fetchall()
        }
        assert "user_id" in cols
        rows = conn2.execute(
            "SELECT user_id FROM chat_messages WHERE content = 'hello'",
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] is not None  # backfilled


def test_chat_messages_user_isolation(tmp_path: Path) -> None:
    """Two users writing to chat_messages don't see each other's rows."""
    db = _db(tmp_path)
    init_db(db)

    from database import clear_chat_messages, get_chat_messages, save_chat_message

    user_a = "user-a-123"
    user_b = "user-b-456"

    save_chat_message("user", "msg A1", db, user_id=user_a)
    save_chat_message("user", "msg A2", db, user_id=user_a)
    save_chat_message("user", "msg B1", db, user_id=user_b)

    msgs_a = get_chat_messages(limit=50, db_path=db, user_id=user_a)
    msgs_b = get_chat_messages(limit=50, db_path=db, user_id=user_b)

    assert len(msgs_a) == 2
    assert all(m["content"] in ("msg A1", "msg A2") for m in msgs_a)
    assert len(msgs_b) == 1
    assert msgs_b[0]["content"] == "msg B1"

    # clear should only affect the target user
    clear_chat_messages(db_path=db, user_id=user_a)
    assert get_chat_messages(limit=50, db_path=db, user_id=user_a) == []
    assert len(get_chat_messages(limit=50, db_path=db, user_id=user_b)) == 1


def test_chat_messages_backward_compat(tmp_path: Path) -> None:
    """Calling save/get/clear without user_id still works."""
    db = _db(tmp_path)
    init_db(db)

    from database import clear_chat_messages, get_chat_messages, save_chat_message

    save_chat_message("user", "test", db)
    msgs = get_chat_messages(db_path=db)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "test"

    clear_chat_messages(db_path=db)
    assert get_chat_messages(db_path=db) == []


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: dashboard_insights
# ---------------------------------------------------------------------------


def test_dashboard_insights_migration_and_isolation(tmp_path: Path) -> None:
    """dashboard_insights is migrated from singleton to user_id-scoped."""
    db = _db(tmp_path)
    import sqlite3

    # Simulate pre-migration: create old singleton table
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_insights (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            date TEXT NOT NULL,
            insight_json TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "INSERT INTO dashboard_insights (id, date, insight_json) VALUES (1, '2026-08-01', ?)",
        ('{"fatigue": "Green"}',),
    )
    conn.commit()
    conn.close()

    init_db(db)

    from database import get_dashboard_insight, save_dashboard_insight

    # The old row should have been backfilled to the legacy user
    # Without a user_id filter, we default to None which was backfilled as legacy
    legacy_insight = get_dashboard_insight(db_path=db, user_id=None)
    assert legacy_insight is None  # None doesn't match legacy user in new schema

    user_a = "user-a-123"
    save_dashboard_insight('{"fatigue": "Red"}', db, user_id=user_a)
    insight_a = get_dashboard_insight(db_path=db, user_id=user_a)
    assert insight_a is not None
    assert insight_a["fatigue"] == "Red"

    user_b = "user-b-456"
    save_dashboard_insight('{"fatigue": "Green"}', db, user_id=user_b)
    insight_b = get_dashboard_insight(db_path=db, user_id=user_b)
    assert insight_b is not None
    assert insight_b["fatigue"] == "Green"

    # Users don't see each other's insights
    insight_a2 = get_dashboard_insight(db_path=db, user_id=user_a)
    assert insight_a2 is not None
    assert insight_a2["fatigue"] == "Red"


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: daily_log
# ---------------------------------------------------------------------------


def test_daily_log_migration_adds_user_id_column(tmp_path: Path) -> None:
    """daily_log gets a user_id column via migration and backfills legacy."""
    db = _db(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            day INTEGER,
            focus TEXT NOT NULL,
            carb_tier TEXT NOT NULL,
            plan TEXT NOT NULL,
            lifestyle TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle) "
        "VALUES ('2026-08-01', 1, 'Deadlift', 'high', 'Plan A', 'Walk')",
    )
    conn.commit()
    conn.close()

    init_db(db)

    from database import get_daily_logs

    logs = get_daily_logs(limit=10, db_path=db)
    assert len(logs) == 1
    assert logs[0]["focus"] == "Deadlift"


def test_daily_log_user_isolation(tmp_path: Path) -> None:
    """Two different user_ids don't see each other's daily_log rows."""
    db = _db(tmp_path)
    init_db(db)

    from database import get_daily_logs, save_daily_log

    save_daily_log(
        "2026-08-01",
        1,
        "Deadlift",
        "high",
        "Plan A",
        "Walk",
        db_path=db,
        user_id="user-a",
    )
    save_daily_log(
        "2026-08-01",
        2,
        "Pull-ups",
        "med",
        "Plan B",
        "Run",
        db_path=db,
        user_id="user-b",
    )

    logs_a = get_daily_logs(limit=10, db_path=db, user_id="user-a")
    assert len(logs_a) == 1
    assert logs_a[0]["day"] == 1
    assert logs_a[0]["focus"] == "Deadlift"

    logs_b = get_daily_logs(limit=10, db_path=db, user_id="user-b")
    assert len(logs_b) == 1
    assert logs_b[0]["day"] == 2
    assert logs_b[0]["focus"] == "Pull-ups"


def test_daily_log_backward_compat(tmp_path: Path) -> None:
    """Callers not passing user_id still work (backward compat)."""
    db = _db(tmp_path)
    init_db(db)

    from database import get_daily_logs, save_daily_log

    save_daily_log(
        "2026-08-01",
        1,
        "Deadlift",
        "high",
        "Plan",
        "Lifestyle",
        db_path=db,
    )
    logs = get_daily_logs(limit=10, db_path=db)
    assert len(logs) == 1


def test_daily_log_dedupes_per_user_per_date(tmp_path: Path) -> None:
    """Same user/date writes replace the prior entry; different users don't clash."""
    db = _db(tmp_path)
    init_db(db)

    from database import get_daily_logs, save_daily_log

    save_daily_log(
        "2026-08-01",
        1,
        "A",
        "high",
        "Plan1",
        "L1",
        db_path=db,
        user_id="u1",
    )
    save_daily_log(
        "2026-08-01",
        1,
        "A-v2",
        "high",
        "Plan1b",
        "L1b",
        db_path=db,
        user_id="u1",
    )
    save_daily_log(
        "2026-08-01",
        2,
        "B",
        "med",
        "Plan2",
        "L2",
        db_path=db,
        user_id="u2",
    )

    logs_u1 = get_daily_logs(limit=10, db_path=db, user_id="u1")
    assert len(logs_u1) == 1
    assert logs_u1[0]["focus"] == "A-v2"

    logs_u2 = get_daily_logs(limit=10, db_path=db, user_id="u2")
    assert len(logs_u2) == 1
    assert logs_u2[0]["focus"] == "B"


# ---------------------------------------------------------------------------
# Multi-tenant isolation tests: check_ins
# ---------------------------------------------------------------------------


def test_check_ins_migration_adds_user_id_column(tmp_path: Path) -> None:
    """check_ins gets a user_id column via migration and backfills legacy."""
    db = _db(tmp_path)
    import sqlite3

    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS check_ins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER NOT NULL,
            date TEXT NOT NULL,
            workouts_done INTEGER NOT NULL,
            weeks INTEGER NOT NULL,
            message TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "INSERT INTO check_ins (number, date, workouts_done, weeks, message) "
        "VALUES (1, '2026-08-01', 5, 2, 'Good progress')",
    )
    conn.commit()
    conn.close()

    init_db(db)

    from database import get_checkins

    cks = get_checkins(limit=10, db_path=db)
    assert len(cks) == 1
    assert cks[0]["number"] == 1


def test_check_ins_user_isolation(tmp_path: Path) -> None:
    """Two different user_ids don't see each other's check_ins rows."""
    db = _db(tmp_path)
    init_db(db)

    from database import get_checkins, save_checkin

    save_checkin(1, "2026-08-01", 5, 2, "Message A", db_path=db, user_id="u1")
    save_checkin(1, "2026-08-02", 3, 1, "Message B", db_path=db, user_id="u2")

    cks_a = get_checkins(limit=10, db_path=db, user_id="u1")
    assert len(cks_a) == 1
    assert cks_a[0]["message"] == "Message A"

    cks_b = get_checkins(limit=10, db_path=db, user_id="u2")
    assert len(cks_b) == 1
    assert cks_b[0]["message"] == "Message B"


def test_check_ins_backward_compat(tmp_path: Path) -> None:
    """Callers not passing user_id still work."""
    db = _db(tmp_path)
    init_db(db)

    from database import get_checkins, save_checkin

    save_checkin(1, "2026-08-01", 5, 2, "Legacy checkin", db_path=db)
    cks = get_checkins(limit=10, db_path=db)
    assert len(cks) == 1


def test_deep_correlations_migration_and_isolation(tmp_path: Path) -> None:
    """deep_correlations is migrated from singleton to user_id-scoped."""
    db = _db(tmp_path)
    import sqlite3

    # Simulate pre-migration
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deep_correlations (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            date TEXT NOT NULL,
            insight_markdown TEXT NOT NULL
        )
        """,
    )
    conn.execute(
        "INSERT INTO deep_correlations (id, date, insight_markdown) VALUES (1, '2026-08-01', ?)",
        ("Old insight",),
    )
    conn.commit()
    conn.close()

    init_db(db)

    from database import get_deep_correlation, save_deep_correlation

    user_a = "user-a-123"
    save_deep_correlation("Correlation A", db, user_id=user_a)
    assert get_deep_correlation(db_path=db, user_id=user_a) == "Correlation A"

    user_b = "user-b-456"
    save_deep_correlation("Correlation B", db, user_id=user_b)
    assert get_deep_correlation(db_path=db, user_id=user_b) == "Correlation B"

    # Users don't see each other's correlations
    assert get_deep_correlation(db_path=db, user_id=user_a) == "Correlation A"

    # None user_id returns None (no matching row)
    assert get_deep_correlation(db_path=db, user_id=None) is None
