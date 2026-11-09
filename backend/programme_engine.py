"""Deterministic, bounded and explainable programme adaptation decisions.

This module contains the pure decision logic used by the programme lifecycle.
It deliberately consumes structured observations only: no LLM call, provider
request or mutable template participates in a decision.  Every returned
decision records the rule version, rationale codes and the exact prescription
that was considered.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

RULE_VERSION = 1

MatchConfidence = Literal["high", "probable", "ambiguous", "unmatched"]
BaselineConfidence = Literal["sufficient", "sparse"]

MIN_SUCCESSES = {"conservative": 3, "balanced": 2, "aggressive": 2}
MAX_LOAD_STEP_PCT = {"conservative": 0.025, "balanced": 0.05, "aggressive": 0.075}
MAX_REP_STEP = 1
MAX_SET_STEP = 1
COOLDOWN_EXPOSURES = 2
FATIGUE_ENTER_EXPOSURES = 2
MAX_WEEKLY_LOAD_CHANGE_PCT = 0.10
MAX_WEEKLY_SET_CHANGE = 2
_REDUCTION_TYPES = frozenset(
    {
        "reduce_load",
        "reduce_sets",
        "fatigue_management",
        "programme_fatigue_revision",
    }
)
_PROGRESSION_TYPES = frozenset(
    {
        "progress_load",
        "progress_reps",
        "progress_duration",
        "progress_bodyweight",
    }
)
_REP_RANGE_RE = re.compile(r"(\d+)\s*[-\u2013]\s*(\d+)")
_SUPPORTED_EXERCISE_TYPES = frozenset(
    {
        "weight_reps",
        "weight",
        "bodyweight_reps",
        "bodyweight",
        "assisted_bodyweight",
        "duration",
        "distance",
        "duration_distance",
    }
)


class ProgrammeValidationError(ValueError):
    """Raised when a generated revision is structurally unsafe or inconsistent."""


class DecisionType(StrEnum):
    """Explicit deterministic outcomes supported by the engine."""

    PROGRESS_LOAD = "progress_load"
    PROGRESS_REPS = "progress_reps"
    PROGRESS_DURATION = "progress_duration"
    PROGRESS_BODYWEIGHT = "progress_bodyweight"
    HOLD = "hold"
    REDUCE_LOAD = "reduce_load"
    REDUCE_SETS = "reduce_sets"
    ALTER_REP_RANGE = "alter_rep_range"
    FATIGUE_MANAGEMENT = "fatigue_management"
    PROGRAMME_FATIGUE_REVISION = "programme_fatigue_revision"
    REQUEST_CONFIRMATION = "request_confirmation"
    BLOCK_PROGRESSION = "block_progression"
    PROPOSE_SUBSTITUTION = "propose_substitution"
    USER_OVERRIDE = "user_override"
    REVERT = "revert"


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    """Bounded recovery evidence with explicit freshness and provenance."""

    fresh: bool
    quality: float
    source: str = "unknown"
    signals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriorDecision:
    """One previous decision, oldest first in ``AdaptationContext.history``."""

    decision_type: str
    applied: bool = True
    target_met: bool | None = None
    previous_prescription: dict[str, Any] = field(default_factory=dict)
    proposed_prescription: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdaptationContext:
    """Structured inputs for one exercise exposure."""

    exercise_template_id: str
    exercise_type: str = "weight_reps"
    role: str = ""
    match_confidence: str = "high"
    baseline_confidence: str = "sufficient"
    hard_constraint_block: bool = False
    user_override: str | None = None
    pain_flag: bool = False
    technique_flag: bool = False
    substitution_available: bool = False
    prescribed_sets: int = 3
    prescribed_rep_low: int = 8
    prescribed_rep_high: int = 12
    prescribed_load_kg: float | None = None
    target_rir: str | None = None
    completed_sets: int = 0
    completed_reps: int | None = None
    completed_load_kg: float | None = None
    target_rir_met: bool = True
    performance_trend: str | None = None
    logged_rpe: float | None = None
    adherence_ratio: float = 1.0
    poor_recovery_streak: int = 0
    recovery: RecoveryContext | None = None
    aggressiveness: str = "balanced"
    block_min_sets: int = 1
    block_max_sets: int = 8
    block_rep_low: int | None = None
    block_rep_high: int | None = None
    week_change: dict[str, float] = field(default_factory=dict)
    history: tuple[PriorDecision, ...] = ()

    def prescription(self) -> dict[str, Any]:
        """Return the current stored prescription for this exercise."""
        return {
            "sets": max(1, int(self.prescribed_sets)),
            "rep_range": f"{self.prescribed_rep_low}-{self.prescribed_rep_high}",
            "rep_low": int(self.prescribed_rep_low),
            "rep_high": int(self.prescribed_rep_high),
            "load_kg": self.prescribed_load_kg,
            "target_rir": self.target_rir,
        }


@dataclass(frozen=True, slots=True)
class AdaptationDecision:
    """One auditable, versioned decision."""

    decision_type: DecisionType
    previous_prescription: dict[str, Any]
    proposed_prescription: dict[str, Any]
    rationale_codes: tuple[str, ...]
    confidence: str = "moderate"
    applied: bool = True
    requires_confirmation: bool = False
    programme_level: bool = False
    rule_version: int = RULE_VERSION
    previous_decision: str | None = None


def parse_rep_range(value: Any) -> tuple[int, int] | None:
    """Parse ``"8-12"`` style ranges, returning ``None`` when unusable."""
    match = _REP_RANGE_RE.search(str(value or ""))
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (min(low, high), max(low, high))


def compute_source_hash(source_snapshots: list[dict[str, Any]]) -> str:
    """Return a stable hash over the ordered provider routine snapshots."""
    payload = [
        {
            "routine_id": snapshot.get("id"),
            "source_hash": snapshot.get("source_hash"),
        }
        for snapshot in source_snapshots
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_programme_definition(preview: dict[str, Any]) -> None:
    """Validate a generated/merged revision before it can be persisted.

    This is the deterministic activation gate: it checks source identity, block
    coverage, prescribed weeks and exercise-level prescription completeness.
    """
    if not isinstance(preview, dict) or not preview:
        raise ProgrammeValidationError("Programme definition must be a non-empty object.")
    for key in (
        "engine_version",
        "preview_token",
        "programme_spec",
        "source_snapshots",
        "blocks",
        "days",
    ):
        if key not in preview:
            raise ProgrammeValidationError(f"Programme definition is missing {key!r}.")
    if not str(preview.get("engine_version") or "").strip():
        raise ProgrammeValidationError("Programme engine version is required.")
    if len(str(preview.get("preview_token") or "")) < 16:
        raise ProgrammeValidationError("Programme preview token is invalid.")

    spec = preview.get("programme_spec")
    if not isinstance(spec, dict):
        raise ProgrammeValidationError("Programme specification is invalid.")
    selected = spec.get("selected_routine_ids") or []
    if not selected or len(set(map(str, selected))) != len(selected):
        raise ProgrammeValidationError(
            "Programme must select unique Hevy routine IDs."
        )
    try:
        duration = int(str(spec.get("duration_weeks")))
    except (TypeError, ValueError) as exc:
        raise ProgrammeValidationError("Programme duration is invalid.") from exc
    if duration < 1:
        raise ProgrammeValidationError("Programme duration must be positive.")
    if not str(spec.get("start_date") or "").strip():
        raise ProgrammeValidationError("Programme start date is required.")

    snapshots = preview.get("source_snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ProgrammeValidationError("Programme source snapshots are required.")
    snapshot_ids: list[str] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ProgrammeValidationError("Programme source snapshot is invalid.")
        routine_id = str(snapshot.get("id") or "")
        if not routine_id or not str(snapshot.get("source_hash") or ""):
            raise ProgrammeValidationError(
                "Every source snapshot requires a routine ID and hash."
            )
        snapshot_ids.append(routine_id)
    if len(set(snapshot_ids)) != len(snapshot_ids):
        raise ProgrammeValidationError("Duplicate source snapshots are not allowed.")
    if snapshot_ids != [str(item) for item in selected]:
        raise ProgrammeValidationError(
            "Source snapshot order must match the selected routine order."
        )

    blocks = preview.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ProgrammeValidationError("Programme blocks are required.")
    expected_start = 1
    block_numbers: set[int] = set()
    for block in blocks:
        try:
            number = int(block.get("number"))
            start = int(block.get("start_week"))
            end = int(block.get("end_week"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProgrammeValidationError("Programme block range is invalid.") from exc
        if start != expected_start or end < start:
            raise ProgrammeValidationError(
                "Programme blocks must be contiguous and cover every week once."
            )
        if number in block_numbers:
            raise ProgrammeValidationError("Programme block numbers must be unique.")
        block_numbers.add(number)
        expected_start = end + 1
    if expected_start - 1 != duration:
        raise ProgrammeValidationError(
            "Programme blocks must cover the full requested duration."
        )

    days = preview.get("days")
    if not isinstance(days, list) or not days:
        raise ProgrammeValidationError("Programme routine rotation is required.")
    day_ids = [str(day.get("routine_id") or "") for day in days]
    if day_ids != [str(item) for item in selected]:
        raise ProgrammeValidationError(
            "Programme rotation must preserve the selected routine order."
        )
    for day in days:
        routine_id = str(day.get("routine_id") or "")
        if not str(day.get("source_hash") or ""):
            raise ProgrammeValidationError(
                f"Routine {routine_id!r} is missing its source hash."
            )
        exercises = day.get("exercises")
        if not isinstance(exercises, list) or not exercises:
            raise ProgrammeValidationError(
                f"Routine {routine_id!r} has no exercise prescriptions."
            )
        for exercise in exercises:
            if not str(exercise.get("template_id") or ""):
                raise ProgrammeValidationError(
                    f"Routine {routine_id!r} has an exercise without a provider ID."
                )
            prescriptions = exercise.get("prescriptions")
            if not isinstance(prescriptions, list) or not prescriptions:
                raise ProgrammeValidationError(
                    f"Exercise {exercise.get('template_id')!r} has no prescriptions."
                )
            covered = {
                int(str(item.get("block_number")))
                for item in prescriptions
                if isinstance(item, dict) and item.get("block_number") is not None
            }
            if covered != block_numbers:
                raise ProgrammeValidationError(
                    f"Exercise {exercise.get('template_id')!r} does not cover every block."
                )
            for item in prescriptions:
                try:
                    sets = int(item.get("sets"))
                except (TypeError, ValueError) as exc:
                    raise ProgrammeValidationError(
                        "Prescribed set count must be an integer."
                    ) from exc
                if sets < 1:
                    raise ProgrammeValidationError(
                        "Prescribed set count must be at least one."
                    )
                if not str(item.get("rep_range") or "").strip():
                    raise ProgrammeValidationError(
                        "Every prescription requires a rep range or work target."
                    )


def diff_routine_snapshots(
    active_snapshots: list[dict[str, Any]],
    current_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an exact provider-versus-programme difference.

    The active revision is never modified by this function; callers decide
    whether to keep, merge, rebase or replace after reviewing the result.
    """

    active_by_id = {str(item.get("id")): item for item in active_snapshots}
    current_by_id = {str(item.get("id")): item for item in current_snapshots}
    active_order = list(active_by_id)
    current_order = [item for item in current_by_id if item in active_by_id]

    removed = [item for item in active_order if item not in current_by_id]
    added = [item for item in current_by_id if item not in active_by_id]
    reordered = [
        item for item in active_order if item in current_by_id
    ] != current_order
    changed: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    for routine_id in active_order:
        if routine_id not in current_by_id:
            continue
        before = active_by_id[routine_id]
        after = current_by_id[routine_id]
        if before.get("source_hash") == after.get("source_hash"):
            continue
        before_exercises = {
            str(item.get("template_id")): item
            for item in before.get("exercises") or []
            if isinstance(item, dict)
        }
        after_exercises = {
            str(item.get("template_id")): item
            for item in after.get("exercises") or []
            if isinstance(item, dict)
        }
        renamed = [
            {
                "template_id": template_id,
                "before": before_exercises[template_id].get("title"),
                "after": after_exercises[template_id].get("title"),
            }
            for template_id in before_exercises.keys() & after_exercises.keys()
            if before_exercises[template_id].get("title")
            != after_exercises[template_id].get("title")
        ]
        exercise_added = [
            template_id
            for template_id in after_exercises.keys() - before_exercises.keys()
        ]
        exercise_removed = [
            template_id
            for template_id in before_exercises.keys() - after_exercises.keys()
        ]
        before_order = list(before_exercises)
        after_order = list(after_exercises)
        exercise_reordered = (
            [
                item
                for item in after_order
                if item in before_exercises
            ]
            != [item for item in before_order if item in after_exercises]
        )
        for template_id, exercise in after_exercises.items():
            if str(exercise.get("exercise_type") or "") not in _SUPPORTED_EXERCISE_TYPES:
                unsupported.append(
                    {
                        "routine_id": routine_id,
                        "template_id": template_id,
                        "title": exercise.get("title"),
                    }
                )
        changed.append(
            {
                "routine_id": routine_id,
                "title_before": before.get("title"),
                "title_after": after.get("title"),
                "renamed": before.get("title") != after.get("title"),
                "renamed_exercises": renamed,
                "exercises_added": exercise_added,
                "exercises_removed": exercise_removed,
                "exercises_reordered": exercise_reordered,
                "before_hash": before.get("source_hash"),
                "after_hash": after.get("source_hash"),
            }
        )

    changed_ids = {item["routine_id"] for item in changed}
    deleted = [
        routine_id
        for routine_id in removed
        if str(active_by_id[routine_id].get("title") or "").strip()
    ]
    return {
        # The active snapshot order is the user's deliberate selection order, so
        # a different provider order alone is not structural source drift.
        "has_drift": bool(added or removed or changed or unsupported),
        "added": added,
        "removed": removed,
        "deleted": deleted,
        "renamed": [
            item for item in changed if bool(item.get("renamed"))
        ],
        "reordered": reordered,
        "changed": changed,
        "changed_ids": sorted(changed_ids),
        "unsupported": unsupported,
        "active_order": active_order,
        "current_common_order": current_order,
    }


def _normalise(value: str | None) -> str:
    return str(value or "").strip().lower()


def _is_reduction(value: str) -> bool:
    return _normalise(value) in _REDUCTION_TYPES


def _is_progression(value: str) -> bool:
    return _normalise(value) in _PROGRESSION_TYPES


def _trailing(
    history: tuple[PriorDecision, ...],
    predicate: Any,
) -> int:
    count = 0
    for prior in reversed(history):
        if predicate(prior):
            count += 1
        else:
            break
    return count


def _reduction_age(history: tuple[PriorDecision, ...]) -> int | None:
    for age, prior in enumerate(reversed(history)):
        if prior.applied and _is_reduction(prior.decision_type):
            return age
    return None


def _last_direction(history: tuple[PriorDecision, ...]) -> str | None:
    for prior in reversed(history):
        if not prior.applied:
            continue
        if _is_reduction(prior.decision_type):
            return "reduce"
        if _is_progression(prior.decision_type):
            return "progress"
    return None


def _confidence(match_confidence: str, baseline_confidence: str) -> str:
    match = _normalise(match_confidence)
    baseline = _normalise(baseline_confidence)
    if match == "high" and baseline == "sufficient":
        return "high"
    if match in {"high", "probable"} and baseline == "sparse":
        return "moderate"
    if match == "probable":
        return "moderate"
    return "low"


def _decision(
    decision_type: DecisionType,
    previous: dict[str, Any],
    proposed: dict[str, Any],
    reasons: tuple[str, ...],
    *,
    confidence: str,
    applied: bool = True,
    requires_confirmation: bool = False,
    programme_level: bool = False,
    previous_decision: str | None = None,
) -> AdaptationDecision:
    return AdaptationDecision(
        decision_type=decision_type,
        previous_prescription=dict(previous),
        proposed_prescription=dict(proposed),
        rationale_codes=tuple(dict.fromkeys(reasons)),
        confidence=confidence,
        applied=applied,
        requires_confirmation=requires_confirmation,
        programme_level=programme_level,
        previous_decision=previous_decision,
    )


def _set_delta(previous: dict[str, Any], proposed: dict[str, Any]) -> int:
    try:
        return int(proposed.get("sets", 0)) - int(previous.get("sets", 0))
    except (TypeError, ValueError):
        return 0


def _load_delta_pct(previous: dict[str, Any], proposed: dict[str, Any]) -> float:
    try:
        before = float(previous.get("load_kg") or 0)
        after = float(proposed.get("load_kg") or 0)
    except (TypeError, ValueError):
        return 0.0
    if before <= 0 or after <= 0:
        return 0.0
    return abs(after - before) / before


def _within_bounds(
    context: AdaptationContext,
    previous: dict[str, Any],
    proposed: dict[str, Any],
    *,
    match_confidence: str,
) -> bool:
    set_delta = abs(_set_delta(previous, proposed))
    if set_delta > MAX_SET_STEP:
        return False
    if _is_progression(str(proposed.get("_direction", ""))) and (
        _normalise(match_confidence) == "probable" and set_delta > 0
    ):
        return False
    try:
        sets = int(str(proposed.get("sets")))
    except (TypeError, ValueError):
        return False
    if sets < context.block_min_sets or sets > max(
        context.block_max_sets, context.block_min_sets
    ):
        return False
    load_pct = _load_delta_pct(previous, proposed)
    step = MAX_LOAD_STEP_PCT.get(context.aggressiveness, 0.05)
    if load_pct > step + 1e-9:
        return False
    if (
        context.week_change.get("load_pct", 0.0) + load_pct
        > MAX_WEEKLY_LOAD_CHANGE_PCT + 1e-9
    ):
        return False
    if (
        context.week_change.get("set_delta", 0.0) + set_delta
        > MAX_WEEKLY_SET_CHANGE
    ):
        return False
    rep_low = proposed.get("rep_low")
    rep_high = proposed.get("rep_high")
    if rep_low is not None and rep_high is not None:
        if int(rep_low) > int(rep_high):
            return False
        block_low = context.block_rep_low
        block_high = context.block_rep_high
        if block_low is not None and int(rep_low) < int(block_low):
            return False
        if block_high is not None and int(rep_high) > int(block_high):
            return False
    return True


def _apply_reps(previous: dict[str, Any], delta: int = 1) -> dict[str, Any]:
    proposed = dict(previous)
    parsed = parse_rep_range(proposed.get("rep_range"))
    if parsed is None:
        return proposed
    low = int(proposed.get("rep_low") or parsed[0])
    high = int(proposed.get("rep_high") or parsed[1])
    proposed["rep_low"] = low + delta
    proposed["rep_high"] = high + delta
    proposed["rep_range"] = f"{low + delta}-{high + delta}"
    return proposed


def _apply_load(previous: dict[str, Any], step: float) -> dict[str, Any]:
    proposed = dict(previous)
    load = proposed.get("load_kg")
    if load is None:
        return proposed
    proposed["load_kg"] = round(float(load) * (1.0 + step), 2)
    return proposed


def _apply_duration(previous: dict[str, Any], step: float) -> dict[str, Any]:
    proposed = dict(previous)
    duration = proposed.get("duration_seconds")
    if duration is None:
        return _apply_reps(previous)
    proposed["duration_seconds"] = round(float(duration) * (1.0 + step))
    return proposed


def _override_decision(
    context: AdaptationContext,
    previous: dict[str, Any],
    confidence: str,
) -> AdaptationDecision | None:
    requested = _normalise(context.user_override)
    if requested in {item.value for item in DecisionType}:
        pass
    else:
        return None
    proposed = dict(previous)
    if requested == DecisionType.HOLD.value:
        return _decision(
            DecisionType.HOLD,
            previous,
            proposed,
            ("explicit_user_override",),
            confidence=confidence,
            previous_decision=requested,
        )
    if requested == DecisionType.REDUCE_LOAD.value:
        proposed = _apply_load(previous, -MAX_LOAD_STEP_PCT.get(context.aggressiveness, 0.05))
    elif requested == DecisionType.REDUCE_SETS.value:
        proposed["sets"] = max(1, int(proposed.get("sets", 1)) - 1)
    elif requested == DecisionType.PROGRESS_LOAD.value:
        proposed = _apply_load(previous, MAX_LOAD_STEP_PCT.get(context.aggressiveness, 0.05))
    elif requested in {
        DecisionType.PROGRESS_REPS.value,
        DecisionType.ALTER_REP_RANGE.value,
    }:
        proposed = _apply_reps(previous)
    else:
        return _decision(
            DecisionType.HOLD,
            previous,
            proposed,
            ("override_not_automatically_applicable",),
            confidence=confidence,
            applied=False,
            requires_confirmation=True,
            previous_decision=requested,
        )
    if _load_delta_pct(previous, proposed) > MAX_LOAD_STEP_PCT.get(
        context.aggressiveness, 0.05
    ) + 1e-9:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("override_exceeds_bounded_change",),
            confidence=confidence,
            applied=False,
            requires_confirmation=True,
            previous_decision=requested,
        )
    return _decision(
        DecisionType(requested),
        previous,
        proposed,
        ("explicit_user_override",),
        confidence=confidence,
        previous_decision=requested,
    )


def _progress_decision(
    context: AdaptationContext,
    previous: dict[str, Any],
    confidence: str,
    *,
    match_confidence: str,
    reasons: list[str],
) -> AdaptationDecision:
    role = _normalise(context.role)
    exercise_type = _normalise(context.exercise_type)
    step = MAX_LOAD_STEP_PCT.get(context.aggressiveness, 0.05)
    if _normalise(match_confidence) == "probable":
        step = min(step, 0.025)
    if role == "duration_distance" or exercise_type in {"duration", "distance"}:
        proposed = _apply_duration(previous, 0.05)
        decision_type = DecisionType.PROGRESS_DURATION
        reasons.append("duration_distance_progression")
    elif (
        role in {"bodyweight_progression", "bodyweight"}
        or exercise_type in {"bodyweight_reps", "bodyweight"}
    ):
        proposed = _apply_reps(previous)
        decision_type = DecisionType.PROGRESS_BODYWEIGHT
        reasons.append("bodyweight_progression")
    elif exercise_type in {"weight_reps", "weight", "assisted_bodyweight"}:
        at_top = (
            context.completed_reps is not None
            and int(context.completed_reps) >= int(context.prescribed_rep_high)
            and previous.get("load_kg")
        )
        if at_top:
            proposed = _apply_load(previous, step)
            decision_type = DecisionType.PROGRESS_LOAD
            reasons.append("top_of_rep_range_reached")
        else:
            proposed = _apply_reps(previous)
            decision_type = DecisionType.PROGRESS_REPS
            reasons.append("double_progression_inside_range")
    else:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            tuple(reasons + ["no_deterministic_progression_rule"]),
            confidence=confidence,
        )
    proposed["_direction"] = decision_type.value
    if not _within_bounds(
        context,
        previous,
        proposed,
        match_confidence=match_confidence,
    ):
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            tuple(reasons + ["bounded_change_limit_reached"]),
            confidence=confidence,
        )
    return _decision(
        decision_type,
        previous,
        proposed,
        tuple(reasons),
        confidence=confidence,
    )


def _reduce_decision(
    context: AdaptationContext,
    previous: dict[str, Any],
    confidence: str,
    reasons: list[str],
    *,
    fatigue: bool = False,
) -> AdaptationDecision:
    if fatigue:
        proposed = dict(previous)
        proposed["sets"] = max(1, int(proposed.get("sets", 1)) - 1)
        proposed["_direction"] = DecisionType.REDUCE_SETS.value
        decision_type = DecisionType.FATIGUE_MANAGEMENT
        reasons.append("fatigue_management_reduction")
    elif previous.get("load_kg"):
        proposed = _apply_load(
            previous, -MAX_LOAD_STEP_PCT.get(context.aggressiveness, 0.05)
        )
        proposed["_direction"] = DecisionType.REDUCE_LOAD.value
        decision_type = DecisionType.REDUCE_LOAD
        reasons.append("reduce_load_after_repeated_miss")
    else:
        proposed = dict(previous)
        proposed["sets"] = max(1, int(proposed.get("sets", 1)) - 1)
        proposed["_direction"] = DecisionType.REDUCE_SETS.value
        decision_type = DecisionType.REDUCE_SETS
        reasons.append("reduce_sets_after_repeated_miss")
    if not _within_bounds(
        context,
        previous,
        proposed,
        match_confidence=context.match_confidence,
    ):
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            tuple(reasons + ["bounded_change_limit_reached"]),
            confidence=confidence,
        )
    return _decision(decision_type, previous, proposed, tuple(reasons), confidence=confidence)


def decide(context: AdaptationContext) -> AdaptationDecision:
    """Return one deterministic decision using the documented decision order."""

    previous = context.prescription()
    match_confidence = _normalise(context.match_confidence)
    baseline = _normalise(context.baseline_confidence)
    confidence = _confidence(match_confidence, baseline)
    reasons: list[str] = []

    # 1. Hard safety/profile constraints.
    if context.hard_constraint_block:
        return _decision(
            DecisionType.BLOCK_PROGRESSION,
            previous,
            previous,
            ("hard_safety_or_profile_constraint",),
            confidence="high",
        )

    # 2. Explicit user override.
    override = _override_decision(context, previous, confidence)
    if override is not None:
        return override

    # 3. Routine/workout match confidence.
    if match_confidence in {"ambiguous", "unmatched"}:
        return _decision(
            DecisionType.REQUEST_CONFIRMATION,
            previous,
            previous,
            (f"match_confidence_{match_confidence}",),
            confidence="low",
            applied=False,
            requires_confirmation=True,
        )

    # 4. Pain, injury and technique feedback.
    if context.pain_flag or context.technique_flag:
        if context.substitution_available and context.pain_flag:
            return _decision(
                DecisionType.PROPOSE_SUBSTITUTION,
                previous,
                previous,
                ("pain_with_substitution_candidate",),
                confidence=confidence,
                applied=False,
                requires_confirmation=True,
            )
        return _decision(
            DecisionType.BLOCK_PROGRESSION,
            previous,
            previous,
            (
                "pain_or_injury_feedback"
                if context.pain_flag
                else "technique_feedback",
            ),
            confidence=confidence,
        )

    success = (
        context.completed_sets >= context.prescribed_sets
        and context.target_rir_met
        and (
            context.completed_reps is None
            or int(context.completed_reps) >= context.prescribed_rep_low
        )
    )
    success_streak = _trailing(context.history, lambda item: item.target_met is True)
    failure_streak = _trailing(
        context.history, lambda item: item.target_met is False
    )
    reduction_age = _reduction_age(context.history)
    last_direction = _last_direction(context.history)
    fatigue_streak = _trailing(
        context.history,
        lambda item: _normalise(item.decision_type)
        == DecisionType.FATIGUE_MANAGEMENT.value,
    )
    recovery_bad = bool(
        context.recovery is not None
        and context.recovery.fresh
        and context.recovery.quality < 0.35
    )

    # 6. Performance trend.
    trend = _normalise(context.performance_trend)

    # 8. Fatigue management needs repeated evidence. One noisy or missing
    # recovery reading must never force a change.
    if recovery_bad and (
        fatigue_streak + context.poor_recovery_streak + 1 >= FATIGUE_ENTER_EXPOSURES
    ):
        return _reduce_decision(
            context,
            previous,
            confidence,
            ["repeated_fresh_recovery_signal"],
            fatigue=True,
        )

    # 5/7. Target completion and logged effort.
    if not success or trend == "declining":
        if context.completed_sets < context.prescribed_sets:
            reasons.append("partial_or_missed_sets")
        if context.completed_reps is not None and context.completed_reps < context.prescribed_rep_low:
            reasons.append("below_rep_target")
        if not context.target_rir_met:
            reasons.append("target_effort_not_met")
        if context.logged_rpe is not None and context.logged_rpe >= 9.0:
            reasons.append("high_logged_effort")
        if trend == "declining":
            reasons.append("declining_performance_trend")
        if recovery_bad:
            reasons.append("single_noisy_recovery_signal")
        reduce_after = 2
        if failure_streak < reduce_after:
            return _decision(
                DecisionType.HOLD,
                previous,
                previous,
                tuple(reasons + ["accumulating_failure_evidence"]),
                confidence=confidence,
            )
        if last_direction == "reduce" and reduction_age is not None and reduction_age < COOLDOWN_EXPOSURES:
            reasons.append("reduction_cooldown_active")
        return _reduce_decision(context, previous, confidence, reasons)

    # 5/6. Target met. Gate escalation on adherence, hysteresis and a stable
    # baseline. Low adherence never blocks a necessary reduction above.
    if context.adherence_ratio < 0.6:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("low_adherence_hold",),
            confidence=confidence,
        )
    if context.logged_rpe is not None and context.logged_rpe >= 9.0:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("target_met_at_high_effort",),
            confidence=confidence,
        )
    if trend == "declining":
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("declining_trend_hold",),
            confidence=confidence,
        )
    if reduction_age is not None and reduction_age < COOLDOWN_EXPOSURES:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("cooldown_after_reduction",),
            confidence=confidence,
        )
    minimum = MIN_SUCCESSES.get(context.aggressiveness, 2)
    if match_confidence == "probable":
        minimum += 1
    if baseline == "sparse":
        minimum += 1
    if success_streak < minimum:
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("accumulating_success_evidence",),
            confidence=confidence,
        )
    if (
        last_direction == "reduce"
        and reduction_age is not None
        and success_streak < minimum + 1
    ):
        return _decision(
            DecisionType.HOLD,
            previous,
            previous,
            ("stable_baseline_required_after_reduction",),
            confidence=confidence,
        )

    # 9/10. Block envelope and exercise-specific progression rule.
    reasons.extend(["target_completed", "hysteresis_satisfied"])
    if match_confidence == "probable":
        reasons.append("probable_match_smaller_reversible_change")
    if baseline == "sparse":
        reasons.append("sparse_baseline_conservative_change")
    return _progress_decision(
        context,
        previous,
        confidence,
        match_confidence=match_confidence,
        reasons=reasons,
    )


def programme_fatigue_decision(
    trigger_reasons: list[str],
) -> AdaptationDecision:
    """Create a confirmation-gated whole-programme fatigue revision decision."""
    return AdaptationDecision(
        decision_type=DecisionType.PROGRAMME_FATIGUE_REVISION,
        previous_prescription={},
        proposed_prescription={},
        rationale_codes=tuple(
            dict.fromkeys(trigger_reasons + ["whole_programme_fatigue_revision"])
        ),
        confidence="moderate",
        applied=False,
        requires_confirmation=True,
        programme_level=True,
    )
