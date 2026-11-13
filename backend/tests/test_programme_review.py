"""Adaptation review: ingestion, audit, replay, confirmation, overrides, revert."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from typing import Any

import pytest
from programme_domain import (
    AdaptationService,
    LifecycleService,
    ProgrammeNotFoundError,
    ProgrammeService,
    ProgrammeValidationError,
    WorkoutMatch,
)
from programme_domain import adaptation as engine
from programme_fixtures import activate, make_active, new_user, workout

HIGH = "high"


def now(day: int = 7, hour: int = 18) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def match(workout_id: str, position: int = 1, confidence: str = HIGH, **kw: Any) -> dict[str, WorkoutMatch]:
    return {workout_id: WorkoutMatch(workout_id, confidence, position, 0.9, "test", **kw)}


class Harness:
    def __init__(self, tmp_path: Any, **kwargs: Any) -> None:
        self.db, self.user, self.programme_id, self.preview = make_active(tmp_path, **kwargs)
        self.service = ProgrammeService(self.db)
        self.adaptation = AdaptationService(self.db)
        self.lifecycle = LifecycleService(self.db)

    def review(self, raw: dict[str, Any], position: int = 1, confidence: str = HIGH, **kw: Any):
        return self.adaptation.review(
            self.user, self.programme_id, [raw], match(raw["id"], position, confidence, **kw),
            now=now(),
        )

    def decisions(self, **kwargs: Any):
        return self.service.list_adaptation_decisions(self.user, self.programme_id)

    def exercise_decisions(self, template: str):
        return [item for item in self.decisions() if item.exercise_template_id == template]


def session(wid: str, day: int, reps: list[float], template: str = "bench", **kw: Any) -> dict[str, Any]:
    return workout(wid, day, {template: reps}, rpe=kw.pop("rpe", None), **kw)


# --- audit ----------------------------------------------------------------------


def test_every_decision_is_explainable_from_stored_inputs(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    result = h.review(session("w1", 2, [10, 10]), confidence="probable")
    (decision,) = result.decisions
    assert decision.rule_version == engine.RULE_VERSION
    assert decision.scope["user_id"] == h.user
    assert decision.scope["programme_id"] == h.programme_id
    assert {"revision_id", "week_number", "routine_position", "exercise_template_id"} <= set(decision.scope)
    observation = decision.inputs["observation"]
    assert observation["workout_id"] == "w1" and observation["sets"][0]["reps"] == 10
    assert decision.inputs["match"]["confidence"] == "probable"
    assert {"exposures", "confidence"} <= set(decision.inputs["baseline"])
    assert decision.inputs["assessment"]["signal"] == "success"
    assert decision.inputs["constraints"]["blocked"] is False
    assert decision.inputs["recovery"] is None  # missing recovery data is recorded, not guessed
    assert decision.inputs["policy"]["min_failed_exposures"] == 2
    assert {"previous_prescription", "proposed_prescription", "rationale_codes", "bounds"} <= set(decision.output)
    assert decision.output["rule_version"] == engine.RULE_VERSION
    assert decision.application_state in {"auto_applied", "informational", "pending_confirmation"}
    assert decision.explanation == engine.explain(
        decision.decision_type, decision.output["rationale_codes"]
    )
    # The explanation is rebuilt from stored codes alone (no model conversation).
    from programme_domain import decision_view

    assert decision_view(decision)["explanation"] == decision.explanation


def test_replaying_an_identical_event_is_idempotent(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    raw = session("w1", 2, [10, 10])
    first = h.review(raw)
    before = [item.id for item in h.decisions()]
    state_before = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    for _ in range(3):
        again = h.review(raw)
        assert again.replayed == 1 and again.evaluated == 0 and not again.decisions
    assert [item.id for item in h.decisions()] == before
    state_after = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert state_after.actual == state_before.actual
    assert first.evaluated == 1


def test_one_poor_session_holds_and_never_deloads(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    result = h.review(session("w1", 2, [2, 2]))
    assert [item.decision_type for item in result.decisions] == ["hold"]
    assert result.programme_decision is None
    assert not [item for item in h.decisions() if item.decision_type in {
        "programme_fatigue_management", "exercise_fatigue_management", "reduce_sets"}]
    assert h.service.get_programme(h.user, h.programme_id).current_revision.revision_number == 1  # type: ignore[union-attr]


def test_progression_is_week_level_and_leaves_the_revision_immutable(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    revision_before = h.service.get_active_revision(h.user)
    assert revision_before is not None
    kinds = []
    for index, day in enumerate((2, 3, 4, 5)):
        kinds.append(h.review(session(f"w{index}", day, [10, 10])).decisions[0].decision_type)
    # Primary lifts use the conservative model (3 exposures) plus one for a sparse baseline.
    assert kinds == ["progress_reps", "progress_reps", "progress_reps", "progress_load"]
    history = h.service.list_revision_history(h.user, h.programme_id)
    assert len(history) == 1  # no revision for a week-level change
    revision_after = history[0]
    assert revision_after.prescriptions == revision_before.prescriptions
    effective = h.adaptation.effective_prescription(
        revision_after.settings, h.decisions(), revision_after,
        template_id="bench", routine_position=1, week=1,
    )
    assert effective is not None and effective[0].load_kg == 62.5
    progress = h.exercise_decisions("bench")[-1]
    assert progress.application_state == "auto_applied"
    assert not progress.created_revision
    assert progress.output["proposed_prescription"]["load_change_percent"] <= 5.0


def test_ambiguous_matches_wait_for_confirmation_then_drive_the_decision(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    raw = session("w1", 2, [10, 10])
    result = h.review(raw, confidence="ambiguous")
    (pending,) = result.decisions
    assert pending.decision_type == "request_confirmation"
    assert pending.application_state == "pending_confirmation"
    assert pending.output["proposed_prescription"] == {}
    week = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert week.actual["ambiguous_workouts"] == ["w1"]
    assert week.actual["completed_exposures"] == 0
    assert week.actual["next_exposure"]["routine_position"] == 1  # an ambiguous workout never moves the rotation
    assert week.review_state == "needs_confirmation"

    confirmed = h.adaptation.confirm_match(h.user, h.programme_id, raw, 1, now=now())
    assert confirmed.evaluated == 1
    states = {item.id: item.application_state for item in h.decisions() if item.exercise_template_id == "bench"}
    assert states[pending.id] == "superseded"
    fresh = [item for item in h.exercise_decisions("bench") if item.id != pending.id]
    assert fresh and fresh[0].decision_type == "progress_reps"
    assert fresh[0].inputs["match"]["user_confirmed"] is True
    matches = h.service.list_routine_workout_matches(h.user, h.programme_id)
    assert matches and matches[0].user_confirmed and matches[0].workout_id == "w1"
    week = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert week.actual["ambiguous_workouts"] == []
    assert week.actual["partial_exposures"] == 1  # only one of the routine's exercises was logged
    assert week.actual["next_exposure"]["routine_position"] == 2  # now the confirmed session counts
    assert week.review_state == "reviewed"


def test_unmatched_workouts_are_tracked_but_never_change_prescriptions(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    result = h.adaptation.review(
        h.user, h.programme_id, [session("w9", 2, [10, 10])], {}, now=now()
    )
    assert [item.decision_type for item in result.decisions] == []  # no routine, no exercise decision
    week = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert week.actual["unmatched_workouts"] == ["w9"]
    assert week.actual["next_exposure"]["routine_position"] == 1  # the rotation did not move


def test_partial_and_substituted_sessions_are_tracked(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    h.review(workout("w1", 2, {"bench": [10, 10]}, rpe=None))  # row (planned) is skipped
    state = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert state.actual["partial_exposures"] == 1
    assert state.actual["skipped_exercises"] == ["row"]
    assert 0 < state.adherence < 1  # type: ignore[operator]

    h.adaptation.add_override(
        h.user, h.programme_id, kind="substitution", exercise_template_id="row",
        value={"performed_template_id": "machine-row"}, today=date(2026, 9, 3),
    )
    h.review(workout("w2", 3, {"bench": [10, 10], "machine-row": [10, 10]}, rpe=None), position=1)
    state = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert state.actual["substituted_exposures"] == 1
    assert state.actual["substitutions"] == [{"planned": "row", "performed": "machine-row"}]


# --- structural change -------------------------------------------------------------


def test_structural_adaptation_appends_a_validated_revision_and_preserves_history(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    original = h.service.get_active_revision(h.user)
    assert original is not None
    types = []
    for index, day in enumerate((9, 10, 11)):  # week 2: the calibration block is completed
        raw = session(f"c{index}", day, [14, 14, 14], template="curl", weight=14.0)
        types.append(h.review(raw, position=2).decisions[0].decision_type)
    assert types == ["progress_reps", "progress_reps", "alter_rep_range"]

    history = h.service.list_revision_history(h.user, h.programme_id)
    assert [item.revision_number for item in history] == [1, 2]
    assert history[0].prescriptions == original.prescriptions  # immutable
    decision = h.exercise_decisions("curl")[-1]
    assert decision.created_revision and decision.from_revision_id == history[0].id
    assert decision.to_revision_id == history[1].id
    assert decision.application_state == "auto_applied"

    def curl_ranges(revision: Any) -> dict[int | None, tuple[int, int]]:
        return {
            item.block_position: (item.rep_target["min"], item.rep_target["max"])
            for item in revision.prescriptions
            if item.exercise_template_id == "curl" and item.week_number in (None, 0)
        }

    old, new = curl_ranges(history[0]), curl_ranges(history[1])
    assert new[1] == old[1]  # the completed calibration block is untouched
    assert new[2] == (old[2][0] + 1, old[2][1] + 1)  # current and later blocks widen by one
    assert history[1].settings["days"][1]["exercises"][1]["prescriptions"][1]["rep_target"]["max"] == new[2][1]
    assert h.service.get_programme(h.user, h.programme_id).duration_weeks == 8  # type: ignore[union-attr]


def test_reverting_a_structural_decision_restores_the_earlier_revision_as_a_new_one(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    for index, day in enumerate((2, 3, 4)):
        h.review(session(f"c{index}", day, [14, 14], template="curl", weight=14.0), position=2)
    structural = h.exercise_decisions("curl")[-1]
    reverted = h.adaptation.revert(h.user, h.programme_id, structural.id)
    assert reverted.application_state == "reverted"
    history = h.service.list_revision_history(h.user, h.programme_id)
    assert [item.revision_number for item in history] == [1, 2, 3]
    assert history[2].reason == "revert"
    assert history[2].prescriptions == history[0].prescriptions  # content restored
    audit = [item for item in h.decisions() if item.decision_type == "revert"]
    assert audit and audit[0].from_revision_id == history[1].id and audit[0].to_revision_id == history[2].id
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.revert(h.user, h.programme_id, structural.id)  # already reverted


def test_reverting_a_week_level_change_restores_the_prescription_and_restarts_the_streak(
    tmp_path: Any,
) -> None:
    h = Harness(tmp_path)
    for index, day in enumerate((2, 3, 4, 5)):
        h.review(session(f"w{index}", day, [10, 10]))
    progress = h.exercise_decisions("bench")[-1]
    assert progress.decision_type == "progress_load"
    h.adaptation.revert(h.user, h.programme_id, progress.id)
    refreshed = h.service.get_adaptation_decision(h.user, h.programme_id, progress.id)
    assert refreshed is not None and refreshed.application_state == "reverted"
    effective = h.adaptation.effective_prescription(
        h.service.get_active_revision(h.user).settings,  # type: ignore[union-attr]
        h.decisions(), h.service.get_active_revision(h.user),  # type: ignore[arg-type]
        template_id="bench", routine_position=1, week=1,
    )
    assert effective is not None and effective[0].load_kg is None  # back to the revision's anchor
    # The user's "no" restarts the streak: the very next success cannot re-propose it.
    nxt = h.review(session("w4", 6, [10, 10])).decisions[0]
    assert nxt.decision_type != "progress_load"


# --- user control ---------------------------------------------------------------------


def test_pins_pain_reports_and_time_limits_are_durable_scoped_and_respected(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    pin = h.adaptation.add_override(
        h.user, h.programme_id, kind="pin", exercise_template_id="bench",
        value={"fields": ["load"]}, today=date(2026, 9, 1), weeks=2,
    )
    assert pin.ends_on == "2026-09-15" and pin.status == "active"
    for index, day in enumerate((2, 3, 4, 5)):
        decision = h.review(session(f"w{index}", day, [10, 10])).decisions[0]
    assert decision.decision_type == "hold"
    assert decision.output["rationale_codes"][0] == "user_pin_respected"
    assert decision.inputs["constraints"]["pinned_fields"] == ["load"]

    revoked = h.adaptation.revoke_override(h.user, h.programme_id, pin.id)
    assert revoked.status == "revoked" and revoked.revoked_at
    listed = h.adaptation.list_overrides(h.user, h.programme_id)
    assert [item.status for item in listed] == ["revoked"]  # retained for audit

    h.adaptation.add_override(
        h.user, h.programme_id, kind="pain_report", exercise_template_id="row",
        value={"area": "elbow"}, today=date(2026, 9, 1),
    )
    blocked = h.review(workout("w9", 5, {"row": [12, 12]}, rpe=None)).decisions[0]
    assert blocked.decision_type == "block_progression"
    assert "pain_reported" in blocked.output["rationale_codes"]
    # Pain on one exercise never blocks another.
    other = h.review(session("w10", 6, [10, 10])).decisions[0]
    assert other.decision_type != "block_progression"

    with pytest.raises(ProgrammeValidationError):
        h.adaptation.add_override(h.user, h.programme_id, kind="pin", exercise_template_id="bench",
                                  value={"fields": ["weight"]}, today=date(2026, 9, 1))
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.add_override(h.user, h.programme_id, kind="mystery", today=date(2026, 9, 1))
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.add_override(h.user, h.programme_id, kind="time_limit",
                                  value={"max_sets": 0}, today=date(2026, 9, 1))


def test_equipment_issue_proposes_a_substitution_that_is_never_applied_silently(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    h.adaptation.add_override(
        h.user, h.programme_id, kind="equipment_issue", exercise_template_id="bench",
        value={"reason": "bench out of service", "substitutes": ["Machine Press"]},
        today=date(2026, 9, 1),
    )
    pending = h.review(session("w1", 2, [10, 10])).decisions[0]
    assert pending.decision_type == "propose_substitution"
    assert pending.application_state == "pending_confirmation"
    assert pending.output["proposed_prescription"] == {"substitute_template_ids": ["Machine Press"]}
    revision_before = h.service.get_programme(h.user, h.programme_id).current_revision_id  # type: ignore[union-attr]
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.confirm(h.user, h.programme_id, pending.id, now=now())  # needs the exercise
    done = h.adaptation.confirm(
        h.user, h.programme_id, pending.id,
        adjustment={"performed_template_id": "machine-press"}, now=now(),
    )
    assert done.application_state == "user_confirmed"
    # The programme itself is untouched; the choice is a visible, scoped override.
    assert h.service.get_programme(h.user, h.programme_id).current_revision_id == revision_before  # type: ignore[union-attr]
    kinds = [item.kind for item in h.adaptation.list_overrides(h.user, h.programme_id)]
    assert "substitution" in kinds


def test_confirm_reject_and_user_correction_are_bounded_and_audited(tmp_path: Any) -> None:
    h = Harness(tmp_path, fatigue_management="planned")
    # Broad, corroborated shortfalls: planned fatigue management needs confirmation.
    h.adaptation.add_override(h.user, h.programme_id, kind="fatigue_report", today=date(2026, 9, 3), weeks=2)
    first = workout("a", 2, {"bench": [2, 2], "row": [2, 2]}, rpe=None)
    second = workout("b", 3, {"squat": [2, 2], "curl": [2, 2]}, rpe=None)
    h.review(first, position=1)
    result = h.adaptation.review(
        h.user, h.programme_id, [second], match("b", 2), now=now(3)
    )
    pending = result.programme_decision
    assert pending is not None
    assert pending.decision_type == "programme_fatigue_management"
    assert pending.application_state == "pending_confirmation"
    assert len(h.service.list_revision_history(h.user, h.programme_id)) == 1  # nothing applied yet

    confirmed = h.adaptation.confirm(h.user, h.programme_id, pending.id, now=now(3))
    assert confirmed.application_state == "user_confirmed" and confirmed.confirmed
    history = h.service.list_revision_history(h.user, h.programme_id)
    assert len(history) == 2 and confirmed.to_revision_id == history[1].id
    assert history[1].settings["fatigue_periods"][0]["mode"] in {"volume_only", "load_plus_volume", "frequency"}
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.confirm(h.user, h.programme_id, pending.id, now=now(3))  # no longer pending

    # A rejected proposal is never applied and stays on record.
    ambiguous = h.review(session("w7", 4, [10, 10]), confidence="ambiguous").decisions[0]
    rejected = h.adaptation.reject(h.user, h.programme_id, ambiguous.id)
    assert rejected.application_state == "rejected" and rejected.resolved_at
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.reject(h.user, h.programme_id, ambiguous.id)


def test_a_user_can_override_an_applied_value_within_the_per_exposure_bound(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    for index, day in enumerate((2, 3, 4, 5)):
        h.review(session(f"p{index}", day, [10, 10]))
    progress = h.exercise_decisions("bench")[-1]
    assert progress.decision_type == "progress_load" and progress.output["proposed_prescription"]["load_kg"] == 62.5
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.override(h.user, h.programme_id, progress.id, {"load_kg": 999.0})
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.override(h.user, h.programme_id, progress.id, {"sets": 9})
    # Non-finite or boolean values must not slip past the per-exposure bound.
    for bad in (float("nan"), float("inf"), True):
        with pytest.raises(ProgrammeValidationError):
            h.adaptation.override(h.user, h.programme_id, progress.id, {"load_kg": bad})
    overridden = h.adaptation.override(h.user, h.programme_id, progress.id, {"load_kg": 61.0})
    assert overridden.application_state == "overridden" and overridden.confirmed
    assert overridden.output["accepted_prescription"]["load_kg"] == 61.0
    assert overridden.output["proposed_prescription"]["load_kg"] == 62.5  # the engine's proposal is kept
    revision = h.service.get_active_revision(h.user)
    effective = h.adaptation.effective_prescription(
        revision.settings, h.decisions(), revision,  # type: ignore[union-attr, arg-type]
        template_id="bench", routine_position=1, week=1,
    )
    assert effective is not None and effective[0].load_kg == 61.0


# --- events: update and delete ------------------------------------------------------------


def test_an_edited_workout_supersedes_its_earlier_decisions(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    h.review(session("w1", 2, [10, 10]))
    original = h.exercise_decisions("bench")[0]
    edited = h.review(session("w1", 2, [4, 4]))  # same id, provider edited the sets
    assert edited.superseded == 1 and edited.evaluated == 1
    refreshed = h.service.get_adaptation_decision(h.user, h.programme_id, original.id)
    assert refreshed is not None and refreshed.application_state == "superseded"
    live = [item for item in h.exercise_decisions("bench") if item.application_state != "superseded"]
    assert len(live) == 1 and live[0].inputs["assessment"]["signal"] == "failure"
    week = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert len(week.actual["exposures"]) == 1  # the workout is counted once


def test_a_deleted_workout_is_retracted_and_flags_dependent_revisions(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    for index, day in enumerate((2, 3, 4)):
        h.review(session(f"c{index}", day, [14, 14], template="curl", weight=14.0), position=2)
    assert len(h.service.list_revision_history(h.user, h.programme_id)) == 2
    total = h.adaptation.retract_workouts(h.user, h.programme_id, ["c2"], now=now())
    assert total == 1
    structural = next(item for item in h.decisions() if item.decision_type == "alter_rep_range")
    assert structural.application_state == "superseded"
    # An immutable revision is never withdrawn silently: the user is asked.
    review = [item for item in h.decisions() if "evidence_retracted_review_revision" in item.output.get("rationale_codes", [])]
    assert review and review[0].application_state == "pending_confirmation"
    assert len(h.service.list_revision_history(h.user, h.programme_id)) == 2
    week = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert "c2" not in {item["workout_id"] for item in week.actual["exposures"]}
    # Confirming the review reverts the revision through a new one.
    h.adaptation.confirm(h.user, h.programme_id, review[0].id, now=now())
    assert len(h.service.list_revision_history(h.user, h.programme_id)) == 3


def test_workouts_missing_from_a_complete_provider_listing_are_retracted(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    h.review(session("w1", 2, [10, 10]))
    h.review(session("w2", 3, [10, 10]))
    result = h.adaptation.review(
        h.user, h.programme_id, [session("w2", 3, [10, 10])], match("w2"),
        now=now(), known_workout_ids=["w2"],
    )
    assert result.superseded == 1 and result.replayed == 1
    assert {item.source_workout_id for item in h.decisions() if item.application_state != "superseded" and item.source_workout_id} == {"w2"}


# --- timezone and tenancy --------------------------------------------------------------------


def test_workouts_are_assigned_to_weeks_in_the_users_timezone(tmp_path: Any) -> None:
    h = Harness(tmp_path, timezone_name="Pacific/Auckland")
    # 23:30 UTC on 7 Sep is 8 Sep in Auckland, i.e. week 2 of a 1 Sep start.
    raw = workout("tz", 7, {"bench": [10, 10]}, rpe=None, stamp="2026-09-07T23:30:00+00:00")
    result = h.adaptation.review(h.user, h.programme_id, [raw], match("tz"), now=now(8, 12))
    assert result.decisions[0].week_number == 2
    assert h.service.get_programme_week(h.user, h.programme_id, 2).state.actual["exposures"][0]["local_date"] == "2026-09-08"  # type: ignore[union-attr]

    utc = Harness(tmp_path / "utc")  # same instant, UTC user: still week 1
    raw_utc = workout("tz", 7, {"bench": [10, 10]}, rpe=None, stamp="2026-09-07T23:30:00+00:00")
    other = utc.adaptation.review(utc.user, utc.programme_id, [raw_utc], match("tz"), now=now(8, 12))
    assert other.decisions[0].week_number == 1


def test_adaptation_is_tenant_isolated(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    other = new_user(h.db, "athlete-b@example.test")
    other_programme = activate(h.db, other)
    h.review(session("w1", 2, [10, 10]))
    decision = h.exercise_decisions("bench")[0]

    stranger = h.adaptation
    with pytest.raises(ProgrammeNotFoundError):
        stranger.review(other, h.programme_id, [session("w5", 2, [10, 10])], match("w5"), now=now())
    with pytest.raises(ProgrammeNotFoundError):
        stranger.confirm(other, h.programme_id, decision.id, now=now())
    with pytest.raises(ProgrammeNotFoundError):
        stranger.revert(other, h.programme_id, decision.id)
    with pytest.raises(ProgrammeNotFoundError):
        stranger.add_override(other, h.programme_id, kind="review_request", today=date(2026, 9, 2))
    assert stranger.list_overrides(other, h.programme_id) == []
    assert h.service.list_adaptation_decisions(other, h.programme_id) == []
    assert h.service.get_adaptation_decision(other, h.programme_id, decision.id) is None
    # The same workout id under another tenant creates an independent decision.
    stranger.review(other, other_programme, [session("w1", 2, [10, 10])], match("w1"), now=now())
    assert len(h.service.list_adaptation_decisions(other, other_programme)) >= 2
    assert all(item.id != decision.id for item in h.service.list_adaptation_decisions(other, other_programme))
    with sqlite3.connect(h.db) as conn:
        owners = {row[0] for row in conn.execute("SELECT DISTINCT user_id FROM adaptation_decisions")}
    assert owners == {h.user, other}


def test_only_the_active_programme_can_be_adapted(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    h.lifecycle.pause(h.user, h.programme_id, now=now(2))
    with pytest.raises(ProgrammeValidationError):
        h.review(session("w1", 3, [10, 10]))
    assert json.dumps(h.decisions(), default=str)  # decisions remain readable while paused


def test_overrides_are_bounded_and_an_early_review_request_is_not_duplicated(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    with pytest.raises(ProgrammeValidationError, match="too large"):
        h.adaptation.add_override(
            h.user, h.programme_id, kind="time_limit",
            value={"max_sets": 3, "blob": "x" * 5000}, today=date(2026, 9, 1),
        )
    first = h.adaptation.request_review(h.user, h.programme_id, now=now(2))
    second = h.adaptation.request_review(h.user, h.programme_id, now=now(2))
    assert first.id == second.id and first.kind == "review_request"
    later = h.adaptation.request_review(h.user, h.programme_id, now=now(3))
    assert later.id != first.id  # a new day is a new request
    with pytest.raises(ProgrammeValidationError):
        h.adaptation.add_override(  # an override cannot outlive a year
            h.user, h.programme_id, kind="review_request", today=date(2026, 9, 1),
            ends_on=date(2028, 1, 1),
        )


def test_week_state_carries_the_review_state_and_a_fatigue_index(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    assert h.service.get_programme_week(h.user, h.programme_id, 1).state.review_state == "planned"  # type: ignore[union-attr]
    h.review(workout("a", 2, {"bench": [2, 2], "row": [10, 12]}, rpe=None))
    state = h.service.get_programme_week(h.user, h.programme_id, 1).state  # type: ignore[union-attr]
    assert state.review_state == "reviewed"
    assert state.fatigue == 0.5  # one of the two evaluated exercises fell short
    assert state.planned["week_number"] == 1 and state.actual["exposures"]


def test_week_level_progression_survives_an_unrelated_structural_revision(tmp_path: Any) -> None:
    h = Harness(tmp_path)
    for index, day in enumerate((2, 3, 4)):
        decisions = h.review(workout(f"w{index}", day, {"bench": [10, 10], "row": [12, 12]}, rpe=None)).decisions
    # The third session widens the row's rep range (a structural revision) ...
    assert [d.decision_type for d in decisions if d.exercise_template_id == "row"] == ["alter_rep_range"]
    assert len(h.service.list_revision_history(h.user, h.programme_id)) == 2
    revision = h.service.get_active_revision(h.user)
    effective = h.adaptation.effective_prescription(
        revision.settings, h.decisions(), revision,  # type: ignore[union-attr, arg-type]
        template_id="bench", routine_position=1, week=1,
    )
    # ... but the bench keeps its within-range progress made under revision 1.
    assert effective is not None and effective[1]["rep_target"] == 8
    # The row itself is rebased: earlier row adjustments no longer apply.
    row = h.adaptation.effective_prescription(
        revision.settings, h.decisions(), revision,  # type: ignore[union-attr, arg-type]
        template_id="row", routine_position=1, week=1,
    )
    assert row is not None and row[1]["rep_target"] == row[0].rep_min
