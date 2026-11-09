from __future__ import annotations

from copy import deepcopy
from datetime import date
from itertools import pairwise

import pytest
from dynamic_programme import (
    ENGINE_VERSION,
    ProgrammePreviewRequest,
    build_programme_preview,
    detect_source_drift,
    goal_options,
    serialise_hevy_source,
)
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
    WorkoutExercise,
)


def _exercise(
    template_id: str,
    title: str,
    *,
    sets: int = 3,
    reps: int = 8,
) -> RoutineExercise:
    return RoutineExercise(
        template_id=template_id,
        title=title,
        sets=sets,
        rest_seconds=120,
        target_reps=reps,
        set_targets=[
            {"type": "normal", "reps": reps, "weight_kg": 50.0} for _ in range(sets)
        ],
    )


@pytest.fixture()
def training_data() -> HevyTrainingData:
    templates = {
        "bench": ExerciseTemplate(
            id="bench",
            title="Bench Press",
            exercise_type="weight_reps",
            primary_muscle_group="chest",
            secondary_muscle_groups=["triceps", "shoulders"],
            equipment="barbell",
        ),
        "row": ExerciseTemplate(
            id="row",
            title="Chest Supported Row",
            exercise_type="weight_reps",
            primary_muscle_group="upper_back",
            secondary_muscle_groups=["biceps"],
            equipment="machine",
        ),
        "squat": ExerciseTemplate(
            id="squat",
            title="Squat",
            exercise_type="weight_reps",
            primary_muscle_group="quads",
            secondary_muscle_groups=["glutes"],
            equipment="barbell",
        ),
        "pullup": ExerciseTemplate(
            id="pullup",
            title="Pull-up",
            exercise_type="bodyweight_reps",
            primary_muscle_group="lats",
            secondary_muscle_groups=["biceps"],
        ),
        "bike": ExerciseTemplate(
            id="bike",
            title="Exercise Bike",
            exercise_type="duration",
            primary_muscle_group="cardio",
        ),
    }
    routines = [
        Routine(
            id="routine-upper",
            title="Upper",
            folder_id=10,
            updated_at="2026-08-18T12:00:00Z",
            exercises=[
                _exercise("bench", "Bench Press", sets=4, reps=6),
                _exercise("row", "Chest Supported Row", sets=3, reps=10),
                _exercise("pullup", "Pull-up", sets=3, reps=8),
            ],
        ),
        Routine(
            id="routine-lower",
            title="Lower",
            folder_id=10,
            updated_at="2026-08-18T13:00:00Z",
            exercises=[
                _exercise("squat", "Squat", sets=4, reps=5),
                RoutineExercise(
                    template_id="bike",
                    title="Exercise Bike",
                    sets=1,
                    rest_seconds=0,
                    set_targets=[{"type": "normal", "duration_seconds": 600}],
                ),
            ],
        ),
    ]
    recent = [
        CompletedWorkout(
            id=f"workout-{index}",
            title="Upper" if index % 2 == 0 else "Lower",
            start_time=f"2026-08-{10 + index:02d}T10:00:00Z",
            exercises=[
                WorkoutExercise(
                    template_id="bench" if index % 2 == 0 else "squat",
                    title="Bench Press" if index % 2 == 0 else "Squat",
                    total_sets=4,
                )
            ],
        )
        for index in range(8)
    ]
    return HevyTrainingData(
        username="tester",
        workout_count=120,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=recent,
        folders={10: "Current split"},
    )


def _request(
    *,
    goal: str = "general_fitness",
    duration: int = 12,
    selected: list[str] | None = None,
) -> ProgrammePreviewRequest:
    return ProgrammePreviewRequest(
        selected_routine_ids=selected or ["routine-upper", "routine-lower"],
        duration_weeks=duration,
        goal=goal,
        start_date=date(2026, 9, 1),
    )


def test_source_serialisation_preserves_provider_identity_and_sets(
    training_data: HevyTrainingData,
) -> None:
    source = serialise_hevy_source(training_data)
    upper = source["routines"][0]

    assert upper["id"] == "routine-upper"
    assert upper["folder_name"] == "Current split"
    assert upper["source_hash"]
    assert upper["exercises"][0]["set_targets"][0] == {
        "type": "normal",
        "reps": 6,
        "weight_kg": 50.0,
    }


def test_every_goal_and_duration_conserves_weeks(
    training_data: HevyTrainingData,
) -> None:
    for goal in [option["key"] for option in goal_options()]:
        for duration in range(4, 53):
            preview = build_programme_preview(
                training_data,
                _request(goal=goal, duration=duration),
            )
            blocks = preview["blocks"]
            assert sum(block["duration_weeks"] for block in blocks) == duration
            assert blocks[0]["start_week"] == 1
            assert blocks[-1]["end_week"] == duration
            for previous, current in pairwise(blocks):
                assert current["start_week"] == previous["end_week"] + 1
            assert all(1 <= block["duration_weeks"] <= 6 for block in blocks)


def test_goals_produce_different_strategies(
    training_data: HevyTrainingData,
) -> None:
    hypertrophy = build_programme_preview(
        training_data,
        _request(goal="hypertrophy"),
    )
    tested_strength = build_programme_preview(
        training_data,
        _request(goal="strength_test"),
    )

    assert [block["key"] for block in hypertrophy["blocks"]] != [
        block["key"] for block in tested_strength["blocks"]
    ]
    assert "taper_test" not in {block["key"] for block in hypertrophy["blocks"]}
    assert "taper_test" in {block["key"] for block in tested_strength["blocks"]}


def test_preview_is_deterministic_and_source_bound(
    training_data: HevyTrainingData,
) -> None:
    request = _request(goal="maximal_strength")
    first = build_programme_preview(training_data, request)
    second = build_programme_preview(training_data, request)

    assert first == second
    assert first["engine_version"] == ENGINE_VERSION
    assert len(first["preview_token"]) == 64


def test_selection_uses_provider_ids_and_preserves_order(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        _request(selected=["routine-lower", "routine-upper"]),
    )

    assert [day["routine_id"] for day in preview["days"]] == [
        "routine-lower",
        "routine-upper",
    ]
    assert preview["programme_spec"]["selected_routine_ids"] == [
        "routine-lower",
        "routine-upper",
    ]


@pytest.mark.parametrize(
    ("selected", "message"),
    [
        (["missing"], "not available"),
        (["routine-upper", "routine-upper"], "only once"),
    ],
)
def test_invalid_source_selection_fails(
    training_data: HevyTrainingData,
    selected: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_programme_preview(
            training_data,
            _request(selected=selected),
        )


def test_prescriptions_are_modality_aware(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    exercises = {
        exercise["template_id"]: exercise
        for day in preview["days"]
        for exercise in day["exercises"]
    }

    assert exercises["pullup"]["role"] == "bodyweight_progression"
    assert (
        exercises["pullup"]["prescriptions"][0]["progression"]["method"]
        == "bodyweight_progression"
    )
    assert exercises["bike"]["role"] == "duration_distance"
    bike_rule = exercises["bike"]["prescriptions"][0]["progression"]
    assert bike_rule["method"] == "duration_distance_progression"
    assert "never calculate e1RQ" in bike_rule["rule"]


def test_sparse_history_is_labelled_low_confidence(
    training_data: HevyTrainingData,
) -> None:
    training_data.recent_workouts = []
    preview = build_programme_preview(training_data, _request())

    assert preview["analysis"]["confidence"] == "low"
    assert any("confidence is low" in warning for warning in preview["warnings"])


def test_source_drift_is_unchanged_for_identical_live_source(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    live = serialise_hevy_source(training_data)

    drift = detect_source_drift(preview, live)

    assert drift["status"] == "unchanged"
    assert drift["has_changes"] is False
    assert [row["state"] for row in drift["routines"]] == ["unchanged", "unchanged"]


def test_source_drift_distinguishes_rename_from_content_change(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    live = serialise_hevy_source(training_data)

    renamed = deepcopy(live)
    renamed["routines"][0]["title"] = "Upper A"
    rename_drift = detect_source_drift(preview, renamed)
    assert rename_drift["status"] == "review_recommended"
    assert rename_drift["has_changes"] is True
    assert rename_drift["routines"][0]["state"] == "renamed"
    assert rename_drift["routines"][0]["current_title"] == "Upper A"

    edited = deepcopy(live)
    edited["routines"][1]["exercises"][0]["sets"] = 5
    edit_drift = detect_source_drift(preview, edited)
    assert edit_drift["status"] == "action_required"
    assert edit_drift["routines"][1]["state"] == "content_changed"
    assert edit_drift["summary"]["content_changed"] == 1


def test_source_drift_reports_removed_routines(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    live = serialise_hevy_source(training_data)
    live["routines"] = [routine for routine in live["routines"] if routine["id"] != "routine-lower"]

    drift = detect_source_drift(preview, live)

    assert drift["status"] == "action_required"
    assert drift["summary"]["removed"] == 1
    assert drift["routines"][1]["state"] == "removed"
    assert drift["routines"][1]["current_hash"] is None


def test_source_drift_without_active_programme_is_empty() -> None:
    drift = detect_source_drift(None, {"routines": []})

    assert drift == {
        "status": "no_active_programme",
        "has_changes": False,
        "routines": [],
        "summary": {"renamed": 0, "content_changed": 0, "removed": 0},
    }


def test_source_drift_ignores_provider_timestamp_only_revision(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    revised = deepcopy(training_data)
    revised.routines[0].updated_at = "2026-09-01T09:00:00Z"
    live = serialise_hevy_source(revised)

    drift = detect_source_drift(preview, live)

    assert drift["status"] == "unchanged"
    assert drift["routines"][0]["source_hash"] != drift["routines"][0]["current_hash"]
