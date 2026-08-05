"""Tests for hevy_reader.py: Hevy data parsing and HevyTrainingData model."""

from __future__ import annotations

from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
    _parse_exercise_template,
    _parse_routine,
    _parse_workout,
    _top_set_from_sets,
)

# ---------------------------------------------------------------------------
# _parse_exercise_template
# ---------------------------------------------------------------------------


def test_parse_exercise_template_basic() -> None:
    raw = {
        "id": "t1",
        "title": "Barbell Bench Press",
        "type": "weight_reps",
        "primary_muscle_group": "chest",
        "secondary_muscle_groups": ["shoulders", "triceps"],
        "equipment": "barbell",
        "is_custom": False,
    }
    result = _parse_exercise_template(raw)
    assert result.id == "t1"
    assert result.title == "Barbell Bench Press"
    assert result.exercise_type == "weight_reps"
    assert result.primary_muscle_group == "chest"
    assert result.secondary_muscle_groups == ["shoulders", "triceps"]
    assert result.equipment == "barbell"
    assert result.is_custom is False


def test_parse_exercise_template_missing_fields() -> None:
    raw: dict = {}
    result = _parse_exercise_template(raw)
    assert result.id == ""
    assert result.title == "Unknown"
    assert result.exercise_type == "weight_reps"
    assert result.primary_muscle_group == "other"
    assert result.secondary_muscle_groups == []
    assert result.equipment is None
    assert result.is_custom is False


def test_parse_exercise_template_none_secondary() -> None:
    raw = {"id": "t2", "title": "Squat", "primary_muscle_group": "quads"}
    result = _parse_exercise_template(raw)
    assert result.secondary_muscle_groups == []


# ---------------------------------------------------------------------------
# _top_set_from_sets
# ---------------------------------------------------------------------------


def test_top_set_from_sets_empty() -> None:
    w, r = _top_set_from_sets([])
    assert w is None
    assert r is None


def test_top_set_from_sets_single() -> None:
    sets = [{"weight_kg": 100, "reps": 10}]
    w, r = _top_set_from_sets(sets)
    assert w == 100.0
    assert r == 10


def test_top_set_from_sets_picks_heaviest() -> None:
    sets = [
        {"weight_kg": 60, "reps": 12},
        {"weight_kg": 80, "reps": 8},
        {"weight_kg": 100, "reps": 5},
    ]
    w, r = _top_set_from_sets(sets)
    assert w == 100.0
    assert r == 5


def test_top_set_from_sets_missing_keys() -> None:
    sets = [{"weight_kg": 50}, {"reps": 8}]
    w, r = _top_set_from_sets(sets)
    # Picks the best single set by (weight, reps) tuple.
    # First set has weight 50 (reps missing -> -1), second has reps 8 (weight missing -> -1).
    # (50, -1) > (-1, 8) so first set wins.
    assert w == 50.0
    assert r is None


def test_top_set_from_sets_skips_non_dict() -> None:
    sets: list = [None, {"weight_kg": 70, "reps": 10}, "not-a-dict"]
    w, r = _top_set_from_sets(sets)
    assert w == 70.0
    assert r == 10


# ---------------------------------------------------------------------------
# _parse_routine
# ---------------------------------------------------------------------------


def test_parse_routine_basic() -> None:
    templates = {
        "t1": ExerciseTemplate(
            id="t1", title="Bench Press", exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
    }
    raw = {
        "id": "r1",
        "title": "Push Day",
        "folder_id": 1,
        "updated_at": "2026-06-01T08:00:00Z",
        "exercises": [
            {
                "exercise_template_id": "t1",
                "sets": [{"weight_kg": 80, "reps": 10}],
                "rest_seconds": 120,
                "notes": "go heavy",
                "superset_id": None,
            }
        ],
    }
    routine = _parse_routine(raw, templates)
    assert routine.id == "r1"
    assert routine.title == "Push Day"
    assert routine.folder_id == 1
    assert routine.updated_at == "2026-06-01T08:00:00Z"
    assert len(routine.exercises) == 1
    assert routine.exercises[0].title == "Bench Press"
    assert routine.exercises[0].sets == 1
    assert routine.exercises[0].rest_seconds == 120
    assert routine.exercises[0].notes == "go heavy"


def test_parse_routine_unknown_template_id() -> None:
    templates: dict = {}
    raw = {
        "id": "r2",
        "title": "Mystery Routine",
        "exercises": [
            {
                "exercise_template_id": "unknown_tid",
                "sets": [],
                "rest_seconds": 90,
            }
        ],
    }
    routine = _parse_routine(raw, templates)
    assert len(routine.exercises) == 1
    assert routine.exercises[0].title == "unknown_tid"  # Falls back to tid


def test_parse_routine_rep_range() -> None:
    templates: dict = {}
    raw = {
        "id": "r3",
        "title": "Range Test",
        "exercises": [
            {
                "exercise_template_id": "t1",
                "sets": [{"rep_range": {"start": 8, "end": 12}}],
                "rest_seconds": 60,
            }
        ],
    }
    routine = _parse_routine(raw, templates)
    ex = routine.exercises[0]
    assert ex.target_rep_range == (8, 12)


def test_parse_routine_no_exercises() -> None:
    raw = {"id": "r4", "title": "Empty"}
    routine = _parse_routine(raw, {})
    assert routine.id == "r4"
    assert routine.exercises == []


# ---------------------------------------------------------------------------
# _parse_workout
# ---------------------------------------------------------------------------


def test_parse_workout_basic() -> None:
    templates = {
        "t1": ExerciseTemplate(
            id="t1", title="Bench Press", exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
    }
    raw = {
        "id": "w1",
        "title": "Push Day",
        "start_time": "2026-06-01T08:00:00Z",
        "end_time": "2026-06-01T09:00:00Z",
        "exercises": [
            {
                "exercise_template_id": "t1",
                "title": "Bench Press",
                "sets": [
                    {"weight_kg": 80, "reps": 10},
                    {"weight_kg": 80, "reps": 8},
                ],
            }
        ],
    }
    wo = _parse_workout(raw, templates)
    assert wo.id == "w1"
    assert wo.title == "Push Day"
    assert wo.start_time == "2026-06-01T08:00:00Z"
    assert wo.end_time == "2026-06-01T09:00:00Z"
    assert wo.duration_seconds == 3600
    assert len(wo.exercises) == 1
    assert wo.exercises[0].top_weight_kg == 80.0
    assert wo.exercises[0].top_reps == 10
    assert wo.exercises[0].total_sets == 2
    assert wo.total_volume_kg == 80 * 10 + 80 * 8


def test_parse_workout_no_title() -> None:
    raw = {"id": "w2", "exercises": []}
    wo = _parse_workout(raw, {})
    assert wo.title == "Workout"


def test_parse_workout_no_duration() -> None:
    raw = {"id": "w3", "exercises": []}
    wo = _parse_workout(raw, {})
    assert wo.duration_seconds is None


def test_parse_workout_volume_with_missing_weight_reps() -> None:
    raw = {
        "id": "w4",
        "title": "Test",
        "exercises": [
            {
                "exercise_template_id": "t1",
                "title": "Test Ex",
                "sets": [
                    {"weight_kg": 100, "reps": 5},
                    {},  # Missing weight and reps
                    {"weight_kg": None, "reps": None},
                ],
            }
        ],
    }
    wo = _parse_workout(raw, {})
    assert wo.total_volume_kg == 500.0  # Only 100*5 counted


# ---------------------------------------------------------------------------
# HevyTrainingData model
# ---------------------------------------------------------------------------


def test_hevy_training_data_routine_titles() -> None:
    data = HevyTrainingData()
    data.routines = [
        Routine(id="1", title="Push"),
        Routine(id="2", title="Pull"),
    ]
    assert data.routine_titles() == ["Push", "Pull"]


def test_hevy_training_data_exercises_for_routine() -> None:
    data = HevyTrainingData()
    ex1 = RoutineExercise(template_id="t1", title="Bench", sets=3, rest_seconds=90)
    ex2 = RoutineExercise(template_id="t2", title="Row", sets=4, rest_seconds=90)
    data.routines = [
        Routine(id="1", title="Push", exercises=[ex1]),
        Routine(id="2", title="Pull", exercises=[ex2]),
    ]
    assert data.exercises_for_routine("Push") == [ex1]
    assert data.exercises_for_routine("Pull") == [ex2]
    assert data.exercises_for_routine("Legs") == []


def test_hevy_training_data_muscle_group_for() -> None:
    data = HevyTrainingData()
    data.exercise_templates = {
        "t1": ExerciseTemplate(
            id="t1", title="Bench", exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
    }
    assert data.muscle_group_for("t1") == "chest"
    assert data.muscle_group_for("unknown") == "other"


def test_hevy_training_data_exercise_name() -> None:
    data = HevyTrainingData()
    data.exercise_templates = {
        "t1": ExerciseTemplate(
            id="t1", title="Bench Press", exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
    }
    assert data.exercise_name("t1") == "Bench Press"
    assert data.exercise_name("unknown") == "unknown"


def test_hevy_training_data_latest_workout() -> None:
    data = HevyTrainingData()
    assert data.latest_workout() is None

    w1 = CompletedWorkout(id="w1", title="First", start_time="2026-06-01T08:00:00Z")
    w2 = CompletedWorkout(id="w2", title="Second", start_time="2026-06-02T08:00:00Z")
    data.recent_workouts = [w2, w1]
    assert data.latest_workout() is not None
    assert data.latest_workout().title == "Second"


def test_hevy_training_data_folders() -> None:
    data = HevyTrainingData()
    data.folders = {1: "My Routines", 2: "Saved"}
    assert data.folders == {1: "My Routines", 2: "Saved"}