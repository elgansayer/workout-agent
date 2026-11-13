"""Pause/resume, extension and read-only week materialisation (#975)."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import pytest
from dynamic_programme import ProgrammeCadence
from programme_domain import (
    AdaptationService,
    LifecycleService,
    ProgrammeNotFoundError,
    ProgrammeService,
    ProgrammeValidationError,
    WorkoutMatch,
)
from programme_domain.lifecycle import extend_definition
from programme_fixtures import activate, build_preview, make_active, new_user, workout


def at(day: int, month: int = 9, hour: int = 12) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def blocks_of(revision: Any) -> list[tuple[int, int, int]]:
    return [(item.position, item.start_week, item.end_week) for item in revision.blocks]


def test_pause_keeps_history_and_resume_continues_the_rotation(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    lifecycle, service, adaptation = LifecycleService(db), ProgrammeService(db), AdaptationService(db)
    raw = workout("w1", 2, {"bench": [10, 10], "row": [12, 12]}, rpe=None)
    adaptation.review(user, pid, [raw], {"w1": WorkoutMatch("w1", "high", 1, 0.9)}, now=at(2))

    paused = lifecycle.pause(user, pid, now=at(3))
    assert paused.status == "paused" and paused.current_revision is not None
    assert service.get_active_programme(user) is None
    import database

    assert database.get_active_programme(user, db_path=db) is None
    from programme_runtime import load_programme_runtime

    runtime = load_programme_runtime(user, db, today=date(2026, 9, 4), timezone_name="UTC")
    assert runtime.status == "paused"
    event = [item for item in service.list_adaptation_decisions(user, pid) if item.decision_type == "pause"]
    assert event and event[0].output["paused_on"] == "2026-09-03"
    with pytest.raises(ProgrammeValidationError):
        lifecycle.pause(user, pid, now=at(3))  # not active any more

    resumed = lifecycle.resume(user, pid, now=at(5), extend=True)
    assert resumed.status == "active"
    assert resumed.duration_weeks == 8  # two days is not a whole lost week
    state = service.get_programme_week(user, pid, 1).state  # type: ignore[union-attr]
    # Completed work survives the pause and the rotation continues after Upper A.
    assert state.actual["completed_exposures"] == 1
    assert state.actual["next_exposure"]["routine_position"] == 2


def test_resume_after_a_long_pause_extends_through_a_new_revision(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path, duration=8)
    lifecycle, service = LifecycleService(db), ProgrammeService(db)
    before = service.get_programme(user, pid)
    assert before is not None and before.current_revision is not None
    lifecycle.pause(user, pid, now=at(3))
    lifecycle.resume(user, pid, now=at(17))  # 14 days -> two lost weeks

    history = service.list_revision_history(user, pid)
    assert [item.revision_number for item in history] == [1, 2]
    assert history[1].reason == "resume"
    programme = service.get_programme(user, pid)
    assert programme is not None and programme.duration_weeks == 10
    assert programme.end_date == date(2026, 11, 9)  # ten weeks from the unchanged start
    assert programme.start_date == before.start_date
    assert history[0].prescriptions == before.current_revision.prescriptions  # untouched
    kinds = [item.decision_type for item in service.list_adaptation_decisions(user, pid)]
    assert kinds == ["activation", "pause", "extension", "resume"]
    covered = sorted(week for _, start, end in blocks_of(history[1]) for week in range(start, end + 1))
    assert covered == list(range(1, 11))  # every week is covered exactly once


def test_resume_can_skip_extension_and_archives_a_newer_active_programme(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    lifecycle, service = LifecycleService(db), ProgrammeService(db)
    lifecycle.pause(user, pid, now=at(3))
    other = activate(db, user, build_preview(duration=6))
    assert service.get_active_programme(user).id == other  # type: ignore[union-attr]
    lifecycle.resume(user, pid, now=at(24), extend=False)
    assert service.get_programme(user, pid).duration_weeks == 8  # type: ignore[union-attr]
    assert len(service.list_revision_history(user, pid)) == 1
    assert service.get_programme(user, other).status == "archived"  # type: ignore[union-attr]
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT count(*) FROM programmes WHERE user_id = ? AND status = 'active'", (user,)
        ).fetchone()[0] == 1


def test_extension_lengthens_the_current_block_and_shifts_later_blocks(tmp_path: Any) -> None:
    db, user, pid, preview = make_active(tmp_path, duration=8)
    lifecycle, service = LifecycleService(db), ProgrammeService(db)
    before = blocks_of(service.get_active_revision(user))  # type: ignore[arg-type]
    programme = lifecycle.extend(user, pid, 2, now=at(10))  # week 2 of the programme
    after = blocks_of(programme.current_revision)  # type: ignore[arg-type]
    assert programme.duration_weeks == 10
    position, start, end = before[1]
    assert after[1] == (position, start, end + 2)
    assert after[0] == before[0]  # completed blocks are untouched
    assert after[2][1] == before[2][1] + 2 and after[-1][2] == 10
    covered = sorted(week for _, s, e in after for week in range(s, e + 1))
    assert covered == list(range(1, 11))
    revision = programme.current_revision
    assert revision is not None and revision.reason == "extension"
    assert revision.settings["cycle_weeks"] == 10 and revision.settings["programme_spec"]["duration_weeks"] == 10
    assert max(entry["week"] for entry in revision.settings["schedule"]) == 10
    assert service.get_programme_week(user, pid, 10) is not None
    decision = next(item for item in service.list_adaptation_decisions(user, pid) if item.decision_type == "extension")
    assert decision.created_revision and decision.output["added_weeks"] == 2
    assert len(preview["blocks"]) == len(after)  # no block was invented or dropped


def test_extension_is_bounded_and_scoped(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    other = new_user(db, "athlete-b@example.test")
    lifecycle = LifecycleService(db)
    for weeks in (0, 9, -1):
        with pytest.raises(ProgrammeValidationError):
            lifecycle.extend(user, pid, weeks, now=at(3))
    with pytest.raises(ProgrammeNotFoundError):
        lifecycle.extend(other, pid, 1, now=at(3))
    with pytest.raises(ProgrammeNotFoundError):
        lifecycle.pause(other, pid, now=at(3))
    with pytest.raises(ProgrammeNotFoundError):
        lifecycle.materialise_week(other, pid, 1, now=at(3))
    ProgrammeService(db).archive(user, pid)
    with pytest.raises(ProgrammeValidationError):
        lifecycle.extend(user, pid, 1, now=at(3))


def test_extend_definition_is_pure_and_conserves_every_week() -> None:
    preview = build_preview(duration=12)
    original_blocks = [dict(block) for block in preview["blocks"]]
    for at_week in (1, 6, 12, 99):
        extended = extend_definition(preview, 3, at_week)
        assert preview["blocks"] == original_blocks  # the input is never mutated
        spans = sorted((b["start_week"], b["end_week"]) for b in extended["blocks"])
        assert spans[0][0] == 1 and spans[-1][1] == 15
        assert all(spans[i][1] + 1 == spans[i + 1][0] for i in range(len(spans) - 1))
        assert extended["cycle_weeks"] == 15


def test_materialised_weeks_are_read_only_dated_and_show_effective_prescriptions(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path, cadence=ProgrammeCadence(mode="weekly", weekdays=[1, 3]))
    lifecycle, adaptation = LifecycleService(db), AdaptationService(db)
    with sqlite3.connect(db) as conn:
        states_before = conn.execute("SELECT count(*) FROM programme_week_states").fetchone()[0]
    week_three = lifecycle.materialise_week(user, pid, 3, now=at(3))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM programme_week_states").fetchone()[0] == states_before
    assert week_three["start_date"] == "2026-09-15" and week_three["end_date"] == "2026-09-21"
    assert week_three["cadence"] == "calendar"
    assert [item["scheduled_date"] for item in week_three["sessions"]] == ["2026-09-15", "2026-09-17"]
    assert week_three["block"]["key"] == "foundation_volume"
    exercises = week_three["sessions"][0]["exercises"]
    assert {item["template_id"] for item in exercises} == {"bench", "row"}
    assert exercises[0]["sets"] >= 2 and exercises[0]["rep_range"]
    with pytest.raises(ProgrammeValidationError):
        lifecycle.materialise_week(user, pid, 9, now=at(3))

    # An applied pin and week-level adjustments appear on the materialised week.
    adaptation.add_override(
        user, pid, kind="pin", exercise_template_id="bench", value={"fields": ["load"]},
        today=date(2026, 9, 2), weeks=3,
    )
    pinned = lifecycle.materialise_week(user, pid, 1, now=at(3))
    bench = next(item for item in pinned["sessions"][0]["exercises"] if item["template_id"] == "bench")
    assert bench["pinned_fields"] == ["load"]


def test_rolling_week_next_exposure_survives_missed_days(tmp_path: Any) -> None:
    db, user, pid, _ = make_active(tmp_path)
    lifecycle, adaptation = LifecycleService(db), AdaptationService(db)
    late = at(12)  # nothing was logged on the nominal days
    quiet = lifecycle.materialise_week(user, pid, 1, now=late)
    assert quiet["next_exposure"]["routine_position"] == 1
    raw = workout("w1", 11, {"bench": [10, 10], "row": [12, 12]}, rpe=None)
    adaptation.review(user, pid, [raw], {"w1": WorkoutMatch("w1", "high", 1, 0.9)}, now=late)
    week = lifecycle.materialise_week(user, pid, 2, now=late)
    assert week["next_exposure"]["routine_position"] == 2  # the rotation continues, nothing skipped


def test_subsequent_week_states_are_reproducible_from_the_stored_revision(tmp_path: Any) -> None:
    from programme_domain.activation import build_week_state, definition_from_revision
    from programme_domain.weeks import rolling_pointer

    db, user, pid, _ = make_active(tmp_path)
    lifecycle, adaptation, service = LifecycleService(db), AdaptationService(db), ProgrammeService(db)
    sessions = [
        (2, 1, {"bench": [10, 10], "row": [12, 12]}),
        (3, 2, {"squat": [8, 8], "curl": [12, 12]}),
        (5, 1, {"bench": [10, 10], "row": [12, 12]}),
    ]
    for index, (day, position, exercises) in enumerate(sessions):
        raw = workout(f"w{index}", day, exercises, rpe=None)
        adaptation.review(
            user, pid, [raw], {raw["id"]: WorkoutMatch(raw["id"], "high", position, 0.9)}, now=at(day)
        )
    programme = service.get_programme(user, pid)
    assert programme is not None and programme.current_revision is not None
    definition = definition_from_revision(programme, programme.current_revision)
    stored = service.get_programme_week(user, pid, 1).state  # type: ignore[union-attr]
    assert stored is not None
    replay = build_week_state(
        definition,
        start_date=programme.start_date,  # type: ignore[arg-type]
        duration_weeks=8,
        week_number=1,
        timezone_name="UTC",
        today=date(2026, 9, 5),
        rotation_pointer=0,
        exposures=stored.actual["exposures"],
    )
    assert replay.planned == stored.planned and replay.actual == stored.actual

    # A week that was never stored is derived identically from the same inputs.
    derived = lifecycle.materialise_week(user, pid, 2, now=at(9))
    pointer = rolling_pointer(definition["days"], [1, 2, 1])
    expected = build_week_state(
        definition,
        start_date=programme.start_date,  # type: ignore[arg-type]
        duration_weeks=8,
        week_number=2,
        timezone_name="UTC",
        today=date(2026, 9, 9),
        rotation_pointer=pointer,
    )
    assert derived["planned"] == expected.planned
    assert derived["next_exposure"]["routine_position"] == 2  # rotation continues after routine 1
