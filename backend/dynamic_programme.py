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
from datetime import date, timedelta
from typing import Any, Literal

from hevy_reader import CompletedWorkout, HevyTrainingData, Routine, RoutineExercise
from programme_inference import infer_programme
from programme_validator import validate_programme
from pydantic import BaseModel, Field, model_validator

ENGINE_VERSION = "hevy-programme-v2"
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
    fatigue_management: FatigueManagement = "planned"
    constraints: ProgrammeConstraints | list[Any] | dict[str, Any] | None = None
    rules: dict[str, Any] | None = None

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


def goal_options() -> list[dict[str, str]]:
    """Return stable user-facing goal choices."""

    return [
        {
            "key": key,
            "label": profile["label"],
            "description": profile["description"],
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


def serialise_hevy_source(
    data: HevyTrainingData,
    profile_rules: Any = None,
    training_rules: Any = None,
) -> dict[str, Any]:
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
    payload = {
        "username": data.username,
        "workout_count": data.workout_count,
        "routine_count": len(routines),
        "recent_workout_count": len(data.recent_workouts),
        "folders": folders,
        "routines": routines,
    }
    validation = validate_programme(
        payload,
        profile_rules=profile_rules,
        training_rules=training_rules,
    )
    payload["blocking_violations"] = validation.blocking_violations
    return payload


_DRIFT_EXERCISE_FIELDS = (
    "template_id",
    "title",
    "exercise_type",
    "sets",
    "set_targets",
    "rest_seconds",
    "notes",
    "superset_id",
    "target_weight_kg",
    "target_reps",
    "target_rep_range",
)


def _routine_content_signature(view: dict[str, Any]) -> str:
    """Hash routine content while ignoring provider revision timestamps."""
    exercises = [
        {field: exercise.get(field) for field in _DRIFT_EXERCISE_FIELDS}
        for exercise in view.get("exercises") or []
        if isinstance(exercise, dict)
    ]
    return _canonical_hash(
        {
            "folder_id": view.get("folder_id"),
            "exercises": exercises,
        }
    )


def detect_source_drift(
    active_definition: dict[str, Any] | None,
    live_source: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare an active programme's source snapshots with the live Hevy library.

    The activated prescription overlay is never rewritten silently. This report
    names every selected routine that was renamed, edited or removed in Hevy so
    the user can compare and deliberately generate a fresh programme version.
    """

    snapshots = [
        snapshot
        for snapshot in (active_definition or {}).get("source_snapshots") or []
        if isinstance(snapshot, dict) and snapshot.get("id")
    ]
    summary = {"renamed": 0, "content_changed": 0, "removed": 0}
    if not snapshots:
        return {
            "status": "no_active_programme",
            "has_changes": False,
            "routines": [],
            "summary": summary,
        }

    live_by_id = {
        str(routine.get("id")): routine
        for routine in (live_source or {}).get("routines") or []
        if isinstance(routine, dict) and routine.get("id")
    }
    routines: list[dict[str, Any]] = []
    for position, snapshot in enumerate(snapshots, start=1):
        routine_id = str(snapshot.get("id"))
        current = live_by_id.get(routine_id)
        if current is None:
            state = "removed"
        elif _routine_content_signature(snapshot) != _routine_content_signature(
            current
        ):
            state = "content_changed"
        elif snapshot.get("title") != current.get("title"):
            state = "renamed"
        else:
            state = "unchanged"
        if state != "unchanged":
            summary[state] += 1
        routines.append(
            {
                "position": position,
                "routine_id": routine_id,
                "title": snapshot.get("title"),
                "current_title": current.get("title") if current else None,
                "state": state,
                "source_hash": snapshot.get("source_hash"),
                "current_hash": current.get("source_hash") if current else None,
                "source_updated_at": snapshot.get("updated_at"),
                "current_updated_at": (
                    current.get("updated_at") if current else None
                ),
                "source_exercise_count": snapshot.get("exercise_count"),
                "current_exercise_count": (
                    current.get("exercise_count") if current else None
                ),
            }
        )

    if summary["content_changed"] or summary["removed"]:
        status = "action_required"
    elif summary["renamed"]:
        status = "review_recommended"
    else:
        status = "unchanged"
    return {
        "status": status,
        "has_changes": status != "unchanged",
        "routines": routines,
        "summary": summary,
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


def _allocate_blocks(
    duration_weeks: int,
    goal: str,
    start_date: date,
) -> list[dict[str, Any]]:
    profile = _GOALS.get(goal)
    if profile is None:
        raise ValueError(f"Unsupported programme goal: {goal}")

    phase_lengths = _allocate_phase_weeks(duration_weeks, profile["phases"])
    blocks: list[dict[str, Any]] = []
    first_week = 1
    block_number = 1

    for phase_key, phase_label, phase_length in phase_lengths:
        waves = _split_into_waves(phase_length)
        for wave_index, wave_length in enumerate(waves, start=1):
            end_week = first_week + wave_length - 1
            settings = _PHASE_SETTINGS[phase_key]
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
                    "source": "hevy_history",
                },
            )
            entry["exposures"] += 1
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


def _classify_role(
    exercise: dict[str, Any],
    position: int,
    goal: str,
) -> tuple[str, str]:
    exercise_type = str(exercise["exercise_type"]).lower()
    title = str(exercise["title"]).lower()
    muscle = str(exercise["primary_muscle_group"]).lower()
    notes = str(exercise.get("notes") or "").lower()

    if exercise_type in {"duration", "distance_duration", "distance"}:
        return (
            "duration_distance",
            "Hevy records this exercise by duration or distance, so load-based strength maths is disabled.",
        )
    if muscle == "cardio":
        return (
            "conditioning",
            "The source exercise is classified as conditioning.",
        )
    if any(
        token in f"{title} {notes}"
        for token in ("rehab", "prehab", "mobility", "physio")
    ):
        return (
            "rehab_prehab",
            "The source title or notes indicate rehabilitation, prehabilitation or mobility work.",
        )
    if exercise_type in {"bodyweight_reps", "reps_only", "assisted_bodyweight"}:
        return (
            "bodyweight_progression",
            "The source modality is bodyweight or assisted bodyweight.",
        )
    if goal == "power_performance" and (
        position == 0
        or any(
            token in title for token in ("jump", "throw", "clean", "snatch", "sprint")
        )
    ):
        return (
            "power_skill",
            "The programme goal and source position favour a low-fatigue power or skill role.",
        )
    if position == 0 and exercise_type in {"weight_reps", "weight_reps_duration"}:
        return (
            "primary_strength",
            "The first loaded movement in the routine is treated as its primary strength exposure.",
        )
    if position <= 2 and exercise_type in {"weight_reps", "weight_reps_duration"}:
        return (
            "secondary_compound",
            "This early loaded movement supports the routine's primary work.",
        )
    return (
        "hypertrophy_accessory",
        "This source exercise is retained as accessory or hypertrophy work.",
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


def _progression_for(role: str) -> dict[str, Any]:
    if role == "primary_strength":
        return {
            "method": "autoregulated_rep_range",
            "rule": "Add the smallest practical load after two successful exposures at the top of the range with the target RIR; old or reduce after repeated misses.",
        }
    if role == "secondary_compound":
        return {
            "method": "bounded_double_progression",
            "rule": "Add reps inside the range first, then the smallest practical load while preserving technique.",
        }
    if role == "bodyweight_progression":
        return {
            "method": "bodyweight_progression",
            "rule": "Progress reps first, then added load, less assistance, range, tempo or variation as appropriate.",
        }
    if role == "duration_distance":
        return {
            "method": "duration_distance_progression",
            "rule": "Progress duration, distance, pace, resistance or work-rest ratio; never calculate e1RQ.",
        }
    if role == "conditioning":
        return {
            "method": "conditioning_progression",
            "rule": "Progress one of duration, pace, resistance or work-rest density while preserving recovery.",
        }
    if role == "power_skill":
        return {
            "method": "quality_velocity_progression",
            "rule": "Progress only while repetitions remain fast and technically consistent; stop before meaningful speed loss.",
        }
    if role == "rehab_prehab":
        return {
            "method": "tolerance_progression",
            "rule": "Progress range, control or load only when symptoms and technique remain acceptable.",
        }
    return {
        "method": "double_progression",
        "rule": "Accumulate clean reps inside the range, then add the smallest practical load after repeated successful exposures.",
    }


def _target_sets(source_sets: int, volume_multiplier: float) -> int:
    source_sets = max(1, source_sets)
    proposed = round(source_sets * volume_multiplier)
    return max(source_sets - 1, min(source_sets + 2, max(1, proposed)))


def _prescriptions_for_exercise(
    source: dict[str, Any],
    position: int,
    goal: str,
    blocks: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    role, role_reason = _classify_role(source, position, goal)
    progression = _progression_for(role)
    prescriptions = []
    for block in blocks:
        prescriptions.append(
            {
                "block_number": block["number"],
                "block_key": block["key"],
                "sets": _target_sets(
                    int(source.get("sets") or 1),
                    float(block["volume_multiplier"]),
                ),
                "rep_range": _rep_range_for(role, block["key"], source),
                "target_rir": block["target_rir"],
                "progression": progression,
                "allowed_set_change_from_source": {"minimum": -1, "maximum": 2},
            }
        )
    return role, role_reason, prescriptions


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
            sets = max(0, int(exercise.get("sets") or 0))
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
            "Direct sets count as 1.0 and meaningful secondary contributions "
            "as 0.5 for the selected weekly schedule."
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


def validate_programme_preview(preview: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the blocking violations of a generated programme preview.

    This is the shared validator used by both the preview and the activation
    route.  A violation always identifies the affected field or routine so the
    UI can point at it.
    """

    violations: list[dict[str, Any]] = []
    spec = preview.get("programme_spec") or {}
    cadence_preview = preview.get("cadence") or {}
    constraints = spec.get("constraints") or {}
    if not isinstance(constraints, dict):
        constraints = {}
    rotation = cadence_preview.get("rotation") or []

    if not rotation:
        violations.append(
            {
                "code": "no_scheduled_routines",
                "field": "selected_routine_ids",
                "routine_id": None,
                "message": "Select at least one Hevy routine before generating a programme.",
            }
        )
        return violations

    unscheduled = [
        entry
        for entry in rotation
        if not entry.get("scheduled") and entry.get("role") == "required"
    ]
    for entry in unscheduled:
        violations.append(
            {
                "code": "routine_not_scheduled",
                "field": "cadence.weekdays",
                "routine_id": entry.get("routine_id"),
                "message": (
                    f"The routine '{entry.get('title')}' is required but has no "
                    "training day. Add a weekday or change its role."
                ),
            }
        )

    sessions_per_week = int(cadence_preview.get("sessions_per_week") or 0)
    limit = constraints.get("max_sessions_per_week")
    if limit is not None and sessions_per_week > int(limit):
        violations.append(
            {
                "code": "session_limit_exceeded",
                "field": "constraints.max_sessions_per_week",
                "routine_id": None,
                "message": (
                    f"The programme schedules {sessions_per_week} sessions per week "
                    f"but the confirmed limit is {limit}."
                ),
            }
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
                {
                    "code": "event_before_start",
                    "field": "event_date",
                    "routine_id": None,
                    "message": (
                        "The test or competition date is before the programme "
                        "start date."
                    ),
                }
            )
        elif (
            start_date
            and event_date
            and spec.get("goal") == "strength_test"
            and preview.get("cycle_weeks")
        ):
            programme_end = start_date + timedelta(weeks=int(preview["cycle_weeks"]))
            if event_date > programme_end:
                violations.append(
                    {
                        "code": "event_outside_programme",
                        "field": "event_date",
                        "routine_id": None,
                        "message": (
                            "The test date falls after the programme ends. Extend "
                            "the duration or choose a different date."
                        ),
                    }
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
    profile_rules: Any = None,
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

    structured_constraints = (
        request.constraints
        if isinstance(request.constraints, ProgrammeConstraints)
        else ProgrammeConstraints()
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
    blocks = _allocate_blocks(
        request.duration_weeks,
        request.goal,
        request.start_date,
    )
    history = _history_analysis(data, selected)
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

    days: list[dict[str, Any]] = []
    for day_number, source_routine in enumerate(source_snapshots, start=1):
        exercises: list[dict[str, Any]] = []
        for position, source_exercise in enumerate(source_routine["exercises"]):
            role, role_reason, prescriptions = _prescriptions_for_exercise(
                source_exercise,
                position,
                request.goal,
                blocks,
            )
            opening = prescriptions[0]
            exercises.append(
                {
                    "name": source_exercise["title"],
                    "template_id": source_exercise["template_id"],
                    "exercise_type": source_exercise["exercise_type"],
                    "equipment": source_exercise.get("equipment"),
                    "role": role,
                    "role_reason": role_reason,
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
        "exercise_baselines": _exercise_baselines(data, selected),
        "priority_coverage": coverage,
    }

    warnings: list[str] = list(cadence_preview["warnings"])
    warnings.extend(muscle_sets["warnings"])
    assumptions = [
        "Original Hevy routines remain read-only; this preview is a prescription overlay.",
        "Routine identity and activation safety use Hevy provider IDs plus source hashes, never titles alone.",
        "Set changes are bounded to one fewer or two more than the imported source within this first engine version.",
        (
            "Weekly muscle-set estimates use the fractional model (direct 1.0, "
            "secondary 0.5) and are review triggers, not prescriptions."
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
    if structured_constraints.concurrent_training:
        assumptions.append(
            "Concurrent training is recorded as context only; no automatic volume "
            "reduction is applied."
        )
    if history["confidence"] == "low":
        warnings.append(
            "Training-history confidence is low. Opening prescriptions are conservative and should be reviewed after the first two weeks."
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
            "No test date was supplied for a strength-test goal. The taper block "
            "is a conservative estimate and not a confirmed peak."
        )
    if drift["state"] == "changed":
        warnings.append(
            "The active programme uses different Hevy routine revisions. "
            "Review the source changes before activating a new programme."
        )

    blocking_violations: list[dict[str, Any]] = []

    programme_spec = {
        **request.model_dump(mode="json"),
        "selected_routine_ids": list(request.selected_routine_ids),
        "selected_routine_order": [
            {"routine_id": entry["routine_id"], "position": entry["position"]}
            for entry in cadence_preview["rotation"]
        ],
        "goal_label": _GOALS[request.goal]["label"],
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

    preview: dict[str, Any] = {
        "name": f"{_GOALS[request.goal]['label']} · Hevy routines",
        "source": "hevy",
        "engine_version": ENGINE_VERSION,
        "preview_token": preview_token,
        "cycle_weeks": request.duration_weeks,
        "total_days": len(days),
        "programme_spec": programme_spec,
        "source_snapshots": source_snapshots,
        "analysis": analysis,
        "cadence": cadence_preview,
        "schedule": schedule,
        "muscle_sets": muscle_sets,
        "drift": drift,
        "blocks": blocks,
        "days": days,
        "warnings": warnings,
        "blocking_violations": blocking_violations,
        "assumptions": assumptions,
        "rules": [
            "Progressions are deterministic, modality-aware and bounded.",
            "Completed weeks are historical records and are never regenerated by this preview.",
            "Low-confidence or structurally changed source data must be reviewed rather than silently merged.",
        ],
    }

    request_rules = (
        request.constraints
        if request.constraints is not None
        and not isinstance(request.constraints, ProgrammeConstraints)
        else None
    )
    effective_profile_rules = (
        profile_rules if profile_rules is not None else request_rules
    )

    local_violations = validate_programme_preview(preview)
    try:
        validation = validate_programme(
            preview,
            profile_rules=effective_profile_rules,
            training_rules=request.rules,
        )
        if (
            request_rules is not None
            and profile_rules is not None
            and request_rules is not effective_profile_rules
        ):
            extra = validate_programme(preview, profile_rules=request_rules)
            validation.blocking_violations.extend(
                violation
                for violation in extra.blocking_violations
                if violation not in validation.blocking_violations
            )
            validation.warnings.extend(
                warning
                for warning in extra.warnings
                if warning not in validation.warnings
            )
    except ValueError as exc:
        if request.rules is not None:
            raise ValueError(
                f"Invalid structured training rules; constraint check failed: {exc}"
            ) from exc
        raise

    blocking = list(local_violations)
    blocking.extend(
        violation
        for violation in validation.blocking_violations
        if violation not in blocking
    )
    preview["blocking_violations"] = blocking
    if validation.warnings:
        preview["warnings"].extend(
            warning
            for warning in validation.warnings
            if warning not in preview["warnings"]
        )

    return preview
