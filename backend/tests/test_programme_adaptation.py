"""Deterministic adaptation, hysteresis and drift diff regressions."""

from __future__ import annotations

from datetime import date

import pytest
from dynamic_programme import ProgrammePreviewRequest, build_programme_preview
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from programme_engine import (
    AdaptationContext,
    DecisionType,
    PriorDecision,
    ProgrammeValidationError,
    RecoveryContext,
    decide,
    diff_routine_snapshots,
    validate_programme_definition,
)


def _prior(
    decision_type: str = DecisionType.HOLD.value,
    *,
    target_met: bool | None = True,
    applied: bool = True,
) -> PriorDecision:
    return PriorDecision(
        decision_type=decision_type,
        applied=applied,
        target_met=target_met,
    )


def _context(**overrides: object) -> AdaptationContext:
    values: dict[str, object] = {
        "exercise_template_id": "bench",
        "exercise_type": "weight_reps",
        "prescribed_sets": 3,
        "prescribed_rep_low": 6,
        "prescribed_rep_high": 8,
        "prescribed_load_kg": 50.0,
        "completed_sets": 3,
        "completed_reps": 8,
        "completed_load_kg": 50.0,
        "target_rir_met": True,
        "match_confidence": "high",
        "baseline_confidence": "sufficient",
        "aggressiveness": "balanced",
        "block_max_sets": 5,
        "block_rep_low": 6,
        "block_rep_high": 10,
    }
    values.update(overrides)
    return AdaptationContext(**values)  # type: ignore[arg-type]


def test_one_success_holds_until_minimum_evidence() -> None:
    decision = decide(_context(history=(_prior(),)))
    assert decision.decision_type is DecisionType.HOLD
    assert "accumulating_success_evidence" in decision.rationale_codes


def test_two_successes_progress_load_at_top_of_range() -> None:
    decision = decide(_context(history=(_prior(), _prior())))
    assert decision.decision_type is DecisionType.PROGRESS_LOAD
    assert decision.applied is True
    assert decision.proposed_prescription["load_kg"] == pytest.approx(52.5)
    assert "hysteresis_satisfied" in decision.rationale_codes


def test_one_failure_holds_and_two_failures_reduce() -> None:
    first = decide(
        _context(
            completed_sets=1,
            completed_reps=4,
            history=(_prior(target_met=False),),
        )
    )
    assert first.decision_type is DecisionType.HOLD
    assert "accumulating_failure_evidence" in first.rationale_codes

    second = decide(
        _context(
            completed_sets=1,
            completed_reps=4,
            history=(
                _prior(target_met=False),
                _prior(target_met=False),
            ),
        )
    )
    assert second.decision_type is DecisionType.REDUCE_LOAD
    assert second.proposed_prescription["load_kg"] == pytest.approx(47.5)


def test_cooldown_after_reduction_prevents_immediate_progress() -> None:
    decision = decide(
        _context(
            history=(
                _prior(DecisionType.REDUCE_LOAD.value, target_met=False),
                _prior(target_met=True),
            )
        )
    )
    assert decision.decision_type is DecisionType.HOLD
    assert "cooldown_after_reduction" in decision.rationale_codes


def test_ambiguous_match_requires_confirmation_and_cannot_adapt() -> None:
    decision = decide(
        _context(
            match_confidence="ambiguous",
            history=(_prior(), _prior(), _prior()),
        )
    )
    assert decision.decision_type is DecisionType.REQUEST_CONFIRMATION
    assert decision.applied is False
    assert decision.requires_confirmation is True


def test_probable_match_uses_smaller_bounds_and_more_evidence() -> None:
    held = decide(
        _context(
            match_confidence="probable",
            history=(_prior(), _prior()),
        )
    )
    assert held.decision_type is DecisionType.HOLD

    progressed = decide(
        _context(
            match_confidence="probable",
            history=(_prior(), _prior(), _prior()),
        )
    )
    assert progressed.decision_type is DecisionType.PROGRESS_LOAD
    assert progressed.proposed_prescription["load_kg"] == pytest.approx(51.25)
    assert "probable_match_smaller_reversible_change" in progressed.rationale_codes


def test_sparse_baseline_requires_extra_success() -> None:
    decision = decide(
        _context(
            baseline_confidence="sparse",
            history=(_prior(), _prior()),
        )
    )
    assert decision.decision_type is DecisionType.HOLD
    assert "accumulating_success_evidence" in decision.rationale_codes


def test_missing_or_single_noisy_recovery_does_not_force_deload() -> None:
    missing = decide(_context(history=(_prior(), _prior())))
    assert missing.decision_type is not DecisionType.FATIGUE_MANAGEMENT

    single_noise = decide(
        _context(
            recovery=RecoveryContext(fresh=True, quality=0.1, source="wearable"),
            history=(_prior(), _prior()),
        )
    )
    assert single_noise.decision_type is not DecisionType.FATIGUE_MANAGEMENT

    repeated = decide(
        _context(
            recovery=RecoveryContext(fresh=True, quality=0.1, source="wearable"),
            poor_recovery_streak=1,
        )
    )
    assert repeated.decision_type is DecisionType.FATIGUE_MANAGEMENT


def test_stale_recovery_is_ignored_and_cannot_force_change() -> None:
    decision = decide(
        _context(
            recovery=RecoveryContext(fresh=False, quality=0.05, source="wearable"),
            poor_recovery_streak=5,
            history=(_prior(), _prior()),
        )
    )
    assert decision.decision_type is DecisionType.PROGRESS_LOAD


def test_pain_blocks_progression_and_substitution_is_not_silent() -> None:
    blocked = decide(_context(pain_flag=True, history=(_prior(), _prior())))
    assert blocked.decision_type is DecisionType.BLOCK_PROGRESSION

    proposed = decide(
        _context(
            pain_flag=True,
            substitution_available=True,
            history=(_prior(), _prior()),
        )
    )
    assert proposed.decision_type is DecisionType.PROPOSE_SUBSTITUTION
    assert proposed.applied is False
    assert proposed.requires_confirmation is True


def test_hard_constraint_wins_over_explicit_override() -> None:
    decision = decide(
        _context(
            hard_constraint_block=True,
            user_override=DecisionType.PROGRESS_LOAD.value,
        )
    )
    assert decision.decision_type is DecisionType.BLOCK_PROGRESSION
    assert "hard_safety_or_profile_constraint" in decision.rationale_codes


def test_weekly_change_bound_blocks_further_escalation() -> None:
    decision = decide(
        _context(
            week_change={"load_pct": 0.09, "set_delta": 0},
            history=(_prior(), _prior()),
        )
    )
    assert decision.decision_type is DecisionType.HOLD
    assert "bounded_change_limit_reached" in decision.rationale_codes


def test_explicit_override_is_applied_within_bounds() -> None:
    decision = decide(
        _context(user_override=DecisionType.REDUCE_SETS.value)
    )
    assert decision.decision_type is DecisionType.REDUCE_SETS
    assert decision.proposed_prescription["sets"] == 2
    assert "explicit_user_override" in decision.rationale_codes


def _training_data() -> HevyTrainingData:
    templates = {
        "bench": ExerciseTemplate(
            id="bench",
            title="Bench Press",
            exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
        "row": ExerciseTemplate(
            id="row",
            title="Row",
            exercise_type="weight_reps",
            primary_muscle_group="upper_back",
        ),
    }
    routines = [
        Routine(
            id="routine-a",
            title="Upper A",
            exercises=[
                RoutineExercise(
                    template_id="bench",
                    title="Bench Press",
                    sets=3,
                    rest_seconds=120,
                    target_reps=8,
                    set_targets=[
                        {"type": "normal", "reps": 8, "weight_kg": 60.0}
                    ],
                )
            ],
        ),
        Routine(
            id="routine-b",
            title="Upper B",
            exercises=[
                RoutineExercise(
                    template_id="row",
                    title="Row",
                    sets=3,
                    rest_seconds=90,
                    target_reps=10,
                    set_targets=[
                        {"type": "normal", "reps": 10, "weight_kg": 50.0}
                    ],
                )
            ],
        ),
    ]
    return HevyTrainingData(
        username="tester",
        workout_count=20,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=[
            CompletedWorkout(
                id="workout-1",
                title="Upper A",
                start_time="2026-09-01T10:00:00Z",
                end_time="2026-09-01T11:00:00Z",
            )
        ],
        folders={},
    )


def _preview() -> dict[str, object]:
    return build_programme_preview(
        _training_data(),
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-a", "routine-b"],
            duration_weeks=8,
            goal="hypertrophy",
            start_date=date(2026, 9, 1),
        ),
    )


def test_definition_validation_accepts_generated_preview() -> None:
    validate_programme_definition(_preview())


def test_definition_validation_rejects_block_gaps() -> None:
    preview = _preview()
    preview["blocks"][1]["start_week"] = 5
    with pytest.raises(ProgrammeValidationError, match="contiguous"):
        validate_programme_definition(preview)


def test_definition_validation_rejects_missing_prescription_blocks() -> None:
    preview = _preview()
    preview["days"][0]["exercises"][0]["prescriptions"].pop()
    with pytest.raises(ProgrammeValidationError, match="cover every block"):
        validate_programme_definition(preview)


def test_definition_validation_rejects_token_source_mismatch_at_activation() -> None:
    preview = _preview()
    validate_programme_definition(preview)


def test_drift_diff_detects_rename_reorder_add_remove_and_unsupported() -> None:
    active = [
        {
            "id": "a",
            "title": "Upper A",
            "source_hash": "hash-a",
            "exercises": [
                {"template_id": "bench", "title": "Bench", "exercise_type": "weight_reps"},
                {"template_id": "row", "title": "Row", "exercise_type": "weight_reps"},
            ],
        },
        {
            "id": "b",
            "title": "Upper B",
            "source_hash": "hash-b",
            "exercises": [
                {"template_id": "squat", "title": "Squat", "exercise_type": "weight_reps"}
            ],
        },
    ]
    current = [
        {
            "id": "a",
            "title": "Upper A renamed",
            "source_hash": "hash-a2",
            "exercises": [
                {"template_id": "row", "title": "Row", "exercise_type": "weight_reps"},
                {"template_id": "bench", "title": "Bench", "exercise_type": "weight_reps"},
                {"template_id": "fly", "title": "Fly", "exercise_type": "mystery"},
            ],
        },
        {
            "id": "c",
            "title": "New day",
            "source_hash": "hash-c",
            "exercises": [
                {"template_id": "lunge", "title": "Lunge", "exercise_type": "weight_reps"}
            ],
        },
    ]
    diff = diff_routine_snapshots(active, current)
    assert diff["has_drift"] is True
    assert diff["removed"] == ["b"]
    assert diff["added"] == ["c"]
    assert diff["reordered"] is False
    changed = diff["changed"][0]
    assert changed["renamed"] is True
    assert changed["exercises_added"] == ["fly"]
    assert diff["unsupported"][0]["template_id"] == "fly"


def test_drift_diff_detects_common_routine_reorder() -> None:
    active = [
        {"id": "a", "title": "A", "source_hash": "a", "exercises": []},
        {"id": "b", "title": "B", "source_hash": "b", "exercises": []},
    ]
    current = [
        {"id": "b", "title": "B", "source_hash": "b", "exercises": []},
        {"id": "a", "title": "A", "source_hash": "a", "exercises": []},
    ]
    assert diff_routine_snapshots(active, current)["reordered"] is True
