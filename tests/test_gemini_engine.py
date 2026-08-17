"""Tests for gemini_engine.py: workout/rest-day/checkin prompt builders and AI generation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ai_provider import get_provider as ai_get_provider
from gemini_engine import (
    _build_autonomous_prompt,
    _build_checkin_prompt,
    _build_prompt,
    _build_rest_prompt,
    _fallback_plan,
    _fallback_rest_message,
    _format_history,
    apply_autonomous_adjustments,
    create_provider,
    generate_checkin_message,
    generate_next_workout,
    generate_rest_day_message,
    get_provider,
)
from program import BLOCKS

# ---------------------------------------------------------------------------
# _format_history
# ---------------------------------------------------------------------------


def test_format_history_empty() -> None:
    assert _format_history({}) == "None on record yet."
    assert _format_history(None) == "None on record yet."


def test_format_history_with_weight_and_reps() -> None:
    history = {"Deadlift (Barbell)": {"top_weight_kg": 140.0, "top_reps": 5}}
    result = _format_history(history)
    assert "Deadlift (Barbell): 140 kg x 5" in result


def test_format_history_bodyweight_only() -> None:
    history = {"Pull-Up": {"top_weight_kg": None, "top_reps": 12}}
    result = _format_history(history)
    assert "12 reps (bodyweight)" in result


def test_format_history_no_weight_no_reps() -> None:
    history = {"Curl": {"top_weight_kg": None, "top_reps": None}}
    assert _format_history(history) == "None on record yet."


def test_format_history_sorted_by_name() -> None:
    history = {
        "Deadlift": {"top_weight_kg": 140, "top_reps": 5},
        "Bench Press": {"top_weight_kg": 100, "top_reps": 8},
    }
    result = _format_history(history)
    # "Bench Press" should come before "Deadlift"
    assert result.index("Bench Press") < result.index("Deadlift")


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_focus_and_week() -> None:
    prompt = _build_prompt(
        day=1,
        week=3,
        block=BLOCKS[1],
        workout_summary=None,
        recovery=None,
        history=None,
        insights=None,
        last_plan=None,
    )
    assert "Week 3 of 12" in prompt
    assert "Block 1" in prompt
    assert "Day 1" in prompt


def test_build_prompt_includes_coaching_rules() -> None:
    prompt = _build_prompt(
        day=1,
        week=1,
        block=BLOCKS[1],
        workout_summary=None,
        recovery=None,
        history=None,
    )
    assert "Coaching rules" in prompt


def test_build_prompt_includes_history_and_recovery() -> None:
    history = {"Deadlift": {"top_weight_kg": 100, "top_reps": 5}}
    recovery = {"sleep_hours": 7.5, "weight_kg": 82}
    prompt = _build_prompt(
        day=1,
        week=1,
        block=BLOCKS[1],
        workout_summary=None,
        recovery=recovery,
        history=history,
    )
    assert "Deadlift: 100 kg x 5" in prompt
    assert '"sleep_hours": 7.5' in prompt


def test_build_prompt_includes_last_plan() -> None:
    prompt = _build_prompt(
        day=1,
        week=1,
        block=BLOCKS[1],
        workout_summary=None,
        recovery=None,
        history=None,
        last_plan="Yesterday's plan was great.",
    )
    assert "Yesterday's plan was great." in prompt


# ---------------------------------------------------------------------------
# _fallback_plan
# ---------------------------------------------------------------------------


def test_fallback_plan_has_focus_and_week() -> None:
    plan = _fallback_plan(day=1, week=4, block=BLOCKS[1])
    assert plan.startswith("Back, Deadlifts & Chest - Week 4")
    assert "Deadlift" in plan


def test_fallback_plan_includes_exercises() -> None:
    plan = _fallback_plan(day=3, week=2, block=BLOCKS[2])
    # Should have focus and exercises
    lines = plan.split("\n")
    assert len(lines) > 1  # at least one exercise


# ---------------------------------------------------------------------------
# generate_next_workout
# ---------------------------------------------------------------------------

_fake_gemini_plan = "Back, Deadlifts & Chest - Week 3 (Hypertrophy)\nDeadlift: 4 x 8 (+2.5 kg)\nPull-Up: 3 x 10 (+1 rep)"


def _make_mock_provider(response_text: str, *, raise_error: bool = False) -> MagicMock:
    """Build a mock AI provider with a ``generate()`` method returning *response_text*."""
    mock = MagicMock()
    if raise_error:
        mock.generate.side_effect = RuntimeError("API error")
    else:
        mock.generate.return_value = response_text
    return mock


def test_generate_next_workout_success() -> None:
    mock_provider = _make_mock_provider(_fake_gemini_plan)

    result = generate_next_workout(
        mock_provider,
        day=1,
        week=3,
        block=BLOCKS[1],
    )
    assert result == _fake_gemini_plan
    mock_provider.generate.assert_called_once()


def test_generate_next_workout_empty_response_falls_back() -> None:
    mock_provider = _make_mock_provider("")

    result = generate_next_workout(
        mock_provider,
        day=1,
        week=3,
        block=BLOCKS[1],
    )
    assert "Back, Deadlifts & Chest - Week 3" in result


def test_generate_next_workout_exception_falls_back() -> None:
    mock_provider = _make_mock_provider("", raise_error=True)

    result = generate_next_workout(
        mock_provider,
        day=1,
        week=3,
        block=BLOCKS[1],
    )
    assert "Back, Deadlifts & Chest - Week 3" in result


# ---------------------------------------------------------------------------
# _build_rest_prompt / generate_rest_day_message
# ---------------------------------------------------------------------------


def test_build_rest_prompt_includes_recovery() -> None:
    recovery = {"sleep_hours": 6.5, "weight_kg": 82}
    prompt = _build_rest_prompt(recovery)
    assert "recovery is where the muscle is built" in prompt.lower()
    assert '"sleep_hours": 6.5' in prompt


def test_build_rest_prompt_none_recovery() -> None:
    prompt = _build_rest_prompt(None)
    assert "recovery is where the muscle is built" in prompt.lower()


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
        block=BLOCKS[2],
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
    )
    assert "Check-in number 2" in prompt.lower() or "check-in" in prompt.lower()
    assert "Week 5" in prompt
    assert "Block 2" in prompt
    assert "Bench is stalling." in prompt


_fake_checkin = "Check-in 2: Block 2 (Strength)\nDeadlift progressing well.\nBench stalled.\nAdd 2.5 kg to deadlift.\nKeep pushing!"


def test_generate_checkin_message_success() -> None:
    mock_provider = _make_mock_provider(_fake_checkin)

    result = generate_checkin_message(
        mock_provider,
        number=2,
        week=5,
        block=BLOCKS[2],
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
        block=BLOCKS[2],
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
        block=BLOCKS[2],
        workouts_done=30,
        weeks=6,
        analysis_text="Bench is stalling.",
        fallback="Fallback check-in.",
    )
    assert result == "Fallback check-in."


# ---------------------------------------------------------------------------
# _build_autonomous_prompt / apply_autonomous_adjustments
# ---------------------------------------------------------------------------


def test_build_autonomous_prompt_includes_routines() -> None:
    routines = {"Push Day": [{"name": "Bench Press", "sets": 4}]}
    prompt = _build_autonomous_prompt(routines, [], None, False)
    assert "Bench Press" in prompt


def test_build_autonomous_prompt_extreme_heat() -> None:
    from weather import WeatherConditions

    weather = WeatherConditions(temperature_c=38, humidity_pct=20, is_extreme_heat=True)
    routines: dict[str, list[dict[str, object]]] = {"Day 1": []}
    prompt = _build_autonomous_prompt(routines, [], weather, False)
    assert "thermal stress" in prompt.lower()


def test_build_autonomous_prompt_catabolic() -> None:
    routines: dict[str, list[dict[str, object]]] = {"Day 1": []}
    prompt = _build_autonomous_prompt(routines, [], None, True)
    assert "catabolic" in prompt.lower()


def test_apply_autonomous_adjustments_success() -> None:
    updated = {"Push Day": [{"name": "Bench Press", "sets": 3}]}
    mock_provider = _make_mock_provider(json.dumps(updated))

    result = apply_autonomous_adjustments(
        mock_provider,
        base_routines={"Push Day": [{"name": "Bench Press", "sets": 4}]},
        hevy_logs=[],
    )
    assert result == updated


def test_apply_autonomous_adjustments_strips_markdown() -> None:
    updated = {"Day 1": [{"name": "Squat", "sets": 3}]}
    mock_provider = _make_mock_provider(f"```json\n{json.dumps(updated)}\n```")

    result = apply_autonomous_adjustments(
        mock_provider,
        base_routines={"Day 1": [{"name": "Squat", "sets": 4}]},
        hevy_logs=[],
    )
    assert result == updated


def test_apply_autonomous_adjustments_non_dict_falls_back() -> None:
    mock_provider = _make_mock_provider("[1, 2, 3]")

    base = {"Day 1": [{"name": "Squat", "sets": 4}]}
    result = apply_autonomous_adjustments(
        mock_provider,
        base_routines=base,
        hevy_logs=[],
    )
    assert result is base  # returned unchanged


def test_apply_autonomous_adjustments_exception_falls_back() -> None:
    mock_provider = _make_mock_provider("", raise_error=True)

    base = {"Day 1": [{"name": "Squat", "sets": 4}]}
    result = apply_autonomous_adjustments(
        mock_provider,
        base_routines=base,
        hevy_logs=[],
    )
    assert result is base  # returned unchanged


def test_apply_autonomous_adjustments_invalid_json_falls_back() -> None:
    mock_provider = _make_mock_provider("not valid json {")

    base = {"Day 1": [{"name": "Squat", "sets": 4}]}
    result = apply_autonomous_adjustments(
        mock_provider,
        base_routines=base,
        hevy_logs=[],
    )
    assert result is base


# ---------------------------------------------------------------------------
# get_provider re-export
# ---------------------------------------------------------------------------


def test_get_provider_reexported() -> None:
    """gemini_engine.get_provider is the same callable as ai_provider.get_provider."""
    assert get_provider is ai_get_provider


# ---------------------------------------------------------------------------
# create_provider
# ---------------------------------------------------------------------------


def test_create_provider_gemini() -> None:
    provider = create_provider("gemini", "test-key")
    assert provider.name() == "Gemini (gemini-2.5-flash)"


def test_create_provider_custom_model() -> None:
    provider = create_provider("openai", "sk-test", model="gpt-4o-mini")
    assert "gpt-4o-mini" in provider.name()
    assert "OpenAI" in provider.name()


def test_create_provider_unknown_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="Unknown AI provider"):
        create_provider("nonexistent", "key")
