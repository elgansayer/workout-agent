"""Runtime, daily-agent and runner wiring for programme adaptation (#975)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import database
import main
import programme_runtime
from dynamic_programme import ProgrammeCadence
from programme_domain import AdaptationService, ProgrammeService, WorkoutMatch
from programme_fixtures import make_active, new_user, workout
from programme_review_runner import run_review


def at(day: int, hour: int = 18) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def rolling_definition(*, mode: str = "rolling") -> dict[str, Any]:
    days = [
        {"number": 1, "routine_id": "r-a", "focus": "Upper A", "exercises": []},
        {"number": 2, "routine_id": "r-b", "focus": "Lower B", "exercises": []},
        {"number": 3, "routine_id": "r-c", "focus": "Push C", "exercises": []},
    ]
    return {
        "name": "Synthetic",
        "source": "hevy",
        "cycle_weeks": 4,
        "programme_spec": {"start_date": "2026-09-01", "duration_weeks": 4},
        "cadence": {"mode": mode},
        "blocks": [
            {"number": 1, "key": "a", "name": "A", "start_week": 1, "end_week": 4}
        ],
        "days": days,
        "schedule": [
            {"week": 1, "date": "2026-09-01", "routine_id": "r-a", "rotation_position": 1},
            {"week": 1, "date": "2026-09-02", "routine_id": "r-b", "rotation_position": 2},
            {"week": 1, "date": "2026-09-03", "routine_id": "r-c", "rotation_position": 3},
        ],
    }


def resolve(definition: dict[str, Any], today: date, workouts: list[dict[str, Any]] | None = None, **kw: Any):
    return programme_runtime.resolve_programme_runtime(
        {"source": "hevy", "definition": definition, "updated_at": "2026-09-01T00:00:00Z"},
        workouts or [],
        today=today,
        **kw,
    )


def test_cadence_is_read_from_the_preview_shape() -> None:
    assert programme_runtime._cadence_for({"cadence": {"mode": "rolling"}}, []) == "rolling"
    assert programme_runtime._cadence_for({"cadence": {"mode": "weekly"}}, []) == "calendar"
    assert programme_runtime._cadence_for({"programme_spec": {"cadence": {"mode": "rolling"}}}, []) == "rolling"


def test_rolling_cadence_does_not_skip_a_missed_routine() -> None:
    definition = rolling_definition()
    # Nothing completed and the nominal dates have long passed: Upper A is still next.
    quiet = resolve(definition, date(2026, 9, 10))
    assert quiet.cadence == "rolling"
    assert quiet.next_day is not None and quiet.next_day["number"] == 1
    assert quiet.next_session_date == "2026-09-10"
    # After Upper A is completed late, the rotation continues with Lower B, not Push C.
    done = resolve(
        definition,
        date(2026, 9, 10),
        completed_positions=[1],
    )
    assert done.next_day is not None and done.next_day["number"] == 2


def test_calendar_cadence_follows_the_scheduled_dates() -> None:
    definition = rolling_definition(mode="weekly")
    today = resolve(definition, date(2026, 9, 2))
    assert today.cadence == "calendar"
    assert today.next_day is not None and today.next_day["number"] == 2
    assert today.next_session_date == "2026-09-02"


def test_a_week_specific_prescription_wins_over_the_block_prescription() -> None:
    definition = rolling_definition()
    definition["days"][0]["exercises"] = [
        {
            "name": "Bench",
            "template_id": "bench",
            "prescriptions": [
                {"block_number": 1, "sets": 4, "rep_range": "6-8", "week_number": None},
                {"block_number": 1, "sets": 3, "rep_range": "6-8", "week_number": 2},
            ],
        }
    ]
    week_one = resolve(definition, date(2026, 9, 2))
    assert week_one.days[0]["exercises"][0]["scheme"] == "4 × 6-8"
    week_two = resolve(definition, date(2026, 9, 9))
    assert week_two.week == 2 and week_two.days[0]["exercises"][0]["scheme"] == "3 × 6-8"


def test_the_loaded_runtime_shows_applied_adjustments_pins_and_recorded_rotation(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    adaptation = AdaptationService(db)
    for index, day in enumerate((2, 3, 4, 5)):
        raw = workout(f"w{index}", day, {"bench": [10, 10], "row": [12, 12]}, rpe=None)
        adaptation.review(user, pid, [raw], {raw["id"]: WorkoutMatch(raw["id"], "high", 1, 0.9)}, now=at(day))
    adaptation.add_override(
        user, pid, kind="pin", exercise_template_id="row", value={"fields": ["sets"]},
        today=date(2026, 9, 5), weeks=2,
    )
    runtime = programme_runtime.load_programme_runtime(
        user, db, today=date(2026, 9, 6), timezone_name="UTC"
    )
    assert runtime.status == "active" and runtime.programme_id == pid and runtime.revision_number == 2
    bench = next(e for e in runtime.days[0]["exercises"] if e["template_id"] == "bench")
    assert bench["load_target_kg"] == 62.5 and "progress_load" in bench["adjustments"]
    row = next(e for e in runtime.days[0]["exercises"] if e["template_id"] == "row")
    assert row["pinned_fields"] == ["sets"]
    # The recorded sessions (all Upper A) mean Lower B is next, whatever the weekday.
    assert runtime.cadence == "rolling" and runtime.next_day is not None
    assert runtime.next_day["number"] == 2


def test_the_runtime_never_fails_when_adaptation_state_is_unreadable(tmp_path: Any, monkeypatch: Any) -> None:
    db, user, _pid, _ = make_active(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(ProgrammeService, "list_week_states", broken)
    runtime = programme_runtime.load_programme_runtime(
        user, db, today=date(2026, 9, 6), timezone_name="UTC"
    )
    assert runtime.status == "active" and runtime.next_day is not None


def test_the_daily_record_merges_into_the_week_state_instead_of_replacing_it(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    adaptation, service = AdaptationService(db), ProgrammeService(db)
    raw = workout("w1", 2, {"bench": [10, 10], "row": [12, 12]}, rpe=None)
    adaptation.review(user, pid, [raw], {"w1": WorkoutMatch("w1", "high", 1, 0.9)}, now=at(2))
    programme = service.get_programme(user, pid)
    runtime = SimpleNamespace(
        programme_id=pid,
        revision_id=programme.current_revision_id,  # type: ignore[union-attr]
        revision_number=1,
        week=1,
        next_day={"routine_id": "routine-b", "focus": "Lower B"},
    )
    config = SimpleNamespace(database_path=db)
    summary = SimpleNamespace(as_text=lambda: "summary text")
    main._persist_week_state(config, runtime, user, "2026-09-03", "plan text", summary)  # type: ignore[arg-type]
    state = service.get_programme_week(user, pid, 1).state  # type: ignore[union-attr]
    assert state.planned["daily"]["2026-09-03"]["plan"] == "plan text"
    assert state.actual["daily_summaries"]["2026-09-03"] == "summary text"
    assert state.actual["exposures"][0]["workout_id"] == "w1"  # recorded exposures survive
    assert state.planned["week_number"] == 1 and state.planned["exposures"]
    # A later review keeps the daily record too.
    raw2 = workout("w2", 3, {"squat": [8, 8], "curl": [12, 12]}, rpe=None)
    adaptation.review(user, pid, [raw2], {"w2": WorkoutMatch("w2", "high", 2, 0.9)}, now=at(3))
    state = service.get_programme_week(user, pid, 1).state  # type: ignore[union-attr]
    assert "2026-09-03" in state.planned["daily"] and len(state.actual["exposures"]) == 2


def test_the_daily_review_hook_never_blocks_the_cycle(tmp_path: Any, monkeypatch: Any) -> None:
    db, user, _pid, _ = make_active(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("programme_review_runner.run_review", broken)
    main._review_programme(SimpleNamespace(database_path=db), user)  # type: ignore[arg-type]


def _cache(db: str, user: str, raw: dict[str, Any]) -> None:
    database.save_workout(raw, db, raw["start_time"][:10], user_id=user)


def test_the_runner_reviews_only_the_tenants_own_cached_workouts(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path, cadence=ProgrammeCadence(mode="rolling"))
    other = new_user(db, "athlete-b@example.test")
    service = ProgrammeService(db)
    assert run_review(other, db, now=at(5)) is None  # no active programme -> nothing to do

    mine = workout("m1", 2, {"bench": [10, 10, 10], "row": [12, 12, 12]}, rpe=None)
    theirs = workout("t1", 3, {"squat": [8, 8, 8], "curl": [12, 12, 12]}, rpe=None)
    _cache(db, user, mine)
    _cache(db, other, theirs)
    result = run_review(user, db, now=at(5))
    assert result is not None and result.evaluated == 1
    seen = {item.source_workout_id for item in service.list_adaptation_decisions(user, pid) if item.source_workout_id}
    assert seen == {"m1"}  # the other tenant's workout is never read
    decision = next(item for item in result.decisions if item.exercise_template_id == "bench")
    assert decision.inputs["match"]["confidence"] in {"high", "probable"}
    assert decision.scope["routine_position"] == 1
    # Running again is a no-op.
    again = run_review(user, db, now=at(5))
    assert again is not None and again.replayed == 1 and not again.decisions


def test_a_user_confirmed_match_is_authoritative_in_the_runner(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    service = ProgrammeService(db)
    odd = workout("x1", 2, {"bench": [10, 10, 10]}, rpe=None)  # only half of Upper A
    _cache(db, user, odd)
    raw = {"id": "x1", "title": "Session", "start_time": odd["start_time"], "end_time": odd["end_time"], "exercises": []}
    from hevy_history import parse_cached_workouts

    parsed = parse_cached_workouts([odd])
    AdaptationService(db).confirm_match(user, pid, parsed[0], 1, now=at(5))
    assert raw["id"] == "x1"
    result = run_review(user, db, now=at(5))
    assert result is not None
    assert not any(
        item.decision_type == "request_confirmation" and item.application_state == "pending_confirmation"
        for item in service.list_adaptation_decisions(user, pid)
    )
