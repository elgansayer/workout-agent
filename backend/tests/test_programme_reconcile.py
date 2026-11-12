"""Hevy routine drift: exact diff, explicit reconciliation and history (#975)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import database
import pytest
from dynamic_programme import serialise_hevy_source
from hevy_reader import ExerciseTemplate, HevyTrainingData, Routine, RoutineExercise
from programme_domain import (
    DriftConflictError,
    DriftService,
    ProgrammeNotFoundError,
    ProgrammeService,
    ProgrammeValidationError,
    Resolution,
)
from programme_domain.drift import diff_programme
from programme_fixtures import make_active, new_user, training_data


def now() -> datetime:
    return datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def live(data: HevyTrainingData) -> dict[str, Any]:
    return serialise_hevy_source(data)


def modified(change: Any, **kwargs: Any) -> HevyTrainingData:
    data = training_data(**kwargs)
    change(data)
    return data


def exercise(template: str, title: str, sets: int, reps: int, weight: float) -> RoutineExercise:
    return RoutineExercise(
        template_id=template, title=title, sets=sets, rest_seconds=90, target_reps=reps,
        set_targets=[{"type": "normal", "reps": reps, "weight_kg": weight}] * sets,
    )


class Env:
    def __init__(self, tmp_path: Any) -> None:
        self.db, self.user, self.programme_id, self.preview = make_active(tmp_path)
        self.service = ProgrammeService(self.db)
        self.drift = DriftService(self.db)

    @property
    def revisions(self) -> int:
        return len(self.service.list_revision_history(self.user, self.programme_id))

    def current(self) -> Any:
        return self.service.get_active_revision(self.user)

    def diff(self, data: HevyTrainingData) -> dict[str, Any]:
        return self.drift.diff(self.user, self.programme_id, live(data))

    def resolve(self, data: HevyTrainingData, *resolutions: Resolution) -> dict[str, Any]:
        return self.drift.resolve(self.user, self.programme_id, live(data), list(resolutions), now=now())


def routine(report: dict[str, Any], routine_id: str) -> dict[str, Any]:
    return next(item for item in report["routines"] if item["routine_id"] == routine_id)


def kinds(entry: dict[str, Any]) -> set[str]:
    return {item["kind"] for item in entry["changes"]}


def test_detection_is_exact_and_never_touches_the_active_revision(tmp_path: Any) -> None:
    env = Env(tmp_path)
    before = env.current()
    assert env.diff(training_data())["has_drift"] is False

    drifted = modified(lambda d: setattr(d.routines[0], "title", "Upper A (new)"))
    report = env.diff(drifted)
    entry = routine(report, "routine-a")
    assert entry["state"] == "renamed" and kinds(entry) == {"routine_renamed"}
    assert entry["current_title"] == "Upper A (new)" and entry["title"] == "Upper A"
    assert report["writes_to_hevy"] is False and report["status"] == "review_recommended"
    # Detection, however often it runs, creates no revision and rewrites nothing.
    for _ in range(3):
        env.diff(drifted)
    assert env.revisions == 1 and env.current() == before
    assert database.get_active_programme(env.user, db_path=env.db)["definition"]["days"][0]["focus"] == "Upper A"


def test_the_diff_names_every_kind_of_change(tmp_path: Any) -> None:
    env = Env(tmp_path)

    def edit(data: HevyTrainingData) -> None:
        upper = data.routines[0]
        upper.exercises = [
            upper.exercises[1],  # reordered: the row now comes first
            exercise("bench", "Barbell Bench", 4, 8, 60.0),  # renamed and 3 -> 4 sets
            exercise("fly", "Cable Fly", 3, 12, 20.0),  # added
        ]
        data.routines[1].exercises = [data.routines[1].exercises[0]]  # curl removed

    report = env.diff(modified(edit))
    upper, lower = routine(report, "routine-a"), routine(report, "routine-b")
    assert kinds(upper) >= {"reordered", "renamed", "sets_changed", "added"}
    assert upper["state"] == "structural_edit"
    assert kinds(lower) == {"removed"} and lower["changes"][0]["key"] == "curl#1"
    ids = [item["id"] for item in upper["changes"]]
    assert len(ids) == len(set(ids))  # change ids are stable and unique
    assert set(upper["available_actions"]) == {"keep", "merge", "rebase", "replace"}
    assert report["status"] == "action_required"


def test_a_compatible_edit_is_distinguished_from_a_structural_one(tmp_path: Any) -> None:
    env = Env(tmp_path)

    def rename_only(data: HevyTrainingData) -> None:
        data.exercise_templates["bench"] = ExerciseTemplate(
            id="bench", title="Barbell Bench", exercise_type="weight_reps",
            primary_muscle_group="chest", equipment="barbell",
        )
        data.routines[0].exercises[0].title = "Barbell Bench"

    entry = routine(env.diff(modified(rename_only)), "routine-a")
    assert entry["state"] == "compatible_edit" and kinds(entry) == {"renamed"}


def test_a_deleted_routine_and_unsupported_exercises_are_reported(tmp_path: Any) -> None:
    env = Env(tmp_path)
    gone = training_data()
    gone.routines = gone.routines[:1]
    entry = routine(env.diff(gone), "routine-b")
    assert entry["state"] == "deleted" and entry["available_actions"] == ["keep", "replace"]
    assert entry["current_hash"] is None

    def unsupported(data: HevyTrainingData) -> None:
        item = exercise("fly", "Cable Fly", 3, 12, 20.0)
        item.set_targets = [{"type": "normal", "reps": 12, "weight_kg": 20.0, "tempo": "3-1-1"}] * 3
        data.routines[0].exercises.append(item)

    added = next(c for c in routine(env.diff(modified(unsupported)), "routine-a")["changes"] if c["kind"] == "added")
    assert added["supported"] is False


def test_keep_records_an_acknowledgement_and_creates_no_revision(tmp_path: Any) -> None:
    env = Env(tmp_path)
    drifted = modified(lambda d: setattr(d.routines[0], "title", "Upper A (new)"))
    result = env.resolve(drifted, Resolution("routine-a", "keep"))
    assert result["revision_created"] is False and env.revisions == 1
    ack = [i for i in env.service.list_adaptation_decisions(env.user, env.programme_id) if i.decision_type == "drift_keep"]
    assert len(ack) == 1 and ack[0].application_state == "acknowledged"
    # Acknowledging the same provider state twice is idempotent.
    env.resolve(drifted, Resolution("routine-a", "keep"))
    assert len([i for i in env.service.list_adaptation_decisions(env.user, env.programme_id) if i.decision_type == "drift_keep"]) == 1
    assert env.diff(drifted)["has_drift"] is True  # still visible; nothing was adopted


def test_rebase_adopts_every_change_in_a_new_validated_revision(tmp_path: Any) -> None:
    env = Env(tmp_path)
    old = env.current()

    def edit(data: HevyTrainingData) -> None:
        data.routines[0].exercises.append(exercise("fly", "Cable Fly", 3, 12, 20.0))
        data.routines[1].title = "Lower B v2"

    data = modified(edit)
    result = env.resolve(data, Resolution("routine-a", "rebase"), Resolution("routine-b", "rebase"))
    assert result["revision_created"] and result["revision_number"] == 2
    revisions = env.service.list_revision_history(env.user, env.programme_id)
    assert [r.revision_number for r in revisions] == [1, 2]
    assert revisions[0] == old  # history is preserved byte for byte
    new = revisions[1]
    assert new.reason == "rebase"
    assert {r.source_routine_id for r in new.routines} == {"routine-a", "routine-b"}
    titles = {r.source_routine_id: r.title for r in new.routines}
    assert titles["routine-b"] == "Lower B v2"
    fly = [p for p in new.prescriptions if p.exercise_template_id == "fly"]
    assert fly and all(p.set_target["target"] == 3 for p in fly)  # adopted at the provider's sets
    assert env.diff(data)["has_drift"] is False  # the snapshots now match the provider
    hashes = {r.source_routine_id: r.snapshot_hash for r in new.routines}
    provider = {item["id"]: item["source_hash"] for item in live(data)["routines"]}
    assert hashes["routine-a"] == provider["routine-a"]
    decision = next(i for i in env.service.list_adaptation_decisions(env.user, env.programme_id) if i.decision_type == "drift_rebase")
    assert decision.created_revision and decision.from_revision_id == old.id and decision.to_revision_id == new.id
    assert decision.output["resolved"][0]["accepted"]
    # The runtime renders the reconciled revision.
    from datetime import date

    from programme_runtime import load_programme_runtime

    runtime = load_programme_runtime(env.user, env.db, today=date(2026, 9, 2), timezone_name="UTC")
    names = {e["name"] for day in runtime.days for e in day["exercises"]}
    assert "Cable Fly" in names and runtime.revision_number == 2


def test_merge_adopts_only_the_selected_changes(tmp_path: Any) -> None:
    env = Env(tmp_path)

    def edit(data: HevyTrainingData) -> None:
        upper = data.routines[0]
        upper.exercises = [exercise("bench", "Bench Press", 4, 8, 60.0), upper.exercises[1],
                           exercise("fly", "Cable Fly", 3, 12, 20.0)]

    data = modified(edit)
    entry = routine(env.diff(data), "routine-a")
    added = next(c["id"] for c in entry["changes"] if c["kind"] == "added")
    result = env.resolve(data, Resolution("routine-a", "merge", accept=(added,)))
    assert result["resolved"][0]["accepted"] == [added]
    assert "sets_changed:bench#1" in result["resolved"][0]["rejected"]
    new = env.current()
    templates = [p.exercise_template_id for p in new.prescriptions if p.routine_position == 1]
    assert "fly" in templates
    bench = next(p for p in new.prescriptions if p.exercise_template_id == "bench")
    old_bench = env.service.get_revision(env.user, env.programme_id, 1).prescriptions[0]  # type: ignore[union-attr]
    assert bench.set_target == old_bench.set_target  # the rejected sets change was not adopted
    remaining = routine(env.diff(data), "routine-a")
    assert kinds(remaining) == {"sets_changed"}  # residual drift stays visible
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-a", "merge", accept=()))
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-a", "merge", accept=("added:nothing#1",)))


def test_removed_and_reordered_exercises_can_be_adopted_selectively(tmp_path: Any) -> None:
    env = Env(tmp_path)

    def edit(data: HevyTrainingData) -> None:
        data.routines[0].exercises = [data.routines[0].exercises[1], data.routines[0].exercises[0]]
        data.routines[1].exercises = [data.routines[1].exercises[0]]

    data = modified(edit)
    env.resolve(
        data,
        Resolution("routine-a", "merge", accept=("reordered:order",)),
        Resolution("routine-b", "merge", accept=("removed:curl#1",)),
    )
    revision = env.current()
    order = [e["name"] for e in revision.settings["days"][0]["exercises"]]
    assert order == ["Cable Row", "Bench Press"]
    assert [e["template_id"] for e in revision.settings["days"][1]["exercises"]] == ["squat"]
    assert {p.exercise_template_id for p in revision.prescriptions if p.routine_position == 2} == {"squat"}


def test_replace_swaps_a_routine_and_never_reuses_one_already_selected(tmp_path: Any) -> None:
    env = Env(tmp_path)
    data = training_data()
    data.routines.append(
        Routine(id="routine-c", title="Legs C", updated_at="2026-09-01T00:00:00Z",
                exercises=[exercise("squat", "Back Squat", 4, 5, 110.0)])
    )
    data.routines = [r for r in data.routines if r.id != "routine-b"]  # routine-b was deleted
    assert routine(env.diff(data), "routine-b")["state"] == "deleted"
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-b", "merge", accept=("x",)))  # nothing to merge from
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-b", "replace", replacement_routine_id="routine-a"))
    result = env.resolve(data, Resolution("routine-b", "replace", replacement_routine_id="routine-c"))
    assert result["revision_created"]
    revision = env.current()
    assert [r.source_routine_id for r in revision.routines] == ["routine-a", "routine-c"]
    assert revision.routines[1].rotation_position == 2  # rotation order is kept
    assert all(
        entry["routine_id"] != "routine-b"
        for entry in revision.settings["schedule"]
    )
    decision = [i for i in env.service.list_adaptation_decisions(env.user, env.programme_id) if i.decision_type == "drift_replace"]
    assert decision and decision[0].output["resolved"][0]["replacement_routine_id"] == "routine-c"


def test_unsupported_exercises_need_explicit_acknowledgement(tmp_path: Any) -> None:
    env = Env(tmp_path)

    def edit(data: HevyTrainingData) -> None:
        item = exercise("fly", "Cable Fly", 3, 12, 20.0)
        item.set_targets = [{"type": "normal", "reps": 12, "weight_kg": 20.0, "tempo": "3-1-1"}] * 3
        data.routines[0].exercises.append(item)

    data = modified(edit)
    with pytest.raises(ProgrammeValidationError, match="acknowledge"):
        env.resolve(data, Resolution("routine-a", "rebase"))
    assert env.revisions == 1
    env.resolve(data, Resolution("routine-a", "rebase", acknowledge_unsupported=True))
    assert env.revisions == 2


def test_reconciled_prescriptions_must_pass_the_shared_validator(tmp_path: Any) -> None:
    env = Env(tmp_path)
    database.save_training_rules(env.user, {"volume": {"max_sets_per_exercise": 4}}, env.db)
    # Saving the rule keeps the programme (all prescriptions already comply).
    assert env.service.get_active_programme(env.user) is not None
    data = modified(lambda d: d.routines[0].exercises.append(exercise("fly", "Cable Fly", 9, 12, 20.0)))
    with pytest.raises(ProgrammeValidationError, match="violates blocking constraints"):
        env.resolve(data, Resolution("routine-a", "rebase"))
    assert env.revisions == 1  # nothing was persisted


def test_a_stale_diff_is_refused_when_the_provider_changed_again(tmp_path: Any) -> None:
    env = Env(tmp_path)
    first = modified(lambda d: setattr(d.routines[0], "title", "One"))
    seen = routine(env.diff(first), "routine-a")["current_hash"]
    second = modified(lambda d: setattr(d.routines[0], "title", "Two"))
    with pytest.raises(DriftConflictError):
        env.resolve(second, Resolution("routine-a", "rebase", expected_hash=seen))
    assert env.revisions == 1
    env.resolve(first, Resolution("routine-a", "rebase", expected_hash=seen))
    assert env.revisions == 2


def test_reconciliation_never_calls_hevy_and_is_tenant_scoped(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    other = new_user(env.db, "athlete-b@example.test")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reconciliation must not call the Hevy API")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    data = modified(lambda d: setattr(d.routines[0], "title", "Renamed"))
    env.resolve(data, Resolution("routine-a", "rebase"))

    with pytest.raises(ProgrammeNotFoundError):
        env.drift.diff(other, env.programme_id, live(data))
    with pytest.raises(ProgrammeNotFoundError):
        env.drift.resolve(other, env.programme_id, live(data), [Resolution("routine-a", "keep")], now=now())
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-zzz", "keep"))
    with pytest.raises(ProgrammeValidationError):
        env.resolve(data, Resolution("routine-a", "explode"))
    with pytest.raises(ProgrammeValidationError):
        env.drift.resolve(env.user, env.programme_id, live(data), [], now=now())


def test_diff_programme_is_a_pure_function(tmp_path: Any) -> None:
    env = Env(tmp_path)
    definition = copy.deepcopy(env.current().settings)
    snapshot = copy.deepcopy(definition)
    diff_programme(definition, live(modified(lambda d: setattr(d.routines[0], "title", "X"))))
    assert definition == snapshot
