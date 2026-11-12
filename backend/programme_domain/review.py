"""Orchestration of deterministic adaptation for an active programme.

:mod:`programme_domain.adaptation` decides; this module gathers the stored
inputs, applies the bounded result and records an audit row for every
evaluation.  Applying a decision follows one documented policy
(``docs/PROGRAMME_ADAPTATION.md``):

* progression, load/duration changes, within-range rep targets and exercise
  fatigue management are *week-state level*: they live only as applied
  decisions folded over the immutable revision (reversible, never copied into
  a revision);
* set reductions, rep-range changes and programme-level fatigue management are
  *structural*: they append a new immutable revision after the shared validator
  accepts it;
* substitutions are only ever proposed; accepting one records a visible
  user override instead of rewriting the programme.

Everything is tenant scoped: every query filters by ``user_id`` and every
cross-record lookup goes through the repository.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from . import adaptation as engine
from .activation import build_week_state, definition_from_revision, definition_to_draft
from .models import (
    APPLIED_STATES,
    STATE_AUTO_APPLIED,
    STATE_CONFIRMED,
    STATE_INFORMATIONAL,
    STATE_OVERRIDDEN,
    STATE_PENDING,
    STATE_REJECTED,
    STATE_REVERTED,
    STATE_SUPERSEDED,
    AdaptationDecision,
    AdaptationDecisionInput,
    Programme,
    ProgrammeNotFoundError,
    ProgrammeOverride,
    ProgrammeRevision,
    ProgrammeValidationError,
)
from .repository import ProgrammeRepository
from .weeks import (
    classify_session,
    local_date_of,
    parse_timestamp,
    rolling_pointer,
    week_for_date,
)

OVERRIDE_KINDS = frozenset(
    {
        "pin",
        "pain_report",
        "technique_report",
        "equipment_issue",
        "time_limit",
        "substitution",
        "fatigue_report",
        "review_request",
    }
)
PIN_FIELDS = frozenset({"load", "sets", "reps"})
_STRUCTURAL_WEEK_KEYS = ("sets", "rep_min", "rep_max")


@dataclass(frozen=True)
class WorkoutMatch:
    """Routine-match evidence for one completed workout (#972)."""

    workout_id: str
    confidence: str = engine.MATCH_UNMATCHED
    routine_position: int | None = None
    score: float = 0.0
    method: str = ""
    user_confirmed: bool = False
    matcher_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "routine_position": self.routine_position,
            "score": self.score,
            "method": self.method,
            "user_confirmed": self.user_confirmed,
            "matcher_version": self.matcher_version,
        }


@dataclass(frozen=True)
class WorkoutEvidence:
    """A completed workout reduced to the fields adaptation reads."""

    workout_id: str
    performed_at: str
    title: str
    exercises: tuple[tuple[str, str, tuple[dict[str, Any], ...]], ...]

    @property
    def content_hash(self) -> str:
        payload = [
            self.performed_at,
            [
                [template, [_set_signature(item) for item in sets]]
                for template, _title, sets in self.exercises
            ],
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _set_signature(raw: Mapping[str, Any]) -> list[Any]:
    return [
        raw.get("type") or raw.get("set_type") or "normal",
        raw.get("reps"),
        raw.get("weight_kg"),
        raw.get("duration_seconds"),
        raw.get("distance_meters"),
        raw.get("rpe"),
    ]


def workout_evidence(raw: Any) -> WorkoutEvidence | None:
    """Normalise a ``CompletedWorkout`` or a raw Hevy workout payload."""

    if isinstance(raw, Mapping):
        identifier = str(raw.get("id") or "")
        stamp = raw.get("end_time") or raw.get("start_time")
        title = str(raw.get("title") or "")
        exercises = tuple(
            (
                str(item.get("exercise_template_id") or item.get("template_id") or ""),
                str(item.get("title") or ""),
                tuple(dict(s) for s in item.get("sets") or [] if isinstance(s, Mapping)),
            )
            for item in raw.get("exercises") or []
            if isinstance(item, Mapping)
        )
    else:
        identifier = str(getattr(raw, "id", "") or "")
        stamp = getattr(raw, "end_time", None) or getattr(raw, "start_time", None)
        title = str(getattr(raw, "title", "") or "")
        exercises = tuple(
            (
                str(getattr(item, "template_id", "") or ""),
                str(getattr(item, "title", "") or ""),
                tuple(dict(s) for s in getattr(item, "sets", None) or [] if isinstance(s, Mapping)),
            )
            for item in getattr(raw, "exercises", None) or []
        )
    parsed = parse_timestamp(stamp)
    if not identifier or parsed is None:
        return None
    return WorkoutEvidence(identifier, parsed.isoformat(), title, exercises)


def _observation_sets(sets: Sequence[Mapping[str, Any]]) -> tuple[engine.SetObservation, ...]:
    def number(raw: Mapping[str, Any], key: str) -> float | None:
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    result: list[engine.SetObservation] = []
    for raw in sets:
        kind = str(raw.get("type") or raw.get("set_type") or "normal").strip().lower()
        if "warm" in kind:
            kind = "warmup"
        elif "drop" in kind:
            kind = "dropset"
        elif "fail" in kind or "amrap" in kind:
            kind = "failure"
        else:
            kind = "normal"
        result.append(
            engine.SetObservation(
                kind=kind,
                reps=number(raw, "reps"),
                weight_kg=number(raw, "weight_kg"),
                duration_seconds=number(raw, "duration_seconds"),
                distance_meters=number(raw, "distance_meters"),
                rpe=number(raw, "rpe"),
            )
        )
    return tuple(result)


@dataclass
class ReviewResult:
    """Outcome of one adaptation review."""

    evaluated: int = 0
    replayed: int = 0
    superseded: int = 0
    decisions: list[AdaptationDecision] = field(default_factory=list)
    programme_decision: AdaptationDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated_workouts": self.evaluated,
            "replayed_workouts": self.replayed,
            "superseded_decisions": self.superseded,
            "decisions": [decision_view(item) for item in self.decisions],
            "programme_decision": (
                decision_view(self.programme_decision) if self.programme_decision else None
            ),
        }


def decision_view(decision: AdaptationDecision) -> dict[str, Any]:
    """The complete, LLM-free explanation of one stored decision."""

    output = decision.output or {}
    codes = list(output.get("rationale_codes") or [])
    return {
        "id": decision.id,
        "programme_id": decision.programme_id,
        "decision_type": decision.decision_type,
        "application_state": decision.application_state,
        "week_number": decision.week_number,
        "exercise_template_id": decision.exercise_template_id,
        "scope": decision.scope,
        "inputs": decision.inputs,
        "output": output,
        "rationale_codes": codes,
        "explanation": engine.explain(decision.decision_type, codes)
        if decision.decision_type in engine.DECISION_TYPES
        else decision.explanation,
        "confidence": decision.confidence,
        "rule_version": decision.rule_version,
        "from_revision_id": decision.from_revision_id,
        "to_revision_id": decision.to_revision_id,
        "created_revision": decision.created_revision,
        "source_workout_id": decision.source_workout_id,
        "idempotency_key": decision.idempotency_key,
        "created_at": decision.created_at,
        "resolved_at": decision.resolved_at,
    }


# ---------------------------------------------------------------------------
# Definition helpers
# ---------------------------------------------------------------------------


def _block_number(definition: Mapping[str, Any], week: int) -> int | None:
    for block in definition.get("blocks") or []:
        if int(block.get("start_week") or 0) <= week <= int(block.get("end_week") or 0):
            return int(block.get("number") or 0) or None
    return None


def _find_exercise(
    definition: Mapping[str, Any],
    template_id: str,
    routine_position: int | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for day in definition.get("days") or []:
        if routine_position is not None and int(day.get("number") or 0) != routine_position:
            continue
        for exercise in day.get("exercises") or []:
            if str(exercise.get("template_id")) == template_id:
                return day, exercise
    return None


def _prescription_entry(
    exercise: Mapping[str, Any], block: int | None, week: int
) -> dict[str, Any] | None:
    entries = [item for item in exercise.get("prescriptions") or [] if isinstance(item, Mapping)]
    for item in entries:
        if item.get("block_number") == block and item.get("week_number") == week:
            return dict(item)
    for item in entries:
        if item.get("block_number") == block and item.get("week_number") in (None, 0):
            return dict(item)
    return dict(entries[0]) if entries else None


_MODALITY = {"load": "load", "bodyweight": "bodyweight", "duration": "duration", "distance": "distance"}


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed and parsed > 0 else None


def build_prescription(
    exercise: Mapping[str, Any],
    entry: Mapping[str, Any],
    policy: engine.AdaptationPolicy,
) -> engine.Prescription:
    """Translate a stored exercise prescription into the engine's contract."""

    progression = entry.get("progression") or {}
    allowed = entry.get("allowed_change") or {}
    set_bounds = entry.get("allowed_set_change_from_source") or {}
    rep = entry.get("rep_target") or {}
    effort = entry.get("effort_target") or {}
    anchor = entry.get("load_anchor") or {}
    exercise_type = str(exercise.get("exercise_type") or "").lower()
    modality = _MODALITY.get(str(progression.get("modality") or "load"), "load")
    if modality == "bodyweight" and "assisted" in exercise_type:
        modality = "assisted"
    source_sets = int(entry.get("source_sets") or exercise.get("source_sets") or entry.get("sets") or 1)
    minimum = set_bounds.get("minimum")
    maximum = set_bounds.get("maximum")
    rep_max = rep.get("max")
    return engine.Prescription(
        template_id=str(exercise.get("template_id")),
        title=str(exercise.get("name") or exercise.get("template_id")),
        role=str(exercise.get("role") or ""),
        modality=modality,
        method=str(progression.get("method") or "double_progression"),
        sets=int(entry.get("sets") or 1),
        source_sets=source_sets,
        rep_min=rep.get("min"),
        rep_max=rep_max,
        target_rir_min=effort.get("min"),
        load_kg=(float(anchor["value"]) if anchor.get("value") is not None else None),
        increment_kg=_positive_float(entry.get("equipment_increment_kg")),
        max_load_percent=float(allowed.get("max_load_percent") or progression.get("max_load_percent") or policy.default_max_load_percent),
        min_successful_exposures=int(progression.get("minimum_successful_exposures") or policy.min_successful_exposures),
        # Automatic set reductions never go below what the block prescribes or
        # the bound relative to the provider's sets, whichever is lower.
        set_floor=max(
            1,
            min(
                int(entry.get("sets") or 1),
                source_sets + (int(minimum) if minimum is not None else -1),
            ),
        ),
        set_ceiling=min(12, source_sets + (int(maximum) if maximum is not None else 2)),
        rep_ceiling=(int(rep_max) + 2) if rep_max is not None else None,
        progress_order=tuple(
            str(item)
            for item in (progression.get("type_specific") or {}).get("progress_order", [])
        ),
    )


def _evidence_signature(workout: WorkoutEvidence, match: WorkoutMatch) -> str:
    return hashlib.sha256(
        f"{workout.content_hash}:{match.confidence}:{int(match.user_confirmed)}:"
        f"{match.routine_position}".encode()
    ).hexdigest()


def _idempotency_key(
    user_id: str,
    programme_id: int,
    workout_id: str,
    signature: str,
    template_id: str,
    routine_position: int | None,
) -> str:
    return hashlib.sha256(
        f"{user_id}|{programme_id}|{workout_id}|{signature}|{template_id}|"
        f"{routine_position}|{engine.RULE_VERSION}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class AdaptationService:
    """Tenant-scoped adaptation, confirmation, revert and override operations."""

    def __init__(
        self,
        db_path: str,
        repository: ProgrammeRepository | None = None,
        policy: engine.AdaptationPolicy = engine.DEFAULT_POLICY,
    ) -> None:
        self.repository = repository or ProgrammeRepository(db_path)
        self.db_path = db_path
        self.policy = policy

    # ---- context ---------------------------------------------------------

    def _active_programme(self, conn: Any, user_id: str, programme_id: int) -> Programme:
        programme = self.repository.programme_in(conn, user_id, programme_id)
        if programme.status != "active":
            raise ProgrammeValidationError(
                "Only the active programme can be adapted; resume or activate it first."
            )
        if programme.current_revision is None or programme.start_date is None:
            raise ProgrammeValidationError("The programme has no materialised revision.")
        return programme

    def _timezone(self, conn: Any, user_id: str) -> str:
        row = conn.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()
        return str((row["timezone"] if row else None) or "UTC")

    def applied_for(
        self,
        decisions: Sequence[AdaptationDecision],
        revision_id: int,
        template_id: str,
        routine_position: int | None,
    ) -> list[dict[str, Any]]:
        """Applied week-level decisions that still shape one exercise.

        Week-level adjustments carry across revisions: an unrelated structural
        change must not erase an exercise's progression.  A structural decision
        for the *same* exercise (set reduction, rep-range change) rebases it, so
        only adjustments recorded after the latest such decision still apply.
        """

        del revision_id  # adjustments follow the exercise, not a single revision
        relevant = [
            item
            for item in decisions
            if item.exercise_template_id == template_id
            and item.application_state in APPLIED_STATES
            and item.scope.get("routine_position") == routine_position
        ]
        last_structural = max(
            (
                item.id
                for item in relevant
                if item.created_revision and item.decision_type in engine.STRUCTURAL_TYPES
            ),
            default=0,
        )
        selected: list[dict[str, Any]] = []
        for item in relevant:
            if item.created_revision or item.id < last_structural:
                continue
            selected.append(
                {
                    "decision_type": item.decision_type,
                    "proposed": (item.output or {}).get("accepted_prescription")
                    or (item.output or {}).get("proposed_prescription")
                    or {},
                }
            )
        return selected

    def effective_prescription(
        self,
        definition: Mapping[str, Any],
        decisions: Sequence[AdaptationDecision],
        revision: ProgrammeRevision,
        *,
        template_id: str,
        routine_position: int | None,
        week: int,
    ) -> tuple[engine.Prescription, dict[str, Any]] | None:
        """The prescription in force for one exercise: revision ⊕ applied decisions."""

        found = _find_exercise(definition, template_id, routine_position)
        if found is None:
            return None
        _day, exercise = found
        entry = _prescription_entry(exercise, _block_number(definition, week), week)
        if entry is None:
            return None
        base = build_prescription(exercise, entry, self.policy)
        folded = engine.fold_effective(
            base.to_dict(),
            self.applied_for(decisions, revision.id, template_id, routine_position),
        )
        updates = {
            key: folded[key]
            for key in ("sets", "rep_min", "rep_max", "rep_target", "load_kg", "duration_seconds", "distance_meters")
            if key in folded
        }
        return replace(base, **updates), folded

    def overrides_for(
        self, overrides: Sequence[ProgrammeOverride], template_id: str, day: date
    ) -> list[ProgrammeOverride]:
        return [
            item
            for item in overrides
            if item.is_active_on(day)
            and item.exercise_template_id in ("", template_id)
        ]

    def constraints_for(
        self,
        overrides: Sequence[ProgrammeOverride],
        template_id: str,
        title: str,
        profile_rules: Any,
        training_rules: Any,
        day: date,
    ) -> engine.Constraints:
        pinned: set[str] = set()
        pain = technique = blocked = False
        cap: int | None = None
        for item in self.overrides_for(overrides, template_id, day):
            if item.kind == "pin":
                pinned.update(
                    str(field_name)
                    for field_name in item.value.get("fields", [])
                    if field_name in PIN_FIELDS
                )
            elif item.kind == "pain_report" and item.exercise_template_id:
                pain = True
            elif item.kind == "technique_report" and item.exercise_template_id:
                technique = True
            elif item.kind == "equipment_issue" and item.exercise_template_id:
                blocked = True
            elif item.kind == "time_limit":
                limit = item.value.get("max_sets")
                if isinstance(limit, int) and limit >= 1:
                    cap = limit if cap is None else min(cap, limit)
        substitutes: tuple[str, ...] = ()
        if blocked:
            suggested = tuple(
                str(name)
                for item in self.overrides_for(overrides, template_id, day)
                if item.kind == "equipment_issue"
                for name in item.value.get("substitutes", [])
            )
            from programme_validator import (
                _exercise_matches_exclusion,
                parse_profile_rules,
            )

            parsed = parse_profile_rules(profile_rules)
            if training_rules is not None:
                from programme_validator import _merge_profile_constraints

                parsed = _merge_profile_constraints(parsed, parse_profile_rules(training_rules))
            substitutes = suggested + tuple(
                rule.replacement
                for rule in parsed.substitution_rules
                if rule.replacement and _exercise_matches_exclusion(title, rule.pattern)
            )
        return engine.Constraints(
            blocked=blocked,
            substitution_candidates=substitutes,
            pain_flag=pain,
            technique_flag=technique,
            pinned_fields=frozenset(pinned),
            max_sets=cap,
        )

    def history_for(
        self,
        user_id: str,
        decisions: Sequence[AdaptationDecision],
        template_id: str,
        routine_position: int | None,
        week: int,
        *,
        exclude_workout: str,
    ) -> engine.ExerciseHistory:
        prior: list[engine.PriorExposure] = []
        week_load = 0.0
        week_sets = 0
        fatigue_active = False
        for item in decisions:
            if (
                item.exercise_template_id != template_id
                or item.scope.get("routine_position") != routine_position
                or item.application_state == STATE_SUPERSEDED
                or not item.source_workout_id
                or item.source_workout_id == exclude_workout
            ):
                continue
            output = item.output or {}
            assessment = (item.inputs or {}).get("assessment") or {}
            match = (item.inputs or {}).get("match") or {}
            proposed = output.get("accepted_prescription") or output.get("proposed_prescription") or {}
            applied = item.application_state in APPLIED_STATES
            load_change = float(proposed.get("load_change_percent") or 0.0) if applied else 0.0
            previous = output.get("previous_prescription") or {}
            set_change = (
                int(proposed.get("sets", previous.get("sets", 0)) or 0) - int(previous.get("sets", 0) or 0)
                if applied and "sets" in proposed
                else 0
            )
            prior.append(
                engine.PriorExposure(
                    workout_id=item.source_workout_id,
                    performed_at=str((item.inputs or {}).get("observation", {}).get("performed_at", "")),
                    signal=str(assessment.get("signal") or engine.SIGNAL_UNKNOWN),
                    result=str(assessment.get("result") or engine.RESULT_NO_DATA),
                    effort=str(assessment.get("effort") or "unknown"),
                    match_confidence=(
                        engine.MATCH_HIGH
                        if match.get("user_confirmed")
                        else str(match.get("confidence") or engine.MATCH_UNMATCHED)
                    ),
                    decision_type=item.decision_type,
                    applied=applied,
                    week_number=item.week_number,
                    load_change_percent=load_change,
                    set_change=set_change,
                    rejected=item.application_state in {STATE_REJECTED, STATE_REVERTED},
                )
            )
            if applied and item.week_number == week:
                week_load += abs(load_change)
                week_sets += abs(set_change)
            if item.decision_type == engine.EXERCISE_FATIGUE and applied:
                fatigue_active = proposed.get("phase") != "exit"
        prior.sort(key=lambda entry: (entry.performed_at, entry.workout_id))
        baseline = self._baseline(user_id, template_id)
        return engine.ExerciseHistory(
            prior=tuple(prior),
            baseline_exposures=int(baseline.get("exposure_count") or 0),
            baseline_confidence=baseline.get("confidence"),
            week_load_change_percent=week_load,
            week_set_change=week_sets,
            fatigue_active=fatigue_active,
        )

    def _baseline(self, user_id: str, template_id: str) -> dict[str, Any]:
        from database import get_exercise_baselines

        try:
            rows = get_exercise_baselines(user_id, self.db_path, template_id=template_id)
        except Exception:  # noqa: BLE001 - baselines are optional evidence
            return {}
        return rows[-1] if rows else {}

    # ---- review ----------------------------------------------------------

    def review(
        self,
        user_id: str,
        programme_id: int,
        workouts: Sequence[Any],
        matches: Mapping[str, WorkoutMatch] | None = None,
        *,
        now: datetime,
        recovery: engine.RecoveryEvidence | None = None,
        known_workout_ids: Sequence[str] | None = None,
    ) -> ReviewResult:
        """Evaluate completed workouts and apply the bounded, audited result.

        Replaying identical workouts changes nothing.  An edited workout
        supersedes its earlier decisions; when ``known_workout_ids`` is the
        complete set of workouts the provider still reports, workouts missing
        from it are retracted as deleted.
        """

        result = ReviewResult()
        evidence = sorted(
            (item for raw in workouts if (item := workout_evidence(raw)) is not None),
            key=lambda item: (item.performed_at, item.workout_id),
        )
        match_index = dict(matches or {})
        if known_workout_ids is not None:
            result.superseded += self.retract_missing_workouts(
                user_id, programme_id, known_workout_ids, now=now
            )
        for workout in evidence:
            self._process_workout(
                user_id,
                programme_id,
                workout,
                match_index.get(workout.workout_id)
                or WorkoutMatch(workout.workout_id),
                now=now,
                recovery=recovery,
                result=result,
            )
        result.programme_decision = self._programme_level(
            user_id, programme_id, now=now, recovery=recovery
        )
        return result

    def _process_workout(
        self,
        user_id: str,
        programme_id: int,
        workout: WorkoutEvidence,
        match: WorkoutMatch,
        *,
        now: datetime,
        recovery: engine.RecoveryEvidence | None,
        result: ReviewResult,
    ) -> None:
        signature = _evidence_signature(workout, match)
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self._active_programme(conn, user_id, programme_id)
            timezone_name = self._timezone(conn, user_id)
            start = programme.start_date
            local = local_date_of(workout.performed_at, timezone_name)
            if start is None or local is None:
                return
            week = week_for_date(start, int(programme.duration_weeks or 0), local)
            if week is None:
                return  # before the programme started or after it ended
            existing = self.repository.list_decisions_in(
                conn, user_id, programme_id, source_workout_id=workout.workout_id
            )
            live = [item for item in existing if item.application_state != STATE_SUPERSEDED]
            stale = [item for item in live if item.source_workout_hash != signature]
            if stale:
                self._supersede(conn, user_id, programme_id, stale)
                result.superseded += len(stale)
            decisions_all = self.repository.list_decisions_in(conn, user_id, programme_id)
            profile_rules, training_rules = _rules(conn, user_id)
            overrides = self.repository.list_overrides_in(
                conn, user_id, programme_id, active_only=True
            )
            first_revision = programme.current_revision
            if first_revision is None:  # pragma: no cover - guarded by _active_programme
                return
            definition = definition_from_revision(
                programme, first_revision, timezone_name=timezone_name
            )
            routine_position = match.routine_position if match.confidence != engine.MATCH_UNMATCHED else None
            created = 0
            reused = 0
            # An unmatched workout is evidence of nothing programme-specific: it is
            # tracked in the week state and can be assigned to a routine by the
            # user, but it never produces per-exercise decisions.
            candidates = (
                ()
                if match.confidence == engine.MATCH_UNMATCHED
                else workout.exercises
            )
            for template_id, _title, sets in candidates:
                found = _find_exercise(definition, template_id, routine_position)
                if found is None:
                    continue
                key = _idempotency_key(
                    user_id, programme_id, workout.workout_id, signature, template_id, routine_position
                )
                if any(item.idempotency_key == key for item in decisions_all):
                    reused += 1
                    continue
                programme = self.repository.programme_in(conn, user_id, programme_id)
                revision = programme.current_revision
                if revision is None:  # pragma: no cover - guarded above
                    return
                definition = definition_from_revision(
                    programme, revision, timezone_name=timezone_name
                )
                decisions_all = self.repository.list_decisions_in(conn, user_id, programme_id)
                effective = self.effective_prescription(
                    definition,
                    decisions_all,
                    revision,
                    template_id=template_id,
                    routine_position=routine_position,
                    week=week,
                )
                if effective is None:
                    continue
                prescription, folded = effective
                observation = engine.Observation(
                    workout_id=workout.workout_id,
                    workout_hash=signature,
                    performed_at=workout.performed_at,
                    template_id=template_id,
                    sets=_observation_sets(sets),
                    match_confidence=match.confidence,
                    match_user_confirmed=match.user_confirmed,
                    routine_position=routine_position,
                )
                history = self.history_for(
                    user_id,
                    decisions_all,
                    template_id,
                    routine_position,
                    week,
                    exclude_workout=workout.workout_id,
                )
                constraints = self.constraints_for(
                    overrides, template_id, prescription.title, profile_rules, training_rules, local
                )
                outcome = engine.decide(
                    prescription, observation, history, constraints, recovery, self.policy
                )
                outcome = self._validated(
                    conn, user_id, outcome, definition, routine_position, template_id,
                    week, profile_rules, training_rules,
                )
                decision = self._record(
                    conn,
                    user_id,
                    programme,
                    revision,
                    definition,
                    outcome,
                    observation=observation,
                    match=match,
                    week=week,
                    key=key,
                    constraints=constraints,
                    history=history,
                    recovery=recovery,
                    folded=folded,
                    timezone_name=timezone_name,
                )
                result.decisions.append(decision)
                created += 1
            if created:
                result.evaluated += 1
            elif reused:
                result.replayed += 1
                return  # an identical event: nothing to write
            programme = self.repository.programme_in(conn, user_id, programme_id)
            self._record_exposure(
                conn,
                user_id,
                programme,
                workout,
                match,
                signature,
                routine_position,
                week,
                local,
                timezone_name,
                today=local_date_of(now, timezone_name) or local,
            )

    # ---- applying outcomes ----------------------------------------------

    def _validated(
        self,
        conn: Any,
        user_id: str,
        outcome: engine.AdaptationOutcome,
        definition: Mapping[str, Any],
        routine_position: int | None,
        template_id: str,
        week: int,
        profile_rules: Any,
        training_rules: Any,
    ) -> engine.AdaptationOutcome:
        """The shared structured validator is the last gate for any change."""

        # Only changes that rewrite block prescriptions can newly violate a rule;
        # week-level load and rep-target steps stay inside the validated range.
        if not outcome.structural or outcome.decision_type not in {
            engine.REDUCE_SETS,
            engine.ALTER_REP_RANGE,
        }:
            return outcome
        from programme_validator import validate_programme

        candidate = copy.deepcopy(dict(definition))
        _apply_structural(candidate, outcome, routine_position, template_id, week)
        result = validate_programme(
            candidate, profile_rules=profile_rules, training_rules=training_rules
        )
        if not result.blocking_violations:
            return outcome
        return engine.AdaptationOutcome(
            decision_type=engine.BLOCK_PROGRESSION,
            rationale_codes=("proposal_violates_training_rules",),
            previous=outcome.previous,
            proposed={},
            requires_confirmation=False,
            structural=False,
            confidence=outcome.confidence,
            assessment=outcome.assessment,
            bounds={**outcome.bounds, "violations": list(result.blocking_violations)[:3]},
        )

    def _record(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        revision: ProgrammeRevision,
        definition: dict[str, Any],
        outcome: engine.AdaptationOutcome,
        *,
        observation: engine.Observation,
        match: WorkoutMatch,
        week: int,
        key: str,
        constraints: engine.Constraints,
        history: engine.ExerciseHistory,
        recovery: engine.RecoveryEvidence | None,
        folded: Mapping[str, Any],
        timezone_name: str,
    ) -> AdaptationDecision:
        if outcome.requires_confirmation:
            state = STATE_PENDING
        elif outcome.decision_type == engine.HOLD:
            state = STATE_INFORMATIONAL
        else:
            state = STATE_AUTO_APPLIED
        scope = {
            "level": "exercise",
            "user_id": user_id,
            "programme_id": programme.id,
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "week_number": week,
            "routine_position": observation.routine_position,
            "exercise_template_id": observation.template_id,
        }
        inputs = {
            "observation": {
                "type": "hevy_workout",
                "workout_id": observation.workout_id,
                "evidence_hash": observation.workout_hash,
                "performed_at": observation.performed_at,
                "sets": [item.to_dict() for item in observation.sets],
            },
            "match": match.to_dict(),
            "baseline": {
                "exposures": history.baseline_exposures,
                "confidence": history.baseline_confidence,
            },
            "assessment": outcome.assessment,
            "prior_exposures": [
                {"workout_id": item.workout_id, "signal": item.signal, "decision_type": item.decision_type, "applied": item.applied}
                for item in history.prior[-6:]
            ],
            "constraints": constraints.to_dict(),
            "recovery": recovery.to_dict(self.policy) if recovery else None,
            "policy": self.policy.to_dict(),
            "effective_before": dict(folded),
        }
        output = outcome.to_dict()
        output["assessment"] = outcome.assessment
        common: dict[str, Any] = {
            "scope": scope,
            "inputs": inputs,
            "output": output,
            "confidence": outcome.confidence,
            "rule_version": engine.RULE_VERSION,
            "explanation": outcome.explanation,
            "confirmed": False,
            "decision_type": outcome.decision_type,
            "application_state": state,
            "idempotency_key": key,
            "week_number": week,
            "exercise_template_id": observation.template_id,
            "source_workout_id": observation.workout_id,
            "source_workout_hash": observation.workout_hash,
        }
        if state == STATE_AUTO_APPLIED and outcome.structural and outcome.decision_type != engine.PROPOSE_SUBSTITUTION:
            candidate = copy.deepcopy(definition)
            _apply_structural(candidate, outcome, observation.routine_position, observation.template_id, week)
            draft = definition_to_draft(
                candidate,
                user_id,
                reason="adaptation",
                audit={"decision_type": outcome.decision_type, "rule_version": engine.RULE_VERSION},
            )
            new_revision, decision_id = self.repository.create_revision_in(
                conn,
                user_id,
                programme.id,
                draft,
                reason="adaptation",
                adaptation=AdaptationDecisionInput(**common),
            )
            self._carry_week_state(conn, user_id, programme.id, revision, new_revision, week)
            stored = self.repository.get_decision_in(conn, user_id, programme.id, int(decision_id or 0))
            if stored is None:  # pragma: no cover - defensive
                raise RuntimeError("Failed to read back adaptation decision.")
            return stored
        stored, _created = self.repository.record_decision_in(
            conn,
            user_id,
            programme.id,
            revision.id,
            revision.id,
            AdaptationDecisionInput(**common),
        )
        return stored

    def _carry_week_state(
        self,
        conn: Any,
        user_id: str,
        programme_id: int,
        old: ProgrammeRevision,
        new: ProgrammeRevision,
        week: int,
    ) -> None:
        state = self.repository.get_week_state_in(conn, user_id, programme_id, old.id, week)
        if state is not None:
            self.repository.save_week_state_in(conn, user_id, programme_id, new.id, state)

    # ---- exposure/week state ---------------------------------------------

    def exposures_for_week(
        self, conn: Any, user_id: str, programme_id: int, week: int
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT actual_json FROM programme_week_states WHERE user_id = ? "
            "AND programme_id = ? AND week_number = ? ORDER BY revision_id ASC",
            (user_id, programme_id, week),
        ).fetchall()
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                actual = json.loads(row["actual_json"] or "{}")
            except (TypeError, ValueError):
                continue
            for item in actual.get("exposures") or []:
                if isinstance(item, dict) and item.get("workout_id"):
                    merged[str(item["workout_id"])] = item
        return sorted(merged.values(), key=lambda item: (str(item.get("performed_at")), str(item.get("workout_id"))))

    def _record_exposure(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        workout: WorkoutEvidence,
        match: WorkoutMatch,
        signature: str,
        routine_position: int | None,
        week: int,
        local: date,
        timezone_name: str,
        *,
        today: date,
    ) -> None:
        revision = programme.current_revision
        if revision is None:  # pragma: no cover - guarded by the caller
            return
        definition = definition_from_revision(programme, revision, timezone_name=timezone_name)
        confident = match.confidence in {engine.MATCH_HIGH, engine.MATCH_PROBABLE}
        record: dict[str, Any] = {
            "workout_id": workout.workout_id,
            "evidence_hash": signature,
            "performed_at": workout.performed_at,
            "local_date": local.isoformat(),
            # Only a confident match may fill a slot or move the rotation; an
            # ambiguous workout keeps its candidate separately until confirmed.
            "routine_position": routine_position if confident else None,
            "candidate_routine_position": None if confident else routine_position,
            "match_confidence": match.confidence,
            "status": "unmatched",
        }
        if routine_position is not None and confident:
            day = next(
                (item for item in definition.get("days") or [] if int(item.get("number") or 0) == routine_position),
                None,
            )
            if day is not None:
                planned = [
                    (str(ex.get("template_id")), int(ex.get("sets") or 1))
                    for ex in day.get("exercises") or []
                ]
                performed = {
                    template: sum(
                        1
                        for item in sets
                        if str(item.get("type") or "normal").lower() not in {"warmup", "warm_up", "warm-up"}
                    )
                    for template, _title, sets in workout.exercises
                }
                overrides = self.repository.list_overrides_in(conn, user_id, programme.id, active_only=True)
                substitutions = {
                    item.exercise_template_id: str(item.value.get("performed_template_id") or "")
                    for item in overrides
                    if item.kind == "substitution" and item.is_active_on(local)
                }
                record.update(
                    classify_session(planned, performed, substitutions),
                    source_routine_id=str(day.get("routine_id") or ""),
                )
        elif match.confidence == engine.MATCH_AMBIGUOUS:
            record["status"] = "ambiguous"
        self._save_exposures(
            conn, user_id, programme, definition, week, timezone_name, today,
            replace=record, review=True,
        )

    def _save_exposures(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        definition: Mapping[str, Any],
        week: int,
        timezone_name: str,
        today: date,
        *,
        replace: dict[str, Any] | None = None,
        drop: Sequence[str] = (),
        review: bool = False,
    ) -> None:
        revision = programme.current_revision
        if revision is None or programme.start_date is None:  # pragma: no cover
            return
        exposures = [
            item
            for item in self.exposures_for_week(conn, user_id, programme.id, week)
            if item.get("workout_id") not in set(drop)
            and (replace is None or item.get("workout_id") != replace.get("workout_id"))
        ]
        if replace is not None:
            exposures.append(replace)
        exposures.sort(key=lambda item: (str(item.get("performed_at")), str(item.get("workout_id"))))
        completed_before: list[int | None] = []
        for earlier in range(1, week):
            completed_before.extend(
                item.get("routine_position")
                for item in self.exposures_for_week(conn, user_id, programme.id, earlier)
                if item.get("status") in {"completed", "partial", "substituted"}
            )
        pointer = rolling_pointer(definition.get("days") or [], completed_before)
        previous = self.repository.get_week_state_in(
            conn, user_id, programme.id, revision.id, week
        ) or self.repository.get_latest_week_state_in(conn, user_id, programme.id, week)
        week_decisions = [
            item
            for item in self.repository.list_decisions_in(conn, user_id, programme.id)
            if item.week_number == week
            and item.source_workout_id
            and item.application_state != STATE_SUPERSEDED
        ]
        latest: dict[tuple[Any, str], AdaptationDecision] = {}
        for item in week_decisions:
            latest[(item.scope.get("routine_position"), item.exercise_template_id)] = item
        troubled = sum(
            1
            for item in latest.values()
            if (item.inputs or {}).get("assessment", {}).get("signal") == engine.SIGNAL_FAILURE
        )
        review_state = previous.review_state if previous else "planned"
        if review:
            review_state = (
                "needs_confirmation"
                if any(item.application_state == STATE_PENDING for item in week_decisions)
                else "reviewed"
            )
        state = build_week_state(
            definition,
            start_date=programme.start_date,
            duration_weeks=int(programme.duration_weeks or 0),
            week_number=week,
            timezone_name=timezone_name,
            today=today,
            rotation_pointer=pointer,
            exposures=exposures,
            review_state=review_state,
            fatigue=round(troubled / len(latest), 3) if latest else None,
        )
        carried_planned = dict(previous.planned) if previous else {}
        carried_actual = dict(previous.actual) if previous else {}
        if carried_planned.get("daily"):
            state.planned["daily"] = carried_planned["daily"]
        if carried_actual.get("daily_summaries"):
            state.actual["daily_summaries"] = carried_actual["daily_summaries"]
        self.repository.save_week_state_in(conn, user_id, programme.id, revision.id, state)

    # ---- retraction (update/delete events) --------------------------------

    def _supersede(
        self,
        conn: Any,
        user_id: str,
        programme_id: int,
        decisions: Sequence[AdaptationDecision],
    ) -> None:
        for item in decisions:
            was_applied = item.application_state in APPLIED_STATES
            output = dict(item.output or {})
            output["superseded"] = True
            self.repository.set_decision_state_in(
                conn, user_id, programme_id, item.id, STATE_SUPERSEDED, output=output
            )
            if was_applied and item.created_revision:
                # An immutable revision cannot be withdrawn silently; ask the user.
                self.repository.record_decision_in(
                    conn,
                    user_id,
                    programme_id,
                    item.to_revision_id,
                    item.to_revision_id,
                    AdaptationDecisionInput(
                        scope={**item.scope, "level": "revision_review"},
                        inputs={"superseded_decision_id": item.id},
                        output={
                            "decision_type": engine.REQUEST_CONFIRMATION,
                            "rationale_codes": ["evidence_retracted_review_revision"],
                            "proposed_prescription": {"revert_decision_id": item.id},
                            "previous_prescription": {},
                            "requires_confirmation": True,
                            "structural": False,
                        },
                        confidence=0.5,
                        rule_version=engine.RULE_VERSION,
                        explanation=engine.explain(
                            engine.REQUEST_CONFIRMATION, ["evidence_retracted_review_revision"]
                        ),
                        decision_type=engine.REQUEST_CONFIRMATION,
                        application_state=STATE_PENDING,
                        idempotency_key=f"retract-review:{item.id}",
                        week_number=item.week_number,
                        exercise_template_id=item.exercise_template_id,
                    ),
                )

    def retract_workouts(
        self,
        user_id: str,
        programme_id: int,
        workout_ids: Sequence[str],
        *,
        now: datetime,
    ) -> int:
        """Withdraw the evidence of deleted workouts (a Hevy delete event)."""

        wanted = {str(item) for item in workout_ids}
        total = 0
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            timezone_name = self._timezone(conn, user_id)
            for workout_id in sorted(wanted):
                stale = [
                    item
                    for item in self.repository.list_decisions_in(
                        conn, user_id, programme_id, source_workout_id=workout_id
                    )
                    if item.application_state != STATE_SUPERSEDED
                ]
                if stale:
                    self._supersede(conn, user_id, programme_id, stale)
                    total += len(stale)
            if programme.current_revision is not None and programme.start_date is not None:
                definition = definition_from_revision(
                    programme, programme.current_revision, timezone_name=timezone_name
                )
                today = local_date_of(now, timezone_name) or now.date()
                weeks = {
                    int(row["week_number"])
                    for row in conn.execute(
                        "SELECT week_number FROM programme_week_states "
                        "WHERE user_id = ? AND programme_id = ?",
                        (user_id, programme_id),
                    ).fetchall()
                }
                for week in sorted(weeks):
                    if any(
                        item.get("workout_id") in wanted
                        for item in self.exposures_for_week(conn, user_id, programme_id, week)
                    ):
                        self._save_exposures(
                            conn, user_id, programme, definition, week,
                            timezone_name, today, drop=sorted(wanted),
                        )
        return total

    def retract_missing_workouts(
        self,
        user_id: str,
        programme_id: int,
        known_workout_ids: Sequence[str],
        *,
        now: datetime,
    ) -> int:
        """Retract ingested workouts the provider no longer reports."""

        known = {str(item) for item in known_workout_ids}
        with self.repository.transaction() as conn:
            ingested = {
                item.source_workout_id
                for item in self.repository.list_decisions_in(conn, user_id, programme_id)
                if item.source_workout_id and item.application_state != STATE_SUPERSEDED
            }
        missing = sorted(ingested - known)
        return self.retract_workouts(user_id, programme_id, missing, now=now) if missing else 0

    # ---- user control: confirm, reject, revert ---------------------------

    def _current_effective(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        decision: AdaptationDecision,
        timezone_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        revision = programme.current_revision
        if revision is None or programme.start_date is None:
            return None
        definition = definition_from_revision(programme, revision, timezone_name=timezone_name)
        decisions = self.repository.list_decisions_in(conn, user_id, programme.id)
        effective = self.effective_prescription(
            definition,
            decisions,
            revision,
            template_id=decision.exercise_template_id,
            routine_position=decision.scope.get("routine_position"),
            week=decision.week_number or 1,
        )
        if effective is None:
            return None
        return definition, effective[1]

    @staticmethod
    def _is_stale(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
        return any(
            key in previous and previous.get(key) != current.get(key)
            for key in ("sets", "rep_min", "rep_max", "load_kg", "duration_seconds", "distance_meters")
        )

    def _checked_adjustment(
        self, decision: AdaptationDecision, adjustment: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate a user-corrected value against the same per-exposure bounds."""

        output = decision.output or {}
        previous = output.get("previous_prescription") or {}
        bounds = output.get("bounds") or {}
        corrected = dict(output.get("proposed_prescription") or {})
        for key, value in adjustment.items():
            if key == "load_kg" and decision.decision_type in {engine.PROGRESS_LOAD, engine.REDUCE_LOAD}:
                base = float(
                    previous.get("load_kg")
                    or (decision.inputs or {}).get("assessment", {}).get("reference_load")
                    or 0.0
                )
                limit = float(bounds.get("max_load_percent") or self.policy.default_max_load_percent)
                if (
                    base <= 0
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or abs(value - base) > base * limit / 100.0 + 1e-9
                ):
                    raise ProgrammeValidationError(
                        f"The corrected load must stay within {limit:g}% of the current load."
                    )
                corrected["load_kg"] = round(float(value), 4)
                corrected["load_change_percent"] = round((float(value) - base) / base * 100.0, 3)
            elif key == "rep_target" and decision.decision_type == engine.PROGRESS_REPS:
                low, high = previous.get("rep_min"), previous.get("rep_max")
                if isinstance(value, bool) or not isinstance(value, int) or low is None or high is None or not low <= value <= high:
                    raise ProgrammeValidationError("The corrected rep target must stay inside the rep range.")
                corrected["rep_target"] = value
            else:
                raise ProgrammeValidationError(f"{key!r} cannot be corrected for this decision.")
        return corrected

    def confirm(
        self,
        user_id: str,
        programme_id: int,
        decision_id: int,
        *,
        adjustment: Mapping[str, Any] | None = None,
        now: datetime,
    ) -> AdaptationDecision:
        """Apply a pending decision, optionally with a bounded user correction."""

        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self._active_programme(conn, user_id, programme_id)
            decision = self.repository.get_decision_in(conn, user_id, programme_id, decision_id)
            if decision is None:
                raise ProgrammeNotFoundError("Decision not found for this user.")
            if decision.application_state != STATE_PENDING:
                raise ProgrammeValidationError("Only a pending decision can be confirmed.")
            timezone_name = self._timezone(conn, user_id)
            today = local_date_of(now, timezone_name) or now.date()
            output = dict(decision.output or {})
            codes = output.get("rationale_codes") or []
            if decision.decision_type == engine.REQUEST_CONFIRMATION:
                if "programme_fatigue_unconfirmed" in codes:
                    self.repository.create_override_in(
                        conn, user_id, programme_id, kind="fatigue_report",
                        exercise_template_id="", scope={"source_decision_id": decision.id},
                        value={}, starts_on=today.isoformat(),
                        ends_on=(today + timedelta(weeks=2)).isoformat(),
                        note="Confirmed fatigue from a programme review prompt.",
                    )
                    return self.repository.set_decision_state_in(
                        conn, user_id, programme_id, decision.id, STATE_CONFIRMED, confirmed=True
                    )
                if "evidence_retracted_review_revision" in codes:
                    target = (output.get("proposed_prescription") or {}).get("revert_decision_id")
                    self._revert_in(
                        conn, user_id, programme, int(target or 0), timezone_name,
                        allow_superseded=True,
                    )
                    return self.repository.set_decision_state_in(
                        conn, user_id, programme_id, decision.id, STATE_CONFIRMED, confirmed=True
                    )
                raise ProgrammeValidationError(
                    "Confirm the routine for this workout through the workout-match endpoint."
                )
            if decision.decision_type == engine.PROPOSE_SUBSTITUTION:
                performed = str((adjustment or {}).get("performed_template_id") or "").strip()
                if not performed:
                    raise ProgrammeValidationError(
                        "Name the exercise you performed instead (performed_template_id)."
                    )
                self.repository.create_override_in(
                    conn, user_id, programme_id, kind="substitution",
                    exercise_template_id=decision.exercise_template_id,
                    scope={"source_decision_id": decision.id},
                    value={"performed_template_id": performed},
                    starts_on=today.isoformat(),
                    ends_on=(today + timedelta(weeks=4)).isoformat(),
                    note="Substitution accepted from a proposal.",
                )
                return self.repository.set_decision_state_in(
                    conn, user_id, programme_id, decision.id, STATE_CONFIRMED, confirmed=True
                )
            programme_level = decision.decision_type == engine.PROGRAMME_FATIGUE
            current: tuple[dict[str, Any], dict[str, Any]] | None
            if programme_level:
                revision_now = programme.current_revision
                if revision_now is None:  # pragma: no cover - guarded by _active_programme
                    raise ProgrammeValidationError("The programme has no revision.")
                current = (
                    definition_from_revision(
                        programme, revision_now, timezone_name=timezone_name
                    ),
                    {},
                )
            else:
                current = self._current_effective(conn, user_id, programme, decision, timezone_name)
            if current is None or self._is_stale(output.get("previous_prescription") or {}, current[1]):
                raise ProgrammeValidationError(
                    "This decision is out of date; run a review to get a fresh one."
                )
            state = STATE_CONFIRMED
            if adjustment:
                output["accepted_prescription"] = self._checked_adjustment(decision, adjustment)
                state = STATE_OVERRIDDEN
            if output.get("structural"):
                revision = programme.current_revision
                if revision is None:  # pragma: no cover - guarded by _active_programme
                    raise ProgrammeValidationError("The programme has no revision.")
                definition, _folded = current
                candidate = copy.deepcopy(definition)
                outcome = _outcome_from_output(output)
                _apply_structural(
                    candidate, outcome, decision.scope.get("routine_position"),
                    decision.exercise_template_id, decision.week_number or 1,
                )
                from programme_validator import validate_programme

                profile_rules, training_rules = _rules(conn, user_id)
                checked = validate_programme(
                    candidate, profile_rules=profile_rules, training_rules=training_rules
                )
                if checked.blocking_violations:
                    raise ProgrammeValidationError(
                        "The change would break a saved training rule and cannot be applied."
                    )
                draft = definition_to_draft(
                    candidate, user_id, reason="adaptation",
                    audit={"decision_type": decision.decision_type, "confirmed_decision_id": decision.id},
                )
                new_revision, _ = self.repository.create_revision_in(
                    conn, user_id, programme_id, draft, reason="adaptation"
                )
                self._carry_week_state(conn, user_id, programme_id, revision, new_revision, decision.week_number or 1)
                return self.repository.set_decision_state_in(
                    conn, user_id, programme_id, decision.id, state,
                    output=output, confirmed=True, to_revision_id=new_revision.id,
                    from_revision_id=revision.id,
                )
            revision = programme.current_revision
            return self.repository.set_decision_state_in(
                conn, user_id, programme_id, decision.id, state, output=output, confirmed=True,
                from_revision_id=revision.id if revision else None,
                to_revision_id=revision.id if revision else None,
            )

    def override(
        self,
        user_id: str,
        programme_id: int,
        decision_id: int,
        adjustment: Mapping[str, Any],
    ) -> AdaptationDecision:
        """Replace an applied week-level value with a bounded user correction."""

        if not adjustment:
            raise ProgrammeValidationError("Provide the corrected value.")
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_programme(conn, user_id, programme_id)
            decision = self.repository.get_decision_in(conn, user_id, programme_id, decision_id)
            if decision is None:
                raise ProgrammeNotFoundError("Decision not found for this user.")
            if (
                decision.application_state not in APPLIED_STATES
                or decision.created_revision
                or decision.decision_type
                not in {engine.PROGRESS_LOAD, engine.REDUCE_LOAD, engine.PROGRESS_REPS}
            ):
                raise ProgrammeValidationError(
                    "Only an applied load or rep-target change can be overridden."
                )
            output = dict(decision.output or {})
            output["accepted_prescription"] = self._checked_adjustment(decision, adjustment)
            return self.repository.set_decision_state_in(
                conn, user_id, programme_id, decision.id, STATE_OVERRIDDEN,
                output=output, confirmed=True,
            )

    def reject(self, user_id: str, programme_id: int, decision_id: int) -> AdaptationDecision:
        """Decline a pending proposal; nothing is applied and the streak restarts."""

        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            decision = self.repository.get_decision_in(conn, user_id, programme_id, decision_id)
            if decision is None:
                raise ProgrammeNotFoundError("Decision not found for this user.")
            if decision.application_state != STATE_PENDING:
                raise ProgrammeValidationError("Only a pending decision can be rejected.")
            return self.repository.set_decision_state_in(
                conn, user_id, programme_id, decision.id, STATE_REJECTED
            )

    def revert(
        self, user_id: str, programme_id: int, decision_id: int
    ) -> AdaptationDecision:
        """Undo an applied decision without touching history."""

        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self._active_programme(conn, user_id, programme_id)
            return self._revert_in(
                conn, user_id, programme, decision_id, self._timezone(conn, user_id)
            )

    def _revert_in(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        decision_id: int,
        timezone_name: str,
        *,
        allow_superseded: bool = False,
    ) -> AdaptationDecision:
        decision = self.repository.get_decision_in(conn, user_id, programme.id, decision_id)
        if decision is None:
            raise ProgrammeNotFoundError("Decision not found for this user.")
        revertible = APPLIED_STATES | ({STATE_SUPERSEDED} if allow_superseded else set())
        if decision.application_state not in revertible or (
            decision.decision_type not in engine.CHANGING_TYPES
            and decision.decision_type != engine.PROGRAMME_FATIGUE
        ):
            raise ProgrammeValidationError("Only an applied programme change can be reverted.")
        if not decision.created_revision:
            return self.repository.set_decision_state_in(
                conn, user_id, programme.id, decision.id, STATE_REVERTED
            )
        revision = programme.current_revision
        if revision is None or decision.to_revision_id != revision.id:
            raise ProgrammeValidationError(
                "Only the most recent revision change can be reverted."
            )
        if decision.from_revision_id is None:
            raise ProgrammeValidationError("This revision has no predecessor to restore.")
        previous = self.repository.revision_in(conn, user_id, decision.from_revision_id)
        definition = definition_from_revision(programme, previous, timezone_name=timezone_name)
        draft = definition_to_draft(
            definition, user_id, reason="revert",
            audit={"reverts_decision_id": decision.id, "restored_revision": previous.revision_number},
        )
        restored, _ = self.repository.create_revision_in(
            conn, user_id, programme.id, draft, reason="revert",
            adaptation=AdaptationDecisionInput(
                scope={**decision.scope, "level": "revert", "revision_number": None},
                inputs={"reverts_decision_id": decision.id},
                output={
                    "decision_type": "revert",
                    "reverts_decision_id": decision.id,
                    "restored_revision_number": previous.revision_number,
                    "rationale_codes": [],
                },
                confidence=1.0,
                rule_version=engine.RULE_VERSION,
                explanation="The user reverted a programme revision; an earlier revision was restored as a new one.",
                confirmed=True,
                decision_type="revert",
                application_state=STATE_CONFIRMED,
                week_number=decision.week_number,
                exercise_template_id=decision.exercise_template_id,
            ),
        )
        self._carry_week_state(conn, user_id, programme.id, revision, restored, decision.week_number or 1)
        return self.repository.set_decision_state_in(
            conn,
            user_id,
            programme.id,
            decision.id,
            STATE_SUPERSEDED if decision.application_state == STATE_SUPERSEDED else STATE_REVERTED,
        )

    # ---- user control: overrides -------------------------------------------

    def add_override(
        self,
        user_id: str,
        programme_id: int,
        *,
        kind: str,
        exercise_template_id: str = "",
        value: Mapping[str, Any] | None = None,
        today: date,
        weeks: int | None = None,
        ends_on: date | None = None,
        note: str = "",
    ) -> ProgrammeOverride:
        """Record a durable, scoped user instruction (never mutates provider data)."""

        if kind not in OVERRIDE_KINDS:
            raise ProgrammeValidationError(f"Unsupported override kind: {kind}")
        payload = dict(value or {})
        if len(json.dumps(payload, default=str)) > 4096:
            raise ProgrammeValidationError("The override value is too large.")
        needs_exercise = kind in {"pin", "pain_report", "technique_report", "equipment_issue", "substitution"}
        if needs_exercise and not exercise_template_id.strip():
            raise ProgrammeValidationError(f"A {kind} override needs an exercise.")
        if kind == "pin":
            fields = [item for item in payload.get("fields", []) if item in PIN_FIELDS]
            if not fields:
                raise ProgrammeValidationError("Pin at least one of: load, sets, reps.")
            payload["fields"] = sorted(set(fields))
        if kind == "time_limit":
            limit = payload.get("max_sets")
            if not isinstance(limit, int) or not 1 <= limit <= 12:
                raise ProgrammeValidationError("A time limit needs max_sets between 1 and 12.")
        if kind == "substitution" and not str(payload.get("performed_template_id") or "").strip():
            raise ProgrammeValidationError("A substitution needs performed_template_id.")
        end = ends_on or today + timedelta(weeks=weeks or (4 if kind in {"pin", "time_limit", "substitution"} else 8))
        if end < today or end > today + timedelta(weeks=52):
            raise ProgrammeValidationError("An override must end within the next 52 weeks.")
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_programme(conn, user_id, programme_id)
            return self.repository.create_override_in(
                conn, user_id, programme_id, kind=kind,
                exercise_template_id=exercise_template_id.strip(),
                scope={"exercise_template_id": exercise_template_id.strip()},
                value=payload, starts_on=today.isoformat(), ends_on=end.isoformat(), note=note[:500],
            )

    def revoke_override(self, user_id: str, programme_id: int, override_id: int) -> ProgrammeOverride:
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.repository.revoke_override_in(conn, user_id, programme_id, override_id)

    def list_overrides(self, user_id: str, programme_id: int) -> list[ProgrammeOverride]:
        return self.repository.list_overrides(user_id, programme_id)

    def request_review(
        self, user_id: str, programme_id: int, *, now: datetime
    ) -> ProgrammeOverride:
        """Record an early-review request so the next review is explicit and visible."""

        timezone_name = "UTC"
        with self.repository.transaction() as conn:
            timezone_name = self._timezone(conn, user_id)
        today = local_date_of(now, timezone_name) or now.date()
        for existing in self.repository.list_overrides(user_id, programme_id, active_only=True):
            if existing.kind == "review_request" and existing.starts_on == today.isoformat():
                return existing
        return self.add_override(
            user_id, programme_id, kind="review_request", today=today, weeks=1,
            note="Early review requested by the user.",
        )

    def confirm_match(
        self,
        user_id: str,
        programme_id: int,
        workout: Any,
        routine_position: int,
        *,
        now: datetime,
    ) -> ReviewResult:
        """Confirm or correct a workout's routine and re-evaluate that workout."""

        evidence = workout_evidence(workout)
        if evidence is None:
            raise ProgrammeValidationError("The workout is not available for confirmation.")
        programme = self.repository.get_programme(user_id, programme_id)
        revision = programme.current_revision if programme else None
        if programme is None or revision is None:
            raise ProgrammeNotFoundError("Programme not found for this user.")
        routine = next(
            (item for item in revision.routines if item.rotation_position == routine_position), None
        )
        if routine is None:
            raise ProgrammeValidationError("The routine is not part of this programme.")
        from .models import RoutineWorkoutMatch

        self.repository.save_routine_workout_match(
            user_id,
            RoutineWorkoutMatch(
                programme_id=programme.id,
                revision_id=revision.id,
                routine_position=routine_position,
                source_routine_id=routine.source_routine_id,
                workout_id=evidence.workout_id,
                score=1.0,
                confidence=1.0,
                method="user_confirmed_override",
                user_confirmed=True,
            ),
        )
        match = WorkoutMatch(
            evidence.workout_id, engine.MATCH_HIGH, routine_position, 1.0,
            "user_confirmed_override", True,
        )
        result = ReviewResult()
        self._process_workout(
            user_id, programme_id, evidence, match, now=now, recovery=None, result=result
        )
        return result

    # ---- programme-level fatigue -----------------------------------------

    def _programme_level(
        self,
        user_id: str,
        programme_id: int,
        *,
        now: datetime,
        recovery: engine.RecoveryEvidence | None,
    ) -> AdaptationDecision | None:
        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            if programme.status != "active" or programme.current_revision is None or programme.start_date is None:
                return None
            timezone_name = self._timezone(conn, user_id)
            today = local_date_of(now, timezone_name) or now.date()
            week = week_for_date(programme.start_date, int(programme.duration_weeks or 0), today)
            if week is None:
                return None
            decisions = self.repository.list_decisions_in(conn, user_id, programme_id)
            overrides = self.repository.list_overrides_in(conn, user_id, programme_id, active_only=True)
            definition = definition_from_revision(
                programme, programme.current_revision, timezone_name=timezone_name
            )
            window = [
                item
                for item in decisions
                if item.source_workout_id
                and item.application_state != STATE_SUPERSEDED
                and item.week_number >= week - 1
                and (item.inputs or {}).get("match", {}).get("confidence") in {engine.MATCH_HIGH, engine.MATCH_PROBABLE}
            ]
            latest: dict[tuple[Any, str], AdaptationDecision] = {}
            for item in window:
                latest[(item.scope.get("routine_position"), item.exercise_template_id)] = item
            in_trouble = [
                item
                for item in latest.values()
                if (item.inputs or {}).get("assessment", {}).get("signal") == engine.SIGNAL_FAILURE
                or item.decision_type == engine.EXERCISE_FATIGUE
                and (item.output or {}).get("proposed_prescription", {}).get("phase") == "enter"
            ]
            sessions = {
                item.source_workout_id
                for item in window
                if (item.inputs or {}).get("assessment", {}).get("signal") == engine.SIGNAL_FAILURE
            }
            last_programme = [
                item
                for item in decisions
                if item.decision_type == engine.PROGRAMME_FATIGUE
                and item.application_state in APPLIED_STATES
            ]
            state = self.repository.get_week_state_in(
                conn, user_id, programme_id, programme.current_revision.id, week
            )
            adherence = state.adherence if state else None
            evidence = engine.WeekEvidence(
                exercises_evaluated=len(latest),
                exercises_in_trouble=len(in_trouble),
                sessions_with_trouble=len(sessions),
                adherence=adherence,
                user_fatigue_report=any(
                    item.kind == "fatigue_report" and item.is_active_on(today) for item in overrides
                ),
                recovery=recovery,
                weeks_since_programme_fatigue=(
                    week - last_programme[-1].week_number if last_programme else None
                ),
                high_confidence=all(
                    (item.inputs or {}).get("match", {}).get("confidence") == engine.MATCH_HIGH
                    for item in window
                ),
                planned_fatigue_management=(
                    str((definition.get("programme_spec") or {}).get("fatigue_management")) == "planned"
                ),
            )
            outcome = engine.evaluate_programme_fatigue(evidence, self.policy)
            if outcome is None:
                return None
            key = f"programme-fatigue:{programme_id}:{week}:{outcome.decision_type}:{engine.RULE_VERSION}"
            if any(item.idempotency_key == key for item in decisions):
                return next(item for item in decisions if item.idempotency_key == key)
            state_name = STATE_PENDING if outcome.requires_confirmation else STATE_AUTO_APPLIED
            revision = programme.current_revision
            scope = {
                "level": "programme",
                "user_id": user_id,
                "programme_id": programme_id,
                "revision_id": revision.id,
                "revision_number": revision.revision_number,
                "week_number": week,
            }
            common: dict[str, Any] = {
                "scope": scope,
                "inputs": {"evidence": evidence.to_dict(), "policy": self.policy.to_dict()},
                "output": outcome.to_dict(),
                "confidence": outcome.confidence,
                "rule_version": engine.RULE_VERSION,
                "explanation": outcome.explanation,
                "decision_type": outcome.decision_type,
                "application_state": state_name,
                "idempotency_key": key,
                "week_number": week,
            }
            if outcome.decision_type == engine.PROGRAMME_FATIGUE and state_name == STATE_AUTO_APPLIED:
                _revision, decision_id = self._apply_programme_fatigue(
                    conn, user_id, programme, definition, outcome, week, common
                )
                return self.repository.get_decision_in(conn, user_id, programme_id, int(decision_id or 0))
            stored, _created = self.repository.record_decision_in(
                conn, user_id, programme_id, revision.id, revision.id, AdaptationDecisionInput(**common)
            )
            return stored

    def _apply_programme_fatigue(
        self,
        conn: Any,
        user_id: str,
        programme: Programme,
        definition: dict[str, Any],
        outcome: engine.AdaptationOutcome,
        week: int,
        common: Mapping[str, Any],
    ) -> tuple[ProgrammeRevision, int | None]:
        candidate = copy.deepcopy(definition)
        _apply_structural(candidate, outcome, None, "", week)
        draft = definition_to_draft(
            candidate,
            user_id,
            reason="adaptation",
            audit={"decision_type": outcome.decision_type, "rule_version": engine.RULE_VERSION},
        )
        revision = programme.current_revision
        new_revision, decision_id = self.repository.create_revision_in(
            conn,
            user_id,
            programme.id,
            draft,
            reason="adaptation",
            adaptation=AdaptationDecisionInput(**common),
        )
        if revision is not None:
            self._carry_week_state(conn, user_id, programme.id, revision, new_revision, week)
        return new_revision, decision_id


# ---------------------------------------------------------------------------
# Structural edits on a definition copy
# ---------------------------------------------------------------------------


def _outcome_from_output(output: Mapping[str, Any]) -> engine.AdaptationOutcome:
    """Rebuild the engine outcome a stored decision was made from."""

    proposed = dict(output.get("accepted_prescription") or output.get("proposed_prescription") or {})
    return engine.AdaptationOutcome(
        decision_type=str(output.get("decision_type")),
        rationale_codes=tuple(output.get("rationale_codes") or ()),
        previous=dict(output.get("previous_prescription") or {}),
        proposed=proposed,
        requires_confirmation=False,
        structural=bool(output.get("structural")),
        confidence=0.0,
        assessment=dict(output.get("assessment") or {}),
        bounds=dict(output.get("bounds") or {}),
        direction=int(output.get("direction") or 0),
    )


def _rules(conn: Any, user_id: str) -> tuple[Any, Any]:
    from database import load_validation_rules

    return load_validation_rules(conn, user_id)


def _affected_blocks(definition: Mapping[str, Any], week: int) -> set[int]:
    return {
        int(block.get("number") or 0)
        for block in definition.get("blocks") or []
        if int(block.get("end_week") or 0) >= week
    }


def _apply_structural(
    definition: dict[str, Any],
    outcome: engine.AdaptationOutcome,
    routine_position: int | None,
    template_id: str,
    week: int,
) -> None:
    """Apply a structural outcome to a definition copy (current block onwards)."""

    affected = _affected_blocks(definition, week)
    proposed = outcome.proposed
    previous = outcome.previous
    if outcome.decision_type == engine.REDUCE_SETS:
        found = _find_exercise(definition, template_id, routine_position)
        if found is None:
            return
        delta = int(proposed.get("sets", previous.get("sets", 0))) - int(previous.get("sets", 0))
        for item in found[1].get("prescriptions") or []:
            if item.get("block_number") in affected and item.get("week_number") in (None, 0):
                floor = max(1, int(item.get("source_sets") or 1) - 1)
                item["sets"] = max(floor, int(item.get("sets") or 1) + delta)
    elif outcome.decision_type == engine.ALTER_REP_RANGE:
        found = _find_exercise(definition, template_id, routine_position)
        if found is None:
            return
        d_min = int(proposed.get("rep_min", previous.get("rep_min", 0)) or 0) - int(previous.get("rep_min", 0) or 0)
        d_max = int(proposed.get("rep_max", previous.get("rep_max", 0)) or 0) - int(previous.get("rep_max", 0) or 0)
        for item in found[1].get("prescriptions") or []:
            target = item.get("rep_target") or {}
            if item.get("block_number") in affected and target.get("min") is not None and target.get("max") is not None:
                target["min"] = int(target["min"]) + d_min
                target["max"] = int(target["max"]) + d_max
                text = f"{target['min']}-{target['max']}"
                target["description"] = text
                item["rep_range"] = text
                item["rep_target"] = target
    elif outcome.decision_type == engine.PROGRAMME_FATIGUE:
        last_week = int(definition.get("cycle_weeks") or week)
        weeks = [
            number
            for number in range(week, week + int(proposed.get("weeks", 1)))
            if number <= last_week
        ]
        multiplier = float(proposed.get("set_multiplier", 0.7))
        rir_delta = int(proposed.get("rir_delta", 1))
        mode = str(proposed.get("mode", "volume_only"))
        block = _block_number(definition, week)
        for day in definition.get("days") or []:
            for exercise in day.get("exercises") or []:
                base = _prescription_entry(exercise, block, week)
                if base is None:
                    continue
                for target_week in weeks:
                    entry = copy.deepcopy(base)
                    entry["week_number"] = target_week
                    entry["sets"] = max(
                        max(1, int(entry.get("source_sets") or 1) - 1),
                        round(int(entry.get("sets") or 1) * multiplier),
                    )
                    effort = dict(entry.get("effort_target") or {})
                    if effort.get("min") is not None:
                        effort["min"] = int(effort["min"]) + rir_delta
                    if effort.get("max") is not None:
                        effort["max"] = int(effort["max"]) + rir_delta
                    entry["effort_target"] = effort
                    entry["fatigue_management"] = mode
                    exercise.setdefault("prescriptions", [])
                    exercise["prescriptions"] = [
                        item
                        for item in exercise["prescriptions"]
                        if not (item.get("block_number") == entry.get("block_number") and item.get("week_number") == target_week)
                    ] + [entry]
        periods = list(definition.get("fatigue_periods") or [])
        periods.append(
            {
                "weeks": weeks,
                "mode": mode,
                "set_multiplier": multiplier,
                "rir_delta": rir_delta,
                "load_percent": proposed.get("load_percent", 0.0),
                "exit_criteria": proposed.get("exit_criteria", {}),
                "trigger": proposed.get("trigger_evidence", {}),
                "sessions_per_week": (
                    max(1, int((definition.get("programme_spec") or {}).get("sessions_per_week") or len(definition.get("days") or [])) - 1)
                    if mode == "frequency"
                    else None
                ),
            }
        )
        definition["fatigue_periods"] = periods


__all__ = [
    "OVERRIDE_KINDS",
    "AdaptationService",
    "ReviewResult",
    "WorkoutEvidence",
    "WorkoutMatch",
    "build_prescription",
    "decision_view",
    "workout_evidence",
]
