"""Transactional activation, week materialisation and adaptation persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

import database
import pytest
from dynamic_programme import ProgrammePreviewRequest, build_programme_preview
from hevy_reader import (
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from programme_engine import (
    DecisionType,
    ProgrammeValidationError,
    compute_source_hash,
)
from programme_lifecycle import (
    activate_from_preview,
    current_week_number,
    delete_workout_event,
    extend_programme,
    get_active_revision,
    get_revision,
    ingest_workout_event,
    list_decisions,
    list_overrides,
    list_revisions,
    materialise_week,
    next_exposure,
    programme_state,
    rebase_active_programme,
    record_override,
    resolve_decision,
    revert_override,
    set_programme_paused,
)


def _training_data(*, duplicate_days: bool = False) -> HevyTrainingData:
    bench = ExerciseTemplate(
        id="bench",
        title="Bench Press",
        exercise_type="weight_reps",
        primary_muscle_group="chest",
    )
    row = ExerciseTemplate(
        id="row",
        title="Row",
        exercise_type="weight_reps",
        primary_muscle_group="upper_back",
    )
    templates = {"bench": bench, "row": row}

    def exercises() -> list[RoutineExercise]:
        return [
            RoutineExercise(
                template_id="bench",
                title="Bench Press",
                sets=3,
                rest_seconds=120,
                target_reps=8,
                set_targets=[
                    {"type": "normal", "reps": 8, "weight_kg": 60.0}
                ],
            )
        ]

    routines = [
        Routine(
            id="routine-a",
            title="Upper A",
            exercises=exercises(),
        ),
        Routine(
            id="routine-b",
            title="Upper B",
            exercises=exercises() if duplicate_days else [
                RoutineExercise(
                    template_id="row",
                    title="Row",
                    sets=3,
                    rest_seconds=90,
                    target_reps=10,
                    set_targets=[
                        {"type": "normal", "reps": 10, "weight_kg": 50.0}
                    ],
                )
            ],
        ),
    ]
    return HevyTrainingData(
        username="tester",
        workout_count=24,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=[],
        folders={},
    )


def _preview(
    *,
    start_date: date | None = None,
    cadence: str = "rolling",
    weekdays: list[int] | None = None,
    data: HevyTrainingData | None = None,
) -> dict[str, Any]:
    return build_programme_preview(
        data or _training_data(),
        ProgrammePreviewRequest(
            selected_routine_ids=["routine-a", "routine-b"],
            duration_weeks=8,
            goal="hypertrophy",
            start_date=start_date or _today(),
            sessions_per_week=2,
            cadence=cadence,
            weekdays=weekdays,
        ),
    )


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _db(tmp_path: Any) -> tuple[str, str]:
    db_path = str(tmp_path / "programme.db")
    database.init_db(db_path)
    return db_path, database.get_legacy_user_id(db_path)


def _workout(
    workout_id: str,
    routine_id: str | None,
    *,
    template_id: str = "bench",
    sets: int = 3,
    day: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": workout_id,
        "title": "Synthetic session",
        "end_time": f"{day or _today().isoformat()}T10:00:00Z",
        "exercises": [
            {
                "exercise_template_id": template_id,
                "title": template_id,
                "sets": [
                    {"reps": 8, "weight_kg": 60.0} for _ in range(sets)
                ],
            }
        ],
    }
    if routine_id:
        payload["routine_id"] = routine_id
    return payload


def _decision_for(decisions: list[dict[str, Any]], template_id: str) -> dict[str, Any]:
    return next(
        item
        for item in decisions
        if item.get("exercise_template_id") == template_id
        and not item.get("programme_level")
    )


def test_activation_is_atomic_and_materialises_first_week(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    preview = _preview()

    result = activate_from_preview(user_id, preview, db_path)

    assert result.revision["revision_number"] == 1
    assert result.revision["source_hash"] == compute_source_hash(
        preview["source_snapshots"]
    )
    assert result.week["week_number"] == 1
    assert len(result.exposures) == 2
    assert result.exposures[0]["routine_id"] == "routine-a"

    active = database.get_active_programme(user_id, db_path)
    assert active is not None
    assert active["definition"]["preview_token"] == preview["preview_token"]
    assert get_active_revision(user_id, db_path)["id"] == result.revision["id"]

    with database._connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM programmes WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM programme_audit_events "
            "WHERE user_id = ? AND event_type = 'programme_activated'",
            (user_id,),
        ).fetchone()[0] == 1


def test_stale_or_tampered_preview_fails_without_activating(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    preview = _preview()

    with pytest.raises(ProgrammeValidationError, match="stale"):
        activate_from_preview(
            user_id,
            preview,
            db_path,
            expected_token="0" * 64,
        )
    assert get_active_revision(user_id, db_path) is None

    tampered = json.loads(json.dumps(preview))
    tampered["preview_token"] = "f" * 64
    with pytest.raises(ProgrammeValidationError, match="token"):
        activate_from_preview(user_id, tampered, db_path)
    assert get_active_revision(user_id, db_path) is None


def test_replacement_activation_archives_previous_revision(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    first = activate_from_preview(user_id, _preview(), db_path)
    second = activate_from_preview(
        user_id,
        _preview(start_date=_today() + timedelta(days=1)),
        db_path,
    )

    assert second.revision["revision_number"] == 2
    revisions = list_revisions(user_id, db_path)
    assert [item["revision_number"] for item in revisions] == [2, 1]
    assert get_revision(user_id, first.revision["id"], db_path) is not None
    with database._connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM programmes WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchone()[0] == 1


def test_week_states_are_reproducible_and_independent_of_materialise_order(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)

    week_three = materialise_week(user_id, 3, db_path)
    week_two = materialise_week(user_id, 2, db_path)
    repeat = materialise_week(user_id, 2, db_path)

    assert week_two == repeat
    assert week_two["week_number"] == 2
    assert week_two["rotation_offset"] == 0
    assert week_three["rotation_offset"] == 0
    assert [item["slot"] for item in week_two["exposures"]] == [0, 1]


def test_calendar_cadence_uses_explicit_local_weekdays(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    monday = date(2026, 9, 7)
    assert monday.weekday() == 0
    activate_from_preview(
        user_id,
        _preview(start_date=monday, cadence="calendar", weekdays=[0, 2]),
        db_path,
    )

    week = materialise_week(user_id, 1, db_path)
    assert week["cadence"] == "calendar"
    assert [item["planned_date"] for item in week["exposures"]] == [
        "2026-09-07",
        "2026-09-09",
    ]


def test_missed_rolling_session_is_not_skipped_when_date_passes(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(
        user_id,
        _preview(start_date=_today() - timedelta(weeks=2)),
        db_path,
    )

    candidate = next_exposure(user_id, db_path)
    assert candidate is not None
    assert candidate["week_number"] == 1
    assert candidate["slot"] == 0
    assert candidate["planned_date"] < _today().isoformat()


def test_ingestion_is_idempotent_for_identical_replay(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    workout = _workout("workout-1", "routine-a")

    first = ingest_workout_event(user_id, workout, db_path)
    second = ingest_workout_event(user_id, workout, db_path)

    assert first.match_confidence == "high"
    assert first.duplicate is False
    assert second.duplicate is True
    assert first.exposure_id == second.exposure_id
    assert len(list_decisions(user_id, db_path)) == len(first.decisions)
    with database._connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM programme_workout_observations "
            "WHERE user_id = ? AND workout_id = 'workout-1'",
            (user_id,),
        ).fetchone()[0] == 1


def test_repeated_successes_progress_only_after_hysteresis(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    for week in range(1, 5):
        materialise_week(user_id, week, db_path)

    results = [
        ingest_workout_event(
            user_id,
            _workout(
                f"s{index}",
                "routine-a",
                day=(_today() + timedelta(days=index)).isoformat(),
            ),
            db_path,
        )
        for index in range(4)
    ]

    assert all(
        _decision_for(item.decisions, "bench")["decision_type"]
        == DecisionType.HOLD.value
        for item in results[:3]
    )
    progressed = _decision_for(results[3].decisions, "bench")
    assert progressed["decision_type"] in {
        DecisionType.PROGRESS_LOAD.value,
        DecisionType.PROGRESS_REPS.value,
    }
    assert progressed["applied"] is True
    assert progressed["rationale_codes"]


def test_single_poor_session_does_not_deload(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    materialise_week(user_id, 1, db_path)

    result = ingest_workout_event(
        user_id,
        _workout("poor-1", "routine-a", sets=1),
        db_path,
    )

    decision = _decision_for(result.decisions, "bench")
    assert decision["decision_type"] == DecisionType.HOLD.value
    assert decision["decision_type"] != DecisionType.FATIGUE_MANAGEMENT.value
    assert not any(
        item["decision_type"] == DecisionType.PROGRAMME_FATIGUE_REVISION.value
        for item in list_decisions(user_id, db_path)
    )


def test_repeated_failure_reduces_within_bounds(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    for week in range(1, 5):
        materialise_week(user_id, week, db_path)

    ingest_workout_event(user_id, _workout("f1", "routine-a", sets=1), db_path)
    ingest_workout_event(user_id, _workout("f2", "routine-a", sets=1), db_path)
    result = ingest_workout_event(user_id, _workout("f3", "routine-a", sets=1), db_path)

    decision = _decision_for(result.decisions, "bench")
    assert decision["decision_type"] in {
        DecisionType.REDUCE_LOAD.value,
        DecisionType.REDUCE_SETS.value,
    }
    accepted = decision["accepted_prescription"]
    assert int(accepted["sets"]) >= 1
    if accepted.get("load_kg"):
        assert float(accepted["load_kg"]) <= 60.0


def test_ambiguous_match_requires_confirmation_without_structural_change(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(
        user_id,
        _preview(data=_training_data(duplicate_days=True)),
        db_path,
    )
    result = ingest_workout_event(
        user_id,
        _workout("ambiguous-1", None),
        db_path,
    )

    assert result.match_confidence == "ambiguous"
    assert result.exposure_id is None
    pending = list_decisions(user_id, db_path)
    assert pending[0]["decision_type"] == DecisionType.REQUEST_CONFIRMATION.value
    assert pending[0]["requires_confirmation"] is True
    with database._connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM programme_exposures WHERE status != 'planned'"
        ).fetchone()[0] == 0


def test_unmatched_workout_cannot_drive_adaptation(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    result = ingest_workout_event(
        user_id,
        {"id": "unknown-1", "routine_id": "not-in-programme", "exercises": []},
        db_path,
    )

    assert result.status == "unmatched"
    assert result.decisions == []
    assert list_decisions(user_id, db_path)[0]["requires_confirmation"] is True


def test_durable_override_is_applied_and_revertible(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    revision = activate_from_preview(user_id, _preview(), db_path).revision
    materialise_week(user_id, 1, db_path)
    override = record_override(
        user_id,
        int(revision["id"]),
        "bench",
        DecisionType.HOLD.value,
        {"sets": 5, "rep_range": "6-8", "load_kg": 55.0},
        db_path,
        start_week=1,
        end_week=8,
        reason="travel week",
    )

    first = ingest_workout_event(user_id, _workout("o1", "routine-a"), db_path)
    decision = _decision_for(first.decisions, "bench")
    assert decision["decision_type"] == DecisionType.USER_OVERRIDE.value
    assert decision["accepted_prescription"]["sets"] == 5

    assert revert_override(user_id, override["id"], db_path) is True
    assert list_overrides(user_id, db_path) == []


def test_pending_programme_fatigue_revision_is_confirmation_gated(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    for week in range(1, 4):
        materialise_week(user_id, week, db_path)
    ingest_workout_event(user_id, _workout("fat-1", "routine-a", sets=1), db_path)
    ingest_workout_event(user_id, _workout("fat-2", "routine-a", sets=1), db_path)

    decisions = list_decisions(user_id, db_path)
    pending = next(
        (
            item
            for item in decisions
            if item["decision_type"] == DecisionType.PROGRAMME_FATIGUE_REVISION.value
        ),
        None,
    )
    if pending is not None:
        assert pending["requires_confirmation"] is True
        assert pending["applied"] is False


def test_source_drift_never_mutates_active_revision_and_rebase_preserves_history(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    first = activate_from_preview(user_id, _preview(), db_path)
    original_definition = first.revision["definition"]

    changed = _training_data()
    changed.routines[0].title = "Upper A renamed"
    changed.routines[0].exercises[0].set_targets = [
        {"type": "normal", "reps": 6, "weight_kg": 65.0}
    ]

    keep = rebase_active_programme(user_id, changed, db_path, action="keep")
    assert keep["action"] == "keep"
    assert keep["diff"]["has_drift"] is True
    assert get_active_revision(user_id, db_path)["definition"] == original_definition

    rebased = rebase_active_programme(user_id, changed, db_path, action="rebase")
    assert rebased["revision"]["revision_number"] == 2
    assert rebased["revision"]["supersedes_revision_id"] == first.revision["id"]
    assert get_revision(user_id, first.revision["id"], db_path)["definition"] == original_definition
    assert get_active_revision(user_id, db_path)["id"] == rebased["revision"]["id"]


def test_tenant_isolation_for_revisions_decisions_and_overrides(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    revision = activate_from_preview(user_id, _preview(), db_path).revision
    ingest_workout_event(user_id, _workout("tenant-1", "routine-a"), db_path)
    record_override(
        user_id,
        int(revision["id"]),
        "bench",
        DecisionType.HOLD.value,
        {"sets": 3, "rep_range": "8-10"},
        db_path,
    )
    other = database.get_or_create_user("other@example.test", db_path=db_path)

    assert get_revision(other["id"], int(revision["id"]), db_path) is None
    assert get_active_revision(other["id"], db_path) is None
    assert list_decisions(other["id"], db_path) == []
    assert list_overrides(other["id"], db_path) == []
    with pytest.raises(ValueError):
        ingest_workout_event(other["id"], _workout("tenant-2", "routine-a"), db_path)


def test_programme_state_reports_week_and_next_exposure(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    assert programme_state(user_id, db_path)["setup_required"] is True

    activate_from_preview(user_id, _preview(), db_path)
    state = programme_state(user_id, db_path)
    assert state["setup_required"] is False
    assert state["week"]["week_number"] == current_week_number(user_id, db_path)
    assert state["next_exposure"]["routine_id"] == "routine-a"


def test_completed_weeks_remain_immutable_after_revision(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    first = activate_from_preview(user_id, _preview(), db_path)
    week_one = materialise_week(user_id, 1, db_path)

    rebase_active_programme(
        user_id,
        _training_data(),
        db_path,
        action="rebase",
    )

    with database._connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute(
            """
            SELECT * FROM programme_week_states
            WHERE user_id = ? AND revision_id = ? AND week_number = 1
            """,
            (user_id, first.revision["id"]),
        ).fetchone()
    assert stored is not None
    assert stored["id"] == week_one["id"]
    assert json.loads(stored["overrides"]) == week_one["overrides"]
    assert get_active_revision(user_id, db_path)["id"] != first.revision["id"]


def test_decision_resolution_keeps_audit_row(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    ingest_workout_event(
        user_id,
        {"id": "unknown-2", "routine_id": "not-in-programme", "exercises": []},
        db_path,
    )
    pending = list_decisions(user_id, db_path)[0]
    resolved = resolve_decision(
        user_id, int(pending["id"]), db_path, confirmed=False
    )
    assert resolved is not None
    assert resolved["resolution"] == "reverted"
    stored = list_decisions(user_id, db_path)[0]
    assert stored["id"] == pending["id"]
    assert stored["resolution"] == "reverted"


def test_workout_update_and_delete_replay_preserve_audit(tmp_path: Any) -> None:
    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    materialise_week(user_id, 1, db_path)
    first = ingest_workout_event(
        user_id, _workout("mutable-1", "routine-a"), db_path
    )
    updated = ingest_workout_event(
        user_id, _workout("mutable-1", "routine-a", sets=1), db_path
    )

    assert first.duplicate is False
    assert updated.duplicate is False
    with database._connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        observations = conn.execute(
            "SELECT status FROM programme_workout_observations "
            "WHERE user_id = ? AND workout_id = 'mutable-1' ORDER BY id",
            (user_id,),
        ).fetchall()
    assert [row["status"] for row in observations] == ["completed", "partial"]
    assert len(list_decisions(user_id, db_path)) == 2

    assert delete_workout_event(user_id, "mutable-1", db_path) is True
    with database._connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT status FROM programme_workout_observations "
                "WHERE user_id = ? AND workout_id = 'mutable-1'",
                (user_id,),
            )
        }
        exposure = conn.execute(
            "SELECT status, completed_workout_id FROM programme_exposures "
            "WHERE user_id = ? AND routine_id = 'routine-a' ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
    assert statuses == {"deleted"}
    assert exposure["status"] == "planned"
    assert exposure["completed_workout_id"] is None
    assert len(list_decisions(user_id, db_path)) == 2


def test_programme_extension_creates_revision_and_covers_new_weeks(
    tmp_path: Any,
) -> None:
    db_path, user_id = _db(tmp_path)
    first = activate_from_preview(user_id, _preview(), db_path)
    extended = extend_programme(user_id, 3, db_path)

    assert extended["revision_number"] == 2
    assert extended["supersedes_revision_id"] == first.revision["id"]
    definition = extended["definition"]
    assert definition["programme_spec"]["duration_weeks"] == 11
    assert definition["blocks"][-1]["end_week"] == 11
    assert sum(block["duration_weeks"] for block in definition["blocks"]) == 11
    assert all(
        len(exercise["prescriptions"]) == len(definition["blocks"])
        for day in definition["days"]
        for exercise in day["exercises"]
    )
    assert get_revision(user_id, first.revision["id"], db_path) is not None


def test_pause_and_resume_create_revisions_and_shift_start_date(
    tmp_path: Any,
) -> None:
    from datetime import datetime, timezone

    from programme_runtime import resolve_programme_runtime

    db_path, user_id = _db(tmp_path)
    activate_from_preview(user_id, _preview(), db_path)
    paused_at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    paused = set_programme_paused(user_id, True, db_path, now=paused_at)
    assert paused["definition"]["programme_spec"]["paused"] is True
    assert paused["definition"]["programme_spec"]["paused_at"] == "2026-09-10"

    active = database.get_active_programme(user_id, db_path)
    runtime = resolve_programme_runtime(
        active, [], today=date(2026, 9, 11), timezone_name="UTC"
    )
    assert runtime.status == "paused"
    assert runtime.next_day is None

    resumed = set_programme_paused(
        user_id,
        False,
        db_path,
        now=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert resumed["revision_number"] == 3
    spec = resumed["definition"]["programme_spec"]
    assert spec["paused"] is False
    expected_start = date.fromisoformat(_preview()["programme_spec"]["start_date"])
    assert spec["start_date"] == (expected_start + timedelta(days=4)).isoformat()
