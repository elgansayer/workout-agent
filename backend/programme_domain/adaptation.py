"""Deterministic, bounded, confidence-aware programme adaptation engine.

This module is a pure function layer: it never reads the clock, the database or
a language model.  Identical inputs always produce an identical
:class:`AdaptationOutcome`, so every decision can be explained from the inputs
stored beside it (``docs/PROGRAMME_ADAPTATION.md``).

Decision order (the first step that applies wins):

1. hard safety / profile constraints;
2. explicit user overrides (pins lock a dimension; they are never out-voted);
3. routine/workout match confidence;
4. pain, injury or technique feedback;
5. target completion and set type;
6. performance trend;
7. logged RPE / effort;
8. adherence and fresh recovery evidence;
9. the current block target envelope;
10. the exercise-specific progression rule.

Anti-oscillation is structural, not a tuning knob: escalation needs consecutive
successes *since the last applied change*, a reduction starts a cooldown,
reversal needs the opposite evidence to repeat, fatigue management has separate
enter/exit thresholds, and cumulative weekly change is capped.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

RULE_VERSION = "programme-adaptation/v1"

# Decision types -----------------------------------------------------------
PROGRESS_LOAD = "progress_load"
PROGRESS_REPS = "progress_reps"
PROGRESS_DURATION_DISTANCE = "progress_duration_distance"
PROGRESS_BODYWEIGHT = "progress_bodyweight_variable"
HOLD = "hold"
REDUCE_LOAD = "reduce_load"
REDUCE_SETS = "reduce_sets"
ALTER_REP_RANGE = "alter_rep_range"
EXERCISE_FATIGUE = "exercise_fatigue_management"
PROGRAMME_FATIGUE = "programme_fatigue_management"
REQUEST_CONFIRMATION = "request_confirmation"
BLOCK_PROGRESSION = "block_progression"
PROPOSE_SUBSTITUTION = "propose_substitution"

DECISION_TYPES = frozenset(
    {
        PROGRESS_LOAD,
        PROGRESS_REPS,
        PROGRESS_DURATION_DISTANCE,
        PROGRESS_BODYWEIGHT,
        HOLD,
        REDUCE_LOAD,
        REDUCE_SETS,
        ALTER_REP_RANGE,
        EXERCISE_FATIGUE,
        PROGRAMME_FATIGUE,
        REQUEST_CONFIRMATION,
        BLOCK_PROGRESSION,
        PROPOSE_SUBSTITUTION,
    }
)
#: Applied changes that restart the success/failure streaks (``progress_reps``
#: is within-range accumulation and deliberately does not).
RESETTING_TYPES = frozenset(
    {
        PROGRESS_LOAD,
        PROGRESS_DURATION_DISTANCE,
        PROGRESS_BODYWEIGHT,
        REDUCE_LOAD,
        REDUCE_SETS,
        ALTER_REP_RANGE,
        EXERCISE_FATIGUE,
        PROGRAMME_FATIGUE,
    }
)
REDUCTION_TYPES = frozenset({REDUCE_LOAD, REDUCE_SETS, EXERCISE_FATIGUE, PROGRAMME_FATIGUE})
#: Decisions that change the effective prescription when applied.
CHANGING_TYPES = frozenset(
    {
        PROGRESS_LOAD,
        PROGRESS_REPS,
        PROGRESS_DURATION_DISTANCE,
        PROGRESS_BODYWEIGHT,
        REDUCE_LOAD,
        REDUCE_SETS,
        ALTER_REP_RANGE,
        EXERCISE_FATIGUE,
    }
)
#: Changes that rewrite the block prescription and therefore need a new revision.
STRUCTURAL_TYPES = frozenset({REDUCE_SETS, ALTER_REP_RANGE, PROGRAMME_FATIGUE, PROPOSE_SUBSTITUTION})

# Match confidence (shared vocabulary with ``hevy_history``) ---------------
MATCH_HIGH = "high"
MATCH_PROBABLE = "probable"
MATCH_AMBIGUOUS = "ambiguous"
MATCH_UNMATCHED = "unmatched"

# Exposure results ----------------------------------------------------------
RESULT_EXCEEDED = "exceeded"
RESULT_MET = "met"
RESULT_PARTIAL = "partial"
RESULT_MISSED = "missed"
RESULT_NO_DATA = "no_data"

SIGNAL_SUCCESS = "success"
SIGNAL_FAILURE = "failure"
SIGNAL_NEUTRAL = "neutral"
SIGNAL_UNKNOWN = "unknown"

RATIONALE_TEXT: dict[str, str] = {
    "constraint_blocked_exercise": "A saved training rule or an equipment report blocks this exercise.",
    "substitution_proposed_not_applied": "A substitute from an approved equivalence set is proposed; nothing was changed.",
    "no_approved_substitute": "No approved substitute exists, so progression is blocked until you decide.",
    "user_pin_respected": "You pinned this value, so it was not changed automatically.",
    "ambiguous_routine_match": "The workout could belong to more than one routine.",
    "unmatched_workout": "The workout could not be matched to a routine in this programme.",
    "confirm_match_required": "Confirm the routine for this workout before it can change a prescription.",
    "pain_reported": "You reported pain for this exercise, so automatic progression is paused.",
    "technique_flagged": "A technique concern was reported, so automatic progression is paused.",
    "progression_paused_pending_review": "Progression stays paused until you clear the report or review it.",
    "no_working_sets_recorded": "No working sets were recorded, so there is no evidence to act on.",
    "no_rep_target": "The prescription has no rep or duration target to assess against.",
    "single_miss_no_change": "One missed session is not a trend, so nothing was changed.",
    "single_miss_after_progression_no_reversal": "One miss after a progression does not reverse it; a repeat would.",
    "cooldown_after_reduction": "A recent reduction is still settling, so no further change is made.",
    "building_within_rep_range": "Reps are still building inside the prescribed range.",
    "awaiting_required_exposures": "The criteria were met; progression waits for the required number of consecutive exposures.",
    "effort_above_target": "Reported effort was above target, so load was not increased.",
    "performed_below_prescribed_load": "The logged load was below the prescription, so it does not count as success.",
    "fresh_recovery_signals_defer_progression": "Several fresh recovery signals agree, so progression is deferred (no reduction).",
    "recovery_unavailable_ignored": "No recovery data was available; it did not influence this decision.",
    "recovery_stale_ignored": "Recovery data was stale or low quality; it did not influence this decision.",
    "recovery_signal_insufficient": "A single recovery signal cannot change a prescription on its own.",
    "sparse_baseline_conservative": "There is little history for this exercise, so changes are smaller and slower.",
    "probable_match_reduced_bounds": "The routine match is probable rather than certain, so changes are halved and reversible.",
    "probable_match_structural_needs_confirmation": "A probable match cannot drive a structural change without your confirmation.",
    "progression_criteria_met": "All sets reached the top of the range at target effort for the required exposures.",
    "load_increment_exceeds_bound": "The smallest load step is larger than the allowed change.",
    "weekly_change_cap_reached": "The weekly change cap for this exercise has been reached.",
    "no_load_reference": "No load is on record for this exercise, so a load change cannot be computed.",
    "repeated_underperformance": "Repeated sessions fell short of the target, so the load is reduced within bounds.",
    "reduction_at_set_floor": "The prescription is already at its minimum number of sets.",
    "fatigue_management_entered": "Repeated shortfalls despite a reduction: this exercise enters fatigue management.",
    "fatigue_management_active": "Fatigue management is active; progression resumes after the exit criteria are met.",
    "fatigue_exit_criteria_met": "Consecutive sessions met the target, so fatigue management ends.",
    "rep_range_widened": "Load steps are too coarse, so the rep range is widened within policy.",
    "time_limit_set_cap": "Your session time limit caps the number of sets.",
    "within_block_envelope": "The change stays inside the current block envelope.",
    "bodyweight_variable_selected": "A bodyweight or assistance variable is progressed after the rep criteria were met.",
    "variable_needs_confirmation": "This progression variable cannot be quantified automatically; please confirm it.",
    "programme_fatigue_triggered": "Several exercises and sessions show repeated shortfalls with corroborating evidence.",
    "programme_fatigue_unconfirmed": "Performance suggests fatigue, but nothing independent supports it; please confirm how you feel.",
    "programme_fatigue_confirmation_required": "A programme-level change needs your confirmation.",
    "proposal_violates_training_rules": "The proposed change would break one of your saved training rules, so it was not applied.",
    "evidence_retracted_review_revision": "The workout behind a programme revision was edited or deleted; review whether to revert that revision.",
    "user_requested_review": "You asked for an early review.",
}


@dataclass(frozen=True)
class AdaptationPolicy:
    """Versioned thresholds.  Every automatic change stays inside these bounds."""

    min_successful_exposures: int = 2
    min_failed_exposures: int = 2
    cooldown_exposures: int = 2
    stable_exposures_before_reescalation: int = 2
    sparse_baseline_exposures: int = 3
    probable_scale: float = 0.5
    sparse_scale: float = 0.5
    default_max_load_percent: float = 5.0
    weekly_load_percent_cap: float = 5.0
    max_set_delta: int = 1
    weekly_set_delta_cap: int = 1
    bodyweight_step_kg: float = 1.25
    fatigue_enter_failures: int = 3
    fatigue_window: int = 4
    fatigue_exit_successes: int = 2
    fatigue_set_multiplier: float = 0.7
    fatigue_load_percent: float = 5.0
    programme_fatigue_min_exercises: int = 3
    programme_fatigue_min_ratio: float = 0.5
    programme_fatigue_min_sessions: int = 2
    programme_fatigue_cooldown_weeks: int = 3
    programme_fatigue_adherence_floor: float = 0.6
    recovery_max_age_hours: float = 36.0
    recovery_min_corroborating: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


DEFAULT_POLICY = AdaptationPolicy()


@dataclass(frozen=True)
class SetObservation:
    kind: str = "normal"
    reps: float | None = None
    weight_kg: float | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    rpe: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reps": self.reps,
            "weight_kg": self.weight_kg,
            "duration_seconds": self.duration_seconds,
            "distance_meters": self.distance_meters,
            "rpe": self.rpe,
        }


@dataclass(frozen=True)
class Observation:
    """One exercise exposure inside one completed workout."""

    workout_id: str
    workout_hash: str
    performed_at: str
    template_id: str
    sets: tuple[SetObservation, ...]
    match_confidence: str
    match_user_confirmed: bool = False
    routine_position: int | None = None

    @property
    def effective_confidence(self) -> str:
        return MATCH_HIGH if self.match_user_confirmed else self.match_confidence


@dataclass(frozen=True)
class Prescription:
    """The effective prescription in force before a decision."""

    template_id: str
    title: str
    role: str
    modality: str  # load | bodyweight | assisted | duration | distance
    method: str
    sets: int
    source_sets: int
    rep_min: int | None
    rep_max: int | None
    rep_target: int | None = None
    target_rir_min: float | None = None
    load_kg: float | None = None
    increment_kg: float | None = None
    duration_seconds: float | None = None
    distance_meters: float | None = None
    max_load_percent: float = DEFAULT_POLICY.default_max_load_percent
    min_successful_exposures: int = DEFAULT_POLICY.min_successful_exposures
    set_floor: int = 1
    set_ceiling: int = 12
    rep_ceiling: int | None = None
    progress_order: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "sets": self.sets,
            "rep_min": self.rep_min,
            "rep_max": self.rep_max,
            "rep_target": self.rep_target if self.rep_target is not None else self.rep_min,
            "load_kg": self.load_kg,
            "duration_seconds": self.duration_seconds,
            "distance_meters": self.distance_meters,
            "modality": self.modality,
            "method": self.method,
        }


@dataclass(frozen=True)
class Constraints:
    """Hard rules and explicit user instructions in force for one exercise."""

    blocked: bool = False
    substitution_candidates: tuple[str, ...] = ()
    pain_flag: bool = False
    technique_flag: bool = False
    pinned_fields: frozenset[str] = frozenset()
    max_sets: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "substitution_candidates": list(self.substitution_candidates),
            "pain_flag": self.pain_flag,
            "technique_flag": self.technique_flag,
            "pinned_fields": sorted(self.pinned_fields),
            "max_sets": self.max_sets,
        }


@dataclass(frozen=True)
class RecoveryEvidence:
    """Optional recovery input with explicit quality and freshness."""

    risk_signals: int
    quality: str = "medium"  # high | medium | low
    age_hours: float | None = None
    source: str = ""

    def usable(self, policy: AdaptationPolicy = DEFAULT_POLICY) -> bool:
        return (
            self.quality in {"high", "medium"}
            and self.age_hours is not None
            and 0 <= self.age_hours <= policy.recovery_max_age_hours
        )

    def to_dict(self, policy: AdaptationPolicy = DEFAULT_POLICY) -> dict[str, Any]:
        return {
            "risk_signals": self.risk_signals,
            "quality": self.quality,
            "age_hours": self.age_hours,
            "source": self.source,
            "usable": self.usable(policy),
        }


def recovery_evidence_from_features(
    features: Any,
    *,
    age_hours: float | None,
    quality: str = "medium",
    source: str = "health",
) -> RecoveryEvidence:
    """Count concordant adverse indicators in ``recovery.RecoveryFeatures``.

    The thresholds mirror ``adaptive_training.recommend_adjustment`` so both
    layers describe the same physiology.
    """

    risk = 0
    sleep = getattr(features, "sleep_delta_minutes", None)
    heart = getattr(features, "resting_hr_delta_bpm", None)
    hrv = getattr(features, "hrv_delta_pct", None)
    load = getattr(features, "training_load_delta_pct", None)
    if sleep is not None and sleep <= -45:
        risk += 1
    if heart is not None and heart >= 5:
        risk += 1
    if hrv is not None and hrv <= -20:
        risk += 1
    if load is not None and load >= 30:
        risk += 1
    if float(getattr(features, "confidence", 0.0) or 0.0) < 0.2:
        quality = "low"
    return RecoveryEvidence(risk_signals=risk, quality=quality, age_hours=age_hours, source=source)


@dataclass(frozen=True)
class PriorExposure:
    """A previously evaluated exposure of the same exercise (oldest first)."""

    workout_id: str
    performed_at: str
    signal: str
    result: str
    effort: str
    match_confidence: str
    decision_type: str
    applied: bool
    week_number: int = 0
    load_change_percent: float = 0.0
    set_change: int = 0
    #: The user rejected or reverted this decision.  The change was not (or is
    #: no longer) applied, but the user's "no" restarts the streak so the same
    #: proposal cannot be re-issued on the very next exposure.
    rejected: bool = False


@dataclass(frozen=True)
class ExerciseHistory:
    prior: tuple[PriorExposure, ...] = ()
    baseline_exposures: int = 0
    baseline_confidence: str | None = None
    week_load_change_percent: float = 0.0
    week_set_change: int = 0
    fatigue_active: bool = False


@dataclass(frozen=True)
class Assessment:
    result: str
    effort: str
    prescribed_sets: int
    working_sets: int
    min_reps: float | None
    reference_load: float | None
    below_prescribed_load: bool
    signal: str
    top_of_range: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "effort": self.effort,
            "prescribed_sets": self.prescribed_sets,
            "working_sets": self.working_sets,
            "min_reps": self.min_reps,
            "reference_load": self.reference_load,
            "below_prescribed_load": self.below_prescribed_load,
            "signal": self.signal,
            "top_of_range": self.top_of_range,
        }


@dataclass(frozen=True)
class AdaptationOutcome:
    decision_type: str
    rationale_codes: tuple[str, ...]
    previous: dict[str, Any]
    proposed: dict[str, Any]
    requires_confirmation: bool
    structural: bool
    confidence: float
    assessment: dict[str, Any]
    bounds: dict[str, Any] = field(default_factory=dict)
    direction: int = 0

    @property
    def changes_prescription(self) -> bool:
        return self.decision_type in CHANGING_TYPES or self.decision_type in {
            PROGRAMME_FATIGUE,
            PROPOSE_SUBSTITUTION,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type,
            "rationale_codes": list(self.rationale_codes),
            "previous_prescription": self.previous,
            "proposed_prescription": self.proposed,
            "requires_confirmation": self.requires_confirmation,
            "structural": self.structural,
            "bounds": self.bounds,
            "direction": self.direction,
            "rule_version": RULE_VERSION,
        }

    @property
    def explanation(self) -> str:
        return explain(self.decision_type, self.rationale_codes)


def explain(decision_type: str, codes: Iterable[str]) -> str:
    """Deterministic explanation text built only from stored rationale codes."""

    reasons = [RATIONALE_TEXT.get(code, code) for code in codes]
    headline = decision_type.replace("_", " ").capitalize()
    return f"{headline}. " + " ".join(reasons) if reasons else f"{headline}."


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _round_down(value: float, increment: float | None) -> float:
    step = increment if increment and increment > 0 else 0.25
    return round(math.floor(value / step + 1e-9) * step, 4)


def _metric(modality: str, observation: SetObservation) -> float | None:
    if modality == "duration":
        return observation.duration_seconds
    if modality == "distance":
        return observation.distance_meters
    return observation.reps


def _working_sets(prescription: Prescription, observation: Observation) -> list[SetObservation]:
    return [
        item
        for item in observation.sets
        if item.kind != "warmup" and _metric(prescription.modality, item) is not None
    ]


def assess_exposure(prescription: Prescription, observation: Observation) -> Assessment:
    """Step 5 and 7: target completion, set type and effort."""

    working = _working_sets(prescription, observation)
    prescribed = max(1, prescription.sets)
    modality = prescription.modality
    if modality == "duration":
        target = prescription.duration_seconds
        floor, top = (target * 0.9, target) if target else (None, None)
    elif modality == "distance":
        target = prescription.distance_meters
        floor, top = (target * 0.9, target) if target else (None, None)
    else:
        floor = float(prescription.rep_min) if prescription.rep_min is not None else (
            float(prescription.rep_max) if prescription.rep_max is not None else None
        )
        top = float(prescription.rep_max) if prescription.rep_max is not None else floor

    if not working or floor is None or top is None:
        return Assessment(RESULT_NO_DATA, "unknown", prescribed, len(working), None, None, False, SIGNAL_UNKNOWN, False)

    considered = working[:prescribed]
    values = [float(_metric(modality, item) or 0.0) for item in considered]
    below = [value for value in values if value < floor]
    ratio = len(considered) / prescribed
    if ratio < 0.5 or len(below) > len(considered) / 2:
        result = RESULT_MISSED
    elif len(considered) < prescribed or below:
        result = RESULT_PARTIAL
    elif all(value >= top for value in values):
        result = RESULT_EXCEEDED
    else:
        result = RESULT_MET

    ceiling = None if prescription.target_rir_min is None else 10.0 - prescription.target_rir_min
    rpes = [item.rpe for item in considered if item.rpe is not None]
    if ceiling is None or not rpes:
        effort = "unknown"
    else:
        mean_rpe = statistics.fmean(rpes)
        effort = "high" if mean_rpe > ceiling else ("low" if mean_rpe < ceiling - 2.0 else "ok")

    weights = [item.weight_kg for item in considered if item.weight_kg and item.weight_kg > 0]
    reference = float(statistics.median(weights)) if weights else None
    below_load = bool(
        prescription.load_kg
        and reference is not None
        and prescription.modality in {"load", "bodyweight"}
        and reference < 0.9 * prescription.load_kg
    )
    if result == RESULT_EXCEEDED and effort != "high" and not below_load:
        signal = SIGNAL_SUCCESS
    elif result == RESULT_MISSED or (result == RESULT_PARTIAL and effort == "high"):
        signal = SIGNAL_FAILURE
    else:
        signal = SIGNAL_NEUTRAL
    return Assessment(
        result=result,
        effort=effort,
        prescribed_sets=prescribed,
        working_sets=len(working),
        min_reps=min(values) if values else None,
        reference_load=reference,
        below_prescribed_load=below_load,
        signal=signal,
        top_of_range=result == RESULT_EXCEEDED,
    )


# ---------------------------------------------------------------------------
# The decision pipeline
# ---------------------------------------------------------------------------


def _confidence_value(match: str, sparse: bool) -> float:
    base = {MATCH_HIGH: 0.9, MATCH_PROBABLE: 0.6}.get(match, 0.3)
    return round(base * (0.8 if sparse else 1.0), 3)


def _trailing(signals: Sequence[str], kind: str) -> int:
    count = 0
    for signal in reversed(signals):
        if signal != kind:
            break
        count += 1
    return count


def decide(
    prescription: Prescription,
    observation: Observation,
    history: ExerciseHistory | None = None,
    constraints: Constraints | None = None,
    recovery: RecoveryEvidence | None = None,
    policy: AdaptationPolicy = DEFAULT_POLICY,
) -> AdaptationOutcome:
    """Evaluate one exercise exposure and return one bounded outcome."""

    history = history or ExerciseHistory()
    constraints = constraints or Constraints()
    assessment = assess_exposure(prescription, observation)
    previous = prescription.to_dict()
    match = observation.effective_confidence
    reliable_prior = tuple(
        item
        for item in history.prior
        if item.match_confidence in {MATCH_HIGH, MATCH_PROBABLE}
    )
    known = history.baseline_exposures + len(reliable_prior)
    sparse = known < policy.sparse_baseline_exposures
    locked = constraints.pinned_fields
    bounds: dict[str, Any] = {
        "max_load_percent": prescription.max_load_percent,
        "weekly_load_percent_cap": policy.weekly_load_percent_cap,
        "max_set_delta": policy.max_set_delta,
        "weekly_set_delta_cap": policy.weekly_set_delta_cap,
        "set_floor": prescription.set_floor,
        "set_ceiling": min(prescription.set_ceiling, constraints.max_sets or prescription.set_ceiling),
        "match_confidence": match,
        "sparse_baseline": sparse,
    }

    def outcome(
        decision_type: str,
        codes: Sequence[str],
        proposed: Mapping[str, Any] | None = None,
        *,
        confirm: bool = False,
        structural: bool = False,
        direction: int = 0,
    ) -> AdaptationOutcome:
        for code in codes:
            if code not in RATIONALE_TEXT:  # pragma: no cover - programmer error
                raise KeyError(f"Unknown rationale code: {code}")
        return AdaptationOutcome(
            decision_type=decision_type,
            rationale_codes=tuple(codes),
            previous=previous,
            proposed=dict(proposed or {}),
            requires_confirmation=confirm,
            structural=structural,
            confidence=_confidence_value(match, sparse),
            assessment=assessment.to_dict(),
            bounds=bounds,
            direction=direction,
        )

    # 1. hard safety / profile constraints ---------------------------------
    if constraints.blocked:
        if constraints.substitution_candidates:
            return outcome(
                PROPOSE_SUBSTITUTION,
                ("constraint_blocked_exercise", "substitution_proposed_not_applied"),
                {"substitute_template_ids": list(constraints.substitution_candidates)},
                confirm=True,
                structural=True,
            )
        return outcome(BLOCK_PROGRESSION, ("constraint_blocked_exercise", "no_approved_substitute"))

    # 3. match confidence (step 2, pins, is applied as a lock at every change)
    if match == MATCH_AMBIGUOUS:
        return outcome(REQUEST_CONFIRMATION, ("ambiguous_routine_match", "confirm_match_required"), confirm=True)
    if match == MATCH_UNMATCHED:
        return outcome(REQUEST_CONFIRMATION, ("unmatched_workout", "confirm_match_required"), confirm=True)

    # 4. pain / injury / technique feedback --------------------------------
    if constraints.pain_flag:
        return outcome(BLOCK_PROGRESSION, ("pain_reported", "progression_paused_pending_review"))
    if constraints.technique_flag:
        return outcome(BLOCK_PROGRESSION, ("technique_flagged", "progression_paused_pending_review"))

    # User-instructed cap (time limit) is an explicit instruction, not inference.
    cap = constraints.max_sets
    if cap is not None and prescription.sets > cap and "sets" not in locked:
        return outcome(
            REDUCE_SETS,
            ("time_limit_set_cap",),
            {"sets": max(prescription.set_floor, cap)},
            structural=False,
            direction=-1,
        )

    if assessment.result == RESULT_NO_DATA:
        return outcome(
            HOLD,
            ("no_rep_target" if _no_target(prescription) else "no_working_sets_recorded",),
        )

    # 6/7. trend and effort -------------------------------------------------
    signals = [item.signal for item in reliable_prior] + [assessment.signal]
    last_reset = next(
        (
            index
            for index in range(len(reliable_prior) - 1, -1, -1)
            if (reliable_prior[index].applied or reliable_prior[index].rejected)
            and reliable_prior[index].decision_type in RESETTING_TYPES
        ),
        None,
    )
    since = signals if last_reset is None else signals[last_reset + 1 :]
    success_streak = _trailing(since, SIGNAL_SUCCESS)
    failure_streak = _trailing(since, SIGNAL_FAILURE)
    reset_applied = last_reset is not None and reliable_prior[last_reset].applied
    reset_type = (
        reliable_prior[last_reset].decision_type
        if last_reset is not None and reset_applied
        else None
    )
    after_reset = (
        (len(reliable_prior) - 1 - last_reset) if last_reset is not None else None
    )
    in_cooldown = (
        reset_type in REDUCTION_TYPES
        and reset_type != EXERCISE_FATIGUE
        and after_reset is not None
        and after_reset + 1 <= policy.cooldown_exposures
    )
    notes: list[str] = []
    if match == MATCH_PROBABLE:
        notes.append("probable_match_reduced_bounds")
    if sparse:
        notes.append("sparse_baseline_conservative")

    def with_notes(*codes: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*codes, *notes)))

    # Fatigue management: separate enter and exit thresholds ---------------
    if history.fatigue_active:
        exits = _trailing(
            [
                SIGNAL_SUCCESS if item.result in {RESULT_MET, RESULT_EXCEEDED} and item.effort != "high" else SIGNAL_NEUTRAL
                for item in reliable_prior[(last_reset + 1 if last_reset is not None else 0) :]
            ]
            + [
                SIGNAL_SUCCESS
                if assessment.result in {RESULT_MET, RESULT_EXCEEDED} and assessment.effort != "high"
                else SIGNAL_NEUTRAL
            ],
            SIGNAL_SUCCESS,
        )
        if exits >= policy.fatigue_exit_successes:
            return outcome(
                EXERCISE_FATIGUE,
                with_notes("fatigue_exit_criteria_met"),
                {"phase": "exit"},
                direction=1,
            )
        return outcome(HOLD, with_notes("fatigue_management_active"))

    # 8. recovery evidence (never dominates; one signal is never enough) ----
    recovery_defers = False
    if recovery is None:
        pass  # missing recovery data never forces (or explains) a change
    elif not recovery.usable(policy):
        notes.append("recovery_stale_ignored")
    elif recovery.risk_signals >= policy.recovery_min_corroborating:
        recovery_defers = True
    elif recovery.risk_signals == 1:
        notes.append("recovery_signal_insufficient")

    needed = max(policy.min_successful_exposures, prescription.min_successful_exposures) + (1 if sparse else 0)
    if reset_type in REDUCTION_TYPES:
        needed = max(needed, policy.stable_exposures_before_reescalation)

    # De-escalation -----------------------------------------------------------
    if assessment.signal == SIGNAL_FAILURE:
        if failure_streak < policy.min_failed_exposures:
            code = (
                "single_miss_after_progression_no_reversal"
                if reset_type in {PROGRESS_LOAD, PROGRESS_DURATION_DISTANCE, PROGRESS_BODYWEIGHT}
                else "single_miss_no_change"
            )
            return outcome(HOLD, with_notes(code))
        if in_cooldown:
            return outcome(HOLD, with_notes("cooldown_after_reduction"))
        window = signals[-policy.fatigue_window :]
        if (
            reset_type in REDUCTION_TYPES
            or window.count(SIGNAL_FAILURE) >= policy.fatigue_enter_failures
        ) and match == MATCH_HIGH:
            return _enter_fatigue(prescription, assessment, constraints, policy, outcome, with_notes)
        return _reduce(prescription, assessment, history, locked, match, policy, outcome, with_notes, constraints)

    # Escalation -------------------------------------------------------------
    return _progress(
        prescription,
        assessment,
        history,
        locked,
        match,
        policy,
        outcome,
        with_notes,
        success_streak,
        needed,
        in_cooldown,
        recovery_defers,
        sparse,
    )


def _no_target(prescription: Prescription) -> bool:
    if prescription.modality == "duration":
        return not prescription.duration_seconds
    if prescription.modality == "distance":
        return not prescription.distance_meters
    return prescription.rep_min is None and prescription.rep_max is None


def _weekly_load_room(history: ExerciseHistory, policy: AdaptationPolicy) -> float:
    return max(0.0, policy.weekly_load_percent_cap - history.week_load_change_percent)


def _load_step(
    reference: float,
    percent: float,
    increment: float | None,
    *,
    room: float,
) -> tuple[float, float] | None:
    """Return ``(delta_kg, percent)`` of the largest in-bound step, or ``None``."""

    allowed_percent = min(percent, room)
    allowed_kg = reference * allowed_percent / 100.0
    if increment and increment > 0:
        steps = math.floor(allowed_kg / increment + 1e-9)
        delta = steps * increment
    else:
        delta = _round_down(allowed_kg, None)
    if delta <= 0:
        return None
    return round(delta, 4), round(delta / reference * 100.0, 3)


def _progress(
    prescription: Prescription,
    assessment: Assessment,
    history: ExerciseHistory,
    locked: frozenset[str],
    match: str,
    policy: AdaptationPolicy,
    outcome: Any,
    with_notes: Any,
    success_streak: int,
    needed: int,
    in_cooldown: bool,
    recovery_defers: bool,
    sparse: bool,
) -> AdaptationOutcome:
    modality = prescription.modality
    if assessment.below_prescribed_load:
        return outcome(HOLD, with_notes("performed_below_prescribed_load"))
    if assessment.effort == "high":
        return outcome(HOLD, with_notes("effort_above_target"))

    rep_based = modality in {"load", "bodyweight", "assisted"}
    # Within-range accumulation first (double progression).
    if (
        rep_based
        and assessment.result in {RESULT_MET, RESULT_EXCEEDED}
        and prescription.rep_max is not None
        and not (assessment.top_of_range and success_streak >= needed)
    ):
        current = prescription.rep_target if prescription.rep_target is not None else prescription.rep_min
        achieved = assessment.min_reps or 0
        if current is not None and achieved >= current and current < prescription.rep_max:
            if "reps" in locked:
                return outcome(HOLD, with_notes("user_pin_respected"))
            return outcome(
                PROGRESS_REPS,
                with_notes("within_block_envelope"),
                {"rep_target": min(prescription.rep_max, int(current) + 1)},
                direction=1,
            )
        return outcome(
            HOLD,
            with_notes(
                "awaiting_required_exposures"
                if assessment.top_of_range
                else "building_within_rep_range"
            ),
        )

    if assessment.signal != SIGNAL_SUCCESS:
        return outcome(HOLD, with_notes("building_within_rep_range"))
    if success_streak < needed or in_cooldown:
        return outcome(
            HOLD,
            with_notes("cooldown_after_reduction" if in_cooldown else "awaiting_required_exposures"),
        )
    if recovery_defers:
        return outcome(HOLD, with_notes("fresh_recovery_signals_defer_progression"))

    room = _weekly_load_room(history, policy)
    percent = prescription.max_load_percent
    scale = 1.0 if match == MATCH_HIGH else policy.probable_scale
    if sparse:
        scale *= policy.sparse_scale
    percent *= scale

    if modality == "load":
        if "load" in locked:
            return outcome(HOLD, with_notes("user_pin_respected"))
        reference = assessment.reference_load or prescription.load_kg
        if not reference:
            return outcome(HOLD, with_notes("no_load_reference"))
        step = _load_step(reference, percent, prescription.increment_kg, room=room)
        if step is None:
            if room <= 0:
                return outcome(HOLD, with_notes("weekly_change_cap_reached"))
            if (
                match == MATCH_HIGH
                and "reps" not in locked
                and prescription.rep_ceiling is not None
                and prescription.rep_max is not None
                and prescription.rep_max < prescription.rep_ceiling
                and prescription.rep_min is not None
            ):
                return outcome(
                    ALTER_REP_RANGE,
                    with_notes("load_increment_exceeds_bound", "rep_range_widened"),
                    {"rep_min": prescription.rep_min + 1, "rep_max": prescription.rep_max + 1, "rep_target": prescription.rep_min + 1},
                    structural=True,
                    direction=1,
                )
            return outcome(HOLD, with_notes("load_increment_exceeds_bound"))
        delta, applied_percent = step
        return outcome(
            PROGRESS_LOAD,
            with_notes("progression_criteria_met", "within_block_envelope"),
            {
                "load_kg": round(reference + delta, 4),
                "rep_target": prescription.rep_min,
                "load_change_percent": applied_percent,
            },
            direction=1,
        )

    if modality in {"bodyweight", "assisted"}:
        order = [item for item in prescription.progress_order if item != "reps"] or ["added_load"]
        variable = "less_assistance" if modality == "assisted" else order[0]
        if variable in {"added_load", "less_assistance"}:
            if "load" in locked:
                return outcome(HOLD, with_notes("user_pin_respected"))
            step_kg = prescription.increment_kg or policy.bodyweight_step_kg
            reference = assessment.reference_load or prescription.load_kg or 0.0
            if variable == "less_assistance":
                if reference <= 0:
                    return outcome(HOLD, with_notes("no_load_reference"))
                return outcome(
                    PROGRESS_BODYWEIGHT,
                    with_notes("bodyweight_variable_selected", "progression_criteria_met"),
                    {"variable": variable, "load_kg": round(max(0.0, reference - step_kg), 4), "rep_target": prescription.rep_min},
                    direction=1,
                )
            return outcome(
                PROGRESS_BODYWEIGHT,
                with_notes("bodyweight_variable_selected", "progression_criteria_met"),
                {"variable": variable, "load_kg": round(reference + step_kg, 4), "rep_target": prescription.rep_min},
                direction=1,
            )
        return outcome(
            PROGRESS_BODYWEIGHT,
            with_notes("bodyweight_variable_selected", "variable_needs_confirmation"),
            {"variable": variable},
            confirm=True,
            direction=1,
        )

    # duration / distance
    key = "duration_seconds" if modality == "duration" else "distance_meters"
    target = prescription.duration_seconds if modality == "duration" else prescription.distance_meters
    if not target:
        return outcome(HOLD, with_notes("no_rep_target"))
    allowed = min(percent, room)
    new_target = round(target * (1 + allowed / 100.0), 1)
    if allowed <= 0 or new_target <= target:
        return outcome(HOLD, with_notes("weekly_change_cap_reached"))
    return outcome(
        PROGRESS_DURATION_DISTANCE,
        with_notes("progression_criteria_met", "within_block_envelope"),
        {key: new_target, "load_change_percent": round((new_target / target - 1) * 100.0, 3)},
        direction=1,
    )


def _reduce(
    prescription: Prescription,
    assessment: Assessment,
    history: ExerciseHistory,
    locked: frozenset[str],
    match: str,
    policy: AdaptationPolicy,
    outcome: Any,
    with_notes: Any,
    constraints: Constraints,
) -> AdaptationOutcome:
    percent = prescription.max_load_percent * (1.0 if match == MATCH_HIGH else policy.probable_scale)
    reference = assessment.reference_load or prescription.load_kg
    if prescription.modality == "load" and reference:
        if "load" in locked:
            return outcome(HOLD, with_notes("user_pin_respected"))
        increment = prescription.increment_kg
        room = _weekly_load_room(history, policy)
        step = _load_step(reference, percent, increment, room=room)
        if step is None and increment and increment <= 2 * reference * percent / 100.0 and room > 0:
            step = (increment, round(increment / reference * 100.0, 3))
        if step is not None:
            delta, applied = step
            return outcome(
                REDUCE_LOAD,
                with_notes("repeated_underperformance", "within_block_envelope"),
                {"load_kg": round(reference - delta, 4), "load_change_percent": -applied},
                direction=-1,
            )
    if prescription.modality in {"duration", "distance"}:
        key = "duration_seconds" if prescription.modality == "duration" else "distance_meters"
        target = prescription.duration_seconds if prescription.modality == "duration" else prescription.distance_meters
        if target:
            new_target = round(target * (1 - percent / 100.0), 1)
            return outcome(
                REDUCE_LOAD,
                with_notes("repeated_underperformance", "within_block_envelope"),
                {key: new_target, "load_change_percent": -round(percent, 3)},
                direction=-1,
            )
    # Set reduction is structural; a probable match needs confirmation first.
    if "sets" in locked:
        return outcome(HOLD, with_notes("user_pin_respected"))
    if prescription.sets - 1 < prescription.set_floor:
        return outcome(HOLD, with_notes("reduction_at_set_floor"))
    if history.week_set_change >= policy.weekly_set_delta_cap:
        return outcome(HOLD, with_notes("weekly_change_cap_reached"))
    structural_codes = with_notes("repeated_underperformance")
    if match != MATCH_HIGH:
        return outcome(
            REDUCE_SETS,
            tuple(dict.fromkeys((*structural_codes, "probable_match_structural_needs_confirmation"))),
            {"sets": prescription.sets - 1},
            confirm=True,
            structural=True,
            direction=-1,
        )
    return outcome(
        REDUCE_SETS,
        structural_codes,
        {"sets": prescription.sets - 1},
        structural=True,
        direction=-1,
    )


def _enter_fatigue(
    prescription: Prescription,
    assessment: Assessment,
    constraints: Constraints,
    policy: AdaptationPolicy,
    outcome: Any,
    with_notes: Any,
) -> AdaptationOutcome:
    # Fatigue management is the one bounded exception to the set floor: it
    # temporarily cuts work by roughly 30-50% and is removed again on exit.
    sets = max(1, round(prescription.sets * policy.fatigue_set_multiplier))
    sets = min(sets, prescription.sets)
    proposed: dict[str, Any] = {
        "phase": "enter",
        "mode": "volume_only",
        "sets": sets,
        "exit_criteria": {"consecutive_met_exposures": policy.fatigue_exit_successes},
    }
    reference = assessment.reference_load or prescription.load_kg
    if assessment.effort == "high" and prescription.modality == "load" and reference:
        step = _load_step(reference, policy.fatigue_load_percent, prescription.increment_kg, room=policy.fatigue_load_percent)
        if step:
            proposed["mode"] = "load_plus_volume"
            proposed["load_kg"] = round(reference - step[0], 4)
            proposed["load_change_percent"] = -step[1]
    return outcome(
        EXERCISE_FATIGUE,
        with_notes("fatigue_management_entered"),
        proposed,
        direction=-1,
    )


# ---------------------------------------------------------------------------
# Programme-level fatigue management
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeekEvidence:
    """Aggregated, reliably-matched evidence for the programme-level review."""

    exercises_evaluated: int
    exercises_in_trouble: int
    sessions_with_trouble: int
    adherence: float | None = None
    user_fatigue_report: bool = False
    recovery: RecoveryEvidence | None = None
    weeks_since_programme_fatigue: int | None = None
    high_confidence: bool = True
    planned_fatigue_management: bool = False
    match_confidence: str = MATCH_HIGH

    def to_dict(self) -> dict[str, Any]:
        return {
            "exercises_evaluated": self.exercises_evaluated,
            "exercises_in_trouble": self.exercises_in_trouble,
            "sessions_with_trouble": self.sessions_with_trouble,
            "adherence": self.adherence,
            "user_fatigue_report": self.user_fatigue_report,
            "recovery": self.recovery.to_dict() if self.recovery else None,
            "weeks_since_programme_fatigue": self.weeks_since_programme_fatigue,
            "high_confidence": self.high_confidence,
            "planned_fatigue_management": self.planned_fatigue_management,
        }


def evaluate_programme_fatigue(
    evidence: WeekEvidence,
    policy: AdaptationPolicy = DEFAULT_POLICY,
) -> AdaptationOutcome | None:
    """Decide whether a programme-level fatigue-management revision is warranted.

    One poor session, one stale reading or one noisy wearable metric can never
    trigger it: it needs breadth (several exercises, several sessions) *and*
    independent corroboration, and it respects a cooldown between revisions.
    """

    if evidence.exercises_evaluated <= 0:
        return None
    ratio = evidence.exercises_in_trouble / evidence.exercises_evaluated
    breadth = (
        evidence.exercises_in_trouble >= policy.programme_fatigue_min_exercises
        and ratio >= policy.programme_fatigue_min_ratio
        and evidence.sessions_with_trouble >= policy.programme_fatigue_min_sessions
    )
    if not breadth:
        return None
    if (
        evidence.weeks_since_programme_fatigue is not None
        and evidence.weeks_since_programme_fatigue < policy.programme_fatigue_cooldown_weeks
    ):
        return None

    recovery = evidence.recovery
    recovery_support = bool(
        recovery is not None
        and recovery.usable(policy)
        and recovery.risk_signals >= policy.recovery_min_corroborating
    )
    low_adherence = bool(
        evidence.adherence is not None
        and evidence.adherence < policy.programme_fatigue_adherence_floor
    )
    corroborated = evidence.user_fatigue_report or low_adherence or recovery_support
    assessment = evidence.to_dict()
    if not corroborated:
        return AdaptationOutcome(
            decision_type=REQUEST_CONFIRMATION,
            rationale_codes=("programme_fatigue_unconfirmed",),
            previous={},
            proposed={},
            requires_confirmation=True,
            structural=False,
            confidence=0.4,
            assessment=assessment,
            bounds={},
        )
    if low_adherence and not recovery_support and not evidence.user_fatigue_report:
        mode = "frequency"
    elif recovery_support and evidence.exercises_in_trouble >= policy.programme_fatigue_min_exercises:
        mode = "load_plus_volume"
    else:
        mode = "volume_only"
    proposed = {
        "mode": mode,
        "weeks": 1,
        "set_multiplier": policy.fatigue_set_multiplier,
        "rir_delta": 1,
        "load_percent": -policy.fatigue_load_percent if mode == "load_plus_volume" else 0.0,
        "exit_criteria": {
            "consecutive_met_exposures": policy.fatigue_exit_successes,
            "max_weeks": 2,
        },
        "trigger_evidence": assessment,
    }
    confirm = evidence.planned_fatigue_management or not evidence.high_confidence
    codes = ["programme_fatigue_triggered"]
    if confirm:
        codes.append("programme_fatigue_confirmation_required")
    return AdaptationOutcome(
        decision_type=PROGRAMME_FATIGUE,
        rationale_codes=tuple(codes),
        previous={},
        proposed=proposed,
        requires_confirmation=confirm,
        structural=True,
        confidence=0.8 if evidence.high_confidence else 0.5,
        assessment=assessment,
        bounds={"max_weeks": 2, "set_multiplier": policy.fatigue_set_multiplier},
        direction=-1,
    )


# ---------------------------------------------------------------------------
# Folding applied decisions into the effective prescription
# ---------------------------------------------------------------------------

_FIELD_KEYS = (
    "sets",
    "rep_min",
    "rep_max",
    "rep_target",
    "load_kg",
    "duration_seconds",
    "distance_meters",
)


def fold_effective(
    base: Mapping[str, Any],
    decisions: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply stored, *applied* decisions (oldest first) onto a base prescription.

    Each decision mapping carries ``decision_type`` and ``proposed``.  Fatigue
    management is a temporary overlay: an ``exit`` phase removes it and the
    prescription falls back to the progression state underneath.
    """

    effective = dict(base)
    overlay: dict[str, Any] | None = None
    for decision in decisions:
        decision_type = str(decision.get("decision_type"))
        proposed = dict(decision.get("proposed") or {})
        if decision_type == EXERCISE_FATIGUE:
            overlay = None if proposed.get("phase") == "exit" else {
                key: proposed[key] for key in ("sets", "load_kg") if key in proposed
            }
            continue
        if decision_type in CHANGING_TYPES:
            for key in _FIELD_KEYS:
                if key in proposed and proposed[key] is not None:
                    effective[key] = proposed[key]
    if overlay:
        effective.update(overlay)
        effective["fatigue_management"] = True
    return effective


__all__ = [
    "DECISION_TYPES",
    "DEFAULT_POLICY",
    "RATIONALE_TEXT",
    "RULE_VERSION",
    "AdaptationOutcome",
    "AdaptationPolicy",
    "Assessment",
    "Constraints",
    "ExerciseHistory",
    "Observation",
    "Prescription",
    "PriorExposure",
    "RecoveryEvidence",
    "SetObservation",
    "WeekEvidence",
    "assess_exposure",
    "decide",
    "evaluate_programme_fatigue",
    "explain",
    "fold_effective",
    "recovery_evidence_from_features",
]
