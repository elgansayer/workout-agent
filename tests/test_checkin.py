"""Tests for the periodic programme check-in engine."""

from __future__ import annotations

from datetime import date, timedelta

import checkin
from config import Config
from database import get_checkins, get_meta, init_db, set_meta
from program import BLOCKS


def _config(tmp_path, hevy_api_key="key") -> Config:
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return Config(
        hevy_api_key=hevy_api_key,
        gemini_api_key="g",
        telegram_bot_token="t",
        telegram_chat_id="c",
        gemini_model="gemini-2.5-flash",
        health_connect_file=None,
        database_path=db_path,
        telegram_parse_mode=None,
        hevy_sync_routines=True,
        hevy_prefill_weights=True,
        checkin_enabled=True,
        lifestyle_enabled=True,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=True,
        self_review_weekday=6,
    )


def test_session_top_sets_picks_heaviest_per_session():
    history = [
        {"workout_start_time": "2026-01-01T08:00:00Z", "weight_kg": 90, "reps": 8},
        {"workout_start_time": "2026-01-01T08:00:00Z", "weight_kg": 100, "reps": 5},
        {"workout_start_time": "2026-01-08T08:00:00Z", "weight_kg": 105, "reps": 5},
    ]
    tops = checkin._session_top_sets(history)
    assert tops == [(100, 5), (105, 5)]


def test_review_exercise_detects_progress():
    history = [
        {"workout_start_time": "2026-01-01T08:00:00Z", "weight_kg": 100, "reps": 8},
        {"workout_start_time": "2026-01-08T08:00:00Z", "weight_kg": 110, "reps": 8},
    ]
    review = checkin._review_exercise("Deadlift", "4 x 5-8", "5-8", history)
    assert review.sessions == 2
    assert review.change_kg == 10
    assert review.hit_top is True
    assert review.stalled is False
    assert review.latest == "110 kg x 8"


def test_review_exercise_detects_stall():
    history = [
        {"workout_start_time": f"2026-01-0{i}T08:00:00Z", "weight_kg": 100, "reps": 12}
        for i in range(1, 4)
    ]
    review = checkin._review_exercise("Leg Press", "4 x 10-12", "10-12", history)
    assert review.sessions == 3
    assert review.change_kg == 0
    assert review.stalled is True


def test_due_seeds_baseline_then_not_due(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.setattr(checkin, "get_workout_count", lambda _key: 40)
    # First ever call seeds the baseline at the current total and is not due.
    assert checkin.due(config) is None
    assert get_meta("last_checkin_workout_count", config.database_path) == "40"
    assert get_meta("checkin_number", config.database_path) == "0"


def test_due_fires_after_target_workouts(monkeypatch, tmp_path):
    config = _config(tmp_path)
    monkeypatch.setattr(checkin, "get_workout_count", lambda _key: 40)
    checkin.due(config)  # seed at 40
    # 24 more sessions logged -> due.
    monkeypatch.setattr(checkin, "get_workout_count", lambda _key: 64)
    due_info = checkin.due(config)
    assert due_info is not None
    assert due_info.number == 1
    assert due_info.workouts_done == 24
    assert due_info.total_count == 64


def test_due_calendar_fallback_without_hevy(tmp_path):
    config = _config(tmp_path, hevy_api_key=None)
    checkin.due(config)  # seeds number=0, last date today
    # Pretend five weeks have passed since the last check-in.
    five_weeks_ago = (date.today() - timedelta(weeks=5)).isoformat()
    set_meta("last_checkin_date", five_weeks_ago, config.database_path)
    due_info = checkin.due(config)
    assert due_info is not None
    assert due_info.weeks_elapsed >= 4
    assert due_info.total_count is None


def test_record_persists_and_resets_baseline(tmp_path):
    config = _config(tmp_path)
    due_info = checkin.CheckinDue(
        number=2, workouts_done=24, weeks_elapsed=4, total_count=64
    )
    checkin.record(config, due_info, "Check-in 2: looking strong.", today=date(2026, 3, 1))
    assert get_meta("checkin_number", config.database_path) == "2"
    assert get_meta("last_checkin_date", config.database_path) == "2026-03-01"
    assert get_meta("last_checkin_workout_count", config.database_path) == "64"
    saved = get_checkins(db_path=config.database_path)
    assert len(saved) == 1
    assert saved[0]["number"] == 2
    assert saved[0]["workouts_done"] == 24


def test_analyse_without_hevy_returns_empty(tmp_path):
    config = _config(tmp_path, hevy_api_key=None)
    assert checkin._analyse(config, BLOCKS[1]) == []
