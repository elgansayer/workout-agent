"""Deterministic programmes generated from user-selected Hevy routines.

The imported Hevy routines remain immutable source material.  This module builds
an explainable prescription overlay for a requested goal and duration without
depending on the legacy static programme.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from typing import Any, Literal

from hevy_reader import HevyTrainingData, Routine, RoutineExercise
from programme_inference import infer_programme
from pydantic import BaseModel, Field

ENGINE_VERSION = "hevy-programme-v1"
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


class ProgrammePreviewRequest(BaseModel):
    """Validated inputs for a generated Hevy-native programme preview."""

    selected_routine_ids: list[str] = Field(min_length=1)
    duration_weeks: int = Field(
        default=12, ge=MIN_DURATION_WEEKS, le=MAX_DURATION_WEEKS
    )
    goal: GoalKey = "general_fitness"
    start_date: date = Field(default_factory=date.today)
    sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    experience: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    max_session_minutes: int | None = Field(default=None, ge=20, le=240)
    adaptation_aggressiveness: Aggressiveness = "balanced"


class ProgrammeActivationRequest(ProgrammePreviewRequest):
    """Preview inputs plus the source-bound token being activated."""

    preview_token: str = Field(min_length=16)


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


def _serialise_exercise(
    exercise: RoutineExercise,
    data: HevyTrainingData,
) -> dict[str, Any]:
    template = data.exercise_templates.get(exercise.template_id)
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
        "set_targets": [_normalise_set_target(item) for item in exercise.set_targets],
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


def _serialise_routine(routine: Routine, data: HevyTrainingData) -> dict[str, Any]:
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
    return {
        **source_payload,
        "folder_name": data.folders.get(routine.folder_id)
        if routine.folder_id is not None
        else None,
        "exercise_count": len(exercises),
        "work_set_count": work_sets,
        "estimated_duration_minutes": max(10, round(estimated_seconds / 60)),
        "muscle_summary": direct_muscles,
        "source_hash": _canonical_hash(source_payload),
    }


def serialise_hevy_source(data: HevyTrainingData) -> dict[str, Any]:
    """Return the exact selectable Hevy source view used by the builder."""

    routines = [_serialise_routine(routine, data) for routine in data.routines]
    return {
        "username": data.username,
        "workout_count": data.workout_count,
        "routine_count": len(routines),
        "recent_workout_count": len(data.recent_workouts),
        "folders": [
            {"id": folder_id, "title": title}
            for folder_id, title in sorted(data.folders.items())
        ],
        "routines": routines,
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


def build_programme_preview(
    data: HevyTrainingData,
    request: ProgrammePreviewRequest,
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
    blocks = _allocate_blocks(
        request.duration_weeks,
        request.goal,
        request.start_date,
    )
    history = _history_analysis(data, selected)

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
                    "role": role,
                    "role_reason": role_reason,
                    "sets": opening["sets"],
                    "rep_range": opening["rep_range"],
                    "scheme": f"{opening['sets']} × {opening['rep_range']}",
                    "note": source_exercise.get("notes") or "",
                    "rest_seconds": source_exercise["rest_seconds"],
                    "source_sets": source_exercise["sets"],
                    "source_targets": source_exercise["set_targets"],
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

    if request.sessions_per_week is not None:
        sessions_per_week = request.sessions_per_week
    elif inferred.sessions_per_week > 0:
        sessions_per_week = max(1, round(inferred.sessions_per_week))
    else:
        sessions_per_week = min(7, len(selected))
    analysis = {
        **history,
        "split_type": inferred.split_type,
        "observed_sessions_per_week": inferred.sessions_per_week,
        "planned_sessions_per_week": sessions_per_week,
        "selected_routine_count": len(selected),
        "next_observed_routine": (
            inferred.next_routine.title if inferred.next_routine else None
        ),
    }

    warnings: list[str] = []
    assumptions = [
        "Original Hevy routines remain read-only; this preview is a prescription overlay.",
        "Routine identity and activation safety use Hevy provider IDs plus source hashes, never titles alone.",
        "Set changes are bounded to one fewer or two more than the imported source within this first engine version.",
    ]
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
            "No reliable recent frequency was detected; the selected routine count is used as the initial weekly cadence."
        )

    programme_spec = {
        **request.model_dump(mode="json"),
        "selected_routine_ids": list(request.selected_routine_ids),
        "selected_routine_order": [
            {"routine_id": routine.id, "position": index + 1}
            for index, routine in enumerate(selected)
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

    return {
        "name": f"{_GOALS[request.goal]['label']} · Hevy routines",
        "source": "hevy",
        "engine_version": ENGINE_VERSION,
        "preview_token": preview_token,
        "cycle_weeks": request.duration_weeks,
        "total_days": len(days),
        "programme_spec": programme_spec,
        "source_snapshots": source_snapshots,
        "analysis": analysis,
        "blocks": blocks,
        "days": days,
        "warnings": warnings,
        "blocking_violations": [],
        "assumptions": assumptions,
        "rules": [
            "Progressions are deterministic, modality-aware and bounded.",
            "Completed weeks are historical records and are never regenerated by this preview.",
            "Low-confidence or structurally changed source data must be reviewed rather than silently merged.",
        ],
    }
