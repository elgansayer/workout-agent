"""Tests for hevy_reader.py — pure parsing functions and dataclass methods.

No test in this module may hit a real external API. All network/AI calls are
mocked or exercised through pure-data constructors.
"""

from __future__ import annotations

from unittest.mock import patch

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
    fetch_user_training,
)

# ---------------------------------------------------------------------------
# _parse_exercise_template
# ---------------------------------------------------------------------------


class TestParseExerciseTemplate:
    def test_full_record(self) -> None:
        raw = {
            "id": "130",
            "title": "Barbell Bench Press",
            "type": "weight_reps",
            "primary_muscle_group": "chest",
            "secondary_muscle_groups": ["shoulders", "triceps"],
            "equipment": "barbell",
            "is_custom": False,
        }
        tmpl = _parse_exercise_template(raw)
        assert tmpl.id == "130"
        assert tmpl.title == "Barbell Bench Press"
        assert tmpl.exercise_type == "weight_reps"
        assert tmpl.primary_muscle_group == "chest"
        assert tmpl.secondary_muscle_groups == ["shoulders", "triceps"]
        assert tmpl.equipment == "barbell"
        assert tmpl.is_custom is False

    def test_minimal_record(self) -> None:
        raw: dict = {}
        tmpl = _parse_exercise_template(raw)
        assert tmpl.id == ""
        assert tmpl.title == "Unknown"
        assert tmpl.exercise_type == "weight_reps"
        assert tmpl.primary_muscle_group == "other"
        assert tmpl.secondary_muscle_groups == []
        assert tmpl.equipment is None
        assert tmpl.is_custom is False

    def test_none_lists_default_to_empty(self) -> None:
        raw = {"id": "1", "title": "Squat", "secondary_muscle_groups": None}
        tmpl = _parse_exercise_template(raw)
        assert tmpl.secondary_muscle_groups == []


# ---------------------------------------------------------------------------
# _parse_routine
# ---------------------------------------------------------------------------


class TestParseRoutine:
    def test_routine_with_exercises_and_rep_range(self) -> None:
        templates = {
            "101": ExerciseTemplate(
                id="101",
                title="Deadlift",
                exercise_type="weight_reps",
                primary_muscle_group="back",
            ),
            "102": ExerciseTemplate(
                id="102",
                title="Pull-up",
                exercise_type="bodyweight_reps",
                primary_muscle_group="back",
            ),
        }
        raw = {
            "id": "abc",
            "title": "Pull Day",
            "folder_id": 3,
            "updated_at": "2026-01-01T12:00:00Z",
            "exercises": [
                {
                    "exercise_template_id": "101",
                    "rest_seconds": 120,
                    "notes": "Go heavy",
                    "superset_id": None,
                    "sets": [
                        {"weight_kg": 100.0, "reps": 5},
                        {"weight_kg": 100.0, "reps": 5},
                    ],
                },
                {
                    "exercise_template_id": "102",
                    "rest_seconds": 90,
                    "notes": None,
                    "superset_id": 1,
                    "sets": [
                        {"rep_range": {"start": 8, "end": 12}},
                    ],
                },
            ],
        }
        routine = _parse_routine(raw, templates)
        assert routine.id == "abc"
        assert routine.title == "Pull Day"
        assert routine.folder_id == 3
        assert routine.updated_at == "2026-01-01T12:00:00Z"
        assert len(routine.exercises) == 2

        ex1 = routine.exercises[0]
        assert ex1.template_id == "101"
        assert ex1.title == "Deadlift"
        assert ex1.sets == 2
        assert ex1.rest_seconds == 120
        assert ex1.notes == "Go heavy"
        assert ex1.superset_id is None
        assert ex1.target_weight_kg == 100.0
        assert ex1.target_reps == 5
        assert ex1.target_rep_range is None

        ex2 = routine.exercises[1]
        assert ex2.title == "Pull-up"
        assert ex2.sets == 1
        assert ex2.sets == 1
        assert ex2.superset_id == 1
        assert ex2.target_rep_range == (8, 12)

    def test_routine_unknown_template_ids(self) -> None:
        templates: dict = {}
        raw = {
            "id": "r1",
            "title": "Mystery Routine",
            "exercises": [
                {"exercise_template_id": "999", "rest_seconds": 60, "sets": []},
            ],
        }
        routine = _parse_routine(raw, templates)
        assert routine.exercises[0].title == "999"


# ---------------------------------------------------------------------------
# _top_set_from_sets
# ---------------------------------------------------------------------------


class TestTopSetFromSets:
    def test_heaviest_weight_wins(self) -> None:
        sets = [
            {"weight_kg": 80.0, "reps": 10},
            {"weight_kg": 100.0, "reps": 3},
            {"weight_kg": 90.0, "reps": 8},
        ]
        w, r = _top_set_from_sets(sets)
        assert w == 100.0
        assert r == 3

    def test_same_weight_higher_reps_wins(self) -> None:
        sets = [
            {"weight_kg": 80.0, "reps": 5},
            {"weight_kg": 80.0, "reps": 12},
        ]
        w, r = _top_set_from_sets(sets)
        assert w == 80.0
        assert r == 12

    def test_empty(self) -> None:
        w, r = _top_set_from_sets([])
        assert w is None
        assert r is None

    def test_missing_weight(self) -> None:
        sets: list[dict[str, object]] = [
            {"reps": 10},
            {"weight_kg": 60.0, "reps": 8},
        ]
        w, r = _top_set_from_sets(sets)  # type: ignore[arg-type]
        assert w == 60.0
        assert r == 8

    def test_non_dict_entries(self) -> None:
        sets: list[dict[str, object] | str] = [
            "not-a-dict",
            {"weight_kg": 50.0, "reps": 6},
        ]
        w, r = _top_set_from_sets(sets)  # type: ignore[arg-type]
        assert w == 50.0
        assert r == 6


# ---------------------------------------------------------------------------
# _parse_workout
# ---------------------------------------------------------------------------


class TestParseWorkout:
    def test_complete_workout_with_volume(self) -> None:
        templates = {
            "201": ExerciseTemplate(
                id="201",
                title="Squat",
                exercise_type="weight_reps",
                primary_muscle_group="legs",
            ),
        }
        raw = {
            "id": "w1",
            "title": "Leg Day",
            "start_time": "2026-05-01T09:00:00Z",
            "end_time": "2026-05-01T10:15:00Z",
            "exercises": [
                {
                    "exercise_template_id": "201",
                    "title": "Squat",
                    "sets": [
                        {"weight_kg": 80.0, "reps": 8},
                        {"weight_kg": 90.0, "reps": 5},
                        {"weight_kg": 100.0, "reps": 3},
                    ],
                },
            ],
        }
        workout = _parse_workout(raw, templates)
        assert workout.id == "w1"
        assert workout.title == "Leg Day"
        assert workout.start_time == "2026-05-01T09:00:00Z"
        assert workout.end_time == "2026-05-01T10:15:00Z"
        assert workout.duration_seconds == 4500  # 75 minutes
        assert workout.total_volume_kg == 1390.0  # 80*8 + 90*5 + 100*3

        ex = workout.exercises[0]
        assert ex.template_id == "201"
        assert ex.title == "Squat"
        assert ex.top_weight_kg == 100.0
        assert ex.top_reps == 3
        assert ex.total_sets == 3

    def test_duration_calc_fallback(self) -> None:
        """Unparseable times should yield None duration."""
        raw = {
            "id": "w2",
            "title": "Workout",
            "start_time": "not-a-time",
            "end_time": "also-not",
            "exercises": [],
        }
        workout = _parse_workout(raw, {})
        assert workout.duration_seconds is None

    def test_missing_times(self) -> None:
        raw = {"id": "w3", "title": "Quick", "exercises": []}
        workout = _parse_workout(raw, {})
        assert workout.duration_seconds is None


# ---------------------------------------------------------------------------
# HevyTrainingData methods
# ---------------------------------------------------------------------------


class TestHevyTrainingData:
    def _make_data(self) -> HevyTrainingData:
        return HevyTrainingData(
            username="testuser",
            workout_count=42,
            exercise_templates={
                "1": ExerciseTemplate(
                    id="1",
                    title="Bench Press",
                    exercise_type="weight_reps",
                    primary_muscle_group="chest",
                ),
                "2": ExerciseTemplate(
                    id="2",
                    title="Squat",
                    exercise_type="weight_reps",
                    primary_muscle_group="legs",
                ),
                "3": ExerciseTemplate(
                    id="3",
                    title="Deadlift",
                    exercise_type="weight_reps",
                    primary_muscle_group="back",
                ),
            },
            routines=[
                Routine(
                    id="a",
                    title="Push Day",
                    exercises=[
                        RoutineExercise(
                            template_id="1",
                            title="Bench Press",
                            sets=3,
                            rest_seconds=90,
                        ),
                    ],
                ),
                Routine(
                    id="b",
                    title="Leg Day",
                    exercises=[
                        RoutineExercise(
                            template_id="2", title="Squat", sets=5, rest_seconds=120
                        ),
                    ],
                ),
                Routine(
                    id="c",
                    title="Pull Day",
                    exercises=[
                        RoutineExercise(
                            template_id="3", title="Deadlift", sets=3, rest_seconds=120
                        ),
                    ],
                ),
            ],
            recent_workouts=[
                CompletedWorkout(id="w1", title="Push Day", total_volume_kg=1000),
                CompletedWorkout(id="w2", title="Leg Day", total_volume_kg=1500),
            ],
            folders={1: "Upper", 2: "Lower"},
        )

    def test_routine_titles(self) -> None:
        data = self._make_data()
        assert data.routine_titles() == ["Push Day", "Leg Day", "Pull Day"]

    def test_exercises_for_routine(self) -> None:
        data = self._make_data()
        exs = data.exercises_for_routine("Leg Day")
        assert len(exs) == 1
        assert exs[0].title == "Squat"
        assert exs[0].sets == 5

    def test_exercises_for_unknown_routine(self) -> None:
        data = self._make_data()
        assert data.exercises_for_routine("Nonexistent") == []

    def test_muscle_group_for_known(self) -> None:
        data = self._make_data()
        assert data.muscle_group_for("1") == "chest"

    def test_muscle_group_for_unknown(self) -> None:
        data = self._make_data()
        assert data.muscle_group_for("999") == "other"

    def test_exercise_name_known(self) -> None:
        data = self._make_data()
        assert data.exercise_name("2") == "Squat"

    def test_exercise_name_unknown(self) -> None:
        data = self._make_data()
        assert data.exercise_name("999") == "999"

    def test_latest_workout(self) -> None:
        data = self._make_data()
        latest = data.latest_workout()
        assert latest is not None
        assert latest.id == "w1"

    def test_latest_workout_empty(self) -> None:
        data = HevyTrainingData()
        assert data.latest_workout() is None


# ---------------------------------------------------------------------------
# fetch_user_training — mock all network calls
# ---------------------------------------------------------------------------


class TestFetchUserTraining:
    def test_integration_all_successful(self) -> None:
        """End-to-end fetch with all API calls returning data."""
        fake_templates = [
            {
                "id": "1",
                "title": "Bench Press",
                "type": "weight_reps",
                "primary_muscle_group": "chest",
            },
        ]
        fake_routines = [
            {"id": "r1", "title": "Push Day", "exercises": []},
        ]
        fake_workouts = [
            {"id": "w1", "title": "Push Day", "exercises": []},
        ]
        fake_folders = [
            {"id": 1, "title": "Upper Body"},
        ]

        with (
            patch("hevy_reader.get_exercise_templates", return_value=fake_templates),
            patch("hevy_reader.get_routines", return_value=fake_routines),
            patch("hevy_reader.get_recent_workouts", return_value=fake_workouts),
            patch("hevy_reader.get_routine_folders", return_value=fake_folders),
            patch("hevy_reader.get_user_info", return_value={"username": "bob"}),
            patch("hevy_reader.get_workout_count", return_value=99),
        ):
            data = fetch_user_training("fake-key")

        assert data.username == "bob"
        assert data.workout_count == 99
        assert len(data.exercise_templates) == 1
        assert len(data.routines) == 1
        assert len(data.recent_workouts) == 1
        assert data.folders == {1: "Upper Body"}

    def test_all_calls_return_none(self) -> None:
        """Graceful degradation when every API call returns None/empty."""
        with (
            patch("hevy_reader.get_exercise_templates", return_value=None),
            patch("hevy_reader.get_routines", return_value=None),
            patch("hevy_reader.get_recent_workouts", return_value=None),
            patch("hevy_reader.get_routine_folders", return_value=None),
            patch("hevy_reader.get_user_info", return_value=None),
            patch("hevy_reader.get_workout_count", return_value=None),
        ):
            data = fetch_user_training("fake-key")

        assert data.username is None
        assert data.workout_count is None
        assert data.exercise_templates == {}
        assert data.routines == []
        assert data.recent_workouts == []
        assert data.folders == {}
