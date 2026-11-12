"""Synthetic fixtures shared by the programme activation and adaptation tests.

Only synthetic users, routines and workouts are used; nothing here resembles a
real provider payload or personal record.

synthetic-profile: true
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import database
import programme_service
from dynamic_programme import (
    ProgrammeCadence,
    ProgrammePreviewRequest,
    build_programme_preview,
)
from hevy_reader import (
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from programme_domain import AdaptationService, ProgrammeService

START = date(2026, 9, 1)  # a Tuesday


def _exercise(
    template_id: str, title: str, sets: int, reps: int, weight: float, rest: int = 120
) -> RoutineExercise:
    return RoutineExercise(
        template_id=template_id,
        title=title,
        sets=sets,
        rest_seconds=rest,
        target_reps=reps,
        set_targets=[
            {"type": "normal", "reps": reps, "weight_kg": weight} for _ in range(sets)
        ],
    )


def training_data(*, bench_sets: int = 3, extra_exercise: bool = False) -> HevyTrainingData:
    templates = {
        "bench": ExerciseTemplate(
            id="bench", title="Bench Press", exercise_type="weight_reps",
            primary_muscle_group="chest", equipment="barbell",
        ),
        "row": ExerciseTemplate(
            id="row", title="Cable Row", exercise_type="weight_reps",
            primary_muscle_group="upper_back", equipment="cable",
        ),
        "squat": ExerciseTemplate(
            id="squat", title="Back Squat", exercise_type="weight_reps",
            primary_muscle_group="quads", equipment="barbell",
        ),
        "curl": ExerciseTemplate(
            id="curl", title="Dumbbell Curl", exercise_type="weight_reps",
            primary_muscle_group="biceps", equipment="dumbbell",
        ),
        "fly": ExerciseTemplate(
            id="fly", title="Cable Fly", exercise_type="weight_reps",
            primary_muscle_group="chest", equipment="cable",
        ),
    }
    upper = [
        _exercise("bench", "Bench Press", bench_sets, 8, 60.0),
        _exercise("row", "Cable Row", 3, 10, 50.0, 90),
    ]
    if extra_exercise:
        upper.append(_exercise("fly", "Cable Fly", 3, 12, 20.0, 60))
    return HevyTrainingData(
        username="fixture-user",
        workout_count=0,
        exercise_templates=templates,
        routines=[
            Routine(id="routine-a", title="Upper A", exercises=upper, updated_at="2026-08-01T00:00:00Z"),
            Routine(
                id="routine-b",
                title="Lower B",
                exercises=[
                    _exercise("squat", "Back Squat", 3, 6, 100.0, 180),
                    _exercise("curl", "Dumbbell Curl", 3, 12, 14.0, 60),
                ],
                updated_at="2026-08-01T00:00:00Z",
            ),
        ],
        recent_workouts=[],
        folders={},
    )


def build_preview(
    data: HevyTrainingData | None = None,
    *,
    duration: int = 8,
    start: date = START,
    cadence: ProgrammeCadence | None = None,
    sessions: int = 4,
    fatigue_management: str = "adaptive",
) -> dict[str, Any]:
    request = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        duration_weeks=duration,
        goal="hypertrophy",
        start_date=start,
        sessions_per_week=sessions,
        cadence=cadence or ProgrammeCadence(mode="rolling"),
        fatigue_management=fatigue_management,  # type: ignore[arg-type]
    )
    return build_programme_preview(data or training_data(), request)


def new_user(db: str, email: str = "athlete-a@example.com", timezone_name: str = "UTC") -> str:
    user = database.get_or_create_user(email, "Synthetic", db)
    if timezone_name != "UTC":
        with database.transaction(db) as conn:
            conn.execute(
                "UPDATE users SET timezone = ? WHERE id = ?", (timezone_name, user["id"])
            )
    return str(user["id"])


def activate(
    db: str,
    user_id: str,
    preview: dict[str, Any] | None = None,
    **kwargs: Any,
) -> int:
    preview = preview or build_preview()
    programme_service.activate_programme(
        user_id,
        preview,
        db_path=db,
        presented_token=preview["preview_token"],
        now=kwargs.pop("now", datetime(2026, 9, 1, 8, tzinfo=timezone.utc)),
        **kwargs,
    )
    active = ProgrammeService(db).get_active_programme(user_id)
    assert active is not None
    return active.id


def make_active(
    tmp_path: Any,
    *,
    duration: int = 8,
    cadence: ProgrammeCadence | None = None,
    timezone_name: str = "UTC",
    fatigue_management: str = "adaptive",
    data: HevyTrainingData | None = None,
) -> tuple[str, str, int, dict[str, Any]]:
    db = str(tmp_path / "programme.db")
    database.init_db(db)
    user_id = new_user(db, timezone_name=timezone_name)
    preview = build_preview(
        data, duration=duration, cadence=cadence, fatigue_management=fatigue_management
    )
    programme_id = activate(db, user_id, preview)
    return db, user_id, programme_id, preview


def workout(
    workout_id: str,
    day: int,
    exercises: dict[str, list[float]],
    *,
    weight: float | dict[str, float] = 60.0,
    rpe: float | None = 7.0,
    hour: int = 10,
    month: int = 9,
    stamp: str | None = None,
) -> dict[str, Any]:
    start = stamp or f"2026-{month:02d}-{day:02d}T{hour:02d}:00:00+00:00"
    items = []
    for template, reps in exercises.items():
        load = weight[template] if isinstance(weight, dict) else weight
        items.append(
            {
                "exercise_template_id": template,
                "title": template.title(),
                "sets": [
                    {"type": "normal", "reps": rep, "weight_kg": load, **({"rpe": rpe} if rpe is not None else {})}
                    for rep in reps
                ],
            }
        )
    return {
        "id": workout_id,
        "title": "Session",
        "start_time": start,
        "end_time": start,
        "exercises": items,
    }


def prescription_of(
    db: str, user_id: str, programme_id: int, template_id: str, *, position: int, week: int = 1
) -> Any:
    service = ProgrammeService(db)
    programme = service.get_programme(user_id, programme_id)
    assert programme is not None and programme.current_revision is not None
    adaptation = AdaptationService(db)
    from programme_domain.activation import definition_from_revision

    definition = definition_from_revision(programme, programme.current_revision)
    decisions = service.list_adaptation_decisions(user_id, programme_id)
    found = adaptation.effective_prescription(
        definition, decisions, programme.current_revision,
        template_id=template_id, routine_position=position, week=week,
    )
    assert found is not None
    return found[0]


def rep_top(prescription: Any) -> int:
    return int(prescription.rep_max or prescription.rep_min or 8)
