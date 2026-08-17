"""Tests for gemini_engine.py: prompt building, fallback plans, and AI generation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gemini_engine import (
    _build_autonomous_prompt,
    _build_checkin_prompt,
    _build_prompt,
    _build_rest_prompt,
    _fallback_plan,
    _fallback_rest_message,
    _format_history,
    apply_autonomous_adjustments,
    generate_checkin_message,
    generate_next_workout,
    generate_rest_day_message,
)
from hevy_parser import WorkoutSummary
from insights import LiftInsight, RecoveryInsight, TrainingInsights
from program import BLOCKS

# ---------------------------------------------------------------------------
# _format_history tests
# ---------------------------------------------------------------------------


def test_format_history_none_returns_placeholder():
    assert _format_history(None) == "None on record yet."


def test_format_history_empty_returns_placeholder():
    assert _format_history({}) == "None on record yet."


def test_format_history_weighted_lift():
    history = {"Deadlift": {"top_weight_kg": 140.0, "top_reps": 5}}
    result = _format_history(history)
    assert "Deadlift: 140 kg x 5" in result


def test_format_history_bodyweight_lift():
    history = {"Pull-Ups": {"top_weight_kg": None, "top_reps": 10}}
    result = _format_history(history)
    assert "Pull-Ups: 10 reps (bodyweight)" in result


def test_format_history_sorts_by_name():
    history = {
        "Squat": {"top_weight_kg": 120, "top_reps": 8},
        "Bench Press": {"top_weight_kg": 80, "top_reps": 10},
        "Deadlift": {"top_weight_kg": 140, "top_reps": 5},
    }
    result = _format_history(history)
    lines = result.split("\n")
    assert len(lines) == 3
    assert "Bench" in lines[0]
    assert "Deadlift" in lines[1]
    assert "Squat" in lines[2]


def test_format_history_skips_missing_data():
    history = {"Curls": {"top_weight_kg": 15, "top_reps": None}}
    result = _format_history(history)
    assert result == "None on record yet."


# ---------------------------------------------------------------------------
# _fallback_plan tests
# ---------------------------------------------------------------------------


def test_fallback_plan_returns_correct_header():
    block = BLOCKS[1]
    plan = _fallback_plan(1, 3, block)
    assert plan.startswith("Back, Deadlifts & Chest - Week 3 (Accumulation)")


def test_fallback_plan_includes_exercises():
    block = BLOCKS[1]
    plan = _fallback_plan(1, 3, block)
    assert "Deadlift" in plan
    assert "Pull-Up" in plan
    # Verify each exercise line has "x" between sets and rep range
    for line in plan.split("\n"):
        if ":" in line:
            assert " x " in line


# ---------------------------------------------------------------------------
# _fallback_rest_message tests
# ---------------------------------------------------------------------------


def test_fallback_rest_message_is_non_empty_string():
    msg = _fallback_rest_message()
    assert isinstance(msg, str)
    assert len(msg) > 0
    assert "rest day" in msg.lower()
    assert "sleep" in msg.lower()


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------


def test_build_prompt_includes_key_sections():
    block = BLOCKS[1]
    prompt = _build_prompt(
        day=1, week=3, block=block,
        workout_summary=None, recovery=None, history=None,
        insights=None, last_plan=None,
    )
    assert "Week 3 of 12" in prompt
    assert "Block 1: Accumulation" in prompt
    assert "Day 1: Back, Deadlifts & Chest" in prompt
    assert "Coaching rules" in prompt
    assert "baseline plan" in prompt
    assert "EXECUTED routine" in prompt


def test_build_prompt_includes_workout_summary():
    block = BLOCKS[1]
    summary = WorkoutSummary(
        title="Back Day",
        date="2026-08-01",
        duration_seconds=3600,
        total_volume_kg=5000,
        exercises=[],
    )
    prompt = _build_prompt(
        day=1, week=3, block=block,
        workout_summary=summary, recovery=None, history=None,
    )
    assert "Back Day" in prompt


def test_build_prompt_includes_recovery_json():
    block = BLOCKS[1]
    recovery = {"sleep_hours": 7.5, "resting_hr": 55}
    prompt = _build_prompt(
        day=1, week=3, block=block,
        workout_summary=None, recovery=recovery, history=None,
    )
    assert '"sleep_hours"' in prompt
    assert "7.5" in prompt


def test_build_prompt_includes_insights():
    block = BLOCKS[1]
    rec = RecoveryInsight(
        sleep_hours=7.5, resting_hr=58, resting_hr_trend="steady",
        weight_kg=82.0, weight_trend="falling",
        body_fat_pct=15.0, body_fat_trend="falling",
        muscle_pct=None, is_catabolic=False,
        status="good", directive="push confidently",
    )
    lift = LiftInsight(
        name="Deadlift", metric="kg", sessions=4, latest=115.0, best=115.0,
        change_pct=0.15, slope_per_session=3.5, trend="progressing",
        sessions_since_best=0, intervention=None,
    )
    insights = TrainingInsights(lifts=[lift], recovery=rec, headline="All good.")
    prompt = _build_prompt(
        day=1, week=3, block=block,
        workout_summary=None, recovery=None, history=None,
        insights=insights,
    )
    assert "All good." in prompt or "Deadlift" in prompt


def test_build_prompt_includes_last_plan():
    block = BLOCKS[1]
    prompt = _build_prompt(
        day=1, week=3, block=block,
        workout_summary=None, recovery=None, history=None,
        last_plan="Deadlift: 4 x 5-8",
    )
    assert "Deadlift: 4 x 5-8" in prompt


# ---------------------------------------------------------------------------
# _build_rest_prompt tests
# ---------------------------------------------------------------------------


def test_build_rest_prompt_includes_rules():
    prompt = _build_rest_prompt({"sleep_hours": 6.0})
    assert "Coaching rules" in prompt
    assert "rest day" in prompt.lower()
    assert '"sleep_hours"' in prompt


def test_build_rest_prompt_without_recovery():
    prompt = _build_rest_prompt(None)
    assert "None available" in prompt


# ---------------------------------------------------------------------------
# _build_checkin_prompt tests
# ---------------------------------------------------------------------------


def test_build_checkin_prompt():
    block = BLOCKS[2]
    prompt = _build_checkin_prompt(
        number=2, week=6, block=block,
        workouts_done=20, weeks=4, analysis_text="Squat: +10kg",
    )
    assert "check-in number 2" in prompt.lower()
    assert "Week 6" in prompt
    # Block info may wrap across lines due to long focus text
    assert "Block 2" in prompt
    assert "Intensification" in prompt
    assert "Squat: +10kg" in prompt


# ---------------------------------------------------------------------------
# _build_autonomous_prompt tests
# ---------------------------------------------------------------------------


def test_build_autonomous_prompt_basic():
    routines = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    logs = [{"workout_name": "Back Day", "date": "2026-08-01"}]
    prompt = _build_autonomous_prompt(routines, logs, weather=None, is_catabolic=False)
    assert "base_routines" in prompt
    assert '"Back Day"' in prompt
    assert "hevy_logs" in prompt


def test_build_autonomous_prompt_with_weather():
    from unittest.mock import MagicMock
    weather = MagicMock()
    weather.is_extreme_heat = True
    weather.as_text.return_value = "EXTREME HEAT WARNING"

    routines = {"Back Day": []}
    logs: list = []
    prompt = _build_autonomous_prompt(routines, logs, weather=weather, is_catabolic=False)
    assert "EXTREME HEAT WARNING" in prompt
    assert "Thermal Scaling" in prompt


def test_build_autonomous_prompt_catabolic():
    routines = {"Back Day": []}
    logs: list = []
    prompt = _build_autonomous_prompt(routines, logs, weather=None, is_catabolic=True)
    assert "catabolic" in prompt.lower()
    assert "2.5 g/kg" in prompt


# ---------------------------------------------------------------------------
# generate_next_workout tests (with mocked Gemini)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_genai_configure():
    with patch("gemini_engine.genai") as mock_genai:
        yield mock_genai


def test_generate_next_workout_success(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "Back, Deadlifts & Chest - Week 3 (Accumulation)\nDeadlift: 4 x 5-8"
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_next_workout(
        api_key="test-key", model_name="gemini-2.5-flash",
        day=1, week=3, block=BLOCKS[1],
    )
    assert "Back, Deadlifts & Chest" in result
    assert "Week 3" in result


def test_generate_next_workout_empty_response_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = ""
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_next_workout(
        api_key="test-key", model_name="gemini-2.5-flash",
        day=1, week=3, block=BLOCKS[1],
    )
    assert "Back, Deadlifts & Chest - Week 3 (Accumulation)" in result


def test_generate_next_workout_error_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.side_effect = RuntimeError("API error")
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_next_workout(
        api_key="test-key", model_name="gemini-2.5-flash",
        day=1, week=3, block=BLOCKS[1],
    )
    assert "Back, Deadlifts & Chest - Week 3 (Accumulation)" in result


# ---------------------------------------------------------------------------
# generate_rest_day_message tests
# ---------------------------------------------------------------------------


def test_generate_rest_day_success(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "Enjoy your rest day. Focus on recovery."
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_rest_day_message("test-key", "gemini-2.5-flash")
    assert "rest day" in result.lower()


def test_generate_rest_day_empty_response_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = ""
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_rest_day_message("test-key", "gemini-2.5-flash")
    assert "rest day" in result.lower()
    assert "sleep" in result.lower()


def test_generate_rest_day_error_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.side_effect = ConnectionError("timeout")
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_rest_day_message("test-key", "gemini-2.5-flash")
    assert "rest day" in result.lower()


# ---------------------------------------------------------------------------
# generate_checkin_message tests
# ---------------------------------------------------------------------------


def test_generate_checkin_success(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "Check-in 2: Block 2 (Intensification)\nLooking strong."
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_checkin_message(
        api_key="test-key", model_name="gemini-2.5-flash",
        number=2, week=6, block=BLOCKS[2],
        workouts_done=24, weeks=4, analysis_text="Deadlift: +15kg",
        fallback="Fallback check-in text.",
    )
    assert "Check-in 2" in result
    assert "Intensification" in result


def test_generate_checkin_error_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.side_effect = RuntimeError("fail")
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_checkin_message(
        api_key="test-key", model_name="gemini-2.5-flash",
        number=2, week=6, block=BLOCKS[2],
        workouts_done=24, weeks=4, analysis_text="...",
        fallback="Fallback check-in text.",
    )
    assert result == "Fallback check-in text."


def test_generate_checkin_empty_response_returns_fallback(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = ""
    mock_genai_configure.GenerativeModel.return_value = mock_model

    result = generate_checkin_message(
        api_key="test-key", model_name="gemini-2.5-flash",
        number=2, week=6, block=BLOCKS[2],
        workouts_done=24, weeks=4, analysis_text="...",
        fallback="Fallback check-in text.",
    )
    assert result == "Fallback check-in text."


# ---------------------------------------------------------------------------
# apply_autonomous_adjustments tests
# ---------------------------------------------------------------------------


def test_apply_autonomous_adjustments_success(mock_genai_configure):
    updated = {"Back Day": [{"exercise": "Deadlift", "sets": 5}]}
    import json
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = json.dumps(updated)
    mock_genai_configure.GenerativeModel.return_value = mock_model

    base = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    result = apply_autonomous_adjustments(
        api_key="test-key", model_name="gemini-2.5-flash",
        base_routines=base, hevy_logs=[],
    )
    assert result["Back Day"][0]["sets"] == 5


def test_apply_autonomous_adjustments_strips_markdown(mock_genai_configure):
    updated = {"Back Day": [{"exercise": "Deadlift", "sets": 5}]}
    import json
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "```json\n" + json.dumps(updated) + "\n```"
    mock_genai_configure.GenerativeModel.return_value = mock_model

    base = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    result = apply_autonomous_adjustments(
        api_key="test-key", model_name="gemini-2.5-flash",
        base_routines=base, hevy_logs=[],
    )
    assert result["Back Day"][0]["sets"] == 5


def test_apply_autonomous_adjustments_invalid_json_returns_baseline(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "not valid json at all"
    mock_genai_configure.GenerativeModel.return_value = mock_model

    base = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    result = apply_autonomous_adjustments(
        api_key="test-key", model_name="gemini-2.5-flash",
        base_routines=base, hevy_logs=[],
    )
    assert result == base


def test_apply_autonomous_adjustments_non_dict_returns_baseline(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.return_value.text = "[1, 2, 3]"
    mock_genai_configure.GenerativeModel.return_value = mock_model

    base = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    result = apply_autonomous_adjustments(
        api_key="test-key", model_name="gemini-2.5-flash",
        base_routines=base, hevy_logs=[],
    )
    assert result == base


def test_apply_autonomous_adjustments_error_returns_baseline(mock_genai_configure):
    mock_model = MagicMock()
    mock_model.generate_content.side_effect = RuntimeError("API down")
    mock_genai_configure.GenerativeModel.return_value = mock_model

    base = {"Back Day": [{"exercise": "Deadlift", "sets": 4}]}
    result = apply_autonomous_adjustments(
        api_key="test-key", model_name="gemini-2.5-flash",
        base_routines=base, hevy_logs=[],
    )
    assert result == base
