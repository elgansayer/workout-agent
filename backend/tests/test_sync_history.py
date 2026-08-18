"""Tests for sync_history.py — historical Hevy backfill utility."""

from __future__ import annotations

import pathlib
import sqlite3
from unittest.mock import patch

from scripts import sync_history


def _new_temp_db(tmp_path: pathlib.Path) -> str:
    from database import init_db

    db_path = str(tmp_path / "sync_test.db")
    init_db(db_path)
    return db_path


_SAMPLE_WORKOUTS: list[dict[str, object]] = [
    {
        "id": "W2",
        "title": "Leg Day",
        "start_time": "2026-03-02T08:00:00Z",
        "end_time": "2026-03-02T09:30:00Z",
        "exercises": [
            {
                "title": "Squat (Barbell)",
                "sets": [
                    {"weight_kg": 100.0, "reps": 5},
                    {"weight_kg": 100.0, "reps": 5},
                    {"weight_kg": 100.0, "reps": 5},
                ],
            },
        ],
    },
    {
        "id": "W1",
        "title": "Push Day",
        "start_time": "2026-03-01T08:00:00Z",
        "end_time": "2026-03-01T09:00:00Z",
        "exercises": [
            {
                "title": "Bench Press (Barbell)",
                "sets": [
                    {"weight_kg": 80.0, "reps": 10},
                    {"weight_kg": 85.0, "reps": 8},
                ],
            },
        ],
    },
]


def test_no_api_key_returns_error() -> None:
    result = sync_history.sync_all("")
    assert result == {"error": "No Hevy API key configured."}


def test_get_all_workouts_returns_none(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    with patch("scripts.sync_history.get_all_workouts", return_value=None):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 0, "processed": 0}


def test_get_all_workouts_returns_empty_list(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    with patch("scripts.sync_history.get_all_workouts", return_value=[]):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 0, "processed": 0}


def test_sync_all_rebuilds_and_saves_workouts(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    with patch("scripts.sync_history.get_all_workouts", return_value=_SAMPLE_WORKOUTS):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 2, "processed": 2}
    conn = sqlite3.connect(db_path, timeout=10)
    assert conn.execute("SELECT COUNT(*) FROM workout_history").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM exercise_progress").fetchone()[0] == 2
    conn.close()


def test_sync_all_respects_user_id(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    with patch("scripts.sync_history.get_all_workouts", return_value=_SAMPLE_WORKOUTS):
        result = sync_history.sync_all("fake-key", db_path, user_id="user-1")
    assert result == {"workouts_found": 2, "processed": 2}
    conn = sqlite3.connect(db_path, timeout=10)
    user_ids = conn.execute("SELECT DISTINCT user_id FROM workout_history").fetchall()
    conn.close()
    assert user_ids == [("user-1",)]


def test_sync_all_workout_missing_start_time(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    workouts: list[dict[str, object]] = [
        {
            "id": "W1",
            "title": "Evening",
            "end_time": "2026-03-03T20:00:00Z",
            "exercises": [
                {"title": "Curl", "sets": [{"weight_kg": 20.0, "reps": 15}]},
            ],
        },
    ]
    with patch("scripts.sync_history.get_all_workouts", return_value=workouts):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 1, "processed": 1}


def test_sync_all_parse_failure_skips_progress(tmp_path: pathlib.Path) -> None:
    """Exercises key missing — parse_workout returns None, save_progress skipped."""
    db_path = _new_temp_db(tmp_path)
    workouts: list[dict[str, object]] = [
        {
            "id": "W1",
            "title": "Bad workout",
            "start_time": "2026-03-01T08:00:00Z",
            "end_time": "2026-03-01T09:00:00Z",
        },
    ]
    with patch("scripts.sync_history.get_all_workouts", return_value=workouts):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 1, "processed": 0}


def test_sync_all_no_date_uses_none(tmp_path: pathlib.Path) -> None:
    db_path = _new_temp_db(tmp_path)
    workouts: list[dict[str, object]] = [
        {
            "id": "W1",
            "title": "Timeless",
            "exercises": [
                {"title": "Plank", "sets": [{"weight_kg": 0.0, "reps": 1}]},
            ],
        },
    ]
    with patch("scripts.sync_history.get_all_workouts", return_value=workouts):
        result = sync_history.sync_all("fake-key", db_path)
    assert result == {"workouts_found": 1, "processed": 1}


def test_rebuild_clears_tables(tmp_path: pathlib.Path) -> None:
    from database import save_workout

    db_path = _new_temp_db(tmp_path)
    payload = {"workouts": [{"id": "W1", "title": "Test"}]}
    save_workout(payload, db_path, when="2026-03-01")
    save_workout(payload, db_path, when="2026-03-02")

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO exercise_progress (date, exercise_name, top_weight_kg, top_reps, sets) "
        "VALUES (?, ?, ?, ?, ?)",
        ("2026-03-01", "Squat", 100.0, 5, 3),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path, timeout=10)
    assert conn.execute("SELECT COUNT(*) FROM workout_history").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM exercise_progress").fetchone()[0] == 1
    conn.close()

    sync_history._rebuild_tables(db_path)

    conn = sqlite3.connect(db_path, timeout=10)
    assert conn.execute("SELECT COUNT(*) FROM workout_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM exercise_progress").fetchone()[0] == 0
    conn.close()
