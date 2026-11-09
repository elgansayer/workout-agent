"""Tests for the deterministic Hevy history engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from database import (
    get_exercise_baselines,
    get_routine_match_overrides,
    get_routine_snapshots,
    get_routine_workout_matches,
    init_db,
    save_exercise_baselines,
    save_routine_match_override,
    save_routine_snapshots,
    save_routine_workout_matches,
)
from hevy_history import (
    BASELINE_VERSION,
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_HIGH,
    CONFIDENCE_PROBABLE,
    CONFIDENCE_UNMATCHED,
    MATCHER_VERSION,
    MatchOverride,
    analyse_history,
    build_snapshots,
    match_workouts,
    merge_workout_history,
    parse_cached_workouts,
    select_evidence_window,
)
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
    WorkoutExercise,
)


def _template(
    template_id: str,
    title: str,
    exercise_type: str = "weight_reps",
    muscle: str = "chest",
    equipment: str | None = "barbell",
) -> ExerciseTemplate:
    return ExerciseTemplate(
        id=template_id,
        title=title,
        exercise_type=exercise_type,
        primary_muscle_group=muscle,
        equipment=equipment,
    )


def _routine(
    routine_id: str,
    title: str,
    exercises: list[tuple[str, int]],
    *,
    supersets: dict[str, int] | None = None,
) -> Routine:
    supersets = supersets or {}
    return Routine(
        id=routine_id,
        title=title,
        updated_at="2026-08-01T00:00:00Z",
        exercises=[
            RoutineExercise(
                template_id=template_id,
                title=template_id.title(),
                sets=sets,
                rest_seconds=90,
                superset_id=supersets.get(template_id),
            )
            for template_id, sets in exercises
        ],
    )


def _workout(
    workout_id: str,
    title: str,
    start: str,
    exercises: list[tuple[str, list[dict[str, Any]]]],
    *,
    end: str | None = None,
) -> CompletedWorkout:
    return CompletedWorkout(
        id=workout_id,
        title=title,
        start_time=start,
        end_time=end or start.replace(":00:00Z", ":59:00Z"),
        duration_seconds=3540,
        exercises=[
            WorkoutExercise(
                template_id=template_id,
                title=template_id.title(),
                sets=sets,
                total_sets=len(sets),
            )
            for template_id, sets in exercises
        ],
    )


def _normal(weight: float, reps: int, **extra: Any) -> dict[str, Any]:
    return {"type": "normal", "weight_kg": weight, "reps": reps, **extra}


def _base_data(workouts: list[CompletedWorkout]) -> HevyTrainingData:
    templates = {
        "bench": _template("bench", "Bench Press"),
        "row": _template("row", "Row", muscle="upper_back"),
        "pull": _template("pull", "Pull-up", "bodyweight_reps", "lats"),
        "squat": _template("squat", "Squat", muscle="quads"),
        "bike": _template("bike", "Bike", "duration", "cardio", None),
    }
    routines = [
        _routine("upper", "Upper", [("bench", 4), ("row", 3), ("pull", 3)]),
        _routine("lower", "Lower", [("squat", 4), ("bike", 1)]),
    ]
    return HevyTrainingData(
        exercise_templates=templates,
        routines=routines,
        recent_workouts=workouts,
    )


# ---------------------------------------------------------------------------
# Routine matching
# ---------------------------------------------------------------------------


def test_exact_and_near_exact_matches_are_high_confidence() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [
                    ("bench", [_normal(80, 8)] * 4),
                    ("row", [_normal(60, 10)] * 3),
                    ("pull", [{"type": "normal", "reps": 8}] * 3),
                ],
            )
        ]
    )
    analysis = analyse_history(data, ["upper", "lower"])
    match = analysis["matches"][0]
    assert match["routine_id"] == "upper"
    assert match["confidence"] == CONFIDENCE_HIGH
    assert match["user_confirmed"] is False


def test_renamed_routine_and_workout_still_matches_by_structure() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Push Session",
                "2026-08-10T10:00:00Z",
                [
                    ("bench", [_normal(80, 8)] * 4),
                    ("row", [_normal(60, 10)] * 3),
                    ("pull", [{"type": "normal", "reps": 8}] * 3),
                ],
            )
        ]
    )
    data.routines[0].title = "Upper Body A"
    analysis = analyse_history(data, ["upper", "lower"])
    assert analysis["matches"][0]["routine_id"] == "upper"
    assert analysis["matches"][0]["confidence"] in {
        CONFIDENCE_HIGH,
        CONFIDENCE_PROBABLE,
    }


def test_changed_order_and_added_accessory_remain_matched() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [
                    ("pull", [{"type": "normal", "reps": 8}] * 3),
                    ("bench", [_normal(80, 8)] * 4),
                    ("row", [_normal(60, 10)] * 3),
                    ("lateral", [_normal(10, 15)] * 2),
                ],
            )
        ]
    )
    data.exercise_templates["lateral"] = _template(
        "lateral", "Lateral Raise", muscle="shoulders", equipment="dumbbell"
    )
    analysis = analyse_history(data, ["upper", "lower"])
    assert analysis["matches"][0]["routine_id"] == "upper"
    assert analysis["matches"][0]["confidence"] != CONFIDENCE_UNMATCHED


def test_title_only_cannot_force_a_match() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [("curl", [_normal(20, 12)] * 4)],
            )
        ]
    )
    data.exercise_templates["curl"] = _template(
        "curl", "Curl", muscle="biceps", equipment="dumbbell"
    )
    analysis = analyse_history(data, ["upper", "lower"])
    assert analysis["matches"][0]["confidence"] == CONFIDENCE_UNMATCHED
    assert analysis["matches"][0]["routine_id"] is None


def test_duplicate_routines_are_ambiguous_and_exposed() -> None:
    data = _base_data([])
    data.routines = [
        _routine("upper-a", "Upper", [("bench", 4), ("row", 3)]),
        _routine("upper-b", "Upper", [("bench", 4), ("row", 3)]),
    ]
    data.recent_workouts = [
        _workout(
            "w1",
            "Upper",
            "2026-08-10T10:00:00Z",
            [("bench", [_normal(80, 8)] * 4), ("row", [_normal(60, 10)] * 3)],
        )
    ]
    analysis = analyse_history(data, ["upper-a", "upper-b"])
    assert analysis["matches"][0]["is_ambiguous"] is True
    assert analysis["matches"][0]["confidence"] == CONFIDENCE_AMBIGUOUS
    assert analysis["ambiguous_workout_count"] == 1


def test_user_override_wins_over_automatic_ambiguity() -> None:
    data = _base_data([])
    data.routines = [
        _routine("upper-a", "Upper", [("bench", 4), ("row", 3)]),
        _routine("upper-b", "Upper", [("bench", 4), ("row", 3)]),
    ]
    data.recent_workouts = [
        _workout(
            "w1",
            "Upper",
            "2026-08-10T10:00:00Z",
            [("bench", [_normal(80, 8)] * 4), ("row", [_normal(60, 10)] * 3)],
        )
    ]
    snapshots = build_snapshots(data.routines, data.exercise_templates)
    target = next(
        snapshot for snapshot in snapshots if snapshot.routine_id == "upper-b"
    )
    matches = match_workouts(
        snapshots,
        data.recent_workouts,
        overrides=[
            MatchOverride(
                pattern_type="exercise_signature",
                pattern_value="bench|row",
                routine_snapshot_id=target.id,
            )
        ],
    )
    assert matches[0].routine_id == "upper-b"
    assert matches[0].user_confirmed is True
    assert matches[0].confidence == CONFIDENCE_HIGH


def test_match_scores_are_bounded_and_deterministic() -> None:
    data = _base_data(
        [
            _workout(
                f"w{index}",
                "Upper" if index % 2 == 0 else "Lower",
                f"2026-08-{10 + index:02d}T10:00:00Z",
                (
                    [("bench", [_normal(80 + index, 8)] * 4)]
                    if index % 2 == 0
                    else [("squat", [_normal(100 + index, 5)] * 4)]
                ),
            )
            for index in range(6)
        ]
    )
    first = analyse_history(data, ["upper", "lower"])
    second = analyse_history(data, ["upper", "lower"])
    assert first == second
    assert all(0.0 <= match["score"] <= 1.0 for match in first["matches"])


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def test_baseline_separates_set_types_and_excludes_warmups() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [
                    (
                        "bench",
                        [
                            {"type": "warmup", "weight_kg": 40, "reps": 10},
                            _normal(80, 8, rpe=8),
                            _normal(80, 8, rpe=9),
                            {"type": "failure", "weight_kg": 70, "reps": 10, "rpe": 10},
                            {"type": "dropset", "weight_kg": 60, "reps": 12},
                        ],
                    )
                ],
            )
        ]
    )
    analysis = analyse_history(data, ["upper", "lower"], programme_weeks=8)
    baseline = analysis["baselines"]["bench"]
    assert baseline["warmup_set_count"] == 1
    assert baseline["failure_set_count"] == 1
    assert baseline["dropset_set_count"] == 1
    assert baseline["working_set_count"] == 4
    assert baseline["rpe"]["median"] == 9.0
    assert baseline["e1rm_kg"]["valid"] is True


def test_outlier_personal_record_does_not_become_the_baseline() -> None:
    workouts = []
    for index in range(6):
        weight = 100.0 if index < 5 else 220.0
        workouts.append(
            _workout(
                f"w{index}",
                "Upper",
                f"2026-08-{10 + index:02d}T10:00:00Z",
                [("bench", [_normal(weight, 8)] * 3)],
            )
        )
    data = _base_data(workouts)
    analysis = analyse_history(data, ["upper", "lower"])
    baseline = analysis["baselines"]["bench"]
    assert baseline["e1rm_kg"]["outlier_count"] >= 1
    assert baseline["e1rm_kg"]["value"] < 180
    assert "outlier_personal_records_excluded" in baseline["warnings"]


def test_bodyweight_assisted_duration_distance_and_custom_sets() -> None:
    data = _base_data([])
    data.exercise_templates.update(
        {
            "assist": _template(
                "assist", "Assisted Pull-up", "assisted_bodyweight", "lats", None
            ),
            "run": _template("run", "Run", "distance_duration", "cardio", None),
            "sled": _template("sled", "Sled", "weight_reps", "quads", "sled"),
        }
    )
    data.routines = [
        _routine("full", "Full", [("pull", 3), ("assist", 3), ("run", 1), ("sled", 2)])
    ]
    data.recent_workouts = [
        _workout(
            "w1",
            "Full",
            "2026-08-10T10:00:00Z",
            [
                ("pull", [{"type": "normal", "reps": 8}] * 3),
                ("assist", [_normal(35, 8)] * 3),
                (
                    "run",
                    [
                        {
                            "type": "normal",
                            "duration_seconds": 1800,
                            "distance_meters": 5000,
                        }
                    ],
                ),
                ("sled", [{"type": "normal", "weight_kg": 60, "reps": 20, "custom_metric": 1.5}] * 2),
            ],
        )
    ]
    analysis = analyse_history(data, ["full"])
    pull = analysis["baselines"]["pull"]
    assisted = analysis["baselines"]["assist"]
    run = analysis["baselines"]["run"]
    sled = analysis["baselines"]["sled"]
    assert pull["e1rm_kg"]["valid"] is False
    assert pull["reps"]["median"] == 8.0
    assert assisted["e1rm_kg"]["valid"] is False
    assert assisted["e1rm_kg"]["load_basis"] == "assisted"
    assert run["duration_seconds"]["median"] == 1800.0
    assert run["distance_meters"]["median"] == 5000.0
    assert run["e1rm_kg"]["valid"] is False
    assert sled["custom_metric"]["median"] == 1.5


def test_stale_and_incomplete_history_is_conservative() -> None:
    old = (datetime.now(tz=timezone.utc) - timedelta(days=200)).isoformat()
    data = _base_data(
        [
            CompletedWorkout(
                id="w1",
                title="Upper",
                start_time=old,
                end_time=None,
                duration_seconds=None,
                exercises=[
                    WorkoutExercise(
                        template_id="bench",
                        title="Bench Press",
                        sets=[_normal(80, 8)] * 3,
                        total_sets=3,
                    )
                ],
            )
        ]
    )
    analysis = analyse_history(
        data,
        ["upper", "lower"],
        now=datetime.now(tz=timezone.utc),
    )
    baseline = analysis["baselines"]["bench"]
    assert "stale_history" in baseline["warnings"]
    assert "incomplete_sessions_observed" in baseline["warnings"]
    assert analysis["calibration_required"] is True


def test_sparse_history_expands_window_and_requests_calibration() -> None:
    workouts = [
        _workout(
            f"w{index}",
            "Upper",
            f"2026-08-{10 + index:02d}T10:00:00Z",
            [("bench", [_normal(80, 8)] * 3)],
        )
        for index in range(2)
    ]
    window = select_evidence_window(workouts, programme_weeks=4)
    assert window.workout_count == 2
    data = _base_data(workouts)
    analysis = analyse_history(data, ["upper", "lower"], programme_weeks=4)
    assert analysis["calibration_required"] is True


def test_equipment_increment_uses_observed_loads_or_settings() -> None:
    workouts = [
        _workout(
            f"w{index}",
            "Upper",
            f"2026-08-{10 + index:02d}T10:00:00Z",
            [("bench", [_normal(80 + index * 2.5, 8)] * 3)],
        )
        for index in range(4)
    ]
    data = _base_data(workouts)
    inferred = analyse_history(data, ["upper", "lower"])
    assert inferred["baselines"]["bench"]["equipment_increment_kg"] == 2.5
    configured = analyse_history(
        data,
        ["upper", "lower"],
        equipment_increments={"bench": 5.0},
    )
    assert configured["baselines"]["bench"]["equipment_increment_kg"] == 5.0


def test_baseline_supporting_workouts_explain_the_evidence() -> None:
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [("bench", [_normal(80, 8)] * 3)],
            )
        ]
    )
    analysis = analyse_history(data, ["upper", "lower"])
    supporting = analysis["baselines"]["bench"]["supporting_workouts"]
    assert supporting[0]["workout_id"] == "w1"
    assert supporting[0]["match_confidence"] in {
        CONFIDENCE_HIGH,
        CONFIDENCE_PROBABLE,
        CONFIDENCE_AMBIGUOUS,
        CONFIDENCE_UNMATCHED,
    }


# ---------------------------------------------------------------------------
# Cache parsing
# ---------------------------------------------------------------------------


def test_parse_cached_workouts_accepts_wrapped_and_list_shapes() -> None:
    templates = {"bench": _template("bench", "Bench Press")}
    workout = {
        "id": "w1",
        "title": "Upper",
        "start_time": "2026-08-10T10:00:00Z",
        "exercises": [
            {
                "exercise_template_id": "bench",
                "sets": [_normal(80, 8)],
            }
        ],
    }
    wrapped = parse_cached_workouts([{"workouts": [workout]}], templates)
    listed = parse_cached_workouts([[workout]], templates)
    single = parse_cached_workouts([workout], templates)
    assert [item.id for item in wrapped] == ["w1"]
    assert [item.id for item in listed] == ["w1"]
    assert [item.id for item in single] == ["w1"]


def test_merge_workout_history_prefers_provider_and_deduplicates() -> None:
    cached = CompletedWorkout(id="w1", title="Old", start_time="2026-08-01T10:00:00Z")
    provider = CompletedWorkout(
        id="w1", title="New", start_time="2026-08-01T10:00:00Z"
    )
    older = CompletedWorkout(
        id="w2", title="Older", start_time="2026-07-01T10:00:00Z"
    )
    merged = merge_workout_history([provider], [cached, older])
    assert [item.id for item in merged] == ["w1", "w2"]
    assert merged[0].title == "New"


# ---------------------------------------------------------------------------
# Persistence and tenant isolation
# ---------------------------------------------------------------------------


def _db(tmp_path: Any) -> str:
    path = str(tmp_path / "history.db")
    init_db(path)
    return path


def test_snapshots_matches_overrides_and_baselines_round_trip(tmp_path: Any) -> None:
    db = _db(tmp_path)
    data = _base_data(
        [
            _workout(
                "w1",
                "Upper",
                "2026-08-10T10:00:00Z",
                [("bench", [_normal(80, 8)] * 3)],
            )
        ]
    )
    snapshots = build_snapshots(
        data.routines,
        data.exercise_templates,
        user_id="user-a",
    )
    assert save_routine_snapshots("user-a", [item.to_dict() for item in snapshots], db)
    loaded = get_routine_snapshots("user-a", db)
    assert len(loaded) == 2
    assert get_routine_snapshots("user-b", db) == []

    analysis = analyse_history(data, ["upper", "lower"], user_id="user-a")
    assert save_routine_workout_matches("user-a", analysis["matches"], db)
    matches = get_routine_workout_matches("user-a", db)
    assert len(matches) == 1
    assert matches[0]["features"]
    assert get_routine_workout_matches("user-b", db) == []

    save_routine_match_override(
        "user-a",
        pattern_type="workout",
        pattern_value="w1",
        routine_id="upper",
        routine_snapshot_id=snapshots[0].id,
        db_path=db,
    )
    assert len(get_routine_match_overrides("user-a", db)) == 1
    assert get_routine_match_overrides("user-b", db) == []

    assert save_exercise_baselines("user-a", analysis["baselines"], db)
    baselines = get_exercise_baselines(
        "user-a", db, rule_version=BASELINE_VERSION
    )
    assert baselines[0]["template_id"] == "bench"
    assert get_exercise_baselines("user-b", db) == []


def test_history_persistence_fails_closed_without_user() -> None:
    with pytest.raises(ValueError):
        save_routine_snapshots("", [], "unused.db")
    with pytest.raises(ValueError):
        get_routine_snapshots("  ", "unused.db")


def test_analyse_history_rejects_unknown_selected_routine() -> None:
    data = _base_data([])
    with pytest.raises(ValueError, match="not available"):
        analyse_history(data, ["missing"])


def test_rule_versions_are_reported() -> None:
    analysis = analyse_history(_base_data([]), ["upper", "lower"])
    assert analysis["rule_versions"]["matcher"] == MATCHER_VERSION
    assert analysis["rule_versions"]["baseline"] == BASELINE_VERSION
