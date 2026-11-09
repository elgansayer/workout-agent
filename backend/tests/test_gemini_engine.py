"""Tests for gemini_engine.py: workout/rest-day/checkin prompt builders and AI generation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from gemini_engine import (
    _build_checkin_prompt,
    _build_prompt,
    _build_rest_prompt,
    _fallback_plan,
    _fallback_rest_message,
    _format_history,
    generate_checkin_message,
    generate_next_workout,
    generate_rest_day_message,
)

DAY: dict[str, Any] = {
    "number": 1,
    "focus": "Upper Strength",
    "exercises": [
        {"name": "Bench Press", "sets": 4, "rep_range": "5-8", "scheme": "4 × 5-8"},
        {"name": "Row", "sets": 3, "rep_range": "8-12", "scheme": "3 × 8-12"},
    ],
}
BLOCK: dict[str, Any] = {"number": 1, "name": "Foundation", "focus": "Base"}
RULES = ["Progress load safely.", "Keep selected exercises."]


# ---------------------------------------------------------------------------
# _format_history
# ---------------------------------------------------------------------------


def test_format_history_empty() -> None:
    assert _format_history({}) == "None on record yet."
    assert _format_history(None) == "None on record yet."


def test_format_history_with_weight_and_reps() -> None:
    history = {"Bench Press": {"top_weight_kg": 100.0, "top_reps": 5}}
    result = _format_history(history)
    assert "Bench Press: 100 kg x 5" in result


def test_format_history_bodyweight_only() -> None:
    history = {"Pull-Up": {"top_weight_kg": None, "top_reps": 12}}
    result = _format_history(history)
    assert "12 reps (bodyweight)" in result


def test_format_history_no_weight_no_reps() -> None:
    history = {"Curl": {"top_weight_kg": None, "top_reps": None}}
    assert _format_history(history) == "None on record yet."


def test_format_history_sorted_by_name() -> None:
    history = {
        "Squat": {"top_weight_kg": 140, "top_reps": 5},
        "Bench Press": {"top_weight_kg": 100, "top_reps": 8},
    }
    result = _format_history(history)
    assert result.index("Bench Press") < result.index("Squat")


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_focus_week_and_programme() -> None:
    prompt = _build_prompt(
        day=DAY,
        week=3,
        cycle_weeks=12,
        programme_name="Hypertrophy · Hevy routines",
        rules=RULES,
        workout_summary=None,
        recovery=None,
        history=None,
    )
    assert "Week 3 of" in prompt
    assert "12" in prompt
    assert "Upper Strength" in prompt
    assert "Hypertrophy" in prompt


def test_build_prompt_includes_programme_rules() -> None:
    prompt = _build_prompt(
        day=DAY,
        week=1,
        cycle_weeks=8,
        programme_name="Programme",
        rules=RULES,
        workout_summary=None,
        recovery=None,
        history=None,
    )
    assert "Progress load safely." in prompt


def test_build_prompt_includes_history_and_recovery() -> None:
    history = {"Bench Press": {"top_weight_kg": 100, "top_reps": 5}}
    recovery = {"sleep_hours": 7.5, "weight_kg": 82}
    prompt = _build_prompt(
        day=DAY,
        week=1,
        cycle_weeks=8,
        programme_name="Programme",
        rules=RULES,
        workout_summary=None,
        recovery=recovery,
        history=history,
    )
    assert "Bench Press: 100 kg x 5" in prompt
    assert '"sleep_hours": 7.5' in prompt


def test_build_prompt_includes_last_plan() -> None:
    prompt = _build_prompt(
        day=DAY,
        week=1,
        cycle_weeks=8,
        programme_name="Programme",
        rules=RULES,
        workout_summary=None,
        recovery=None,
        history=None,
        last_plan="Yesterday's plan was great.",
    )
    assert "Yesterday's plan was great." in prompt


def test_build_prompt_does_not_hardcode_stage_prep_goals() -> None:
    prompt = _build_prompt(
        day=DAY,
        week=1,
        cycle_weeks=8,
        programme_name="Programme",
        rules=RULES,
        workout_summary=None,
        recovery=None,
        history=None,
    )
    lowered = prompt.lower()
    assert "stage-prep" not in lowered
    assert "powerbuilding" not in lowered
    assert "six-pack" not in lowered


# ---------------------------------------------------------------------------
# _fallback_plan
# ---------------------------------------------------------------------------


def test_fallback_plan_has_focus_and_week() -> None:
    plan = _fallback_plan(day=DAY, week=4)
    assert plan.startswith("Upper Strength - Week 4")
    assert "Bench Press" in plan


def test_fallback_plan_includes_exercises() -> None:
    lines = _fallback_plan(day=DAY, week=2).split("\n")
    assert len(lines) > 1


# ---------------------------------------------------------------------------
# generate_next_workout
# ---------------------------------------------------------------------------

_fake_plan = "Upper Strength - Week 3\nBench Press: 4 x 5-8 (+2.5 kg)\nRow: 3 x 8-12"


def _make_mock_provider(response_text: str, *, raise_error: bool = False) -> MagicMock:
    """Build a mock AI provider with a ``generate()`` method returning *response_text*."""
    mock = MagicMock()
    if raise_error:
        mock.generate.side_effect = RuntimeError("API error")
    else:
        mock.generate.return_value = response_text
    return mock


def test_generate_next_workout_success() -> None:
    mock_provider = _make_mock_provider(_fake_plan)

    result = generate_next_workout(
        mock_provider,
        day=DAY,
        week=3,
        cycle_weeks=12,
        programme_name="Programme",
        rules=RULES,
    )
    assert result == _fake_plan
    mock_provider.generate.assert_called_once()


def test_generate_next_workout_empty_response_falls_back() -> None:
    mock_provider = _make_mock_provider("")

    result = generate_next_workout(
        mock_provider,
        day=DAY,
        week=3,
        cycle_weeks=12,
        programme_name="Programme",
        rules=RULES,
    )
    assert "Upper Strength - Week 3" in result


def test_generate_next_workout_exception_falls_back() -> None:
    mock_provider = _make_mock_provider("", raise_error=True)

    result = generate_next_workout(
        mock_provider,
        day=DAY,
        week=3,
        cycle_weeks=12,
        programme_name="Programme",
        rules=RULES,
    )
    assert "Upper Strength - Week 3" in result


# ---------------------------------------------------------------------------
# _build_rest_prompt / generate_rest_day_message
# ---------------------------------------------------------------------------


def test_build_rest_prompt_includes_recovery() -> None:
    recovery = {"sleep_hours": 6.5, "weight_kg": 82}
    prompt = _build_rest_prompt(recovery)
    assert "recovery is where adaptation happens" in prompt.lower()
    assert '"sleep_hours": 6.5' in prompt


def test_build_rest_prompt_none_recovery() -> None:
    prompt = _build_rest_prompt(None)
    assert "recovery is where adaptation happens" in prompt.lower()


_fake_rest_message = "Rest up, champ. Recovery is key."


def test_generate_rest_day_message_success() -> None:
    mock_provider = _make_mock_provider(_fake_rest_message)

    result = generate_rest_day_message(
        mock_provider,
        recovery={"sleep_hours": 8},
    )
    assert result == _fake_rest_message


def test_generate_rest_day_message_empty_falls_back() -> None:
    mock_provider = _make_mock_provider("")

    result = generate_rest_day_message(mock_provider)
    assert "rest day" in result.lower()


def test_generate_rest_day_message_exception_falls_back() -> None:
    mock_provider = _make_mock_provider("", raise_error=True)

    result = generate_rest_day_message(mock_provider)
    assert "rest day" in result.lower()


# ---------------------------------------------------------------------------
# _fallback_rest_message
# ---------------------------------------------------------------------------


def test_fallback_rest_message_is_non_empty() -> None:
    msg = _fallback_rest_message()
    assert len(msg) > 50
    assert "rest day" in msg.lower()


# ---------------------------------------------------------------------------
# _build_checkin_prompt / generate_checkin_message
# ---------------------------------------------------------------------------


def test_build_checkin_prompt_includes_block_info() -> None:
    prompt = _build_checkin_prompt(
        number=2,
        week=5,
        cycle_weeks=12,
        block=BLOCK,
        rules=RULES,
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
    )
    assert "check-in" in prompt.lower()
    assert "Week 5" in prompt
    assert "Block 1" in prompt
    assert "Bench is stalling." in prompt


_fake_checkin = "Check-in 2: Block 1 (Foundation)\nBench progressing well.\nKeep pushing!"


def test_generate_checkin_message_success() -> None:
    mock_provider = _make_mock_provider(_fake_checkin)

    result = generate_checkin_message(
        mock_provider,
        number=2,
        week=5,
        cycle_weeks=12,
        block=BLOCK,
        rules=RULES,
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
        fallback="Fallback check-in.",
    )
    assert result == _fake_checkin


def test_generate_checkin_message_empty_falls_back() -> None:
    mock_provider = _make_mock_provider("")

    result = generate_checkin_message(
        mock_provider,
        number=2,
        week=5,
        cycle_weeks=12,
        block=BLOCK,
        rules=RULES,
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
        fallback="Fallback check-in.",
    )
    assert result == "Fallback check-in."


def test_generate_checkin_message_exception_falls_back() -> None:
    mock_provider = _make_mock_provider("", raise_error=True)

    result = generate_checkin_message(
        mock_provider,
        number=2,
        week=5,
        cycle_weeks=12,
        block=BLOCK,
        rules=RULES,
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
        fallback="Fallback check-in.",
    )
    assert result == "Fallback check-in."
