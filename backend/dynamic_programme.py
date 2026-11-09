"""Deterministic programmes generated from user-selected Hevy routines.

The imported Hevy routines remain immutable source material.  This module builds
an explainable prescription overlay for a requested goal and duration without
depending on the legacy static programme.

The builder is split into four deterministic stages:

1. **Source** - the authenticated user's Hevy routines are serialised with
   provider identity, exact set payloads and a content hash.
2. **Cadence** - the selected routines are ordered, marked ``required``,
   ``optional`` or ``rotating`` and mapped onto either a rolling rotation or an
   explicit weekly calendar.
3. **Plan** - goal specific blocks, weekly muscle-set estimates, inferred
   baselines and a prescription overlay for every exercise are generated.
4. **Validation** - a shared validator reports blocking violations that stop
   activation and non-blocking warnings the user should read.

Everything in this module is a pure function of the Hevy source data and the
validated request, so the same input always yields the same preview token.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, timedelta
from typing import Any, Literal

from hevy_reader import CompletedWorkout, HevyTrainingData, Routine, RoutineExercise
from programme_inference import infer_programme
from pydantic import BaseModel, Field, model_validator

ENGINE_VERSION = "hevy-programme-v3"
ROLE_CLASSIFIER_VERSION = "exercise-role-v2"
WORKLOAD_MODEL_VERSION = "workload-estimate-v2"
MIN_DURATION_WEEKS = 4
MAX_DURATION_WEEKS = 52

GoalKey = Literal[
    "general_fitness",
    "hypertrophy",
    "maximal_strength",
    "strength_test",
    "recomposition",
    "maintenance",
    "return_to_training",
    "power_performance",
]
Aggressiveness = Literal["conservative", "balanced", "aggressive"]
RoutineRole = Literal["required", "optional", "rotating"]
CadenceMode = Literal["rolling", "weekly"]
FatigueManagement = Literal["planned", "adaptive"]
StrengthModel = Literal[
    "auto",
    "top_set_back_off",
    "autoregulated_rep_range",
    "conservative_double_progression",
]
RoleKey = Literal[
    "primary_strength",
    "secondary_compound",
    "hypertrophy_accessory",
    "power_skill",
    "bodyweight_progression",
    "duration_distance",
    "rehab_prehab",
    "conditioning",
]
RoleConfidence = Literal["high", "medium", "low", "user_override"]

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Fractional set model (docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md, section 8.4).
DIRECT_SET_WEIGHT = 1.0
SECONDARY_SET_WEIGHT = 0.5

# Reference band for weekly per-muscle hard sets. These are review triggers,
# never prescriptions: a user may deliberately train outside them.
MIN_RECOVERABLE_SETS = 6.0
MAX_RECOVERABLE_SETS = 24.0

# Planning bounds for week one. They are deliberately conservative product
# guardrails, not physiological constants, and are exposed in the preview.
MAX_WEEK_ONE_SET_INCREASE = 1
MIN_WEEK_ONE_SET_DECREASE = 1
MAX_SETS_PER_EXERCISE = 12
MAX_SOURCE_WORK_SETS_PER_ROUTINE = 40
ALLOWED_SET_CHANGE = {"minimum": -1, "maximum": 2}
LOAD_CHANGE_PERCENT = {
    "conservative": 3.0,
    "balanced": 5.0,
    "aggressive": 7.0,
}
EXPECTED_WORKING_SET_TYPES = {"normal", "failure", "drop", "dropset"}

# Equipment increments are planning defaults. A missing or custom template
# stays uncertain instead of claiming a precise increment.
_EQUIPMENT_INCREMENTS_KG = {
    "barbell": 2.5,
    "dumbbell": 2.5,
    "machine": 2.5,
    "cable": 2.5,
    "smith machine": 2.5,
    "kettlebell": 4.0,
    "plate": 2.5,
    "bodyweight": 0.0,
}

# Set payload keys the engine understands. Anything else is reported to the
# user as an unsupported field instead of being silently ignored.
SUPPORTED_SET_TARGET_KEYS = frozenset(
    {
        "type",
        "reps",
        "weight_kg",
        "distance_meters",
        "duration_seconds",
        "rep_range",
        "start_rep_range",
        "end_rep_range",
    }
)


class RoutineSelection(BaseModel):
    """One selected Hevy routine together with its rotation role."""

    routine_id: str = Field(min_length=1)
    role: RoutineRole = "required"
    preferred_weekday: int | None = Field(default=None, ge=0, le=6)


class ProgrammeCadence(BaseModel):
    """How the selected routines are scheduled.

    ``rolling`` keeps the user's routine order and advances one routine per
    training day.  ``weekly`` places routines on explicit weekdays so the
    programme never forces a Monday-to-Saturday pattern.
    """

    mode: CadenceMode = "rolling"
    weekdays: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_weekdays(self) -> ProgrammeCadence:
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise ValueError("Weekdays must be between 0 (Monday) and 6 (Sunday).")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Each weekday can only be selected once.")
        if self.mode == "weekly" and not self.weekdays:
            raise ValueError("A weekly cadence needs at least one selected weekday.")
        return self


class ProgrammeConstraints(BaseModel):
    """Structured constraints the user confirmed before activation."""

    concurrent_training: list[str] = Field(default_factory=list)
    injuries: list[str] = Field(default_factory=list)
    notes: str | None = None
    max_sessions_per_week: int | None = Field(default=None, ge=1, le=14)


class ExerciseOverride(BaseModel):
    """A user decision about one exercise in the generated overlay.

    Overrides never mutate the imported Hevy routine snapshot. They only change
    the planning overlay and remain visible in the preview evidence.
    """

    template_id: str = Field(min_length=1)
    role: RoleKey | None = None
    opening_sets: int | None = Field(default=None, ge=1, le=20)
    weekly_set_cap: float | None = Field(default=None, ge=0, le=60)
    note: str | None = None


class ProgrammePreviewRequest(BaseModel):
    """Validated inputs for a generated Hevy-native programme preview."""

    selected_routine_ids: list[str] = Field(min_length=1)
    routine_selections: list[RoutineSelection] = Field(default_factory=list)
    duration_weeks: int = Field(
        default=12, ge=MIN_DURATION_WEEKS, le=MAX_DURATION_WEEKS
    )
    goal: GoalKey = "general_fitness"
    start_date: date = Field(default_factory=date.today)
    sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    cadence: ProgrammeCadence = Field(default_factory=ProgrammeCadence)
    experience: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    max_session_minutes: int | None = Field(default=None, ge=20, le=240)
    adaptation_aggressiveness: Aggressiveness = "balanced"
    priority_muscles: list[str] = Field(default_factory=list)
    priority_exercises: list[str] = Field(default_factory=list)
    event_date: date | None = None
    fatigue_management: FatigueManagement = "adaptive"
    strength_model: StrengthModel = "auto"
    exercise_overrides: list[ExerciseOverride] = Field(default_factory=list)
    constraints: ProgrammeConstraints = Field(default_factory=ProgrammeConstraints)

    @model_validator(mode="after")
    def _validate_selection(self) -> ProgrammePreviewRequest:
        selection_ids = [item.routine_id for item in self.routine_selections]
        if len(set(selection_ids)) != len(selection_ids):
            raise ValueError("Each routine may appear only once in the selection.")
        unknown = [
            routine_id
            for routine_id in selection_ids
            if routine_id not in self.selected_routine_ids
        ]
        if unknown:
            raise ValueError(
                "Routine selections must reference selected routines: "
                + ", ".join(unknown)
            )
        override_ids = [item.template_id for item in self.exercise_overrides]
        if len(set(override_ids)) != len(override_ids):
            raise ValueError("Each exercise may be overridden only once.")
        return self


class SourceHash(BaseModel):
    """The provider content hash of one routine at preview time."""

    routine_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)


class ProgrammeActivationRequest(ProgrammePreviewRequest):
    """Preview inputs plus the source-bound token being activated."""

    preview_token: str = Field(min_length=16)
    source_hashes: list[SourceHash] = Field(default_factory=list)


_GOALS: dict[str, dict[str, Any]] = {
    "general_fitness": {
        "label": "General strength and fitness",
        "description": "Build useful strength, work capacity and sustainable consistency.",
        "phases": [
            ("foundation", "Foundation", 0.34),
            ("development", "Progressive development", 0.42),
            ("consolidation", "Consolidation and review", 0.24),
        ],
    },
    "hypertrophy": {
        "label": "Hypertrophy",
        "description": "Prioritise recoverable weekly volume and progressive overload without a compulsory peak.",
        "phases": [
            ("foundation_volume", "Foundation volume", 0.30),
            ("overload", "Overload and specialisation", 0.50),
            ("resensitisation", "Resensitisation and assessment", 0.20),
        ],
    },
    "maximal_strength": {
        "label": "Maximal strength",
        "description": "Move from a broad strength base toward heavier, more specific work.",
        "phases": [
            ("strength_base", "Strength base", 0.34),
            ("intensification", "Intensification", 0.46),
            ("expression", "Strength expression", 0.20),
        ],
    },
    "strength_test": {
        "label": "Strength with test or competition",
        "description": "Build, intensify, realise and taper toward a dated strength test.",
        "phases": [
            ("strength_base", "Strength base", 0.28),
            ("intensification", "Intensification", 0.40),
            ("realisation", "Realisation", 0.20),
            ("taper_test", "Taper and test", 0.12),
        ],
    },
    "recomposition": {
        "label": "Recomposition or energy deficit",
        "description": "Retain useful intensity while keeping volume recoverable during an energy deficit.",
        "phases": [
            ("sustainable_base", "Sustainable base", 0.42),
            ("productive_tension", "Productive tension", 0.38),
            ("consolidation", "Consolidation", 0.20),
        ],
    },
    "maintenance": {
        "label": "Maintenance",
        "description": "Maintain skill, strength and muscle with the minimum effective dose.",
        "phases": [
            ("maintenance", "Maintenance", 0.72),
            ("assessment", "Assessment and refresh", 0.28),
        ],
    },
    "return_to_training": {
        "label": "Return to training",
        "description": "Re-establish tolerance conservatively before normal progression.",
        "phases": [
            ("calibration", "Calibration and familiarisation", 0.26),
            ("rebuild", "Rebuild", 0.44),
            ("normal_progression", "Normal progression", 0.30),
        ],
    },
    "power_performance": {
        "label": "Power and performance",
        "description": "Preserve strength while increasing movement quality, speed and specificity.",
        "phases": [
            ("power_base", "Power foundation", 0.30),
            ("power_development", "Power development", 0.46),
            ("performance", "Performance realisation", 0.24),
        ],
    },
}

# Configurable target envelopes. They are review triggers and planning intent,
# not a static workout template.
_GOAL_ENVELOPES: dict[str, dict[str, Any]] = {
    "general_fitness": {
        "weekly_sets_per_muscle": {"min": 5.0, "max": 18.0},
        "rep_range": {"primary": [3, 8], "accessory": [8, 15]},
        "effort_rir": {"min": 1, "max": 4},
        "intensity_distribution": {"heavy": 0.25, "moderate": 0.55, "light": 0.20},
        "specificity": "moderate",
        "exercise_variation": "low",
    },
    "hypertrophy": {
        "weekly_sets_per_muscle": {"min": 8.0, "max": 24.0},
        "rep_range": {"primary": [5, 10], "accessory": [8, 20]},
        "effort_rir": {"min": 0, "max": 3},
        "intensity_distribution": {"heavy": 0.10, "moderate": 0.60, "light": 0.30},
        "specificity": "low to moderate",
        "exercise_variation": "moderate",
    },
    "maximal_strength": {
        "weekly_sets_per_muscle": {"min": 4.0, "max": 16.0},
        "rep_range": {"primary": [1, 6], "accessory": [6, 12]},
        "effort_rir": {"min": 1, "max": 4},
        "intensity_distribution": {"heavy": 0.55, "moderate": 0.35, "light": 0.10},
        "specificity": "high",
        "exercise_variation": "low",
    },
    "strength_test": {
        "weekly_sets_per_muscle": {"min": 4.0, "max": 16.0},
        "rep_range": {"primary": [1, 6], "accessory": [6, 12]},
        "effort_rir": {"min": 1, "max": 5},
        "intensity_distribution": {"heavy": 0.50, "moderate": 0.35, "light": 0.15},
        "specificity": "very high in the terminal block",
        "exercise_variation": "low",
    },
    "recomposition": {
        "weekly_sets_per_muscle": {"min": 6.0, "max": 18.0},
        "rep_range": {"primary": [3, 8], "accessory": [8, 15]},
        "effort_rir": {"min": 1, "max": 4},
        "intensity_distribution": {"heavy": 0.25, "moderate": 0.55, "light": 0.20},
        "specificity": "moderate",
        "exercise_variation": "low",
    },
    "maintenance": {
        "weekly_sets_per_muscle": {"min": 3.0, "max": 12.0},
        "rep_range": {"primary": [3, 8], "accessory": [8, 15]},
        "effort_rir": {"min": 2, "max": 5},
        "intensity_distribution": {"heavy": 0.20, "moderate": 0.50, "light": 0.30},
        "specificity": "moderate",
        "exercise_variation": "low",
    },
    "return_to_training": {
        "weekly_sets_per_muscle": {"min": 3.0, "max": 12.0},
        "rep_range": {"primary": [5, 10], "accessory": [8, 15]},
        "effort_rir": {"min": 3, "max": 5},
        "intensity_distribution": {"heavy": 0.10, "moderate": 0.50, "light": 0.40},
        "specificity": "low",
        "exercise_variation": "low",
    },
    "power_performance": {
        "weekly_sets_per_muscle": {"min": 3.0, "max": 14.0},
        "rep_range": {"primary": [1, 5], "accessory": [6, 12]},
        "effort_rir": {"min": 2, "max": 5},
        "intensity_distribution": {"heavy": 0.20, "moderate": 0.40, "light": 0.40},
        "specificity": "high",
        "exercise_variation": "moderate",
    },
}

_PHASE_SETTINGS: dict[str, dict[str, Any]] = {
    "foundation": {
        "volume_multiplier": 0.95,
        "target_rir": "2-4",
        "rep_emphasis": "moderate reps",
        "specificity": "source-compatible",
        "focus": "Establish repeatable technique and a recoverable baseline.",
    },
    "development": {
        "volume_multiplier": 1.05,
        "target_rir": "1-3",
        "rep_emphasis": "moderate reps with progressive overload",
        "specificity": "moderate",
        "focus": "Progress load or reps while preserving session quality.",
    },
    "consolidation": {
        "volume_multiplier": 0.85,
        "target_rir": "3-4",
        "rep_emphasis": "comfortable quality work",
        "specificity": "source-compatible",
        "focus": "Consolidate gains, reduce accumulated fatigue and review the next cycle.",
    },
    "foundation_volume": {
        "volume_multiplier": 1.00,
        "target_rir": "2-4",
        "rep_emphasis": "6-15 reps",
        "specificity": "low to moderate",
        "focus": "Build a stable volume baseline across the selected routines.",
    },
    "overload": {
        "volume_multiplier": 1.12,
        "target_rir": "1-3",
        "rep_emphasis": "6-15 reps",
        "specificity": "moderate",
        "focus": "Add bounded volume or load to priority movements and muscles.",
    },
    "resensitisation": {
        "volume_multiplier": 0.78,
        "target_rir": "3-5",
        "rep_emphasis": "8-15 reps",
        "specificity": "low",
        "focus": "Reduce fatigue and assess which exercises should continue.",
    },
    "strength_base": {
        "volume_multiplier": 1.00,
        "target_rir": "2-4",
        "rep_emphasis": "4-8 reps",
        "specificity": "moderate",
        "focus": "Build technical strength and repeatable submaximal work.",
    },
    "intensification": {
        "volume_multiplier": 0.92,
        "target_rir": "1-3",
        "rep_emphasis": "2-6 reps",
        "specificity": "high",
        "focus": "Increase load and specificity while controlling total fatigue.",
    },
    "expression": {
        "volume_multiplier": 0.78,
        "target_rir": "1-3",
        "rep_emphasis": "1-5 reps",
        "specificity": "high",
        "focus": "Express strength with lower volume and high-quality heavy work.",
    },
    "realisation": {
        "volume_multiplier": 0.72,
        "target_rir": "1-3",
        "rep_emphasis": "1-4 reps",
        "specificity": "very high",
        "focus": "Practise highly specific work without adding unnecessary fatigue.",
    },
    "taper_test": {
        "volume_multiplier": 0.48,
        "target_rir": "3-5 until test",
        "rep_emphasis": "singles to triples",
        "specificity": "test-specific",
        "focus": "Dissipate fatigue and arrive ready for the planned test.",
    },
    "sustainable_base": {
        "volume_multiplier": 0.88,
        "target_rir": "2-4",
        "rep_emphasis": "5-12 reps",
        "specificity": "moderate",
        "focus": "Use a sustainable dose that fits current recovery and nutrition.",
    },
    "productive_tension": {
        "volume_multiplier": 0.95,
        "target_rir": "1-3",
        "rep_emphasis": "4-12 reps",
        "specificity": "moderate to high",
        "focus": "Retain productive loading without chasing excess fatigue.",
    },
    "maintenance": {
        "volume_multiplier": 0.72,
        "target_rir": "2-4",
        "rep_emphasis": "4-12 reps",
        "specificity": "moderate",
        "focus": "Maintain strength and muscle with an efficient training dose.",
    },
    "assessment": {
        "volume_multiplier": 0.62,
        "target_rir": "3-5",
        "rep_emphasis": "comfortable ranges",
        "specificity": "source-compatible",
        "focus": "Refresh fatigue and assess readiness for the next goal.",
    },
    "calibration": {
        "volume_multiplier": 0.62,
        "target_rir": "4-5",
        "rep_emphasis": "comfortable technique work",
        "specificity": "low",
        "focus": "Relearn movements and establish current tolerance without testing limits.",
    },
    "rebuild": {
        "volume_multiplier": 0.82,
        "target_rir": "3-4",
        "rep_emphasis": "5-12 reps",
        "specificity": "moderate",
        "focus": "Rebuild consistency, work capacity and confidence.",
    },
    "normal_progression": {
        "volume_multiplier": 0.98,
        "target_rir": "2-3",
        "rep_emphasis": "goal-compatible ranges",
        "specificity": "moderate",
        "focus": "Resume normal bounded progression from the rebuilt baseline.",
    },
    "power_base": {
        "volume_multiplier": 0.88,
        "target_rir": "3-5",
        "rep_emphasis": "low-fatigue quality reps",
        "specificity": "moderate",
        "focus": "Establish movement quality and strength reserve.",
    },
    "power_development": {
        "volume_multiplier": 0.82,
        "target_rir": "3-5",
        "rep_emphasis": "fast, crisp repetitions",
        "specificity": "high",
        "focus": "Prioritise speed, intent and repeatable high-quality outputs.",
    },
    "performance": {
        "volume_multiplier": 0.66,
        "target_rir": "3-5",
        "rep_emphasis": "very low fatigue",
        "specificity": "very high",
        "focus": "Express performance while minimising residual fatigue.",
    },
}


def goal_options() -> list[dict[str, Any]]:
    """Return stable user-facing goal choices."""

    return [
        {
            "key": key,
            "label": profile["label"],
            "description": profile["description"],
            "target_envelopes": _GOAL_ENVELOPES[key],
        }
        for key, profile in _GOALS.items()
    ]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalise_set_target(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}


def _set_target_summary(
    target: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    """Describe one provider set target without inventing values."""

    rep_range = target.get("rep_range")
    if not isinstance(rep_range, dict):
        rep_range = None
    unsupported = sorted(
        key
        for key in target
        if key not in SUPPORTED_SET_TARGET_KEYS
    )
    return {
        "index": index,
        "type": target.get("type") or "normal",
        "load_kg": target.get("weight_kg"),
        "reps": target.get("reps"),
        "rep_range": rep_range,
        "distance_meters": target.get("distance_meters"),
        "duration_seconds": target.get("duration_seconds"),
        "rpe": target.get("rpe"),
        "custom_targets": {
            key: value
            for key, value in target.items()
            if key not in SUPPORTED_SET_TARGET_KEYS
        },
        "unsupported_fields": unsupported,
    }


def _serialise_exercise(
    exercise: RoutineExercise,
    data: HevyTrainingData,
) -> dict[str, Any]:
    template = data.exercise_templates.get(exercise.template_id)
    set_targets = [_normalise_set_target(item) for item in exercise.set_targets]
    summaries = [
        _set_target_summary(target, index)
        for index, target in enumerate(set_targets, start=1)
    ]
    unsupported = sorted(
        {
            field
            for summary in summaries
            for field in summary["unsupported_fields"]
        }
    )
    return {
        "template_id": exercise.template_id,
        "title": exercise.title,
        "exercise_type": template.exercise_type if template else "unknown",
        "primary_muscle_group": (
            template.primary_muscle_group if template else "other"
        ),
        "secondary_muscle_groups": (
            list(template.secondary_muscle_groups) if template else []
        ),
        "equipment": template.equipment if template else None,
        "is_custom": bool(template.is_custom) if template else True,
        "sets": exercise.sets,
        "set_targets": set_targets,
        "set_summary": summaries,
        "set_types": sorted(
            {str(summary["type"]) for summary in summaries}
        ),
        "unsupported_fields": unsupported,
        "rest_seconds": exercise.rest_seconds,
        "notes": exercise.notes,
        "superset_id": exercise.superset_id,
        "target_weight_kg": exercise.target_weight_kg,
        "target_reps": exercise.target_reps,
        "target_rep_range": (
            list(exercise.target_rep_range)
            if exercise.target_rep_range is not None
            else None
        ),
    }


def _routine_history_context(
    routine: Routine,
    workouts: list[CompletedWorkout],
) -> dict[str, Any]:
    """Return the observed completion context for one routine."""

    matches = [
        workout
        for workout in workouts
        if workout.title.casefold() == routine.title.casefold()
    ]
    last_performed_at = None
    for workout in matches:
        if workout.end_time or workout.start_time:
            last_performed_at = max(
                filter(None, [last_performed_at, workout.end_time, workout.start_time])
            )
    return {
        "last_performed_at": last_performed_at,
        "recent_completion_count": len(matches),
    }


def _serialise_routine(
    routine: Routine,
    data: HevyTrainingData,
) -> dict[str, Any]:
    exercises = [_serialise_exercise(exercise, data) for exercise in routine.exercises]
    direct_muscles = sorted(
        {
            exercise["primary_muscle_group"]
            for exercise in exercises
            if exercise["primary_muscle_group"] != "other"
        }
    )
    work_sets = sum(int(exercise["sets"]) for exercise in exercises)
    rest_seconds = sum(
        max(0, int(exercise["rest_seconds"])) * max(1, int(exercise["sets"]))
        for exercise in exercises
    )
    estimated_seconds = 8 * 60 + rest_seconds + work_sets * 45
    source_payload = {
        "id": routine.id,
        "title": routine.title,
        "folder_id": routine.folder_id,
        "updated_at": routine.updated_at,
        "exercises": exercises,
    }
    unsupported = sorted(
        {
            field
            for exercise in exercises
            for field in exercise["unsupported_fields"]
        }
    )
    history = _routine_history_context(routine, data.recent_workouts)
    return {
        **source_payload,
        "folder_name": data.folders.get(routine.folder_id)
        if routine.folder_id is not None
        else None,
        "exercise_count": len(exercises),
        "work_set_count": work_sets,
        "estimated_duration_minutes": max(10, round(estimated_seconds / 60)),
        "muscle_summary": direct_muscles,
        "unsupported_fields": unsupported,
        "source_hash": _canonical_hash(source_payload),
        **history,
    }


def serialise_hevy_source(data: HevyTrainingData) -> dict[str, Any]:
    """Return the exact selectable Hevy source view used by the builder."""

    routines = [_serialise_routine(routine, data) for routine in data.routines]
    folders: list[dict[str, Any]] = []
    for folder_id, title in sorted(data.folders.items()):
        folders.append(
            {
                "id": folder_id,
                "title": title,
                "routine_count": sum(
                    1 for routine in routines if routine["folder_id"] == folder_id
                ),
            }
        )
    return {
        "username": data.username,
        "workout_count": data.workout_count,
        "routine_count": len(routines),
        "recent_workout_count": len(data.recent_workouts),
        "folders": folders,
        "routines": routines,
    }


_PHASE_EXIT_CRITERIA: dict[str, str] = {
    "foundation": "Repeatable technique and a recoverable weekly baseline are established.",
    "development": "Target volume or load is progressing without persistent performance decline.",
    "consolidation": "Performance is stable and readiness for the next goal is reviewed.",
    "foundation_volume": "Most working sets are completed inside the source-compatible range.",
    "overload": "Priority work tolerates the planned volume without sustained fatigue signals.",
    "resensitisation": "Fatigue has reduced and retained exercises are chosen for the next cycle.",
    "strength_base": "Submaximal strength work is technically repeatable at target effort.",
    "intensification": "Heavier work holds technique without a sustained readiness drop.",
    "expression": "Target loads are expressed with acceptable technique and effort.",
    "realisation": "Specific work is tolerated and readiness is stable before the test.",
    "taper_test": "Fatigue is reduced and the planned test or event can be attempted.",
    "sustainable_base": "Recovery and adherence remain acceptable under current energy availability.",
    "productive_tension": "Retained intensity does not create persistent performance decline.",
    "maintenance": "Strength and muscle exposure are preserved with the minimum effective dose.",
    "assessment": "A maintenance review decides the next goal or cycle.",
    "calibration": "Current tolerance is established without testing limits.",
    "rebuild": "Recent historical volume and performance are approached progressively.",
    "normal_progression": "Normal bounded progression resumes from the rebuilt baseline.",
    "power_base": "Movement quality and speed intent are repeatable at low fatigue.",
    "power_development": "Power output intent stays high without meaningful speed loss.",
    "performance": "The targeted performance is expressed with minimal residual fatigue.",
}


def _allocate_phase_weeks(
    duration_weeks: int,
    phases: list[tuple[str, str, float]],
) -> list[tuple[str, str, int]]:
    raw = [duration_weeks * phase[2] for phase in phases]
    lengths = [max(1, math.floor(value)) for value in raw]

    while sum(lengths) > duration_weeks:
        candidates = [index for index, value in enumerate(lengths) if value > 1]
        if not candidates:
            raise ValueError("Duration is too short for the selected goal strategy.")
        index = max(
            candidates, key=lambda candidate: lengths[candidate] - raw[candidate]
        )
        lengths[index] -= 1

    while sum(lengths) < duration_weeks:
        index = max(
            range(len(phases)),
            key=lambda candidate: raw[candidate] - lengths[candidate],
        )
        lengths[index] += 1

    return [
        (phase_key, label, lengths[index])
        for index, (phase_key, label, _weight) in enumerate(phases)
    ]


def _split_into_waves(length: int) -> list[int]:
    if length <= 6:
        return [length]
    wave_count = math.ceil(length / 6)
    base = length // wave_count
    remainder = length % wave_count
    return [base + (1 if index < remainder else 0) for index in range(wave_count)]


def _select_phase_plan(
    duration_weeks: int,
    goal: str,
    *,
    has_event: bool,
    history_confidence: str,
) -> list[tuple[str, str, float]]:
    """Return the ordered phase plan for the selected goal and inputs.

    Short plans deliberately collapse to two meaningful phases instead of
    fabricating a template-shaped three- or four-phase ramp. A low-confidence
    history prepends calibration because it is a product safety requirement,
    not an ornamental third phase.
    """

    profile = _GOALS[goal]
    phases = list(profile["phases"])

    if goal == "strength_test" and not has_event:
        # No dated test means no taper/peak block; use maximal-strength intent.
        phases = list(_GOALS["maximal_strength"]["phases"])

    if duration_weeks <= 6:
        if goal == "return_to_training":
            phases = [phases[0], phases[1]]
        else:
            phases = [phases[0], phases[-1]]

    if history_confidence == "low" and goal != "return_to_training":
        calibration_weight = 1.0 / duration_weeks
        calibration = (
            "calibration",
            "Calibration and familiarisation",
            calibration_weight,
        )
        remaining_total = 1.0 - calibration_weight
        remaining_weight = sum(phase[2] for phase in phases) or 1.0
        remaining = [
            (key, label, weight / remaining_weight * remaining_total)
            for key, label, weight in phases
        ]
        return [calibration, *remaining]

    return phases


def _allocate_blocks(
    duration_weeks: int,
    goal: str,
    start_date: date,
    *,
    has_event: bool = True,
    history_confidence: str = "high",
    fatigue_management: str = "adaptive",
    aggressiveness: str = "balanced",
) -> list[dict[str, Any]]:
    profile = _GOALS.get(goal)
    if profile is None:
        raise ValueError(f"Unsupported programme goal: {goal}")
    if not MIN_DURATION_WEEKS <= duration_weeks <= MAX_DURATION_WEEKS:
        raise ValueError("Programme duration is outside the supported range.")

    phases = _select_phase_plan(
        duration_weeks,
        goal,
        has_event=has_event,
        history_confidence=history_confidence,
    )
    phase_lengths = _allocate_phase_weeks(duration_weeks, phases)
    envelope = _GOAL_ENVELOPES[goal]
    blocks: list[dict[str, Any]] = []
    first_week = 1
    block_number = 1

    for phase_key, phase_label, phase_length in phase_lengths:
        waves = _split_into_waves(phase_length)
        for wave_index, wave_length in enumerate(waves, start=1):
            end_week = first_week + wave_length - 1
            settings = dict(_PHASE_SETTINGS[phase_key])
            settings.update(
                {
                    "progression_aggressiveness": aggressiveness,
                    "exercise_variation": envelope["exercise_variation"],
                    "intensity_distribution": envelope["intensity_distribution"],
                    "fatigue_management": (
                        "planned_review_candidate"
                        if fatigue_management == "planned"
                        else "reactive_candidate"
                    ),
                    "review_gate": (
                        wave_index == len(waves) or wave_length >= 4
                    ),
                    "test_intent": phase_key == "taper_test",
                    "calibration": phase_key == "calibration",
                    "exit_criteria": _PHASE_EXIT_CRITERIA.get(
                        phase_key,
                        "Review actual versus planned exposure at the block boundary.",
                    ),
                }
            )
            name = phase_label
            if len(waves) > 1:
                name = f"{phase_label} · wave {wave_index}"
            block_start = start_date + timedelta(weeks=first_week - 1)
            block_end = start_date + timedelta(weeks=end_week) - timedelta(days=1)
            blocks.append(
                {
                    "number": block_number,
                    "key": phase_key,
                    "name": name,
                    "start_week": first_week,
                    "end_week": end_week,
                    "duration_weeks": wave_length,
                    "weeks": (
                        str(first_week)
                        if first_week == end_week
                        else f"{first_week}-{end_week}"
                    ),
                    "start_date": block_start.isoformat(),
                    "end_date": block_end.isoformat(),
                    **settings,
                }
            )
            first_week = end_week + 1
            block_number += 1

    if first_week - 1 != duration_weeks:
        raise AssertionError("Block allocation did not conserve programme duration.")
    return blocks


def _history_analysis(
    data: HevyTrainingData,
    selected: list[Routine],
) -> dict[str, Any]:
    selected_titles = {routine.title.casefold() for routine in selected}
    matched_workouts = sum(
        1
        for workout in data.recent_workouts
        if workout.title.casefold() in selected_titles
    )
    selected_with_match = sum(
        1
        for routine in selected
        if any(
            workout.title.casefold() == routine.title.casefold()
            for workout in data.recent_workouts
        )
    )
    coverage = selected_with_match / len(selected) if selected else 0.0
    history_count = len(data.recent_workouts)

    if history_count >= 8 and coverage >= 0.75:
        confidence = "high"
    elif history_count >= 3 and coverage >= 0.40:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "history_workouts": history_count,
        "matched_workouts": matched_workouts,
        "routine_match_coverage": round(coverage, 2),
        "confidence": confidence,
    }


def _observed_working_sets(exercise: Any) -> tuple[int, dict[str, int]]:
    """Count observed working sets without treating warm-ups as hard sets."""

    sets = getattr(exercise, "sets", None) or []
    if not sets:
        total = max(0, int(getattr(exercise, "total_sets", 0) or 0))
        return total, ({"normal": total} if total else {})
    working = 0
    totals: dict[str, int] = {}
    for item in sets:
        set_type = (
            str(item.get("type") or "normal").casefold()
            if isinstance(item, dict)
            else "normal"
        )
        totals[set_type] = totals.get(set_type, 0) + 1
        if set_type in EXPECTED_WORKING_SET_TYPES:
            working += 1
    return working, totals


def _exercise_baselines(
    data: HevyTrainingData,
    selected: list[Routine],
) -> list[dict[str, Any]]:
    """Derive observed load/rep baselines from the user's own history."""

    selected_template_ids = {
        exercise.template_id
        for routine in selected
        for exercise in routine.exercises
    }
    observations: dict[str, dict[str, Any]] = {}
    for workout in data.recent_workouts:
        for exercise in workout.exercises:
            if exercise.template_id not in selected_template_ids:
                continue
            entry = observations.setdefault(
                exercise.template_id,
                {
                    "template_id": exercise.template_id,
                    "title": exercise.title,
                    "exposures": 0,
                    "top_weight_kg": None,
                    "top_reps": None,
                    "max_sets_per_exposure": 0,
                    "set_type_totals": {},
                    "source": "hevy_history",
                },
            )
            entry["exposures"] += 1
            working_sets, type_totals = _observed_working_sets(exercise)
            entry["max_sets_per_exposure"] = max(
                int(entry["max_sets_per_exposure"]), working_sets
            )
            totals = entry["set_type_totals"]
            for set_type, count in type_totals.items():
                totals[set_type] = int(totals.get(set_type, 0)) + count
            if workout.end_time or workout.start_time:
                observed_at = workout.end_time or workout.start_time
                current_at = entry.get("last_observed_at")
                entry["last_observed_at"] = max(
                    item for item in (current_at, observed_at) if item
                )
            if exercise.top_weight_kg is not None:
                current = entry["top_weight_kg"]
                entry["top_weight_kg"] = (
                    float(exercise.top_weight_kg)
                    if current is None
                    else max(float(current), float(exercise.top_weight_kg))
                )
            if exercise.top_reps is not None:
                current_reps = entry["top_reps"]
                entry["top_reps"] = (
                    int(exercise.top_reps)
                    if current_reps is None
                    else max(int(current_reps), int(exercise.top_reps))
                )
    return [observations[key] for key in sorted(observations)]


def _source_set_counts(source: dict[str, Any]) -> dict[str, int]:
    """Return warm-up, normal, failure and drop-set counts for a source exercise."""

    summaries = source.get("set_summary") or []
    counts = {"warmup": 0, "normal": 0, "failure": 0, "drop": 0}
    if summaries:
        for summary in summaries:
            set_type = str(summary.get("type") or "normal").casefold()
            if set_type in {"warmup", "warm-up", "warm_up"}:
                counts["warmup"] += 1
            elif set_type in {"failure", "dropset", "drop"}:
                key = "drop" if set_type in {"dropset", "drop"} else "failure"
                counts[key] += 1
            else:
                counts["normal"] += 1
    else:
        counts["normal"] = max(0, int(source.get("sets") or 0))
    counts["working"] = counts["normal"] + counts["failure"] + counts["drop"]
    counts["total"] = counts["working"] + counts["warmup"]
    return counts


def _workload_model(
    data: HevyTrainingData,
    selected: list[Routine],
    source_snapshots: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    duration_weeks: int,
    request: ProgrammePreviewRequest,
    baselines_by_template: dict[str, dict[str, Any]],
    muscle_sets: dict[str, Any],
) -> dict[str, Any]:
    """Estimate opening exposure without claiming an exact biological stimulus."""

    overrides = {item.template_id: item for item in request.exercise_overrides}
    occurrences: dict[str, list[str]] = {}
    for routine in selected:
        for exercise in routine.exercises:
            occurrences.setdefault(exercise.template_id, []).append(routine.title)

    bounds: dict[str, dict[str, Any]] = {}
    exercise_rows: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    implausible: list[dict[str, Any]] = []
    uncertainty: list[dict[str, Any]] = []
    warnings: list[str] = []

    for template_id in sorted(occurrences):
        titles = occurrences[template_id]
        if len(titles) > 1:
            duplicates.append(
                {
                    "template_id": template_id,
                    "routines": sorted(titles),
                    "message": (
                        "This exercise appears more than once across the selected "
                        "routines; weekly exposure is aggregated rather than doubled."
                    ),
                }
            )

    for snapshot in source_snapshots:
        for source in snapshot.get("exercises") or []:
            template_id = str(source["template_id"])
            counts = _source_set_counts(source)
            baseline = baselines_by_template.get(template_id) or {}
            observed_max = int(baseline.get("max_sets_per_exposure") or 0) or None
            source_working = max(1, counts["working"])
            override = overrides.get(template_id)
            reasons: list[str] = []

            allowed_min = max(
                1, source_working + ALLOWED_SET_CHANGE["minimum"]
            )
            allowed_max = max(
                allowed_min, source_working + ALLOWED_SET_CHANGE["maximum"]
            )
            if observed_max is not None:
                history_max = observed_max + MAX_WEEK_ONE_SET_INCREASE
                max_sets = max(allowed_min, min(allowed_max, history_max))
                min_sets = max(
                    allowed_min,
                    min(source_working, observed_max)
                    - MIN_WEEK_ONE_SET_DECREASE,
                )
                proposed = max(min_sets, min(max_sets, source_working))
                confidence = "medium" if baseline.get("exposures", 0) >= 2 else "low"
                reasons.append(
                    "Week one is bounded by the user's recent tolerated exposure."
                )
                if source_working > observed_max + MAX_WEEK_ONE_SET_INCREASE:
                    reasons.append(
                        "The imported source volume is above recent tolerated work, "
                        "so the opening prescription is capped."
                    )
            else:
                min_sets = max(1, source_working - MIN_WEEK_ONE_SET_DECREASE)
                max_sets = max(1, source_working)
                proposed = max(1, source_working - MIN_WEEK_ONE_SET_DECREASE)
                confidence = "low"
                reasons.append(
                    "No matching recent exposure exists; the opening prescription "
                    "is conservative rather than fabricated."
                )
                uncertainty.append(
                    {
                        "template_id": template_id,
                        "reason": "no_recent_exposure",
                        "message": (
                            "No matching recent exposure exists for this exercise; "
                            "the opening workload carries extra uncertainty."
                        ),
                    }
                )

            if source_working > MAX_SETS_PER_EXERCISE:
                implausible.append(
                    {
                        "template_id": template_id,
                        "routine_id": snapshot["id"],
                        "working_sets": source_working,
                        "message": (
                            "The imported source exercise has an unusually high "
                            "working-set count for one session."
                        ),
                    }
                )
                reasons.append("The source working-set count is unusually high.")
            if source.get("is_custom") or source.get("exercise_type") == "unknown":
                uncertainty.append(
                    {
                        "template_id": template_id,
                        "reason": "custom_or_unknown_exercise",
                        "message": (
                            "The exercise template is custom or unavailable, so "
                            "load and effort estimates carry extra uncertainty."
                        ),
                    }
                )
            if override and override.opening_sets is not None:
                proposed = override.opening_sets
                reasons.append("The user supplied an opening-set override.")

            bound = {
                "min_sets": max(1, min_sets),
                "max_sets": max(1, max_sets),
                "proposed_sets": max(1, proposed),
                "source_working_sets": source_working,
                "observed_max_sets": observed_max,
                "confidence": confidence,
                "reason": " ".join(reasons),
            }
            bounds[template_id] = bound
            exercise_rows[template_id] = {
                "template_id": template_id,
                "title": source["title"],
                "exercise_type": source["exercise_type"],
                "primary_muscle_group": source["primary_muscle_group"],
                "sets": {
                    "warmup": counts["warmup"],
                    "normal": counts["normal"],
                    "failure": counts["failure"],
                    "drop": counts["drop"],
                    "working": counts["working"],
                    "total": counts["total"],
                },
                "observed": {
                    "exposures": int(baseline.get("exposures") or 0),
                    "max_sets_per_exposure": observed_max,
                    "top_weight_kg": baseline.get("top_weight_kg"),
                },
                "week_one": bound,
                "override": (
                    override.model_dump(mode="json") if override else None
                ),
            }

    for snapshot in source_snapshots:
        if int(snapshot.get("work_set_count") or 0) > MAX_SOURCE_WORK_SETS_PER_ROUTINE:
            implausible.append(
                {
                    "routine_id": snapshot["id"],
                    "work_set_count": snapshot["work_set_count"],
                    "message": (
                        "The imported routine has an unusually high total working-set "
                        "count; review session time and recovery before activation."
                    ),
                }
            )

    for override in request.exercise_overrides:
        if override.template_id not in bounds:
            uncertainty.append(
                {
                    "template_id": override.template_id,
                    "reason": "override_target_missing",
                    "message": (
                        "An exercise override does not match any selected routine "
                        "exercise and was not applied."
                    ),
                }
            )

    for item in duplicates:
        warnings.append(item["message"])
    for item in implausible:
        warnings.append(item["message"])
    for item in uncertainty:
        warnings.append(item["message"])

    return {
        "version": WORKLOAD_MODEL_VERSION,
        "disclaimer": (
            "These are planning estimates and review triggers, not a claim of exact "
            "biological stimulus."
        ),
        "set_model": {
            "direct_set_weight": DIRECT_SET_WEIGHT,
            "secondary_set_weight": SECONDARY_SET_WEIGHT,
            "warmup_sets_excluded": True,
            "failure_and_drop_sets_counted": True,
        },
        "session_duration": [
            {
                "routine_id": snapshot["id"],
                "title": snapshot["title"],
                "estimated_minutes": snapshot["estimated_duration_minutes"],
            }
            for snapshot in source_snapshots
        ],
        "weekly_exposure": muscle_sets.get("weekly_sets", {}),
        "exercises": [
            exercise_rows[template_id] for template_id in sorted(exercise_rows)
        ],
        "bounds_by_template": bounds,
        "duplicates": sorted(duplicates, key=lambda item: item["template_id"]),
        "unsupported_or_implausible": implausible,
        "uncertainty": sorted(
            {
                (str(item["reason"]), str(item["template_id"])): item
                for item in uncertainty
            }.values(),
            key=lambda item: (str(item["reason"]), str(item["template_id"])),
        ),
        "warnings": warnings,
    }


_LOADED_TYPES = {"weight_reps", "weight_reps_duration"}
_BODYWEIGHT_TYPES = {
    "bodyweight_reps",
    "reps_only",
    "assisted_bodyweight",
    "bodyweight_assisted_reps",
}
_DURATION_TYPES = {
    "duration",
    "distance_duration",
    "distance",
    "weight_duration",
    "short_distance_weight",
}


def _classify_role(
    exercise: dict[str, Any],
    position: int,
    goal: str,
    *,
    priority_muscles: tuple[str, ...] = (),
    priority_exercises: tuple[str, ...] = (),
    history_exposures: int = 0,
    override_role: str | None = None,
) -> tuple[str, str, str, list[str]]:
    """Classify one exercise and return role, reason, confidence and evidence."""

    exercise_type = str(exercise["exercise_type"]).lower()
    template_id = str(exercise["template_id"])
    title = str(exercise["title"]).lower()
    muscle = str(exercise["primary_muscle_group"]).lower()
    notes = str(exercise.get("notes") or "").lower()
    secondary = list(exercise.get("secondary_muscle_groups") or [])
    rest_seconds = int(exercise.get("rest_seconds") or 0)
    source_sets = int(exercise.get("sets") or 0)
    priority_muscle_set = {item.casefold() for item in priority_muscles}
    priority_exercise_set = {item.casefold() for item in priority_exercises}

    evidence = [
        f"exercise_type={exercise_type}",
        f"position={position}",
        f"primary_muscle={muscle or 'unknown'}",
        f"secondary_muscle_count={len(secondary)}",
        f"source_sets={source_sets}",
        f"rest_seconds={rest_seconds}",
        f"goal={goal}",
    ]
    if exercise.get("is_custom"):
        evidence.append("custom_exercise=true")
    if history_exposures:
        evidence.append(f"history_exposures={history_exposures}")

    if override_role:
        evidence.append("user_override=true")
        return (
            override_role,
            "The user explicitly overrode the inferred role for this exercise.",
            "user_override",
            evidence,
        )

    priority_match = (
        template_id.casefold() in priority_exercise_set
        or title in priority_exercise_set
    )
    muscle_priority = muscle in priority_muscle_set and muscle not in {"", "other"}
    if priority_match or muscle_priority:
        evidence.append(
            "priority_exercise=true" if priority_match else "priority_muscle=true"
        )

    if exercise_type in _DURATION_TYPES:
        return (
            "duration_distance",
            "Hevy records this exercise by duration or distance, so load-based strength maths is disabled.",
            "high",
            evidence,
        )
    if muscle == "cardio":
        return (
            "conditioning",
            "The source exercise is classified as conditioning.",
            "high",
            evidence,
        )
    if any(
        token in f"{title} {notes}"
        for token in ("rehab", "prehab", "mobility", "physio")
    ):
        return (
            "rehab_prehab",
            "The source title or notes indicate rehabilitation, prehabilitation or mobility work.",
            "high",
            evidence,
        )
    if exercise_type in _BODYWEIGHT_TYPES:
        return (
            "bodyweight_progression",
            "The source modality is bodyweight or assisted bodyweight.",
            "high",
            evidence,
        )
    if goal == "power_performance" and (
        position == 0
        or any(
            token in title
            for token in ("jump", "throw", "sprint", "explosive", "medicine ball")
        )
    ):
        return (
            "power_skill",
            "The programme goal and source position favour a low-fatigue power or skill role.",
            "high" if position == 0 else "medium",
            evidence,
        )

    loaded = exercise_type in _LOADED_TYPES
    compound_score = 0
    if loaded:
        compound_score = min(3, len(secondary))
        if rest_seconds >= 120:
            compound_score += 1
        if source_sets >= 4:
            compound_score += 1
    evidence.append(f"compound_score={compound_score}")
    confidence = (
        "high"
        if compound_score >= 3 or priority_match
        else "medium"
        if compound_score >= 1 or history_exposures >= 2
        else "low"
    )

    if loaded and (position == 0 or priority_match):
        return (
            "primary_strength",
            "This loaded movement carries the routine's primary strength exposure.",
            "high" if priority_match else confidence,
            evidence,
        )
    if loaded and compound_score >= 1:
        return (
            "secondary_compound",
            "This loaded movement supports the routine's primary work with meaningful systemic contribution.",
            confidence,
            evidence,
        )
    return (
        "hypertrophy_accessory",
        "This source exercise is retained as accessory or hypertrophy work.",
        confidence,
        evidence,
    )


def _rep_range_for(
    role: str,
    phase_key: str,
    source: dict[str, Any],
) -> str:
    target_range = source.get("target_rep_range")
    target_reps = source.get("target_reps")
    if role == "duration_distance":
        return "source duration/distance target"
    if role == "conditioning":
        return "source interval or duration target"
    if role == "rehab_prehab":
        return "controlled source range"
    if role == "power_skill":
        return "2-5 quality reps"
    if role == "bodyweight_progression":
        if target_range and len(target_range) == 2:
            return f"{target_range[0]}-{target_range[1]}"
        return f"{target_reps or 6}-{max(target_reps or 10, 10)}"
    if role == "primary_strength":
        if phase_key in {"intensification"}:
            return "2-6"
        if phase_key in {"expression", "realisation", "taper_test"}:
            return "1-4"
        if phase_key in {"foundation_volume", "overload", "resensitisation"}:
            return "5-10"
        return "4-8"
    if role == "secondary_compound":
        if phase_key in {"intensification", "expression", "realisation"}:
            return "4-8"
        return "6-10"
    if target_range and len(target_range) == 2:
        return f"{target_range[0]}-{target_range[1]}"
    if target_reps:
        return f"{max(1, target_reps - 2)}-{target_reps + 2}"
    return "8-15"


def _equipment_increment_kg(equipment: str | None) -> float | None:
    if not equipment:
        return None
    return _EQUIPMENT_INCREMENTS_KG.get(equipment.strip().casefold())


def _parse_rep_range(value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"min": value, "max": value, "description": str(value)}
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {
            "min": int(value[0]),
            "max": int(value[1]),
            "description": f"{value[0]}-{value[1]}",
        }
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", text)
    if match:
        return {
            "min": int(match.group(1)),
            "max": int(match.group(2)),
            "description": text,
        }
    if text.isdigit():
        number = int(text)
        return {"min": number, "max": number, "description": text}
    return {"min": None, "max": None, "description": text}


def _parse_rir(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    numbers = [int(item) for item in re.findall(r"\d+", text)]
    if not numbers:
        return {"min": None, "max": None, "description": text}
    return {
        "min": min(numbers),
        "max": max(numbers),
        "description": text,
    }


def _progression_for(
    role: str,
    *,
    exercise_type: str = "",
    equipment: str | None = None,
    experience: str = "intermediate",
    strength_model: str = "auto",
    history_exposures: int = 0,
    aggressiveness: str = "balanced",
) -> dict[str, Any]:
    increment = _equipment_increment_kg(equipment)
    max_load_percent = LOAD_CHANGE_PERCENT.get(aggressiveness, 5.0)
    base: dict[str, Any] = {
        "minimum_successful_exposures": 2,
        "load_increment_kg": increment,
        "max_load_percent": max_load_percent,
        "hold_rule": (
            "Hold load and effort when reps are improving but the success criteria "
            "are not yet met."
        ),
        "reduce_rule": (
            "Reduce load or target effort after repeated misses, unexpected effort "
            "or a safety signal; do not react to a single bad day."
        ),
        "substitute_rule": (
            "Substitute only from an explicitly approved equivalence set and only "
            "with user confirmation."
        ),
        "modality": "load",
    }

    if role == "primary_strength":
        model = strength_model
        if model == "auto":
            model = (
                "autoregulated_rep_range"
                if experience == "advanced" or history_exposures >= 3
                else "conservative_double_progression"
            )
        result: dict[str, Any] = {
            **base,
            "method": model,
            "model": model,
            "structural_change": False,
            "rule": (
                "Progress load or reps inside a transparent rep range at the target "
                "RIR after the required successful exposures; reduce after repeated "
                "misses."
            ),
        }
        if model == "top_set_back_off":
            result.update(
                {
                    "structural_change": True,
                    "top_set": {
                        "sets": 1,
                        "effort_target": "target RPE/RIR",
                    },
                    "back_off_sets": {
                        "sets": "derived from the achieved top set",
                        "load_reduction_percent": {"min": 5.0, "max": 15.0},
                    },
                    "rule": (
                        "One top set at the target effort plus derived back-off sets; "
                        "cap load changes and never infer a true max from one set."
                    ),
                }
            )
        elif model == "conservative_double_progression":
            result["minimum_successful_exposures"] = 3
        return result

    if role == "secondary_compound":
        return {
            **base,
            "method": "bounded_double_progression",
            "rule": (
                "Add reps inside the range first, then the smallest practical load "
                "while preserving technique."
            ),
        }
    if role == "bodyweight_progression":
        return {
            **base,
            "method": "bodyweight_progression",
            "modality": "bodyweight",
            "load_increment_kg": None,
            "type_specific": {
                "progress_order": [
                    "reps",
                    "added_load",
                    "less_assistance",
                    "range",
                    "tempo",
                    "variation",
                ],
                "assistance": "reduce only after reps and effort criteria are met",
            },
            "rule": (
                "Progress reps first, then added load, less assistance, range, tempo "
                "or variation as appropriate."
            ),
        }
    if role == "duration_distance":
        return {
            **base,
            "method": "duration_distance_progression",
            "modality": (
                "distance" if "distance" in exercise_type else "duration"
            ),
            "load_increment_kg": None,
            "type_specific": {
                "progress_order": [
                    "duration",
                    "distance",
                    "pace",
                    "resistance",
                    "work_rest_ratio",
                ],
                "forbidden_calculation": "e1RM",
            },
            "rule": (
                "Progress duration, distance, pace, resistance or work-rest ratio; "
                "never calculate e1RQ."
            ),
        }
    if role == "conditioning":
        return {
            **base,
            "method": "conditioning_progression",
            "modality": "conditioning",
            "load_increment_kg": None,
            "type_specific": {
                "progress_order": [
                    "duration",
                    "pace",
                    "resistance",
                    "work_rest_density",
                ]
            },
            "rule": (
                "Progress one of duration, pace, resistance or work-rest density "
                "while preserving recovery."
            ),
        }
    if role == "power_skill":
        return {
            **base,
            "method": "quality_velocity_progression",
            "modality": "quality",
            "load_increment_kg": None,
            "type_specific": {
                "stop_cue": "stop before meaningful speed loss",
                "progress_order": ["quality", "range", "load"],
            },
            "rule": (
                "Progress only while repetitions remain fast and technically "
                "consistent; stop before meaningful speed loss."
            ),
        }
    if role == "rehab_prehab":
        return {
            **base,
            "method": "tolerance_progression",
            "modality": "tolerance",
            "load_increment_kg": None,
            "type_specific": {
                "progress_order": ["range", "control", "load"],
                "symptom_rule": "pause when symptoms or technique deteriorate",
            },
            "rule": (
                "Progress range, control or load only when symptoms and technique "
                "remain acceptable."
            ),
        }
    return {
        **base,
        "method": "double_progression",
        "rule": (
            "Accumulate clean reps inside the range, then add the smallest practical "
            "load after repeated successful exposures."
        ),
    }


def _target_sets(source_sets: int, volume_multiplier: float) -> int:
    source_sets = max(1, source_sets)
    proposed = round(source_sets * volume_multiplier)
    return max(
        source_sets + ALLOWED_SET_CHANGE["minimum"],
        min(
            source_sets + ALLOWED_SET_CHANGE["maximum"],
            max(1, proposed),
        ),
    )


def _prescriptions_for_exercise(
    source: dict[str, Any],
    position: int,
    goal: str,
    blocks: list[dict[str, Any]],
    *,
    priority_muscles: tuple[str, ...] = (),
    priority_exercises: tuple[str, ...] = (),
    history_exposures: int = 0,
    override: ExerciseOverride | None = None,
    baseline: dict[str, Any] | None = None,
    workload_bound: dict[str, Any] | None = None,
    experience: str = "intermediate",
    strength_model: str = "auto",
    aggressiveness: str = "balanced",
) -> tuple[str, str, str, list[str], list[dict[str, Any]]]:
    role, role_reason, role_confidence, evidence = _classify_role(
        source,
        position,
        goal,
        priority_muscles=priority_muscles,
        priority_exercises=priority_exercises,
        history_exposures=history_exposures,
        override_role=override.role if override else None,
    )
    progression = _progression_for(
        role,
        exercise_type=str(source.get("exercise_type") or ""),
        equipment=source.get("equipment"),
        experience=experience,
        strength_model=strength_model,
        history_exposures=history_exposures,
        aggressiveness=aggressiveness,
    )
    source_sets = max(1, int(source.get("sets") or 1))
    prescriptions: list[dict[str, Any]] = []
    for block in blocks:
        sets = _target_sets(source_sets, float(block["volume_multiplier"]))
        applied_override = None
        if block["number"] == 1:
            if override and override.opening_sets is not None:
                sets = override.opening_sets
                applied_override = "opening_sets"
            elif workload_bound is not None:
                sets = max(
                    workload_bound["min_sets"],
                    min(
                        workload_bound["max_sets"],
                        workload_bound["proposed_sets"],
                    ),
                )
                applied_override = "workload_bound"
        rep_range = _rep_range_for(role, block["key"], source)
        load_anchor = None
        if (
            progression["modality"] == "load"
            and baseline
            and baseline.get("top_weight_kg") is not None
        ):
            load_anchor = {
                "type": "last_observed_top_set",
                "value": float(baseline["top_weight_kg"]),
                "unit": "kg",
                "source": baseline.get("source", "hevy_history"),
            }
        prescriptions.append(
            {
                "block_number": block["number"],
                "block_key": block["key"],
                "sets": sets,
                "source_sets": source_sets,
                "rep_range": rep_range,
                "rep_target": _parse_rep_range(rep_range),
                "target_rir": block["target_rir"],
                "effort_target": _parse_rir(block["target_rir"]),
                "progression": progression,
                "load_anchor": load_anchor,
                "equipment_increment_kg": progression.get("load_increment_kg"),
                "allowed_set_change_from_source": dict(ALLOWED_SET_CHANGE),
                "allowed_change": {
                    "max_set_delta": ALLOWED_SET_CHANGE["maximum"],
                    "min_set_delta": ALLOWED_SET_CHANGE["minimum"],
                    "max_load_percent": (
                        LOAD_CHANGE_PERCENT.get(aggressiveness, 5.0)
                        if progression["modality"] == "load"
                        else None
                    ),
                    "max_rir_delta": 1,
                },
                "structural_change": progression.get("structural_change", False),
                "modality_fields": progression.get("type_specific"),
                "applied_override": applied_override,
                "rule_version": ENGINE_VERSION,
                "confidence": role_confidence,
            }
        )
    return role, role_reason, role_confidence, evidence, prescriptions


def _resolve_selection(
    request: ProgrammePreviewRequest,
    routine_by_id: dict[str, Routine],
) -> list[dict[str, Any]]:
    """Return the ordered selection with the role of every routine."""

    roles = {item.routine_id: item for item in request.routine_selections}
    selection: list[dict[str, Any]] = []
    for routine_id in request.selected_routine_ids:
        routine = routine_by_id[routine_id]
        item = roles.get(routine_id)
        selection.append(
            {
                "routine_id": routine.id,
                "title": routine.title,
                "role": item.role if item else "required",
                "preferred_weekday": item.preferred_weekday if item else None,
            }
        )
    return selection


def _training_days(cadence: ProgrammeCadence) -> list[int]:
    if cadence.weekdays:
        return sorted(cadence.weekdays)
    return list(range(7))


def _build_cadence(
    selection: list[dict[str, Any]],
    cadence: ProgrammeCadence,
) -> dict[str, Any]:
    """Map the ordered selection onto training days.

    ``weekly`` assigns each routine its own weekday so a routine is never
    silently dropped.  ``rolling`` keeps the user's routine order and advances
    one routine per training day.
    """

    training_days = _training_days(cadence)
    required = [item for item in selection if item["role"] == "required"]
    optional = [item for item in selection if item["role"] == "optional"]
    rotating = [item for item in selection if item["role"] == "rotating"]

    assignments: dict[str, list[int]] = {}
    unscheduled: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _assign(entry: dict[str, Any], day: int) -> None:
        assignments.setdefault(entry["routine_id"], []).append(day)

    if cadence.mode == "weekly":
        free_days = list(training_days)
        # Required routines claim one weekday each, honouring a preferred day.
        for entry in required:
            preferred = entry.get("preferred_weekday")
            if preferred is not None and preferred in free_days:
                _assign(entry, preferred)
                free_days.remove(preferred)
                continue
            if free_days:
                _assign(entry, free_days.pop(0))
                continue
            unscheduled.append(
                {
                    "routine_id": entry["routine_id"],
                    "title": entry["title"],
                    "role": entry["role"],
                    "reason": (
                        "The selected training days do not fit every required routine."
                    ),
                }
            )
        # Rotating routines fill the remaining days in rotation order.
        rotating_cycle = list(rotating)
        rotating_index = 0
        while free_days and rotating_cycle:
            entry = rotating_cycle[rotating_index % len(rotating_cycle)]
            _assign(entry, free_days.pop(0))
            rotating_index += 1
        # Optional routines only take days that are still free.
        for entry in optional:
            if free_days:
                _assign(entry, free_days.pop(0))
            else:
                unscheduled.append(
                    {
                        "routine_id": entry["routine_id"],
                        "title": entry["title"],
                        "role": entry["role"],
                        "reason": (
                            "The selected training days are already used by required "
                            "and rotating routines."
                        ),
                    }
                )
    rotation: list[dict[str, Any]] = []
    for position, entry in enumerate(selection, start=1):
        days = assignments.get(entry["routine_id"], [])
        scheduled = bool(days) or cadence.mode == "rolling"
        rotation.append(
            {
                "routine_id": entry["routine_id"],
                "title": entry["title"],
                "role": entry["role"],
                "position": position,
                "preferred_weekday": entry.get("preferred_weekday"),
                "scheduled_weekdays": sorted(days),
                "scheduled_weekday_names": [
                    WEEKDAY_NAMES[day] for day in sorted(days)
                ],
                "scheduled": scheduled,
            }
        )

    if unscheduled:
        warnings.append(
            "Some routines are not scheduled by the current cadence. "
            "Review the selected training days before activation."
        )

    return {
        "mode": cadence.mode,
        "training_days": training_days,
        "training_day_names": [WEEKDAY_NAMES[day] for day in training_days],
        "sessions_per_week": len(training_days),
        "rotation": rotation,
        "unscheduled_routines": unscheduled,
        "warnings": warnings,
    }


def _build_schedule(
    cadence_preview: dict[str, Any],
    start_date: date,
    duration_weeks: int,
) -> list[dict[str, Any]]:
    """Return the exact programme dates for every scheduled session.

    ``weekly`` maps each training day onto its routine for every week.
    ``rolling`` advances the rotation by one routine per training day, so the
    routine for a date is decided by the number of training days before it.
    """

    rotation = cadence_preview["rotation"]
    training_days = cadence_preview["training_days"]
    programme_days = duration_weeks * 7
    schedule: list[dict[str, Any]] = []

    if cadence_preview["mode"] == "weekly":
        assignments: dict[int, list[dict[str, Any]]] = {}
        for entry in rotation:
            for day in entry["scheduled_weekdays"]:
                assignments.setdefault(day, []).append(entry)
        for week in range(1, duration_weeks + 1):
            week_start = start_date + timedelta(weeks=week - 1)
            for offset in range(7):
                session_date = week_start + timedelta(days=offset)
                day = session_date.weekday()
                if day not in training_days:
                    continue
                for entry in assignments.get(day, []):
                    schedule.append(
                        {
                            "week": week,
                            "date": session_date.isoformat(),
                            "weekday_index": day,
                            "weekday": WEEKDAY_NAMES[day],
                            "routine_id": entry["routine_id"],
                            "routine_title": entry["title"],
                            "rotation_position": entry["position"],
                            "role": entry["role"],
                        }
                    )
        schedule.sort(key=lambda item: (item["date"], item["rotation_position"]))
        return schedule

    training_day_index = 0
    for offset in range(programme_days):
        session_date = start_date + timedelta(days=offset)
        if session_date.weekday() not in training_days:
            continue
        if not rotation:
            break
        entry = rotation[training_day_index % len(rotation)]
        training_day_index += 1
        schedule.append(
            {
                "week": (offset // 7) + 1,
                "date": session_date.isoformat(),
                "weekday_index": session_date.weekday(),
                "weekday": WEEKDAY_NAMES[session_date.weekday()],
                "routine_id": entry["routine_id"],
                "routine_title": entry["title"],
                "rotation_position": entry["position"],
                "role": entry["role"],
            }
        )
    return schedule


def _muscle_set_estimates(
    source_snapshots: list[dict[str, Any]],
    cadence_preview: dict[str, Any],
    schedule: list[dict[str, Any]],
    duration_weeks: int,
) -> dict[str, Any]:
    """Estimate weekly fractional hard sets per muscle.

    The estimate uses the generated schedule so a routine that appears more
    than once a week contributes proportionally more volume.
    """

    routine_by_id = {snapshot["id"]: snapshot for snapshot in source_snapshots}
    occurrences: dict[str, int] = {}
    for session in schedule:
        routine_id = str(session.get("routine_id"))
        occurrences[routine_id] = occurrences.get(routine_id, 0) + 1
    weeks = max(1, duration_weeks)

    weekly: dict[str, float] = {}
    for routine_id, count in occurrences.items():
        snapshot = routine_by_id.get(routine_id)
        if snapshot is None:
            continue
        frequency = count / weeks
        for exercise in snapshot["exercises"]:
            sets = max(0, int(_source_set_counts(exercise)["working"]))
            if not sets:
                continue
            primary = str(exercise.get("primary_muscle_group") or "other")
            weekly[primary] = (
                weekly.get(primary, 0.0) + sets * DIRECT_SET_WEIGHT * frequency
            )
            for secondary in exercise.get("secondary_muscle_groups") or []:
                weekly[str(secondary)] = (
                    weekly.get(str(secondary), 0.0)
                    + sets * SECONDARY_SET_WEIGHT * frequency
                )

    estimates: list[dict[str, Any]] = []
    warnings: list[str] = []
    for muscle in sorted(weekly):
        value = round(weekly[muscle], 1)
        if value < MIN_RECOVERABLE_SETS:
            status = "below_reference_range"
            warnings.append(
                f"Estimated weekly sets for {muscle} are {value}, below the "
                "reference range used for review. Add priority work if this "
                "muscle matters for your goal."
            )
        elif value > MAX_RECOVERABLE_SETS:
            status = "above_reference_range"
            warnings.append(
                f"Estimated weekly sets for {muscle} are {value}, above the "
                "reference range used for review. Check recovery before adding "
                "more volume."
            )
        else:
            status = "within_reference_range"
        estimates.append(
            {"muscle": muscle, "weekly_sets": value, "status": status}
        )

    return {
        "method": (
            "Working sets exclude warm-ups. Direct sets count as 1.0 and "
            "meaningful secondary contributions as 0.5 for the selected weekly "
            "schedule."
        ),
        "weekly_sets": {
            estimate["muscle"]: estimate["weekly_sets"] for estimate in estimates
        },
        "estimates": estimates,
        "warnings": warnings,
    }


def _drift_report(
    source_snapshots: list[dict[str, Any]],
    active_definition: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare the active programme snapshot with the current Hevy source."""

    if not active_definition:
        return {
            "state": "no_active_programme",
            "routines": [],
            "changed_routines": [],
        }

    stored = {
        str(snapshot.get("id")): str(snapshot.get("source_hash") or "")
        for snapshot in active_definition.get("source_snapshots") or []
    }
    current = {
        str(snapshot.get("id")): str(snapshot.get("source_hash") or "")
        for snapshot in source_snapshots
    }
    routines: list[dict[str, Any]] = []
    changed: list[str] = []
    for routine_id, source_hash in current.items():
        previous = stored.get(routine_id)
        if previous is None:
            status = "added"
        elif previous == source_hash:
            status = "unchanged"
        else:
            status = "changed"
        if status != "unchanged":
            changed.append(routine_id)
        routines.append(
            {
                "routine_id": routine_id,
                "status": status,
                "previous_source_hash": previous,
                "current_source_hash": source_hash,
            }
        )
    for routine_id, previous_hash in stored.items():
        if routine_id in current:
            continue
        changed.append(routine_id)
        routines.append(
            {
                "routine_id": routine_id,
                "status": "removed",
                "previous_source_hash": previous_hash,
                "current_source_hash": None,
            }
        )

    return {
        "state": "changed" if changed else "in_sync",
        "routines": routines,
        "changed_routines": changed,
    }


def _violation(
    code: str,
    message: str,
    *,
    field: str,
    routine_id: str | None = None,
    exercise_id: str | None = None,
    week: int | None = None,
    rule: str | None = None,
    remediation: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "field": field,
        "routine_id": routine_id,
        "exercise_id": exercise_id,
        "week": week,
        "rule": rule or code,
        "message": message,
        "remediation": remediation,
    }


def validate_programme_preview(preview: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the blocking violations of a generated programme preview.

    This is the shared validator used by preview, activation and adaptation. A
    violation always identifies the affected routine, exercise, week and rule,
    plus a remediation the UI can show to the user.
    """

    violations: list[dict[str, Any]] = []
    spec = preview.get("programme_spec") or {}
    cadence_preview = preview.get("cadence") or {}
    constraints = spec.get("constraints") or {}
    rotation = cadence_preview.get("rotation") or []
    cycle_weeks = int(preview.get("cycle_weeks") or 0)

    if not rotation:
        violations.append(
            _violation(
                "no_scheduled_routines",
                "Select at least one Hevy routine before generating a programme.",
                field="selected_routine_ids",
                remediation="Select one or more available Hevy routines.",
            )
        )
        return violations

    unscheduled = [
        entry
        for entry in rotation
        if not entry.get("scheduled") and entry.get("role") == "required"
    ]
    for entry in unscheduled:
        violations.append(
            _violation(
                "routine_not_scheduled",
                (
                    f"The routine '{entry.get('title')}' is required but has no "
                    "training day."
                ),
                field="cadence.weekdays",
                routine_id=entry.get("routine_id"),
                rule="required_routine_needs_a_training_day",
                remediation="Add a weekday or change the routine role.",
            )
        )

    sessions_per_week = int(cadence_preview.get("sessions_per_week") or 0)
    limit = constraints.get("max_sessions_per_week")
    if limit is not None and sessions_per_week > int(limit):
        violations.append(
            _violation(
                "session_limit_exceeded",
                (
                    f"The programme schedules {sessions_per_week} sessions per week "
                    f"but the confirmed limit is {limit}."
                ),
                field="constraints.max_sessions_per_week",
                rule="structured_session_limit",
                remediation="Reduce the training days or change the saved limit.",
            )
        )

    max_session_minutes = spec.get("max_session_minutes")
    if max_session_minutes is not None:
        for day in preview.get("days") or []:
            estimate = int(day.get("estimated_duration_minutes") or 0)
            if estimate > int(max_session_minutes):
                violations.append(
                    _violation(
                        "session_duration_exceeded",
                        (
                            f"Routine '{day.get('focus')}' is estimated at "
                            f"{estimate} minutes, above the {max_session_minutes} "
                            "minute session limit."
                        ),
                        field="max_session_minutes",
                        routine_id=day.get("routine_id"),
                        rule="structured_session_time_limit",
                        remediation=(
                            "Shorten the source routine, raise the session limit or "
                            "choose a different routine."
                        ),
                    )
                )

    start_value = spec.get("start_date")
    event_value = spec.get("event_date")
    if start_value and event_value:
        try:
            start_date = date.fromisoformat(str(start_value))
            event_date = date.fromisoformat(str(event_value))
        except ValueError:
            start_date = None
            event_date = None
        if start_date and event_date and event_date < start_date:
            violations.append(
                _violation(
                    "event_before_start",
                    (
                        "The test or competition date is before the programme "
                        "start date."
                    ),
                    field="event_date",
                    rule="event_must_follow_start",
                    remediation="Choose an event date on or after the start date.",
                )
            )
        elif (
            start_date
            and event_date
            and spec.get("goal") == "strength_test"
            and cycle_weeks
        ):
            programme_end = start_date + timedelta(weeks=cycle_weeks)
            if event_date > programme_end:
                violations.append(
                    _violation(
                        "event_outside_programme",
                        "The test date falls after the programme ends.",
                        field="event_date",
                        rule="event_must_fit_duration",
                        remediation=(
                            "Extend the duration or choose a different test date."
                        ),
                    )
                )

    blocks = preview.get("blocks") or []
    if blocks:
        total_weeks = sum(int(block.get("duration_weeks") or 0) for block in blocks)
        if total_weeks != cycle_weeks:
            violations.append(
                _violation(
                    "duration_not_conserved",
                    (
                        f"Blocks cover {total_weeks} weeks but the programme asks "
                        f"for {cycle_weeks}."
                    ),
                    field="blocks.duration_weeks",
                    rule="block_duration_conservation",
                    remediation="Regenerate the programme with a valid duration.",
                )
            )
        expected_week = 1
        for block in blocks:
            if int(block.get("start_week") or 0) != expected_week:
                violations.append(
                    _violation(
                        "block_gap_or_overlap",
                        "Programme blocks do not run continuously.",
                        field="blocks.start_week",
                        week=int(block.get("start_week") or 1),
                        rule="block_contiguity",
                        remediation="Regenerate the block allocation.",
                    )
                )
                break
            expected_week = int(block.get("end_week") or 0) + 1
        if blocks[-1].get("end_week") != cycle_weeks:
            violations.append(
                _violation(
                    "duration_not_conserved",
                    "The final block does not end on the final programme week.",
                    field="blocks.end_week",
                    week=int(blocks[-1].get("end_week") or 0),
                    rule="block_duration_conservation",
                    remediation="Regenerate the block allocation.",
                )
            )

    block_keys = [str(block.get("key")) for block in blocks]
    logical_keys = [key for key in block_keys if key != "calibration"]
    if cycle_weeks <= 6 and len(set(logical_keys)) > 2:
        violations.append(
            _violation(
                "too_many_short_phases",
                "A short programme was split into too many phases.",
                field="blocks",
                rule="short_programme_has_at_most_two_phases",
                remediation="Regenerate with a simpler short-duration phase plan.",
            )
        )
    if "taper_test" in block_keys and (
        spec.get("goal") != "strength_test" or not spec.get("event_date")
    ):
        violations.append(
            _violation(
                "taper_without_event",
                "A taper/test block exists without a strength-test goal and date.",
                field="blocks",
                rule="taper_requires_a_dated_test",
                remediation="Set the strength-test goal and date or remove the taper.",
            )
        )

    for day in preview.get("days") or []:
        routine_id = day.get("routine_id")
        for exercise in day.get("exercises") or []:
            exercise_id = exercise.get("template_id")
            source_sets = max(1, int(exercise.get("source_sets") or 1))
            for prescription in exercise.get("prescriptions") or []:
                block_number = int(prescription.get("block_number") or 0)
                week = block_number
                sets = int(prescription.get("sets") or 0)
                if sets < 1:
                    violations.append(
                        _violation(
                            "invalid_set_target",
                            "A prescription asks for fewer than one working set.",
                            field="prescriptions.sets",
                            routine_id=routine_id,
                            exercise_id=exercise_id,
                            week=week,
                            rule="minimum_one_working_set",
                            remediation="Raise the set target to at least one.",
                        )
                    )
                if prescription.get("applied_override") != "opening_sets":
                    delta = sets - source_sets
                    if not (
                        ALLOWED_SET_CHANGE["minimum"]
                        <= delta
                        <= ALLOWED_SET_CHANGE["maximum"]
                    ):
                        violations.append(
                            _violation(
                                "set_change_exceeds_bound",
                                (
                                    f"A prescription changes sets by {delta:+d}, "
                                    "outside the configured bound."
                                ),
                                field="prescriptions.sets",
                                routine_id=routine_id,
                                exercise_id=exercise_id,
                                week=week,
                                rule="bounded_set_change",
                                remediation=(
                                    "Keep automatic set changes within the "
                                    "configured source bound."
                                ),
                            )
                        )
                rep_target = prescription.get("rep_target") or {}
                if (
                    rep_target.get("min") is not None
                    and rep_target.get("max") is not None
                    and int(rep_target["min"]) > int(rep_target["max"])
                ):
                    violations.append(
                        _violation(
                            "invalid_rep_range",
                            "A prescription rep range is inverted.",
                            field="prescriptions.rep_target",
                            routine_id=routine_id,
                            exercise_id=exercise_id,
                            week=week,
                            rule="rep_range_ordering",
                            remediation="Regenerate the prescription with a valid range.",
                        )
                    )
                if (
                    prescription.get("rep_target", {}).get("min") is not None
                    and int(prescription["rep_target"]["min"]) < 1
                ):
                    violations.append(
                        _violation(
                            "invalid_rep_range",
                            "A prescription rep range is below one repetition.",
                            field="prescriptions.rep_target",
                            routine_id=routine_id,
                            exercise_id=exercise_id,
                            week=week,
                            rule="rep_range_minimum",
                            remediation="Regenerate the prescription with a valid range.",
                        )
                    )
                if exercise.get("exercise_type") in _DURATION_TYPES | _BODYWEIGHT_TYPES:
                    modality = (prescription.get("progression") or {}).get(
                        "modality"
                    )
                    if modality == "load":
                        violations.append(
                            _violation(
                                "invalid_modality_progression",
                                (
                                    "A duration, distance or bodyweight exercise "
                                    "received a load-based progression."
                                ),
                                field="prescriptions.progression",
                                routine_id=routine_id,
                                exercise_id=exercise_id,
                                week=week,
                                rule="modality_specific_progression",
                                remediation=(
                                    "Use the exercise's type-specific progression."
                                ),
                            )
                        )

    return violations


def _priority_coverage(
    priority_muscles: list[str],
    priority_exercises: list[str],
    source_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report whether the selected routines cover the stated priorities."""

    muscles = {
        str(exercise.get("primary_muscle_group") or "").casefold()
        for snapshot in source_snapshots
        for exercise in snapshot.get("exercises") or []
    }
    titles = {
        str(exercise.get("title") or "").casefold()
        for snapshot in source_snapshots
        for exercise in snapshot.get("exercises") or []
    }
    missing_muscles = [
        muscle
        for muscle in priority_muscles
        if muscle.casefold() not in muscles
    ]
    missing_exercises = [
        exercise
        for exercise in priority_exercises
        if exercise.casefold() not in titles
    ]
    return {
        "priority_muscles": list(priority_muscles),
        "priority_exercises": list(priority_exercises),
        "missing_muscles": missing_muscles,
        "missing_exercises": missing_exercises,
    }


def build_programme_preview(
    data: HevyTrainingData,
    request: ProgrammePreviewRequest,
    *,
    active_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, source-bound programme preview."""

    if not data.routines:
        raise ValueError("No Hevy routines are available to build a programme.")
    if len(set(request.selected_routine_ids)) != len(request.selected_routine_ids):
        raise ValueError("Each selected Hevy routine may appear only once.")

    routine_by_id = {routine.id: routine for routine in data.routines}
    missing = [
        routine_id
        for routine_id in request.selected_routine_ids
        if routine_id not in routine_by_id
    ]
    if missing:
        raise ValueError(
            "Selected routine IDs are not available in the current Hevy source: "
            + ", ".join(missing)
        )

    selected = [
        routine_by_id[routine_id] for routine_id in request.selected_routine_ids
    ]
    selected_data = HevyTrainingData(
        username=data.username,
        workout_count=data.workout_count,
        exercise_templates=data.exercise_templates,
        routines=selected,
        recent_workouts=data.recent_workouts,
        folders=data.folders,
    )
    inferred = infer_programme(selected_data)
    source_snapshots = [_serialise_routine(routine, data) for routine in selected]
    history = _history_analysis(data, selected)
    blocks = _allocate_blocks(
        request.duration_weeks,
        request.goal,
        request.start_date,
        has_event=request.event_date is not None,
        history_confidence=history["confidence"],
        fatigue_management=request.fatigue_management,
        aggressiveness=request.adaptation_aggressiveness,
    )
    selection = _resolve_selection(request, routine_by_id)
    cadence_preview = _build_cadence(selection, request.cadence)
    schedule = _build_schedule(
        cadence_preview,
        request.start_date,
        request.duration_weeks,
    )
    muscle_sets = _muscle_set_estimates(
        source_snapshots,
        cadence_preview,
        schedule,
        request.duration_weeks,
    )
    drift = _drift_report(source_snapshots, active_definition)
    coverage = _priority_coverage(
        request.priority_muscles,
        request.priority_exercises,
        source_snapshots,
    )
    baselines = _exercise_baselines(data, selected)
    baselines_by_template = {
        item["template_id"]: item for item in baselines
    }
    workload = _workload_model(
        data,
        selected,
        source_snapshots,
        schedule,
        request.duration_weeks,
        request,
        baselines_by_template,
        muscle_sets,
    )
    overrides_by_template = {
        item.template_id: item for item in request.exercise_overrides
    }
    priority_muscle_tuple = tuple(request.priority_muscles)
    priority_exercise_tuple = tuple(request.priority_exercises)

    days: list[dict[str, Any]] = []
    for day_number, source_routine in enumerate(source_snapshots, start=1):
        exercises: list[dict[str, Any]] = []
        for position, source_exercise in enumerate(source_routine["exercises"]):
            template_id = source_exercise["template_id"]
            baseline = baselines_by_template.get(template_id)
            (
                role,
                role_reason,
                role_confidence,
                role_evidence,
                prescriptions,
            ) = _prescriptions_for_exercise(
                source_exercise,
                position,
                request.goal,
                blocks,
                priority_muscles=priority_muscle_tuple,
                priority_exercises=priority_exercise_tuple,
                history_exposures=(
                    int(baseline.get("exposures") or 0) if baseline else 0
                ),
                override=overrides_by_template.get(template_id),
                baseline=baseline,
                workload_bound=workload["bounds_by_template"].get(template_id),
                experience=request.experience,
                strength_model=request.strength_model,
                aggressiveness=request.adaptation_aggressiveness,
            )
            opening = prescriptions[0]
            exercises.append(
                {
                    "name": source_exercise["title"],
                    "template_id": template_id,
                    "exercise_type": source_exercise["exercise_type"],
                    "role": role,
                    "role_reason": role_reason,
                    "role_confidence": role_confidence,
                    "role_evidence": role_evidence,
                    "role_override": bool(
                        overrides_by_template.get(template_id)
                        and overrides_by_template[template_id].role
                    ),
                    "sets": opening["sets"],
                    "rep_range": opening["rep_range"],
                    "scheme": f"{opening['sets']} × {opening['rep_range']}",
                    "note": source_exercise.get("notes") or "",
                    "rest_seconds": source_exercise["rest_seconds"],
                    "source_sets": source_exercise["sets"],
                    "source_targets": source_exercise["set_targets"],
                    "unsupported_fields": source_exercise["unsupported_fields"],
                    "prescriptions": prescriptions,
                }
            )
        days.append(
            {
                "number": day_number,
                "routine_id": source_routine["id"],
                "source_hash": source_routine["source_hash"],
                "focus": source_routine["title"],
                "muscles": source_routine["muscle_summary"],
                "estimated_duration_minutes": source_routine[
                    "estimated_duration_minutes"
                ],
                "exercises": exercises,
            }
        )

    cadence_sessions = int(cadence_preview.get("sessions_per_week") or 0)
    if request.sessions_per_week is not None:
        sessions_per_week = request.sessions_per_week
    elif inferred.sessions_per_week > 0:
        sessions_per_week = max(1, round(inferred.sessions_per_week))
    else:
        sessions_per_week = cadence_sessions or min(7, len(selected))
    analysis = {
        **history,
        "split_type": inferred.split_type,
        "observed_sessions_per_week": inferred.sessions_per_week,
        "planned_sessions_per_week": sessions_per_week,
        "scheduled_sessions_per_week": cadence_sessions,
        "selected_routine_count": len(selected),
        "next_observed_routine": (
            inferred.next_routine.title if inferred.next_routine else None
        ),
        "exercise_baselines": baselines,
        "priority_coverage": coverage,
        "workload_confidence": (
            "low"
            if workload["uncertainty"] or history["confidence"] == "low"
            else "medium"
            if history["confidence"] == "medium"
            else "high"
        ),
    }

    warnings: list[str] = list(cadence_preview["warnings"])
    warnings.extend(muscle_sets["warnings"])
    warnings.extend(workload["warnings"])
    if history["confidence"] == "low" and not any(
        block.get("calibration") for block in blocks
    ):
        warnings.append(
            "Training-history confidence is low but no calibration block could be "
            "inserted for this duration. Review the opening prescriptions early."
        )
    assumptions = [
        "Original Hevy routines remain read-only; this preview is a prescription overlay.",
        "Routine identity and activation safety use Hevy provider IDs plus source hashes, never titles alone.",
        "Set changes are bounded to one fewer or two more than the imported source within this first engine version.",
        (
            "Weekly muscle-set estimates use the fractional model (direct 1.0, "
            "secondary 0.5) and are review triggers, not prescriptions."
        ),
        (
            "Week-one workload is bounded by recent tolerated training when it is "
            "available; the estimate is a planning aid, not exact biological stimulus."
        ),
    ]
    if request.fatigue_management == "adaptive":
        assumptions.append(
            "Adaptive fatigue management only adjusts future prescriptions inside "
            "the configured bounds and never changes completed weeks."
        )
    else:
        assumptions.append(
            "Planned fatigue management keeps the block structure fixed; no "
            "automatic recovery week is inserted."
        )
    if request.constraints.concurrent_training:
        assumptions.append(
            "Concurrent training is recorded as context only; no automatic volume "
            "reduction is applied."
        )
    if history["confidence"] == "low":
        has_calibration = any(block.get("calibration") for block in blocks)
        warnings.append(
            "Training-history confidence is low. Opening prescriptions are "
            "conservative"
            + (
                " and a calibration block is included."
                if has_calibration
                else " and should be reviewed after the first two weeks."
            )
        )
    if (
        request.adaptation_aggressiveness == "aggressive"
        and history["confidence"] != "high"
    ):
        warnings.append(
            "Aggressive progression was requested but history confidence is not "
            "high. The engine keeps automatic changes inside its bounds and will "
            "not fabricate precise baselines."
        )
    if request.max_session_minutes is not None:
        too_long = [
            routine["title"]
            for routine in source_snapshots
            if routine["estimated_duration_minutes"] > request.max_session_minutes
        ]
        if too_long:
            warnings.append(
                "Estimated source duration exceeds the selected session limit for: "
                + ", ".join(too_long)
            )
    if inferred.sessions_per_week <= 0:
        warnings.append(
            "No reliable recent frequency was detected; the selected cadence is used as the initial weekly schedule."
        )
    if (
        request.sessions_per_week is not None
        and cadence_sessions
        and request.sessions_per_week != cadence_sessions
    ):
        warnings.append(
            f"The requested {request.sessions_per_week} sessions per week differ "
            f"from the {cadence_sessions} scheduled training days. The selected "
            "training days are used."
        )
    if coverage["missing_muscles"]:
        warnings.append(
            "No selected routine trains the priority muscles: "
            + ", ".join(coverage["missing_muscles"])
        )
    if coverage["missing_exercises"]:
        warnings.append(
            "No selected routine contains the priority exercises: "
            + ", ".join(coverage["missing_exercises"])
        )
    weekly_sets = muscle_sets.get("weekly_sets") or {}
    for muscle in request.priority_muscles:
        estimate = weekly_sets.get(muscle)
        if estimate is not None and float(estimate) < MIN_RECOVERABLE_SETS:
            warnings.append(
                f"Priority muscle '{muscle}' is estimated at {estimate} weekly "
                "sets, below the reference range used for review."
            )
    low_confidence_exercises = sorted(
        {
            str(exercise.get("template_id"))
            for day in days
            for exercise in day.get("exercises") or []
            if exercise.get("role_confidence") == "low"
        }
    )
    if low_confidence_exercises:
        warnings.append(
            "Exercise-role confidence is low for: "
            + ", ".join(low_confidence_exercises)
            + ". Review the role before overriding it."
        )
    unsupported_fields = sorted(
        {
            field
            for snapshot in source_snapshots
            for field in snapshot.get("unsupported_fields") or []
        }
    )
    if unsupported_fields:
        warnings.append(
            "Some Hevy set fields are retained but not interpreted by this engine "
            "version: " + ", ".join(unsupported_fields)
        )
    if request.constraints.injuries:
        warnings.append(
            "Saved injury or medical constraints are shown as context only. The "
            "engine does not diagnose or automatically remove exercises; review the "
            "plan with an appropriate professional."
        )
    if request.event_date is not None and request.goal != "strength_test":
        programme_end = request.start_date + timedelta(
            weeks=request.duration_weeks
        )
        if request.event_date > programme_end:
            warnings.append(
                "The test or competition date falls after the programme ends, so "
                "no taper block is generated for it."
            )
    if request.goal == "strength_test" and request.event_date is None:
        warnings.append(
            "No test date was supplied for a strength-test goal, so no taper or "
            "peak block is generated. Add a test date to plan the taper."
        )
    if drift["state"] == "changed":
        warnings.append(
            "The active programme uses different Hevy routine revisions. "
            "Review the source changes before activating a new programme."
        )

    programme_spec = {
        **request.model_dump(mode="json"),
        "selected_routine_ids": list(request.selected_routine_ids),
        "selected_routine_order": [
            {"routine_id": entry["routine_id"], "position": entry["position"]}
            for entry in cadence_preview["rotation"]
        ],
        "goal_label": _GOALS[request.goal]["label"],
        "engine_version": ENGINE_VERSION,
        "role_classifier_version": ROLE_CLASSIFIER_VERSION,
        "workload_model_version": WORKLOAD_MODEL_VERSION,
    }
    token_inputs = {
        "engine_version": ENGINE_VERSION,
        "programme_spec": programme_spec,
        "source_hashes": [
            {
                "routine_id": routine["id"],
                "source_hash": routine["source_hash"],
            }
            for routine in source_snapshots
        ],
    }
    preview_token = _canonical_hash(token_inputs)

    preview = {
        "name": f"{_GOALS[request.goal]['label']} · Hevy routines",
        "source": "hevy",
        "engine_version": ENGINE_VERSION,
        "preview_token": preview_token,
        "cycle_weeks": request.duration_weeks,
        "total_days": len(days),
        "programme_spec": programme_spec,
        "goal_profile": {
            "key": request.goal,
            "label": _GOALS[request.goal]["label"],
            "description": _GOALS[request.goal]["description"],
            "target_envelopes": _GOAL_ENVELOPES[request.goal],
        },
        "engine_config": {
            "engine_version": ENGINE_VERSION,
            "role_classifier_version": ROLE_CLASSIFIER_VERSION,
            "workload_model_version": WORKLOAD_MODEL_VERSION,
            "min_duration_weeks": MIN_DURATION_WEEKS,
            "max_duration_weeks": MAX_DURATION_WEEKS,
            "allowed_set_change": dict(ALLOWED_SET_CHANGE),
            "max_week_one_set_increase": MAX_WEEK_ONE_SET_INCREASE,
            "max_sets_per_exercise": MAX_SETS_PER_EXERCISE,
            "max_source_work_sets_per_routine": MAX_SOURCE_WORK_SETS_PER_ROUTINE,
            "load_change_percent": dict(LOAD_CHANGE_PERCENT),
            "min_recoverable_sets": MIN_RECOVERABLE_SETS,
            "max_recoverable_sets": MAX_RECOVERABLE_SETS,
        },
        "role_classifier": {
            "version": ROLE_CLASSIFIER_VERSION,
            "roles": [
                "primary_strength",
                "secondary_compound",
                "hypertrophy_accessory",
                "power_skill",
                "bodyweight_progression",
                "duration_distance",
                "rehab_prehab",
                "conditioning",
            ],
            "evidence_fields": [
                "exercise_type",
                "position",
                "primary_muscle",
                "secondary_muscle_count",
                "source_sets",
                "rest_seconds",
                "goal",
                "history_exposures",
                "priority_muscle",
                "priority_exercise",
                "user_override",
            ],
        },
        "source_snapshots": source_snapshots,
        "analysis": analysis,
        "cadence": cadence_preview,
        "schedule": schedule,
        "muscle_sets": muscle_sets,
        "workload": workload,
        "drift": drift,
        "blocks": blocks,
        "days": days,
        "warnings": warnings,
        "blocking_violations": [],
        "assumptions": assumptions,
        "rules": [
            "Progressions are deterministic, modality-aware and bounded.",
            "Completed weeks are historical records and are never regenerated by this preview.",
            "Low-confidence or structurally changed source data must be reviewed rather than silently merged.",
            "Every prescription carries its role evidence, confidence and rule version.",
        ],
    }
    preview["blocking_violations"] = validate_programme_preview(preview)
    return preview
