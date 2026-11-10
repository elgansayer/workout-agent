"""Canonical programme definition for Workout Agent.

Provides one canonical, synthetic, provider-neutral training programme definition
that produces:
1. Documentation examples (e.g. current-workout.md).
2. Seeded database rows and migrations.
3. Live web pages (Plan, Dashboard, Coach context).
4. Workout exports.
5. Runtime test fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from training_export import ExportedWorkout

CANONICAL_PROGRAMME_ID = "canonical-hypertrophy-6day"
CANONICAL_TEMPLATE_KEY = "canonical:hypertrophy-6day"
CANONICAL_SPLIT_NAME = "Hypertrophy · Six-day split"
CANONICAL_GOAL = "hypertrophy"
CANONICAL_GOAL_LABEL = "Hypertrophy"
CANONICAL_CYCLE_WEEKS = 12
CANONICAL_DAYS_PER_WEEK = 6
CANONICAL_START_DATE = date(2026, 9, 1)


@dataclass(frozen=True, slots=True)
class CanonicalExerciseSpec:
    template_id: str
    name: str
    muscle_group: str
    sets: int
    rep_range: str
    rest_seconds: int = 90
    weight_kg: float = 20.0
    exercise_type: str = "weight_reps"


CANONICAL_CHEST_BACK_EXERCISES: list[CanonicalExerciseSpec] = [
    CanonicalExerciseSpec("db_fly", "Incline dumbbell fly", "chest", 3, "12-15", 90, 14.0),
    CanonicalExerciseSpec("mach_press", "Incline machine press", "chest", 3, "10-12", 120, 50.0),
    CanonicalExerciseSpec("cs_row", "Chest-supported row", "upper_back", 3, "10-12", 90, 45.0),
    CanonicalExerciseSpec("lat_pd", "Lat pulldown", "lats", 3, "10-15", 90, 55.0),
    CanonicalExerciseSpec("sa_pulldown", "Straight-arm cable pulldown", "lats", 2, "12-15", 60, 25.0),
]

CANONICAL_SHOULDERS_ARMS_EXERCISES: list[CanonicalExerciseSpec] = [
    CanonicalExerciseSpec("lat_raise", "Cable lateral raise", "shoulders", 3, "12-20", 60, 10.0),
    CanonicalExerciseSpec("rev_pec", "Reverse pec deck", "shoulders", 3, "12-20", 60, 40.0),
    CanonicalExerciseSpec("db_curl", "Incline dumbbell curl", "biceps", 3, "10-15", 90, 12.0),
    CanonicalExerciseSpec("tri_ext", "Cable triceps extension", "triceps", 3, "10-15", 60, 25.0),
    CanonicalExerciseSpec("cable_curl", "Cable curl", "biceps", 2, "12-15", 60, 20.0),
]

CANONICAL_LEGS_TRUNK_EXERCISES: list[CanonicalExerciseSpec] = [
    CanonicalExerciseSpec("leg_curl", "Lying leg curl", "hamstrings", 3, "10-15", 90, 40.0),
    CanonicalExerciseSpec("leg_ext", "Leg extension", "quadriceps", 3, "10-15", 90, 50.0),
    CanonicalExerciseSpec("leg_press", "Leg press", "quadriceps", 3, "10-15", 120, 120.0),
    CanonicalExerciseSpec("rdl", "Romanian deadlift", "hamstrings", 3, "8-12", 120, 80.0),
    CanonicalExerciseSpec("calf_raise", "Calf raise", "calves", 3, "12-20", 60, 60.0),
    CanonicalExerciseSpec("leg_raise", "Hanging leg raise", "abs", 3, "10-15", 60, 0.0),
    CanonicalExerciseSpec("cable_crunch", "Cable crunch", "abs", 3, "10-15", 60, 30.0),
]

CANONICAL_ROUTINES_SPECS: list[tuple[str, str, list[CanonicalExerciseSpec]]] = [
    ("routine-chest-back", "Chest and back", CANONICAL_CHEST_BACK_EXERCISES),
    ("routine-shoulders-arms", "Shoulders and arms", CANONICAL_SHOULDERS_ARMS_EXERCISES),
    ("routine-legs-trunk", "Legs and trunk", CANONICAL_LEGS_TRUNK_EXERCISES),
]


def _parse_target_reps(rep_range: str) -> int:
    parts = rep_range.split("-")
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return 10


def get_canonical_training_data() -> HevyTrainingData:
    """Return synthetic Hevy training data for the canonical 6-day split."""
    templates: dict[str, ExerciseTemplate] = {}
    routines: list[Routine] = []

    for routine_id, title, exercise_specs in CANONICAL_ROUTINES_SPECS:
        routine_exercises: list[RoutineExercise] = []
        for spec in exercise_specs:
            if spec.template_id not in templates:
                templates[spec.template_id] = ExerciseTemplate(
                    id=spec.template_id,
                    title=spec.name,
                    exercise_type=spec.exercise_type,
                    primary_muscle_group=spec.muscle_group,
                )
            target_reps = _parse_target_reps(spec.rep_range)
            routine_exercises.append(
                RoutineExercise(
                    template_id=spec.template_id,
                    title=spec.name,
                    sets=spec.sets,
                    rest_seconds=spec.rest_seconds,
                    target_reps=target_reps,
                    set_targets=[
                        {"type": "normal", "reps": target_reps, "weight_kg": spec.weight_kg}
                        for _ in range(spec.sets)
                    ],
                )
            )
        routines.append(
            Routine(
                id=routine_id,
                title=title,
                exercises=routine_exercises,
            )
        )

    recent_workouts = [
        CompletedWorkout(
            id=f"canonical-workout-{idx}",
            title=routines[idx % len(routines)].title,
            start_time=f"2026-08-{10 + idx:02d}T09:00:00Z",
            end_time=f"2026-08-{10 + idx:02d}T10:15:00Z",
        )
        for idx in range(12)
    ]

    return HevyTrainingData(
        username="synthetic-athlete",
        workout_count=24,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=recent_workouts,
        folders={},
    )


def get_canonical_programme_definition(
    start_date: date | None = None,
) -> dict[str, Any]:
    """Produce the canonical active programme definition."""
    effective_start = start_date or CANONICAL_START_DATE

    blocks = [
        {
            "number": 1,
            "name": "Foundation volume",
            "focus": "Establish recoverable weekly volume and progressive movement consistency.",
            "start_week": 1,
            "end_week": 4,
            "weeks": "1–4",
            "volume_multiplier": 1.0,
            "target_rir": 3,
            "rep_emphasis": "10–15",
            "specificity": "General hypertrophy",
        },
        {
            "number": 2,
            "name": "Overload and specialisation",
            "focus": "Progressive overload across target muscle groups with rising intensity.",
            "start_week": 5,
            "end_week": 9,
            "weeks": "5–9",
            "volume_multiplier": 1.1,
            "target_rir": 2,
            "rep_emphasis": "8–12",
            "specificity": "Targeted hypertrophy",
        },
        {
            "number": 3,
            "name": "Resensitisation and assessment",
            "focus": "Consolidate adaptations, dissipate accumulated fatigue, and review baseline.",
            "start_week": 10,
            "end_week": 12,
            "weeks": "10–12",
            "volume_multiplier": 0.9,
            "target_rir": 2,
            "rep_emphasis": "10–15",
            "specificity": "Deload and reassessment",
        },
    ]

    days: list[dict[str, Any]] = []
    for day_number, (routine_id, title, exercise_specs) in enumerate(CANONICAL_ROUTINES_SPECS, start=1):
        exercises: list[dict[str, Any]] = []
        for spec in exercise_specs:
            prescriptions = [
                {
                    "block_number": 1,
                    "sets": spec.sets,
                    "rep_range": spec.rep_range,
                    "scheme": f"{spec.sets} × {spec.rep_range}",
                    "target_rir": 3,
                },
                {
                    "block_number": 2,
                    "sets": spec.sets,
                    "rep_range": spec.rep_range,
                    "scheme": f"{spec.sets} × {spec.rep_range}",
                    "target_rir": 2,
                },
                {
                    "block_number": 3,
                    "sets": max(1, spec.sets - 1) if spec.sets > 2 else spec.sets,
                    "rep_range": spec.rep_range,
                    "scheme": f"{max(1, spec.sets - 1) if spec.sets > 2 else spec.sets} × {spec.rep_range}",
                    "target_rir": 2,
                },
            ]
            opening = prescriptions[0]
            exercises.append(
                {
                    "name": spec.name,
                    "template_id": spec.template_id,
                    "exercise_type": spec.exercise_type,
                    "role": "hypertrophy",
                    "role_reason": f"{spec.muscle_group} volume targeting",
                    "sets": opening["sets"],
                    "rep_range": opening["rep_range"],
                    "scheme": opening["scheme"],
                    "note": "",
                    "rest_seconds": spec.rest_seconds,
                    "source_sets": spec.sets,
                    "source_targets": [
                        {"type": "normal", "reps": _parse_target_reps(spec.rep_range), "weight_kg": spec.weight_kg}
                        for _ in range(spec.sets)
                    ],
                    "prescriptions": prescriptions,
                }
            )

        days.append(
            {
                "number": day_number,
                "routine_id": routine_id,
                "source_hash": f"canonical-hash-{routine_id}",
                "focus": title,
                "muscles": list({spec.muscle_group for spec in exercise_specs}),
                "estimated_duration_minutes": 60,
                "exercises": exercises,
            }
        )

    programme_spec = {
        "selected_routine_ids": [r[0] for r in CANONICAL_ROUTINES_SPECS],
        "selected_routine_order": [
            {"routine_id": r[0], "position": idx + 1}
            for idx, r in enumerate(CANONICAL_ROUTINES_SPECS)
        ],
        "duration_weeks": CANONICAL_CYCLE_WEEKS,
        "goal": CANONICAL_GOAL,
        "goal_label": CANONICAL_GOAL_LABEL,
        "start_date": effective_start.isoformat(),
        "sessions_per_week": CANONICAL_DAYS_PER_WEEK,
        "experience": "intermediate",
        "adaptation_aggressiveness": "balanced",
    }

    return {
        "name": CANONICAL_SPLIT_NAME,
        "source": "hevy",
        "engine_version": "1.0",
        "preview_token": "canonical-hypertrophy-preview-token",
        "cycle_weeks": CANONICAL_CYCLE_WEEKS,
        "total_days": len(days),
        "programme_spec": programme_spec,
        "source_snapshots": [
            {
                "id": r[0],
                "title": r[1],
                "source_hash": f"canonical-hash-{r[0]}",
                "exercises": [
                    {
                        "template_id": ex.template_id,
                        "title": ex.name,
                        "exercise_type": ex.exercise_type,
                        "sets": ex.sets,
                        "rest_seconds": ex.rest_seconds,
                    }
                    for ex in r[2]
                ],
            }
            for r in CANONICAL_ROUTINES_SPECS
        ],
        "analysis": {
            "confidence": "high",
            "split_type": "6-day push/pull/legs rotation",
            "observed_sessions_per_week": 6.0,
            "planned_sessions_per_week": 6,
            "selected_routine_count": len(days),
            "next_observed_routine": days[0]["focus"],
        },
        "blocks": blocks,
        "days": days,
        "warnings": [],
        "blocking_violations": [],
        "assumptions": [
            "Synthetic canonical training profile for development, seeds, and documentation.",
            "Routines rotate across a 6-day training schedule with Sunday rest.",
        ],
        "rules": [
            "Progressions are deterministic, modality-aware and bounded.",
            "Completed weeks are historical records and are never regenerated.",
            "Original Hevy routines remain read-only; prescriptions are an overlay.",
        ],
    }


def render_documentation_example() -> str:
    """Render the canonical documentation markdown document matching current-workout.md."""
    lines = [
        "<!-- synthetic-profile: true -->",
        "# Synthetic six-day hypertrophy example",
        "",
        "This document is a fictional programme-shaped example used for documentation and development. It is not derived from a real user's training history, health conditions, injuries, biometric data, or preferences.",
        "",
        "Production programme selection and coaching must use the authenticated user's imported routines, programme state, goals, and explicitly supplied constraints.",
        "",
        "## Schedule",
        "",
        "| Day | Focus |",
        "| --- | --- |",
        "| Monday / Thursday | Chest and back |",
        "| Tuesday / Friday | Shoulders and arms |",
        "| Wednesday / Saturday | Legs and trunk |",
        "| Sunday | Rest |",
        "",
        "## Day 1 and 4: chest and back",
        "",
    ]
    for ex in CANONICAL_CHEST_BACK_EXERCISES:
        lines.append(f"- {ex.name}: {ex.sets} sets x {ex.rep_range} reps")

    lines.extend([
        "",
        "## Day 2 and 5: shoulders and arms",
        "",
    ])
    for ex in CANONICAL_SHOULDERS_ARMS_EXERCISES:
        lines.append(f"- {ex.name}: {ex.sets} sets x {ex.rep_range} reps")

    lines.extend([
        "",
        "## Day 3 and 6: legs and trunk",
        "",
    ])
    for ex in CANONICAL_LEGS_TRUNK_EXERCISES:
        lines.append(f"- {ex.name}: {ex.sets} sets x {ex.rep_range} reps")

    lines.extend([
        "",
        "The values above are intentionally generic and synthetic. Tests or documentation that need additional health or workout examples should create minimal fictional fixtures rather than copying user exports.",
    ])

    return "\n".join(lines)


def export_canonical_workout(
    user_id: int,
    day_number: int = 1,
    provider: str = "garmin_training",
    explicit_user_approval: bool = True,
) -> ExportedWorkout:
    """Validate and export a workout derived from the canonical programme."""
    definition = get_canonical_programme_definition()
    days = definition["days"]
    day_index = (day_number - 1) % len(days)
    day = days[day_index]
    exported = ExportedWorkout(
        user_id=user_id,
        provider=provider,
        name=day["focus"],
        source_programme_id=CANONICAL_PROGRAMME_ID,
        explicit_user_approval=explicit_user_approval,
    )
    exported.validate(authenticated_user_id=user_id)
    return exported
