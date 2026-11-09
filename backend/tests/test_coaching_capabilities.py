"""Tests for coaching capability and prerequisite evaluation."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from coaching_capabilities import (
    classify_coaching_query,
    get_coaching_capabilities,
)
from database import (
    init_db,
    save_body_metrics,
    save_daily_log,
    set_active_programme,
)


@pytest.fixture
def clean_db(tmp_path: Any) -> str:
    db_file = str(tmp_path / "test_coach.db")
    init_db(db_file)
    return db_file


def test_no_provider_disables_all_capabilities_with_explanation(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("ai_resolver.resolve_provider", lambda **kwargs: (_ for _ in ()).throw(ValueError("No key")))

    state = get_coaching_capabilities("user-1", db_path=clean_db)
    assert not state.provider_ready
    assert state.overall_status == "setup_required"
    assert "Settings" in state.welcome_message
    assert not state.capabilities["educational"].enabled
    assert not state.capabilities["workout_analysis"].enabled
    assert all(not p.enabled for p in state.suggested_prompts)


def test_provider_only_enters_educational_mode(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    state = get_coaching_capabilities("user-1", db_path=clean_db)
    assert state.provider_ready
    assert state.overall_status == "educational_only"
    assert state.capabilities["educational"].enabled
    assert not state.capabilities["workout_analysis"].enabled
    assert "Connect Hevy" in state.capabilities["workout_analysis"].explanation
    assert not state.capabilities["recovery_analysis"].enabled
    assert "Connect a health source" in state.capabilities["recovery_analysis"].explanation
    assert not state.capabilities["body_composition"].enabled
    assert not state.capabilities["programme_guidance"].enabled

    # Suggested prompts requiring data are disabled with clear explanations
    deadlift_prompt = next(p for p in state.suggested_prompts if "deadlift" in p.text.lower())
    assert not deadlift_prompt.enabled
    assert "Connect Hevy" in deadlift_prompt.explanation


def test_fresh_workout_data_enables_workout_analysis(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    today = date(2026, 11, 7)
    save_daily_log(
        today.isoformat(),
        1,
        "Deadlift focus",
        "medium",
        "5x5 Deadlifts",
        "8h sleep",
        db_path=clean_db,
        user_id="user-1",
    )

    state = get_coaching_capabilities("user-1", db_path=clean_db, now=today)
    assert state.capabilities["workout_analysis"].enabled
    assert state.capabilities["workout_analysis"].status == "ready"

    deadlift_prompt = next(p for p in state.suggested_prompts if "deadlift" in p.text.lower())
    assert deadlift_prompt.enabled


def test_stale_workout_data_disables_workout_analysis_with_staleness_explanation(
    clean_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    now = date(2026, 11, 7)
    old_date = (now - timedelta(days=25)).isoformat()
    save_daily_log(
        old_date,
        1,
        "Past session",
        "medium",
        "Squats",
        "rest",
        db_path=clean_db,
        user_id="user-1",
    )

    state = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert not state.capabilities["workout_analysis"].enabled
    assert state.capabilities["workout_analysis"].status == "stale_data"
    assert "stale" in state.capabilities["workout_analysis"].explanation.lower()
    assert old_date in state.capabilities["workout_analysis"].explanation


def test_health_recovery_freshness(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    now = date(2026, 11, 7)

    # 1. No health source
    state = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert not state.capabilities["recovery_analysis"].enabled
    assert state.capabilities["recovery_analysis"].status == "missing_source"

    # 2. Stale health data (4 days old)
    old_date = (now - timedelta(days=4)).isoformat()
    save_body_metrics({"weight_kg": 80.0, "resting_hr": 60, "hrv": 55.0}, when=old_date, db_path=clean_db, user_id="user-1")
    state_stale = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert not state_stale.capabilities["recovery_analysis"].enabled
    assert state_stale.capabilities["recovery_analysis"].status == "stale_data"

    # 3. Fresh health data (today)
    save_body_metrics({"weight_kg": 80.0, "resting_hr": 58, "hrv": 62.0}, when=now.isoformat(), db_path=clean_db, user_id="user-1")
    state_fresh = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert state_fresh.capabilities["recovery_analysis"].enabled
    assert state_fresh.capabilities["recovery_analysis"].status == "ready"


def test_body_composition_freshness(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    now = date(2026, 11, 7)

    # Missing
    state = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert not state.capabilities["body_composition"].enabled
    assert state.capabilities["body_composition"].status == "missing_data"

    # Fresh
    save_body_metrics({"weight_kg": 82.5, "body_fat_pct": 15.0}, when=now.isoformat(), db_path=clean_db, user_id="user-1")
    state_fresh = get_coaching_capabilities("user-1", db_path=clean_db, now=now)
    assert state_fresh.capabilities["body_composition"].enabled
    assert state_fresh.capabilities["body_composition"].status == "ready"


def test_active_programme_guidance(clean_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class _DummyProvider:
        name = "gemini"

    monkeypatch.setattr("coaching_capabilities.resolve_provider", lambda **kwargs: _DummyProvider())

    # None
    state = get_coaching_capabilities("user-1", db_path=clean_db)
    assert not state.capabilities["programme_guidance"].enabled

    # Set active programme
    set_active_programme("user-1", "hevy", "routine-1", {"name": "Hypertrophy Block", "days": [{"name": "Day 1"}]}, db_path=clean_db)
    state_active = get_coaching_capabilities("user-1", db_path=clean_db)
    assert state_active.capabilities["programme_guidance"].enabled
    assert state_active.capabilities["programme_guidance"].status == "ready"


def test_classify_coaching_query() -> None:
    # Educational
    assert classify_coaching_query("What is progressive overload?") == ("educational", None)
    assert classify_coaching_query("How to perform a low bar squat?") == ("educational", None)
    assert classify_coaching_query("Explain RPE vs RIR") == ("educational", None)
    assert classify_coaching_query("What are good protein sources?") == ("educational", None)

    # Data grounded
    assert classify_coaching_query("How is my deadlift progressing?") == ("data_grounded", "workout_analysis")
    assert classify_coaching_query("Which lifts are stalling?") == ("data_grounded", "workout_analysis")
    assert classify_coaching_query("Should I deload this week?") == ("data_grounded", "workout_analysis")
    assert classify_coaching_query("Am I recovering well enough?") == ("data_grounded", "recovery_analysis")
    assert classify_coaching_query("How is my body composition trending?") == ("data_grounded", "body_composition")
    assert classify_coaching_query("What should I focus on this block?") == ("data_grounded", "programme_guidance")
    assert classify_coaching_query("How is my bench press?") == ("data_grounded", "workout_analysis")
    assert classify_coaching_query("Check my recovery readiness") == ("data_grounded", "recovery_analysis")
