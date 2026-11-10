"""Deterministic tests for structured training rules and constraint enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from training_rules import (
    ContextualNote,
    EquipmentRule,
    ExerciseSubstitutionRule,
    FrequencyRule,
    ProhibitedExerciseRule,
    RecoveryRule,
    RepIntensityBoundsRule,
    StructuredTrainingRules,
    VolumeRule,
    enforce_rules_on_plan_text,
    format_rules_for_prompt,
)


def test_rule_schema_validation_success():
    """Verify all 8 rule categories validate cleanly in StructuredTrainingRules."""
    rules = StructuredTrainingRules(
        version=1,
        prohibited_exercises=[
            ProhibitedExerciseRule(
                exercise="Behind The Neck Press", reason="Shoulder impingement"
            )
        ],
        substitutions=[
            ExerciseSubstitutionRule(
                from_exercise="Barbell Back Squat",
                to_exercise="Leg Press",
                reason="Patellar tendinopathy",
            )
        ],
        rep_intensity_bounds=RepIntensityBoundsRule(
            min_reps=5, max_reps=15, max_rpe=9.0, min_rir=1.0, max_intensity_pct=85.0
        ),
        frequency=FrequencyRule(
            max_sessions_per_week=4,
            min_rest_days_between_sessions=1,
            max_frequency_per_muscle=2,
        ),
        volume=VolumeRule(
            max_sets_per_workout=18,
            max_sets_per_exercise=4,
            max_weekly_sets_per_muscle=16,
        ),
        equipment=EquipmentRule(
            available_equipment=["barbell", "dumbbells", "cables"],
            prohibited_equipment=["smith_machine"],
        ),
        recovery=RecoveryRule(
            min_sleep_hours=7.0,
            min_readiness_score=60.0,
            max_consecutive_training_days=3,
            auto_deload_on_fatigue=True,
        ),
        contextual_notes=[
            ContextualNote(
                category="joint",
                note="Right knee clicks on deep squats",
                severity="warning",
            ),
            ContextualNote(
                category="medical",
                note="L5/S1 disc sensitivity under axial load",
                severity="critical",
            ),
        ],
    )
    assert rules.version == 1
    assert len(rules.prohibited_exercises) == 1
    assert rules.rep_intensity_bounds.min_reps == 5
    assert rules.volume.max_sets_per_workout == 18
    assert len(rules.contextual_notes) == 2


def test_substitution_cannot_be_same_exercise():
    """Substitutions must have different from_exercise and to_exercise."""
    with pytest.raises(ValidationError, match="must be different"):
        ExerciseSubstitutionRule(
            from_exercise="Barbell Back Squat", to_exercise="barbell back squat"
        )


def test_rep_bounds_validation():
    """min_reps cannot exceed max_reps."""
    with pytest.raises(ValidationError, match="min_reps .* cannot exceed max_reps"):
        RepIntensityBoundsRule(min_reps=12, max_reps=8)


def test_equipment_disjoint_validation():
    """Equipment cannot be both available and prohibited."""
    with pytest.raises(ValidationError, match="Equipment cannot be both available"):
        EquipmentRule(
            available_equipment=["Barbell", "Dumbbells"],
            prohibited_equipment=["barbell", "Kettlebell"],
        )


def test_substitution_target_cannot_be_prohibited():
    """Substitution cannot replace an exercise with another prohibited exercise."""
    with pytest.raises(ValidationError, match="cannot be a prohibited exercise"):
        StructuredTrainingRules(
            prohibited_exercises=[
                ProhibitedExerciseRule(exercise="Leg Press")
            ],
            substitutions=[
                ExerciseSubstitutionRule(
                    from_exercise="Barbell Back Squat",
                    to_exercise="Leg Press",
                )
            ],
        )


def test_migrate_free_text_constraints():
    """Legacy free-text strings are converted into contextual notes."""
    legacy = ["Bad knees - no jumping", "Shoulder rehab - keep RPE low"]
    rules = StructuredTrainingRules.from_raw(legacy)
    assert rules.version == 1
    assert len(rules.contextual_notes) == 2
    assert rules.contextual_notes[0].category == "joint"
    assert rules.contextual_notes[0].note == "Bad knees - no jumping"


def test_format_rules_for_prompt():
    """Prompt formatting produces structured directives for AI coaching."""
    rules = StructuredTrainingRules(
        version=2,
        prohibited_exercises=[
            ProhibitedExerciseRule(exercise="Good Morning", reason="Lower back strain")
        ],
        substitutions=[
            ExerciseSubstitutionRule(
                from_exercise="Barbell Bench Press",
                to_exercise="Dumbbell Bench Press",
                reason="Acromioclavicular pinch",
            )
        ],
        rep_intensity_bounds=RepIntensityBoundsRule(min_reps=6, max_reps=12, max_rpe=8.5),
        volume=VolumeRule(max_sets_per_workout=15, max_sets_per_exercise=3),
    )
    prompt_str = format_rules_for_prompt(rules)
    assert "MANDATORY TRAINING CONSTRAINTS (Deterministic Rule Schema v2):" in prompt_str
    assert "PROHIBITED EXERCISES" in prompt_str
    assert "Good Morning" in prompt_str
    assert "MANDATORY EXERCISE SUBSTITUTIONS" in prompt_str
    assert "Barbell Bench Press" in prompt_str
    assert "Dumbbell Bench Press" in prompt_str
    assert "REP & INTENSITY BOUNDS" in prompt_str
    assert "Reps strictly between 6 and 12" in prompt_str
    assert "Max RPE: 8.5" in prompt_str
    assert "VOLUME LIMITS" in prompt_str
    assert "Max 15 total sets/workout" in prompt_str


def test_deterministic_enforcement_substitutions_and_prohibited():
    """Deterministic code replaces substitutions and eliminates prohibited exercises."""
    rules = StructuredTrainingRules(
        prohibited_exercises=[
            ProhibitedExerciseRule(exercise="Conventional Deadlift", reason="Back safety")
        ],
        substitutions=[
            ExerciseSubstitutionRule(
                from_exercise="Barbell Back Squat",
                to_exercise="Hack Squat",
            )
        ],
    )
    raw_ai_plan = (
        "Legs & Abs - Week 1 (Foundation)\n"
        "Barbell Back Squat: 3 x 8\n"
        "Conventional Deadlift: 3 x 5\n"
        "Leg Extension: 3 x 12"
    )

    enforced, violations = enforce_rules_on_plan_text(raw_ai_plan, rules)

    assert "Hack Squat: 3 x 8" in enforced
    assert "Barbell Back Squat" not in enforced
    assert "Conventional Deadlift" not in enforced
    assert "Leg Extension: 3 x 12" in enforced

    types = [v.rule_type for v in violations]
    assert "substitution" in types
    assert "prohibited_exercise" in types


def test_deterministic_enforcement_rep_bounds_and_volume_caps():
    """Deterministic code clamps out-of-range reps and enforces volume limits."""
    rules = StructuredTrainingRules(
        rep_intensity_bounds=RepIntensityBoundsRule(min_reps=6, max_reps=12),
        volume=VolumeRule(max_sets_per_exercise=3, max_sets_per_workout=6),
    )
    raw_ai_plan = (
        "Push Day - Week 1 (Foundation)\n"
        "Bench Press: 5 x 3\n"
        "Incline Dumbbell Press: 3 x 15\n"
        "Tricep Pushdown: 3 x 10"
    )

    enforced, violations = enforce_rules_on_plan_text(raw_ai_plan, rules)

    # Bench Press sets clamped from 5 to 3 (max_sets_per_exercise), reps from 3 to 6 (min_reps)
    assert "Bench Press: 3 x 6" in enforced
    # Incline Dumbbell Press: 3 sets (total 6 sets reached), reps clamped from 15 to 12 (max_reps)
    assert "Incline Dumbbell Press: 3 x 12" in enforced
    # Tricep Pushdown omitted because 6 total sets limit is reached
    assert "Tricep Pushdown" not in enforced

    types = [v.rule_type for v in violations]
    assert "rep_bounds_min" in types
    assert "rep_bounds_max" in types
    assert "volume_sets_per_exercise" in types
    assert "volume_max_workout_sets" in types


def test_deterministic_recovery_fatigue_auto_deload():
    """Deterministic code automatically applies volume deload on poor sleep/readiness."""
    rules = StructuredTrainingRules(
        recovery=RecoveryRule(
            min_sleep_hours=7.0,
            min_readiness_score=60.0,
            auto_deload_on_fatigue=True,
        )
    )
    raw_ai_plan = (
        "Pull Day - Week 1\n"
        "Weighted Pull-Up: 4 x 6\n"
        "Barbell Row: 3 x 8"
    )
    poor_recovery = {"sleep_hours": 5.2, "readiness_score": 45.0}

    enforced, violations = enforce_rules_on_plan_text(
        raw_ai_plan, rules, recovery=poor_recovery
    )

    # Sets trimmed by 1 due to recovery deficit: 4 -> 3, 3 -> 2
    assert "Weighted Pull-Up: 3 x 6" in enforced
    assert "Barbell Row: 2 x 8" in enforced
    types = [v.rule_type for v in violations]
    assert "recovery_fatigue" in types
    assert "readiness_deficit" in types
