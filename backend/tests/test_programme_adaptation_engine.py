"""Deterministic adaptation engine: bounds, confidence, hysteresis, fatigue."""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from programme_domain import adaptation as engine
from programme_domain.adaptation import (
    DEFAULT_POLICY,
    Constraints,
    ExerciseHistory,
    Observation,
    Prescription,
    PriorExposure,
    RecoveryEvidence,
    SetObservation,
)


def prescription(**overrides: Any) -> Prescription:
    base: dict[str, Any] = {
        "template_id": "bench",
        "title": "Bench Press",
        "role": "primary_strength",
        "modality": "load",
        "method": "double_progression",
        "sets": 3,
        "source_sets": 3,
        "rep_min": 6,
        "rep_max": 8,
        "rep_target": 6,
        "target_rir_min": 2.0,
        "load_kg": 80.0,
        "increment_kg": 2.5,
        "max_load_percent": 5.0,
        "min_successful_exposures": 2,
        "set_floor": 2,
        "set_ceiling": 5,
        "rep_ceiling": 12,
    }
    base.update(overrides)
    return Prescription(**base)


def observation(
    reps: float | list[float],
    *,
    weight: float = 80.0,
    rpe: float | None = 7.0,
    confidence: str = "high",
    confirmed: bool = False,
    workout_id: str = "w-now",
    sets: int = 3,
    template: str = "bench",
    **fields: Any,
) -> Observation:
    values = reps if isinstance(reps, list) else [reps] * sets
    return Observation(
        workout_id=workout_id,
        workout_hash=workout_id,
        performed_at="2026-09-10T10:00:00+00:00",
        template_id=template,
        sets=tuple(
            SetObservation(reps=value, weight_kg=weight, rpe=rpe, **fields) for value in values
        ),
        match_confidence=confidence,
        match_user_confirmed=confirmed,
        routine_position=1,
    )


def prior(signal: str, **overrides: Any) -> PriorExposure:
    result = {"success": "exceeded", "failure": "missed", "neutral": "met"}[signal]
    data: dict[str, Any] = {
        "workout_id": f"p-{id(overrides)}",
        "performed_at": "2026-09-01T10:00:00+00:00",
        "signal": signal,
        "result": result,
        "effort": "ok",
        "match_confidence": "high",
        "decision_type": engine.HOLD,
        "applied": False,
    }
    data.update(overrides)
    return PriorExposure(**data)


def history(*items: PriorExposure, **fields: Any) -> ExerciseHistory:
    fields.setdefault("baseline_exposures", 6)
    fields.setdefault("baseline_confidence", "high")
    return ExerciseHistory(prior=tuple(items), **fields)


# --- outcomes -------------------------------------------------------------


def test_progress_load_after_required_successes_is_bounded_and_rounded() -> None:
    outcome = engine.decide(
        prescription(), observation(8), history(prior("success"))
    )
    assert outcome.decision_type == engine.PROGRESS_LOAD
    assert outcome.proposed["load_kg"] == 82.5  # one 2.5 kg step is within 5%
    assert 0 < outcome.proposed["load_change_percent"] <= 5.0
    assert outcome.proposed["rep_target"] == 6  # back to the bottom of the range
    assert "progression_criteria_met" in outcome.rationale_codes


def test_one_success_only_builds_reps_inside_the_range() -> None:
    outcome = engine.decide(prescription(), observation(8), history())
    assert outcome.decision_type == engine.PROGRESS_REPS
    assert outcome.proposed == {"rep_target": 7}


def test_hold_is_a_valid_outcome_with_a_reason() -> None:
    first_miss = engine.decide(prescription(), observation(3), history())
    assert first_miss.decision_type == engine.HOLD
    assert first_miss.rationale_codes[0] == "single_miss_no_change"
    assert first_miss.proposed == {}
    building = engine.decide(prescription(rep_target=6), observation([6, 6, 5]), history())
    assert building.decision_type == engine.HOLD
    assert not building.requires_confirmation


def test_high_effort_blocks_progression_even_when_reps_are_met() -> None:
    outcome = engine.decide(
        prescription(), observation(8, rpe=9.5), history(prior("success"))
    )
    assert outcome.decision_type == engine.HOLD
    assert "effort_above_target" in outcome.rationale_codes


def test_load_increment_larger_than_the_bound_widens_the_rep_range_or_holds() -> None:
    light = prescription(load_kg=20.0, increment_kg=2.5)  # 5% of 20 kg is 1 kg
    outcome = engine.decide(light, observation(8, weight=20.0), history(prior("success")))
    assert outcome.decision_type == engine.ALTER_REP_RANGE
    assert outcome.structural and outcome.proposed["rep_max"] == 9
    assert "load_increment_exceeds_bound" in outcome.rationale_codes
    capped = engine.decide(
        replace(light, rep_ceiling=8), observation(8, weight=20.0), history(prior("success"))
    )
    assert capped.decision_type == engine.HOLD
    assert not capped.proposed


def test_two_consecutive_failures_reduce_load_within_bounds() -> None:
    outcome = engine.decide(prescription(), observation(3), history(prior("failure")))
    assert outcome.decision_type == engine.REDUCE_LOAD
    assert outcome.proposed["load_kg"] == 77.5
    assert -5.0 <= outcome.proposed["load_change_percent"] < 0
    assert "repeated_underperformance" in outcome.rationale_codes


def test_bodyweight_assisted_and_duration_modalities_never_use_load_logic() -> None:
    bodyweight = prescription(
        modality="bodyweight", load_kg=None, progress_order=("reps", "added_load")
    )
    outcome = engine.decide(
        bodyweight, observation(8, weight=0), history(prior("success"))
    )
    assert outcome.decision_type == engine.PROGRESS_BODYWEIGHT
    assert outcome.proposed["variable"] == "added_load"

    assisted = prescription(modality="assisted", load_kg=30.0, increment_kg=2.5)
    outcome = engine.decide(assisted, observation(8, weight=30.0), history(prior("success")))
    assert outcome.decision_type == engine.PROGRESS_BODYWEIGHT
    assert outcome.proposed["variable"] == "less_assistance"
    assert outcome.proposed["load_kg"] == 27.5

    duration = prescription(
        modality="duration", rep_min=None, rep_max=None, load_kg=None, duration_seconds=60.0
    )
    observed = Observation(
        "w", "w", "2026-09-10T10:00:00+00:00", "plank",
        tuple(SetObservation(duration_seconds=60.0, rpe=7.0) for _ in range(3)),
        "high", routine_position=1,
    )
    outcome = engine.decide(duration, observed, history(prior("success")))
    assert outcome.decision_type == engine.PROGRESS_DURATION_DISTANCE
    assert 60.0 < outcome.proposed["duration_seconds"] <= 63.0
    assert "load_kg" not in outcome.proposed


# --- confidence -------------------------------------------------------------


@pytest.mark.parametrize("confidence", ["ambiguous", "unmatched"])
def test_ambiguous_or_unmatched_workouts_require_confirmation_and_change_nothing(
    confidence: str,
) -> None:
    outcome = engine.decide(
        prescription(), observation(8, confidence=confidence), history(prior("success"))
    )
    assert outcome.decision_type == engine.REQUEST_CONFIRMATION
    assert outcome.requires_confirmation and not outcome.proposed
    assert "confirm_match_required" in outcome.rationale_codes


def test_a_user_confirmed_match_is_treated_as_high_confidence() -> None:
    outcome = engine.decide(
        prescription(),
        observation(8, confidence="ambiguous", confirmed=True),
        history(prior("success")),
    )
    assert outcome.decision_type == engine.PROGRESS_LOAD


def test_probable_match_uses_smaller_reversible_changes() -> None:
    heavy = prescription(load_kg=200.0, increment_kg=1.25, max_load_percent=5.0)
    high = engine.decide(heavy, observation(8, weight=200.0), history(prior("success")))
    probable = engine.decide(
        heavy, observation(8, weight=200.0, confidence="probable"), history(prior("success"))
    )
    assert high.proposed["load_change_percent"] > probable.proposed["load_change_percent"] > 0
    assert probable.proposed["load_change_percent"] <= 2.5 + 1e-9
    assert "probable_match_reduced_bounds" in probable.rationale_codes


def test_probable_match_cannot_drive_a_structural_change_without_confirmation() -> None:
    fixed = prescription(modality="bodyweight", load_kg=None)
    outcome = engine.decide(
        fixed, observation(2, confidence="probable"), history(prior("failure"))
    )
    assert outcome.decision_type == engine.REDUCE_SETS
    assert outcome.structural and outcome.requires_confirmation
    high = engine.decide(fixed, observation(2), history(prior("failure")))
    assert high.structural and not high.requires_confirmation


def test_sparse_baselines_are_conservative() -> None:
    sparse_history = history(prior("success"), baseline_exposures=0, baseline_confidence=None)
    outcome = engine.decide(prescription(rep_target=8), observation(8), sparse_history)
    assert outcome.decision_type == engine.HOLD  # a sparse history needs one more exposure
    assert "awaiting_required_exposures" in outcome.rationale_codes
    assert "sparse_baseline_conservative" in outcome.rationale_codes
    experienced = engine.decide(
        prescription(load_kg=200.0, increment_kg=1.25),
        observation(8, weight=200.0),
        history(prior("success"), prior("success"), baseline_exposures=0),
    )
    assert experienced.decision_type == engine.PROGRESS_LOAD
    assert experienced.proposed["load_change_percent"] <= 2.5 + 1e-9  # halved


# --- precedence ---------------------------------------------------------------


def test_hard_constraints_win_and_substitutions_are_only_proposed() -> None:
    blocked = Constraints(blocked=True, substitution_candidates=("Machine Press",))
    outcome = engine.decide(prescription(), observation(8), history(prior("success")), blocked)
    assert outcome.decision_type == engine.PROPOSE_SUBSTITUTION
    assert outcome.requires_confirmation  # never silently applied
    assert outcome.proposed == {"substitute_template_ids": ["Machine Press"]}
    no_candidate = engine.decide(
        prescription(), observation(8), history(), Constraints(blocked=True)
    )
    assert no_candidate.decision_type == engine.BLOCK_PROGRESSION


def test_user_pins_are_never_out_voted() -> None:
    pinned = Constraints(pinned_fields=frozenset({"load"}))
    outcome = engine.decide(
        prescription(), observation(8), history(prior("success")), pinned
    )
    assert outcome.decision_type == engine.HOLD
    assert outcome.rationale_codes[0] == "user_pin_respected"
    reps_pinned = engine.decide(
        prescription(), observation(8), history(), Constraints(pinned_fields=frozenset({"reps"}))
    )
    assert reps_pinned.decision_type == engine.HOLD


@pytest.mark.parametrize(
    "constraints,code",
    [
        (Constraints(pain_flag=True), "pain_reported"),
        (Constraints(technique_flag=True), "technique_flagged"),
    ],
)
def test_pain_or_technique_feedback_blocks_progression(constraints: Constraints, code: str) -> None:
    outcome = engine.decide(
        prescription(), observation(8), history(prior("success")), constraints
    )
    assert outcome.decision_type == engine.BLOCK_PROGRESSION
    assert code in outcome.rationale_codes and not outcome.proposed


def test_a_user_time_limit_caps_sets_without_a_structural_revision() -> None:
    outcome = engine.decide(
        prescription(sets=4), observation(8, sets=4), history(), Constraints(max_sets=3)
    )
    assert outcome.decision_type == engine.REDUCE_SETS
    assert outcome.proposed == {"sets": 3} and not outcome.structural


# --- recovery -------------------------------------------------------------------


def test_missing_recovery_data_never_forces_a_change() -> None:
    without = engine.decide(prescription(), observation(8), history(prior("success")))
    quiet = engine.decide(
        prescription(),
        observation(8),
        history(prior("success")),
        recovery=RecoveryEvidence(0, "high", 2.0),
    )
    assert without.decision_type == quiet.decision_type == engine.PROGRESS_LOAD


def test_stale_low_quality_or_single_recovery_signals_are_ignored() -> None:
    for evidence, code in (
        (RecoveryEvidence(3, "high", 80.0), "recovery_stale_ignored"),
        (RecoveryEvidence(3, "low", 2.0), "recovery_stale_ignored"),
        (RecoveryEvidence(1, "high", 2.0), "recovery_signal_insufficient"),
    ):
        outcome = engine.decide(
            prescription(), observation(8), history(prior("success")), recovery=evidence
        )
        assert outcome.decision_type == engine.PROGRESS_LOAD
        assert code in outcome.rationale_codes


def test_corroborating_fresh_recovery_defers_progression_but_never_reduces() -> None:
    poor = RecoveryEvidence(3, "high", 4.0)
    deferred = engine.decide(
        prescription(), observation(8), history(prior("success")), recovery=poor
    )
    assert deferred.decision_type == engine.HOLD
    assert "fresh_recovery_signals_defer_progression" in deferred.rationale_codes
    steady = engine.decide(prescription(), observation([6, 6, 6]), history(), recovery=poor)
    assert steady.decision_type != engine.REDUCE_LOAD


def test_recovery_cannot_override_a_pin() -> None:
    outcome = engine.decide(
        prescription(),
        observation(8),
        history(prior("success")),
        Constraints(pinned_fields=frozenset({"load"})),
        RecoveryEvidence(0, "high", 1.0),
    )
    assert outcome.rationale_codes[0] == "user_pin_respected"


# --- hysteresis -------------------------------------------------------------------


def test_a_miss_right_after_progression_does_not_reverse_it() -> None:
    progressed = prior("success", decision_type=engine.PROGRESS_LOAD, applied=True, load_change_percent=3.1)
    outcome = engine.decide(prescription(load_kg=82.5), observation(3, weight=82.5), history(progressed))
    assert outcome.decision_type == engine.HOLD
    assert "single_miss_after_progression_no_reversal" in outcome.rationale_codes
    repeated = engine.decide(
        prescription(load_kg=82.5), observation(3, weight=82.5), history(progressed, prior("failure"))
    )
    assert repeated.decision_type == engine.REDUCE_LOAD


def test_cooldown_after_a_reduction_blocks_both_directions() -> None:
    reduced = prior("failure", decision_type=engine.REDUCE_LOAD, applied=True, load_change_percent=-3.1)
    for reps in (3, 8):
        outcome = engine.decide(
            prescription(load_kg=77.5), observation(reps, weight=77.5), history(reduced)
        )
        assert outcome.decision_type in {engine.HOLD, engine.PROGRESS_REPS}
        assert outcome.decision_type != engine.PROGRESS_LOAD
    two_successes = engine.decide(
        prescription(load_kg=77.5),
        observation(8, weight=77.5),
        history(reduced, prior("success")),
    )
    assert two_successes.decision_type == engine.HOLD
    assert "cooldown_after_reduction" in two_successes.rationale_codes
    after_cooldown = engine.decide(
        prescription(load_kg=77.5),
        observation(8, weight=77.5),
        history(reduced, prior("success"), prior("success")),
    )
    assert after_cooldown.decision_type == engine.PROGRESS_LOAD


def test_a_user_rejection_restarts_the_streak() -> None:
    rejected = prior("success", decision_type=engine.PROGRESS_LOAD, applied=False, rejected=True)
    outcome = engine.decide(prescription(), observation(8), history(prior("success"), rejected))
    assert outcome.decision_type != engine.PROGRESS_LOAD


def test_weekly_cap_limits_cumulative_change() -> None:
    capped = history(
        prior("success"), week_load_change_percent=5.0
    )
    outcome = engine.decide(prescription(), observation(8), capped)
    assert outcome.decision_type == engine.HOLD
    assert "weekly_change_cap_reached" in outcome.rationale_codes


# --- fatigue management -----------------------------------------------------------


def test_exercise_fatigue_has_separate_enter_and_exit_thresholds() -> None:
    reduced = prior("failure", decision_type=engine.REDUCE_LOAD, applied=True)
    spiral = history(reduced, prior("failure"), prior("failure"))
    entered = engine.decide(prescription(load_kg=77.5), observation(3, weight=77.5), spiral)
    assert entered.decision_type == engine.EXERCISE_FATIGUE
    assert entered.proposed["phase"] == "enter"
    assert entered.proposed["sets"] < 3
    assert entered.proposed["exit_criteria"]["consecutive_met_exposures"] == 2

    active = prior("failure", decision_type=engine.EXERCISE_FATIGUE, applied=True)
    waiting = engine.decide(
        prescription(sets=2),
        observation([7, 7]),
        history(active, fatigue_active=True),
    )
    assert waiting.decision_type == engine.HOLD
    assert "fatigue_management_active" in waiting.rationale_codes
    exited = engine.decide(
        prescription(sets=2),
        observation([7, 7]),
        history(active, prior("neutral"), fatigue_active=True),
    )
    assert exited.decision_type == engine.EXERCISE_FATIGUE
    assert exited.proposed == {"phase": "exit"}


def test_one_poor_session_never_enters_fatigue_management() -> None:
    outcome = engine.decide(prescription(), observation(2), history(prior("success"), prior("success")))
    assert outcome.decision_type == engine.HOLD


def test_programme_fatigue_needs_breadth_and_independent_corroboration() -> None:
    one_poor_session = engine.WeekEvidence(4, 1, 1)
    assert engine.evaluate_programme_fatigue(one_poor_session) is None

    broad = engine.WeekEvidence(4, 3, 2)
    unconfirmed = engine.evaluate_programme_fatigue(broad)
    assert unconfirmed is not None
    assert unconfirmed.decision_type == engine.REQUEST_CONFIRMATION
    assert unconfirmed.rationale_codes == ("programme_fatigue_unconfirmed",)

    noisy_watch = engine.WeekEvidence(4, 1, 1, recovery=RecoveryEvidence(4, "high", 1.0))
    assert engine.evaluate_programme_fatigue(noisy_watch) is None
    stale_watch = engine.WeekEvidence(4, 3, 2, recovery=RecoveryEvidence(4, "high", 90.0))
    stale = engine.evaluate_programme_fatigue(stale_watch)
    assert stale is not None and stale.decision_type == engine.REQUEST_CONFIRMATION

    corroborated = engine.WeekEvidence(4, 3, 2, user_fatigue_report=True)
    outcome = engine.evaluate_programme_fatigue(corroborated)
    assert outcome is not None and outcome.decision_type == engine.PROGRAMME_FATIGUE
    assert outcome.structural and not outcome.requires_confirmation
    assert outcome.proposed["exit_criteria"]["max_weeks"] == 2
    assert outcome.proposed["set_multiplier"] == DEFAULT_POLICY.fatigue_set_multiplier

    planned = engine.evaluate_programme_fatigue(
        replace(corroborated, planned_fatigue_management=True)
    )
    assert planned is not None and planned.requires_confirmation

    cooling = engine.evaluate_programme_fatigue(replace(corroborated, weeks_since_programme_fatigue=1))
    assert cooling is None


def test_programme_fatigue_modes_follow_the_evidence() -> None:
    low_adherence = engine.evaluate_programme_fatigue(engine.WeekEvidence(4, 3, 2, adherence=0.4))
    assert low_adherence is not None and low_adherence.proposed["mode"] == "frequency"
    recovery = engine.evaluate_programme_fatigue(
        engine.WeekEvidence(4, 3, 2, recovery=RecoveryEvidence(3, "high", 3.0))
    )
    assert recovery is not None and recovery.proposed["mode"] == "load_plus_volume"
    assert recovery.proposed["load_percent"] == -DEFAULT_POLICY.fatigue_load_percent


def test_fold_effective_applies_progression_and_fatigue_overlays() -> None:
    base = prescription().to_dict()
    folded = engine.fold_effective(
        base,
        [
            {"decision_type": engine.PROGRESS_LOAD, "proposed": {"load_kg": 82.5, "rep_target": 6}},
            {"decision_type": engine.EXERCISE_FATIGUE, "proposed": {"phase": "enter", "sets": 2, "load_kg": 77.0}},
        ],
    )
    assert folded["sets"] == 2 and folded["load_kg"] == 77.0 and folded["fatigue_management"]
    exited = engine.fold_effective(
        base,
        [
            {"decision_type": engine.PROGRESS_LOAD, "proposed": {"load_kg": 82.5}},
            {"decision_type": engine.EXERCISE_FATIGUE, "proposed": {"phase": "enter", "sets": 2}},
            {"decision_type": engine.EXERCISE_FATIGUE, "proposed": {"phase": "exit"}},
        ],
    )
    assert exited["sets"] == 3 and exited["load_kg"] == 82.5 and "fatigue_management" not in exited


def test_every_rationale_code_has_text_and_explanations_are_deterministic() -> None:
    assert all(engine.RATIONALE_TEXT[code].strip() for code in engine.RATIONALE_TEXT)
    outcome = engine.decide(prescription(), observation(8), history(prior("success")))
    assert outcome.explanation == engine.explain(outcome.decision_type, outcome.rationale_codes)
    assert outcome.to_dict() == engine.decide(
        prescription(), observation(8), history(prior("success"))
    ).to_dict()


def test_recovery_evidence_counts_concordant_adverse_features() -> None:
    from recovery import RecoveryFeatures

    features = RecoveryFeatures(-60.0, 6.0, -25.0, 10.0, 14, 0.9)
    evidence = engine.recovery_evidence_from_features(features, age_hours=3.0)
    assert evidence.risk_signals == 3 and evidence.usable()
    weak = RecoveryFeatures(-60.0, 6.0, -25.0, 10.0, 14, 0.05)
    assert not engine.recovery_evidence_from_features(weak, age_hours=3.0).usable()


# --- properties --------------------------------------------------------------------

_SIGNALS = {"S": 8, "F": 3, "N": 7}


def _simulate(sequence: list[str]) -> list[dict[str, Any]]:
    """Run the engine over a signal sequence, applying every non-confirmed change."""

    load, sets, rep_target = 80.0, 3, 6
    fatigue_active = False
    before_fatigue: tuple[float, int] = (load, sets)
    prior_items: list[PriorExposure] = []
    log: list[dict[str, Any]] = []
    for index, symbol in enumerate(sequence):
        week = index // 2 + 1
        week_load = sum(
            abs(item.load_change_percent)
            for item in prior_items
            if item.applied and item.week_number == week
            and item.decision_type in {engine.PROGRESS_LOAD, engine.REDUCE_LOAD}
        )
        current = prescription(load_kg=load, sets=sets, rep_target=rep_target)
        outcome = engine.decide(
            current,
            observation(_SIGNALS[symbol], weight=load, sets=sets, workout_id=f"w{index}"),
            ExerciseHistory(
                prior=tuple(prior_items),
                baseline_exposures=6,
                baseline_confidence="high",
                week_load_change_percent=week_load,
                fatigue_active=fatigue_active,
            ),
        )
        applied = not outcome.requires_confirmation and outcome.decision_type in engine.CHANGING_TYPES
        proposed = outcome.proposed if applied else {}
        if applied and outcome.decision_type == engine.EXERCISE_FATIGUE:
            if proposed.get("phase") == "exit":
                load, sets = before_fatigue  # the overlay is removed
                fatigue_active = False
            else:
                before_fatigue = (load, sets)
                fatigue_active = True
        if "load_kg" in proposed:
            load = proposed["load_kg"]
        if "sets" in proposed:
            sets = proposed["sets"]
        if "rep_target" in proposed:
            rep_target = proposed["rep_target"]
        prior_items.append(
            PriorExposure(
                workout_id=f"w{index}",
                performed_at=f"2026-09-{index + 1:02d}T10:00:00+00:00",
                signal=outcome.assessment["signal"],
                result=outcome.assessment["result"],
                effort=outcome.assessment["effort"],
                match_confidence="high",
                decision_type=outcome.decision_type,
                applied=applied,
                week_number=week,
                load_change_percent=float(proposed.get("load_change_percent", 0.0)),
            )
        )
        log.append(
            {
                "index": index,
                "week": week,
                "type": outcome.decision_type,
                "applied": applied,
                "signal": outcome.assessment["signal"],
                "load": load,
                "sets": sets,
                "pct": float(proposed.get("load_change_percent", 0.0)),
                "phase": proposed.get("phase"),
                "outcome": outcome.to_dict(),
            }
        )
    return log


_sequences = st.lists(st.sampled_from("SFN"), max_size=30)


@settings(max_examples=200, deadline=None)
@given(_sequences)
def test_property_every_adaptation_is_deterministic_and_bounded(sequence: list[str]) -> None:
    first = _simulate(sequence)
    assert first == _simulate(sequence)
    weekly: dict[int, float] = {}
    for entry in first:
        assert entry["sets"] >= 2  # never below the prescription's set floor
        assert entry["load"] > 0
        if entry["type"] in {engine.PROGRESS_LOAD, engine.REDUCE_LOAD}:
            assert abs(entry["pct"]) <= 5.0 + 1e-9  # per-exposure bound
            weekly[entry["week"]] = weekly.get(entry["week"], 0.0) + abs(entry["pct"])
    assert all(total <= DEFAULT_POLICY.weekly_load_percent_cap + 1e-9 for total in weekly.values())


def _kind(entry: dict[str, Any]) -> str:
    if entry["type"] in {engine.PROGRESS_LOAD, engine.ALTER_REP_RANGE}:
        return "up"
    if entry["type"] == engine.EXERCISE_FATIGUE and entry["phase"] == "exit":
        return "restore"
    return "down"


@settings(max_examples=300, deadline=None)
@given(_sequences)
def test_property_the_engine_never_oscillates(sequence: list[str]) -> None:
    log = _simulate(sequence)
    resets = [
        entry for entry in log if entry["applied"] and entry["type"] in engine.RESETTING_TYPES
    ]
    policy = DEFAULT_POLICY
    for earlier, later in itertools.pairwise(resets):
        gap = [entry for entry in log if earlier["index"] < entry["index"] <= later["index"]]
        signals = [entry["signal"] for entry in gap]
        pair = (_kind(earlier), _kind(later))
        if pair in {("up", "down"), ("restore", "down")}:
            # a reversal needs the opposite evidence to repeat
            assert signals[-policy.min_failed_exposures :] == ["failure"] * policy.min_failed_exposures
        if pair == ("down", "up"):
            # re-escalation needs the cooldown and a stable run after the reduction
            assert len(gap) > policy.cooldown_exposures
            assert signals[-policy.stable_exposures_before_reescalation :] == [
                "success"
            ] * policy.stable_exposures_before_reescalation
        if pair == ("restore", "up"):
            assert signals[-policy.min_successful_exposures :] == ["success"] * policy.min_successful_exposures
        if pair == ("down", "down") and earlier["type"] != engine.EXERCISE_FATIGUE:
            assert len(gap) > policy.cooldown_exposures  # never ratchet down every session


@settings(max_examples=150, deadline=None)
@given(st.lists(st.sampled_from("FN"), min_size=1, max_size=12))
def test_property_without_successes_the_engine_never_escalates(sequence: list[str]) -> None:
    for entry in _simulate(sequence):
        assert entry["type"] not in {engine.PROGRESS_LOAD, engine.ALTER_REP_RANGE}
