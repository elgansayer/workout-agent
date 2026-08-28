"""Tests for programme_inference.py — inferring training split from Hevy data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
    WorkoutExercise,
)
from programme_inference import (
    TrainingDay,
    _classify_routine_muscles,
    _classify_split,
    _compute_frequency,
    _determine_next_routine,
    infer_programme,
)

# ---------------------------------------------------------------------------
# Helpers to build synthetic Hevy data
# ---------------------------------------------------------------------------


def _make_template(
    tid: str,
    title: str,
    primary: str,
    secondaries: list[str] | None = None,
) -> ExerciseTemplate:
    return ExerciseTemplate(
        id=tid,
        title=title,
        exercise_type="weight_reps",
        primary_muscle_group=primary,
        secondary_muscle_groups=secondaries or [],
    )


def _make_routine_exercise(template_id: str, title: str = "") -> RoutineExercise:
    return RoutineExercise(
        template_id=template_id,
        title=title,
        sets=3,
        rest_seconds=90,
    )


def _make_routine(title: str, rid: str, exercises: list[RoutineExercise]) -> Routine:
    return Routine(id=rid, title=title, exercises=exercises)


def _make_training_day(
    title: str,
    rid: str = "",
    primary_muscles: list[str] | None = None,
) -> TrainingDay:
    return TrainingDay(
        title=title,
        routine_id=rid,
        primary_muscles=primary_muscles or [],
    )


# ---------------------------------------------------------------------------
# _classify_routine_muscles
# ---------------------------------------------------------------------------


def test_classify_routine_muscles_ranks_primary_then_secondary() -> None:
    templates = {
        "1": _make_template("1", "Bench Press", "chest", ["triceps"]),
        "2": _make_template("2", "Tricep Pushdown", "triceps"),
    }
    routine = _make_routine(
        "Push Day",
        "r1",
        [_make_routine_exercise("1"), _make_routine_exercise("2")],
    )
    result = _classify_routine_muscles(routine, templates)
    # Chest has count 1.0, triceps has 1.0 + 0.5 = 1.5 -> triceps first.
    assert result[0] == "triceps"
    assert result[1] == "chest"


# ---------------------------------------------------------------------------
# _classify_split
# ---------------------------------------------------------------------------


def test_classify_split_empty_unknown() -> None:
    assert _classify_split([]) == "unknown"


def test_classify_split_full_body() -> None:
    days = [
        _make_training_day("Day1", "r1", ["chest", "lats", "quads"]),
        _make_training_day("Day2", "r2", ["shoulders", "upper_back", "hamstrings"]),
    ]
    assert _classify_split(days) == "full_body"


def test_classify_split_push_pull_legs() -> None:
    days = [
        _make_training_day("Push", "r1", ["chest", "shoulders"]),
        _make_training_day("Pull", "r2", ["lats", "biceps"]),
        _make_training_day("Legs", "r3", ["quads", "hamstrings"]),
    ]
    assert _classify_split(days) == "push_pull_legs"


def test_classify_split_upper_lower() -> None:
    days = [
        _make_training_day("Upper A", "r1", ["chest", "lats"]),
        _make_training_day("Lower A", "r2", ["quads", "calves"]),
        _make_training_day("Upper B", "r3", ["shoulders", "upper_back"]),
        _make_training_day("Lower B", "r4", ["hamstrings", "glutes"]),
    ]
    assert _classify_split(days) == "upper_lower"


def test_classify_split_bro_split() -> None:
    days = [
        _make_training_day("Chest", "r1", ["chest"]),
        _make_training_day("Back", "r2", ["lats"]),
        _make_training_day("Shoulders", "r3", ["shoulders"]),
        _make_training_day("Legs", "r4", ["quads"]),
        _make_training_day("Arms", "r5", ["biceps", "triceps"]),
    ]
    assert _classify_split(days) == "bro_split"


# ---------------------------------------------------------------------------
# _compute_frequency
# ---------------------------------------------------------------------------


def test_compute_frequency_empty() -> None:
    s, mf = _compute_frequency([], {})
    assert s == 0.0
    assert mf == {}


def test_compute_frequency_from_workouts() -> None:
    templates = {
        "1": _make_template("1", "Bench", "chest"),
        "2": _make_template("2", "Deadlift", "lats"),
    }
    now = datetime.now(UTC)
    workouts = [
        CompletedWorkout(
            id="w1",
            title="Push",
            start_time=(now - timedelta(days=7)).isoformat(),
            exercises=[
                _make_workout_exercise("1", "Bench", 100, 5),
                _make_workout_exercise("1", "Bench", 100, 5),  # same muscle
            ],
        ),
        CompletedWorkout(
            id="w2",
            title="Pull",
            start_time=(now - timedelta(days=3)).isoformat(),
            exercises=[_make_workout_exercise("2", "Deadlift", 140, 5)],
        ),
    ]
    s, mf = _compute_frequency(workouts, templates, window_days=28)
    assert 0.4 <= s <= 0.6  # 2 workouts / 4 weeks = 0.5
    assert mf["chest"] == 0.2  # 1 session / 4 weeks rounded to 1 decimal
    assert mf["lats"] == 0.2


# ---------------------------------------------------------------------------
# _determine_next_routine
# ---------------------------------------------------------------------------


def test_determine_next_empty() -> None:
    assert _determine_next_routine([], []) is None


def test_determine_next_first_when_no_workouts() -> None:
    days = [_make_training_day("Push"), _make_training_day("Pull")]
    nxt = _determine_next_routine(days, [])
    assert nxt is not None
    assert nxt.title == "Push"


def test_determine_next_least_recent() -> None:
    push = _make_training_day("Push")
    pull = _make_training_day("Pull")
    legs = _make_training_day("Legs")
    days = [push, pull, legs]
    workouts = [
        CompletedWorkout(id="w1", title="Push", start_time="2026-08-01T10:00:00Z"),
        CompletedWorkout(id="w2", title="Pull", start_time="2026-08-04T10:00:00Z"),
    ]
    nxt = _determine_next_routine(days, workouts)
    # Legs never done -> highest priority.
    assert nxt is not None
    assert nxt.title == "Legs"


def test_determine_next_round_robin() -> None:
    push = _make_training_day("Push")
    pull = _make_training_day("Pull")
    days = [push, pull]
    workouts = [
        CompletedWorkout(id="w1", title="Push", start_time="2026-08-05T10:00:00Z"),
        CompletedWorkout(id="w2", title="Pull", start_time="2026-08-03T10:00:00Z"),
    ]
    nxt = _determine_next_routine(days, workouts)
    # Pull is least recent.
    assert nxt is not None
    assert nxt.title == "Pull"


# ---------------------------------------------------------------------------
# infer_programme — integration test with mock HevyTrainingData
# ---------------------------------------------------------------------------


def _make_workout_exercise(
    tid: str,
    title: str,
    weight: float,
    reps: int,
) -> WorkoutExercise:
    return WorkoutExercise(
        template_id=tid,
        title=title,
        sets=[{"weight_kg": weight, "reps": reps}],
        top_weight_kg=weight,
        top_reps=reps,
        total_sets=1,
    )


def test_infer_programme_full_flow() -> None:
    """End-to-end: build a HevyTrainingData and infer the programme."""
    templates = {
        "1": _make_template("1", "Bench Press", "chest", ["triceps"]),
        "2": _make_template("2", "Deadlift", "lats"),
        "3": _make_template("3", "Squat", "quads", ["glutes"]),
    }
    routines = [
        _make_routine(
            "Push Day",
            "r1",
            [
                _make_routine_exercise("1", "Bench Press"),
                _make_routine_exercise("1", "Bench Press"),
            ],
        ),
        _make_routine(
            "Pull Day",
            "r2",
            [_make_routine_exercise("2", "Deadlift")],
        ),
        _make_routine(
            "Leg Day",
            "r3",
            [_make_routine_exercise("3", "Squat")],
        ),
    ]
    workouts = [
        CompletedWorkout(
            id="w1",
            title="Push Day",
            start_time="2026-08-04T10:00:00Z",
            exercises=[_make_workout_exercise("1", "Bench Press", 100, 5)],
        ),
        CompletedWorkout(
            id="w2",
            title="Pull Day",
            start_time="2026-08-03T10:00:00Z",
            exercises=[_make_workout_exercise("2", "Deadlift", 140, 5)],
        ),
    ]
    data = HevyTrainingData(
        username="test_user",
        workout_count=50,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=workouts,
    )

    prog = infer_programme(data)
    assert prog.split_type == "push_pull_legs"
    assert len(prog.training_days) == 3
    assert prog.training_days[0].title == "Push Day"
    assert prog.sessions_per_week > 0
    assert prog.total_workouts == 50
    assert len(prog.recent_workout_days) == 2

    # Leg Day hasn't been done recently -> should be next.
    assert prog.next_routine is not None
    assert prog.next_routine.title == "Leg Day"


def test_infer_programme_empty_data() -> None:
    data = HevyTrainingData()
    prog = infer_programme(data)
    assert prog.split_type == "unknown"
    assert prog.training_days == []
    assert prog.sessions_per_week == 0.0
    assert prog.next_routine is None
    assert prog.total_workouts == 0
    assert prog.is_rest_day_today() is False


def test_training_day_focus_summary() -> None:
    td = TrainingDay(
        title="Push",
        routine_id="r1",
        primary_muscles=["chest", "shoulders", "triceps"],
    )
    assert "Chest" in td.focus_summary()
    assert "Shoulders" in td.focus_summary()
    assert "Triceps" in td.focus_summary()


def test_training_day_focus_summary_empty() -> None:
    td = TrainingDay(title="Push", routine_id="r1")
    assert td.focus_summary() == "Push"
