"""Tests for gemini_engine.py: validated-prescription explanations."""

from __future__ import annotations

from datetime import date
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
from programme_runtime import ProgrammeRuntime


def _runtime() -> ProgrammeRuntime:
    day = {
        "number": 1,
        "routine_id": "routine-chest-back",
        "focus": "Chest and back",
        "exercises": [
            {
                "name": "Incline press",
                "template_id": "press",
                "sets": 3,
                "rep_range": "8-12",
                "scheme": "3 × 8-12",
                "note": "",
                "role": "primary_strength",
            },
            {
                "name": "Chest-supported row",
                "template_id": "row",
                "sets": 3,
                "rep_range": "10-12",
                "scheme": "3 × 10-12",
                "note": "controlled",
                "role": "secondary_compound",
            },
        ],
    }
    block = {
        "number": 1,
        "name": "Foundation volume",
        "start_week": 1,
        "end_week": 4,
    }
    return ProgrammeRuntime(
        status="active",
        message="Your workout for today.",
        name="Synthetic programme",
        goal="hypertrophy",
        source="hevy",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 10, 26),
        week=3,
        cycle_weeks=8,
        blocks=[block],
        block=block,
        days=[day],
        next_day=day,
    )


# ---------------------------------------------------------------------------
# _format_history
# ---------------------------------------------------------------------------


def test_format_history_empty() -> None:
    assert _format_history({}) == "None on record yet."
    assert _format_history(None) == "None on record yet."


def test_format_history_with_weight_and_reps() -> None:
    history = {"Incline press": {"top_weight_kg": 80.0, "top_reps": 8}}
    assert "Incline press: 80 kg x 8" in _format_history(history)


def test_format_history_bodyweight_only() -> None:
    history = {"Assisted dip": {"top_weight_kg": None, "top_reps": 12}}
    assert "12 reps (bodyweight)" in _format_history(history)


def test_format_history_no_weight_no_reps() -> None:
    assert _format_history({"Curl": {"top_weight_kg": None, "top_reps": None}}) == (
        "None on record yet."
    )


# ---------------------------------------------------------------------------
# Prompt and fallback
# ---------------------------------------------------------------------------


def test_build_prompt_includes_programme_week_and_focus() -> None:
    runtime = _runtime()
    prompt = _build_prompt(
        runtime,
        runtime.next_day or {},
        workout_summary=None,
        recovery=None,
        history=None,
    )
    assert "Week 3 of 8" in prompt
    assert "Foundation volume" in prompt
    assert "Chest and back" in prompt
    assert "validated prescription" in prompt.lower()
    assert "Elgan" not in prompt
    assert "powerbuilding" not in prompt.lower()
    assert "stage-prep" not in prompt.lower()
    assert "12-week" not in prompt.lower()


def test_build_prompt_includes_history_and_recovery() -> None:
    runtime = _runtime()
    prompt = _build_prompt(
        runtime,
        runtime.next_day or {},
        workout_summary=None,
        recovery={"sleep_hours": 7.5, "weight_kg": 82},
        history={"Incline press": {"top_weight_kg": 80, "top_reps": 8}},
    )
    assert "Incline press: 80 kg x 8" in prompt
    assert '"sleep_hours": 7.5' in prompt


def test_build_prompt_includes_last_plan() -> None:
    runtime = _runtime()
    prompt = _build_prompt(
        runtime,
        runtime.next_day or {},
        workout_summary=None,
        recovery=None,
        history=None,
        last_plan="Yesterday's plan was great.",
    )
    assert "Yesterday's plan was great." in prompt


def test_fallback_plan_has_focus_and_week() -> None:
    runtime = _runtime()
    plan = _fallback_plan(runtime, runtime.next_day or {})
    assert plan.startswith("Chest and back - Week 3")
    assert "Incline press: 3 × 8-12" in plan


# ---------------------------------------------------------------------------
# generate_next_workout
# ---------------------------------------------------------------------------

_fake_plan = (
    "Chest and back - Week 3\n"
    "Incline press: 3 × 8-12\n"
    "Chest-supported row: 3 × 10-12"
)


def _mock_provider(text: str, *, raise_error: bool = False) -> MagicMock:
    mock = MagicMock()
    if raise_error:
        mock.generate.side_effect = RuntimeError("API error")
    else:
        mock.generate.return_value = text
    return mock


def test_generate_next_workout_preserves_validated_prescription() -> None:
    runtime = _runtime()
    provider = _mock_provider(_fake_plan)
    result = generate_next_workout(
        provider,
        runtime=runtime,
        day=runtime.next_day or {},
    )
    assert result == _fake_plan
    provider.generate.assert_called_once()


def test_generate_next_workout_rejects_rewritten_prescription() -> None:
    runtime = _runtime()
    provider = _mock_provider("Chest and back - Week 3\nIncline press: 5 x 20")
    result = generate_next_workout(
        provider,
        runtime=runtime,
        day=runtime.next_day or {},
    )
    assert "Incline press: 3 x 8-12" in result.replace("×", "x")
    assert "5 x 20" not in result


def test_generate_next_workout_empty_response_falls_back() -> None:
    runtime = _runtime()
    result = generate_next_workout(
        _mock_provider(""),
        runtime=runtime,
        day=runtime.next_day or {},
    )
    assert "Chest and back - Week 3" in result


def test_generate_next_workout_exception_falls_back() -> None:
    runtime = _runtime()
    result = generate_next_workout(
        _mock_provider("", raise_error=True),
        runtime=runtime,
        day=runtime.next_day or {},
    )
    assert "Chest and back - Week 3" in result


def test_generate_next_workout_enforces_rules_deterministically() -> None:
    from training_rules import (
        ExerciseSubstitutionRule,
        ProhibitedExerciseRule,
        RepIntensityBoundsRule,
        StructuredTrainingRules,
    )

    runtime = _runtime()
    ai_output = (
        "Chest and back - Week 3\n"
        "Barbell Back Squat: 3 × 4\n"
        "Lat Pulldown: 3 × 10"
    )
    provider = _mock_provider(ai_output)
    rules = StructuredTrainingRules(
        prohibited_exercises=[ProhibitedExerciseRule(exercise="Conventional Deadlift")],
        substitutions=[
            ExerciseSubstitutionRule(
                from_exercise="Barbell Back Squat", to_exercise="Leg Press"
            )
        ],
        rep_intensity_bounds=RepIntensityBoundsRule(min_reps=6, max_reps=15),
    )
    result = generate_next_workout(
        provider,
        runtime=runtime,
        day=runtime.next_day or {},
        rules=rules,
    )
    # A rewritten prescription is rejected and the deterministic plan wins.
    assert "Incline press: 3 x 8-12" in result.replace("×", "x")


# ---------------------------------------------------------------------------
# Rest and check-in messages
# ---------------------------------------------------------------------------


def test_build_rest_prompt_includes_recovery_and_no_named_lifts() -> None:
    prompt = _build_rest_prompt(
        _runtime(), recovery={"sleep_hours": 7.5}, rules=None
    )
    assert "no prescribed training" in prompt
    assert '"sleep_hours": 7.5' in prompt
    assert "deadlift" not in prompt.lower()


def test_generate_rest_day_message_success() -> None:
    provider = _mock_provider("Recover well and sleep early.")
    assert generate_rest_day_message(
        provider, runtime=_runtime(), recovery=None
    ) == "Recover well and sleep early."


def test_generate_rest_day_message_empty_falls_back() -> None:
    result = generate_rest_day_message(
        _mock_provider(""), runtime=_runtime(), recovery=None
    )
    assert "rest" in result.lower()


def test_generate_rest_day_message_exception_falls_back() -> None:
    result = generate_rest_day_message(
        _mock_provider("", raise_error=True), runtime=_runtime(), recovery=None
    )
    assert "rest" in result.lower()


def test_fallback_rest_message_is_non_empty() -> None:
    assert "rest" in _fallback_rest_message().lower()


def test_build_checkin_prompt_includes_block_info() -> None:
    prompt = _build_checkin_prompt(
        _runtime(),
        number=1,
        workouts_done=12,
        weeks=4,
        analysis_text="Incline press: on plan",
    )
    assert "Check-in 1" in prompt
    assert "Foundation volume" in prompt
    assert "Incline press: on plan" in prompt
    assert "Elgan" not in prompt


def test_generate_checkin_message_success() -> None:
    provider = _mock_provider("Check-in 1\nAll progressing.")
    result = generate_checkin_message(
        provider,
        runtime=_runtime(),
        number=1,
        workouts_done=12,
        weeks=4,
        analysis_text="ok",
        fallback="fallback",
    )
    assert result == "Check-in 1\nAll progressing."


def test_generate_checkin_message_empty_falls_back() -> None:
    result = generate_checkin_message(
        _mock_provider(""),
        runtime=_runtime(),
        number=1,
        workouts_done=12,
        weeks=4,
        analysis_text="ok",
        fallback="fallback",
    )
    assert result == "fallback"


def test_generate_checkin_message_exception_falls_back() -> None:
    result = generate_checkin_message(
        _mock_provider("", raise_error=True),
        runtime=_runtime(),
        number=1,
        workouts_done=12,
        weeks=4,
        analysis_text="ok",
        fallback="fallback",
    )
    assert result == "fallback"
