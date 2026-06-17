"""Tests for the SQLite persistence layer using a temporary database."""

from __future__ import annotations

from datetime import date

from database import (
    advance_day,
    get_body_metrics,
    get_current_day,
    get_daily_logs,
    get_exercise_volumes,
    get_personal_records,
    get_recent_bests,
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
        WorkoutSummary("S1", "2026-06-10", [ExerciseSummary("Leg Press", 100.0, 10, 3)]),
        db,
    )
    save_progress(
        WorkoutSummary("S2", "2026-06-17", [ExerciseSummary("Leg Press", 110.0, 12, 3)]),
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
            [
                ExerciseSummary("Deadlift", 100.0, 5, 4),  # 100*5*4 = 2000
                ExerciseSummary("Pull-Ups", None, 8, 4),  # bodyweight -> 0
            ],
        ),
        db,
    )
    volumes = get_session_volumes(db)
    # save_progress stamps the row with the run date, so everything lands today.
    assert len(volumes) == 1
    assert volumes[0]["date"] == date.today().isoformat()
    assert volumes[0]["volume"] == 2000.0
    assert volumes[0]["exercises"] == 2


def test_get_personal_records_uses_best_epley_1rm(tmp_path):
    db = _db(tmp_path)
    init_db(db)
    save_progress(
        WorkoutSummary("S1", "2026-06-10", [ExerciseSummary("Deadlift", 100.0, 5, 4)]),
        db,
    )
    save_progress(
        WorkoutSummary("S2", "2026-06-17", [ExerciseSummary("Deadlift", 120.0, 3, 5)]),
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
            [
                ExerciseSummary("Leg Press", 100.0, 10, 3),  # 3000
                ExerciseSummary("Pull-Ups", None, 8, 4),  # 0 (bodyweight)
            ],
        ),
        db,
    )
    save_progress(
        WorkoutSummary("S2", "2026-06-17", [ExerciseSummary("Leg Press", 110.0, 10, 3)]),  # 3300
        db,
    )
    volumes = {row["exercise"]: row for row in get_exercise_volumes(db)}
    assert volumes["Leg Press"]["volume"] == 6300.0
    assert volumes["Leg Press"]["sessions"] == 2
    assert volumes["Pull-Ups"]["volume"] == 0.0

