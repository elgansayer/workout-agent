from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import pytest
from dynamic_programme import (
    ENGINE_VERSION,
    ExerciseOverride,
    ProgrammeCadence,
    ProgrammePreviewRequest,
    RoutineSelection,
    build_programme_preview,
    detect_source_drift,
    goal_options,
    serialise_hevy_source,
    validate_programme_preview,
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
    rules: dict[str, Any] | None = None,
) -> ProgrammePreviewRequest:
    return ProgrammePreviewRequest(
        selected_routine_ids=selected or ["routine-upper", "routine-lower"],
        duration_weeks=duration,
        goal=goal,
        start_date=date(2026, 9, 1),
        rules=rules,
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
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="strength_test",
            start_date=date(2026, 9, 1),
            event_date=date(2026, 11, 20),
        ),
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


def test_history_confidence_tightens_adaptation_bounds(
    training_data: HevyTrainingData,
) -> None:
    informed = build_programme_preview(training_data, _request())
    informed_bounds = informed["days"][0]["exercises"][0]["prescriptions"][0][
        "allowed_set_change_from_source"
    ]
    assert informed["days"][0]["exercises"][0]["baseline"] is not None
    assert informed_bounds["maximum"] >= 1

    training_data.recent_workouts = []
    cold_start = build_programme_preview(training_data, _request())
    cold_exercise = cold_start["days"][0]["exercises"][0]
    assert cold_exercise["baseline"] is None
    assert cold_exercise["prescriptions"][0]["allowed_set_change_from_source"] == {
        "minimum": 0,
        "maximum": 0,
    }
    assert cold_start["analysis"]["calibration_required"] is True
def test_rolling_cadence_keeps_the_selected_order(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        _request(selected=["routine-lower", "routine-upper"]),
    )

    cadence = preview["cadence"]
    assert cadence["mode"] == "rolling"
    assert [entry["routine_id"] for entry in cadence["rotation"]] == [
        "routine-lower",
        "routine-upper",
    ]
    assert cadence["sessions_per_week"] == 7
    assert all(entry["scheduled"] for entry in cadence["rotation"])


def test_weekly_cadence_places_routines_on_the_selected_weekdays(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="general_fitness",
            start_date=date(2026, 9, 7),
            cadence=ProgrammeCadence(mode="weekly", weekdays=[0, 2, 4]),
            routine_selections=[
                RoutineSelection(routine_id="routine-upper", role="required"),
                RoutineSelection(routine_id="routine-lower", role="rotating"),
            ],
        ),
    )

    cadence = preview["cadence"]
    assert cadence["training_day_names"] == ["Monday", "Wednesday", "Friday"]
    assert cadence["sessions_per_week"] == 3
    upper = next(
        entry for entry in cadence["rotation"] if entry["routine_id"] == "routine-upper"
    )
    lower = next(
        entry for entry in cadence["rotation"] if entry["routine_id"] == "routine-lower"
    )
    assert upper["scheduled_weekday_names"] == ["Monday"]
    assert lower["scheduled_weekday_names"] == ["Wednesday", "Friday"]
    assert preview["blocking_violations"] == []
    assert len(preview["schedule"]) == 36
    assert preview["schedule"][0]["date"] == "2026-09-07"


def test_required_routine_without_a_weekday_is_a_blocking_violation(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="general_fitness",
            start_date=date(2026, 9, 7),
            cadence=ProgrammeCadence(mode="weekly", weekdays=[0]),
            routine_selections=[
                RoutineSelection(routine_id="routine-upper", role="required"),
                RoutineSelection(routine_id="routine-lower", role="required"),
            ],
        ),
    )

    codes = {item["code"] for item in preview["blocking_violations"]}
    assert "routine_not_scheduled" in codes
    assert validate_programme_preview(preview) == preview["blocking_violations"]


def test_optional_routines_are_dropped_when_the_week_runs_out_of_days(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=8,
            goal="general_fitness",
            start_date=date(2026, 9, 7),
            cadence=ProgrammeCadence(mode="weekly", weekdays=[0]),
            routine_selections=[
                RoutineSelection(routine_id="routine-upper", role="required"),
                RoutineSelection(routine_id="routine-lower", role="optional"),
            ],
        ),
    )

    assert preview["blocking_violations"] == []
    dropped = preview["cadence"]["unscheduled_routines"]
    assert [entry["routine_id"] for entry in dropped] == ["routine-lower"]
    assert dropped[0]["role"] == "optional"


def test_muscle_set_estimates_use_the_fractional_model(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="general_fitness",
            start_date=date(2026, 9, 7),
            cadence=ProgrammeCadence(mode="weekly", weekdays=[0, 2, 4]),
        ),
    )

    weekly = preview["muscle_sets"]["weekly_sets"]
    # Bench: 4 direct chest sets; pull-up adds 1.5 secondary triceps sets.
    assert weekly["chest"] == 4.0
    assert weekly["upper_back"] == 3.0
    assert weekly["lats"] == 3.0
    assert weekly["triceps"] == 2.0
    assert all(
        estimate["status"] in {"within_reference_range", "below_reference_range"}
        for estimate in preview["muscle_sets"]["estimates"]
    )


def test_source_drift_is_reported_against_the_active_programme(
    training_data: HevyTrainingData,
) -> None:
    first = build_programme_preview(training_data, _request())
    assert first["drift"]["state"] == "no_active_programme"

    unchanged = build_programme_preview(
        training_data,
        _request(),
        active_definition=first,
    )
    assert unchanged["drift"]["state"] == "in_sync"

    training_data.routines[0].exercises[0].sets = 5
    changed = build_programme_preview(
        training_data,
        _request(),
        active_definition=first,
    )
    assert changed["drift"]["state"] == "changed"
    assert changed["drift"]["changed_routines"] == ["routine-upper"]
    assert any("different Hevy routine revisions" in w for w in changed["warnings"])


def test_exercise_baselines_come_from_the_users_own_history(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())

    baselines = {
        entry["template_id"]: entry
        for entry in preview["analysis"]["exercise_baselines"]
    }
    assert baselines["bench"]["exposures"] == 4
    assert baselines["bench"]["source"] == "hevy_history"


def test_event_date_before_the_start_blocks_activation(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper"],
            duration_weeks=12,
            goal="strength_test",
            start_date=date(2026, 9, 1),
            event_date=date(2026, 8, 1),
        ),
    )

    codes = {item["code"] for item in preview["blocking_violations"]}
    assert "event_before_start" in codes


def test_unsupported_set_fields_are_reported_to_the_user(
    training_data: HevyTrainingData,
) -> None:
    training_data.routines[0].exercises[0].set_targets = [
        {"type": "normal", "reps": 6, "weight_kg": 50.0, "rpe": 9, "tempo": "3-1-1"},
    ]

    source = serialise_hevy_source(training_data)
    exercise = source["routines"][0]["exercises"][0]
    assert exercise["unsupported_fields"] == ["rpe", "tempo"]
    assert exercise["set_summary"][0]["rpe"] == 9
    assert source["routines"][0]["unsupported_fields"] == ["rpe", "tempo"]


def test_short_programmes_do_not_fabricate_three_phases(
    training_data: HevyTrainingData,
) -> None:
    for goal in [option["key"] for option in goal_options()]:
        for duration in (4, 5, 6):
            request = ProgrammePreviewRequest(
                selected_routine_ids=["routine-upper", "routine-lower"],
                duration_weeks=duration,
                goal=goal,
                start_date=date(2026, 9, 1),
                event_date=(
                    date(2026, 9, 1) + timedelta(weeks=duration)
                    if goal == "strength_test"
                    else None
                ),
            )
            preview = build_programme_preview(training_data, request)
            keys = [block["key"] for block in preview["blocks"]]
            logical = {key for key in keys if key != "calibration"}
            assert len(logical) <= 2, (goal, duration, keys)
            assert sum(block["duration_weeks"] for block in preview["blocks"]) == (
                duration
            )


def test_strength_test_without_a_date_has_no_taper(
    training_data: HevyTrainingData,
) -> None:
    no_date = build_programme_preview(
        training_data,
        _request(goal="strength_test"),
    )
    with_date = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="strength_test",
            start_date=date(2026, 9, 1),
            event_date=date(2026, 11, 20),
        ),
    )

    assert "taper_test" not in {block["key"] for block in no_date["blocks"]}
    assert "taper_test" in {block["key"] for block in with_date["blocks"]}
    assert any("no taper" in warning for warning in no_date["warnings"])


def test_low_confidence_history_inserts_calibration(
    training_data: HevyTrainingData,
) -> None:
    training_data.recent_workouts = []
    preview = build_programme_preview(training_data, _request(goal="hypertrophy"))

    assert preview["analysis"]["confidence"] == "low"
    assert preview["blocks"][0]["key"] == "calibration"
    assert preview["blocks"][0]["duration_weeks"] == 1
    assert preview["workload"]["version"] == "workload-estimate-v2"
    assert preview["workload"]["uncertainty"]
    assert all(
        exercise["week_one"]["confidence"] == "low"
        for exercise in preview["workload"]["exercises"]
    )


def test_roles_carry_evidence_and_are_user_overrideable(
    training_data: HevyTrainingData,
) -> None:
    default = build_programme_preview(training_data, _request())
    default_bench = next(
        exercise
        for exercise in default["days"][0]["exercises"]
        if exercise["template_id"] == "bench"
    )
    assert default_bench["role"] == "primary_strength"
    assert default_bench["role_confidence"] in {"high", "medium"}
    assert "position=0" in default_bench["role_evidence"]
    assert default_bench["role_override"] is False

    overridden = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="general_fitness",
            start_date=date(2026, 9, 1),
            exercise_overrides=[
                ExerciseOverride(
                    template_id="bench",
                    role="hypertrophy_accessory",
                    opening_sets=2,
                )
            ],
        ),
    )
    bench = next(
        exercise
        for exercise in overridden["days"][0]["exercises"]
        if exercise["template_id"] == "bench"
    )
    assert bench["role"] == "hypertrophy_accessory"
    assert bench["role_confidence"] == "user_override"
    assert bench["sets"] == 2
    assert bench["prescriptions"][0]["applied_override"] == "opening_sets"
    assert bench["source_sets"] == 4
    assert overridden["source_snapshots"][0]["exercises"][0]["sets"] == 4
    assert validate_programme_preview(overridden) == []


def test_workload_distinguishes_set_types_and_bounds_week_one(
    training_data: HevyTrainingData,
) -> None:
    training_data.routines[0].exercises[0].sets = 5
    training_data.routines[0].exercises[0].set_targets = [
        {"type": "warmup", "reps": 5, "weight_kg": 20.0},
        {"type": "normal", "reps": 6, "weight_kg": 50.0},
        {"type": "normal", "reps": 6, "weight_kg": 50.0},
        {"type": "normal", "reps": 6, "weight_kg": 50.0},
        {"type": "failure", "reps": 6, "weight_kg": 50.0},
    ]
    for workout in training_data.recent_workouts:
        for exercise in workout.exercises:
            if exercise.template_id != "bench":
                continue
            exercise.sets = [
                {"type": "normal", "reps": 6, "weight_kg": 50.0},
                {"type": "normal", "reps": 6, "weight_kg": 50.0},
                {"type": "normal", "reps": 6, "weight_kg": 50.0},
            ]
            exercise.total_sets = 3

    preview = build_programme_preview(training_data, _request())
    workload = next(
        item
        for item in preview["workload"]["exercises"]
        if item["template_id"] == "bench"
    )
    bench = next(
        exercise
        for exercise in preview["days"][0]["exercises"]
        if exercise["template_id"] == "bench"
    )

    assert workload["sets"] == {
        "warmup": 1,
        "normal": 3,
        "failure": 1,
        "drop": 0,
        "working": 4,
        "total": 5,
    }
    assert workload["week_one"]["max_sets"] <= 4
    assert bench["sets"] <= 4
    assert bench["sets"] >= 3
    assert preview["workload"]["set_model"]["warmup_sets_excluded"] is True


def test_duplicate_and_aggressive_volume_are_reported(
    training_data: HevyTrainingData,
) -> None:
    training_data.routines[1].exercises.append(
        RoutineExercise(
            template_id="bench",
            title="Bench Press",
            sets=14,
            rest_seconds=180,
            target_reps=6,
            set_targets=[
                {"type": "normal", "reps": 6, "weight_kg": 50.0}
                for _ in range(14)
            ],
        )
    )
    preview = build_programme_preview(training_data, _request())

    assert any("more than once" in warning for warning in preview["warnings"])
    assert preview["workload"]["unsupported_or_implausible"]
    assert any(
        "unusually high" in warning for warning in preview["warnings"]
    )


def test_type_specific_prescriptions_never_use_load_maths(
    training_data: HevyTrainingData,
) -> None:
    templates = dict(training_data.exercise_templates)
    templates["assisted"] = ExerciseTemplate(
        id="assisted",
        title="Assisted Dip",
        exercise_type="assisted_bodyweight",
        primary_muscle_group="chest",
    )
    templates["distance"] = ExerciseTemplate(
        id="distance",
        title="Sled Push",
        exercise_type="distance_duration",
        primary_muscle_group="cardio",
    )
    templates["reps_only"] = ExerciseTemplate(
        id="reps_only",
        title="Push-up",
        exercise_type="reps_only",
        primary_muscle_group="chest",
    )
    templates["assisted_reps"] = ExerciseTemplate(
        id="assisted_reps",
        title="Assisted Pull-up",
        exercise_type="bodyweight_assisted_reps",
        primary_muscle_group="lats",
    )
    templates["weight_duration"] = ExerciseTemplate(
        id="weight_duration",
        title="Farmer Carry",
        exercise_type="weight_duration",
        primary_muscle_group="forearms",
    )
    templates["short_distance"] = ExerciseTemplate(
        id="short_distance",
        title="Shuttle Run",
        exercise_type="short_distance_weight",
        primary_muscle_group="cardio",
    )
    templates["custom"] = ExerciseTemplate(
        id="custom",
        title="Custom Machine",
        exercise_type="weight_reps",
        primary_muscle_group="chest",
        is_custom=True,
    )
    training_data.exercise_templates = templates
    training_data.routines[0].exercises.extend(
        [
            RoutineExercise(
                template_id="assisted",
                title="Assisted Dip",
                sets=3,
                rest_seconds=90,
                set_targets=[{"type": "normal", "reps": 8} for _ in range(3)],
            ),
            RoutineExercise(
                template_id="distance",
                title="Sled Push",
                sets=4,
                rest_seconds=60,
                set_targets=[
                    {"type": "normal", "distance_meters": 20} for _ in range(4)
                ],
            ),
            RoutineExercise(
                template_id="reps_only",
                title="Push-up",
                sets=3,
                rest_seconds=60,
                set_targets=[{"type": "normal", "reps": 15} for _ in range(3)],
            ),
            RoutineExercise(
                template_id="assisted_reps",
                title="Assisted Pull-up",
                sets=3,
                rest_seconds=90,
                set_targets=[{"type": "normal", "reps": 8} for _ in range(3)],
            ),
            RoutineExercise(
                template_id="weight_duration",
                title="Farmer Carry",
                sets=3,
                rest_seconds=60,
                set_targets=[
                    {"type": "normal", "duration_seconds": 45} for _ in range(3)
                ],
            ),
            RoutineExercise(
                template_id="short_distance",
                title="Shuttle Run",
                sets=3,
                rest_seconds=60,
                set_targets=[
                    {"type": "normal", "distance_meters": 30} for _ in range(3)
                ],
            ),
            RoutineExercise(
                template_id="custom",
                title="Custom Machine",
                sets=3,
                rest_seconds=90,
                set_targets=[
                    {"type": "normal", "reps": 10, "weight_kg": 40.0}
                    for _ in range(3)
                ],
            ),
        ]
    )

    preview = build_programme_preview(training_data, _request())
    exercises = {
        exercise["template_id"]: exercise
        for day in preview["days"]
        for exercise in day["exercises"]
    }

    for template_id in (
        "assisted",
        "distance",
        "reps_only",
        "bike",
        "pullup",
        "assisted_reps",
        "weight_duration",
        "short_distance",
    ):
        progression = exercises[template_id]["prescriptions"][0]["progression"]
        assert progression["modality"] != "load"
        assert progression["load_increment_kg"] is None
        assert progression["method"] in {
            "bodyweight_progression",
            "duration_distance_progression",
        }
    assert (
        exercises["distance"]["prescriptions"][0]["progression"]["type_specific"][
            "forbidden_calculation"
        ]
        == "e1RM"
    )
    assert any(
        item["reason"] == "custom_or_unknown_exercise"
        and item["template_id"] == "custom"
        for item in preview["workload"]["uncertainty"]
    )
    assert validate_programme_preview(preview) == []


def test_shared_validator_reports_bounded_prescription_violations(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(training_data, _request())
    assert validate_programme_preview(preview) == []

    exercise = preview["days"][0]["exercises"][0]
    exercise["prescriptions"][0]["sets"] = 99
    preview["blocks"][0]["duration_weeks"] += 1
    violations = validate_programme_preview(preview)
    codes = {violation["code"] for violation in violations}

    assert "set_change_exceeds_bound" in codes
    assert "duration_not_conserved" in codes
    violation = next(
        item for item in violations if item["code"] == "set_change_exceeds_bound"
    )
    assert violation["routine_id"] == "routine-upper"
    assert violation["exercise_id"] == "bench"
    assert violation["week"] == 1
    assert violation["rule"] == "bounded_set_change"
    assert violation["remediation"]


def test_unknown_override_target_is_reported_not_silently_ignored(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal="general_fitness",
            start_date=date(2026, 9, 1),
            exercise_overrides=[
                ExerciseOverride(template_id="not-selected", opening_sets=2)
            ],
        ),
    )

    assert any(
        item["reason"] == "override_target_missing"
        for item in preview["workload"]["uncertainty"]
    )


def test_session_time_limit_is_a_blocking_constraint(
    training_data: HevyTrainingData,
) -> None:
    training_data.routines[0].exercises.append(
        _exercise("row", "Chest Supported Row", sets=4, reps=10)
    )
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=8,
            goal="hypertrophy",
            start_date=date(2026, 9, 1),
            max_session_minutes=20,
        ),
    )

    codes = {item["code"] for item in preview["blocking_violations"]}
    assert "session_duration_exceeded" in codes
    violation = next(
        item
        for item in preview["blocking_violations"]
        if item["code"] == "session_duration_exceeded"
    )
    assert violation["routine_id"] == "routine-upper"
    assert violation["remediation"]
    assert validate_programme_preview(preview) == preview["blocking_violations"]


def test_dated_test_must_fit_inside_the_programme(
    training_data: HevyTrainingData,
) -> None:
    preview = build_programme_preview(
        training_data,
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=4,
            goal="strength_test",
            start_date=date(2026, 9, 1),
            event_date=date(2026, 12, 1),
        ),
    )

    codes = {item["code"] for item in preview["blocking_violations"]}
    assert "event_outside_programme" in codes


def test_headline_goals_produce_distinct_block_strategies(
    training_data: HevyTrainingData,
) -> None:
    strategies = {}
    for goal in (
        "hypertrophy",
        "strength_test",
        "recomposition",
        "maintenance",
        "return_to_training",
    ):
        request = ProgrammePreviewRequest(
            selected_routine_ids=["routine-upper", "routine-lower"],
            duration_weeks=12,
            goal=goal,
            start_date=date(2026, 9, 1),
            event_date=date(2026, 11, 20) if goal == "strength_test" else None,
        )
        preview = build_programme_preview(training_data, request)
        strategies[goal] = tuple(block["key"] for block in preview["blocks"])

    assert len(set(strategies.values())) == len(strategies)
    assert "taper_test" in strategies["strength_test"]
    assert "taper_test" not in strategies["hypertrophy"]
    assert strategies["return_to_training"][0] == "calibration"
    assert "assessment" in strategies["maintenance"]
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


def test_programme_preview_flags_prohibited_exercises(
    training_data: HevyTrainingData,
) -> None:
    rules = {
        "version": 1,
        "prohibited_exercises": [{"exercise": "Squat", "reason": "Knee injury"}],
        "substitutions": [
            {"from_exercise": "Bench Press", "to_exercise": "Dumbbell Press"}
        ],
    }
    request = _request(rules=rules)
    preview = build_programme_preview(training_data, request)

    assert len(preview["blocking_violations"]) > 0
    assert any(
        v["exercise"] == "Squat"
        and "Prohibited exercise" in v["rule"]
        and "Knee injury" in v["rule"]
        and v["permitted_remediation"]
        for v in preview["blocking_violations"]
    )
    assert any(
        "Bench Press" in w
        and "Dumbbell Press" in w
        and "Permitted remediation" in w
        for w in preview["warnings"]
    )



def test_programme_preview_rejects_invalid_structured_rules(
    training_data: HevyTrainingData,
) -> None:
    rules = {"version": 1, "rep_intensity_bounds": {"min_reps": 15, "max_reps": 8}}
    request = _request(rules=rules)

    with pytest.raises(ValueError, match="Invalid structured training rules"):
        build_programme_preview(training_data, request)
