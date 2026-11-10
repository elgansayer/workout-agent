"""Structured, versioned training rules and deterministic constraint enforcement.

Replaces free-text coaching constraints with a typed, validated schema and provides
deterministic enforcement authority over AI-generated or user-selected workouts,
programmes, and routines.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ProhibitedExerciseRule(BaseModel):
    """An exercise that must never be prescribed or performed."""

    exercise: str = Field(min_length=1, max_length=150)
    reason: str | None = Field(default=None, max_length=300)


class ExerciseSubstitutionRule(BaseModel):
    """An explicit replacement of one exercise with another."""

    from_exercise: str = Field(min_length=1, max_length=150)
    to_exercise: str = Field(min_length=1, max_length=150)
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_different(self) -> ExerciseSubstitutionRule:
        if self.from_exercise.strip().lower() == self.to_exercise.strip().lower():
            raise ValueError("from_exercise and to_exercise must be different exercises")
        return self


class RepIntensityBoundsRule(BaseModel):
    """Boundaries on repetitions, RPE, RIR, and intensity."""

    min_reps: int | None = Field(default=None, ge=1, le=100)
    max_reps: int | None = Field(default=None, ge=1, le=100)
    max_rpe: float | None = Field(default=None, ge=1.0, le=10.0)
    min_rir: float | None = Field(default=None, ge=0.0, le=10.0)
    max_intensity_pct: float | None = Field(default=None, ge=10.0, le=100.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> RepIntensityBoundsRule:
        if (
            self.min_reps is not None
            and self.max_reps is not None
            and self.min_reps > self.max_reps
        ):
            raise ValueError(
                f"min_reps ({self.min_reps}) cannot exceed max_reps ({self.max_reps})"
            )
        return self


class FrequencyRule(BaseModel):
    """Scheduling and frequency limits."""

    max_sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    min_rest_days_between_sessions: int | None = Field(default=None, ge=0, le=6)
    max_frequency_per_muscle: int | None = Field(default=None, ge=1, le=7)


class VolumeRule(BaseModel):
    """Volume limits per workout, exercise, or muscle group."""

    max_sets_per_workout: int | None = Field(default=None, ge=1, le=100)
    max_sets_per_exercise: int | None = Field(default=None, ge=1, le=30)
    max_weekly_sets_per_muscle: int | None = Field(default=None, ge=1, le=50)


class EquipmentRule(BaseModel):
    """Equipment availability and prohibition constraints."""

    available_equipment: list[str] = Field(default_factory=list)
    prohibited_equipment: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_disjoint(self) -> EquipmentRule:
        avail = {e.strip().lower() for e in self.available_equipment if e.strip()}
        prohib = {e.strip().lower() for e in self.prohibited_equipment if e.strip()}
        overlap = avail.intersection(prohib)
        if overlap:
            raise ValueError(
                f"Equipment cannot be both available and prohibited: {', '.join(sorted(overlap))}"
            )
        return self


class RecoveryRule(BaseModel):
    """Readiness, sleep, and recovery thresholds."""

    min_sleep_hours: float | None = Field(default=None, ge=3.0, le=16.0)
    min_readiness_score: float | None = Field(default=None, ge=0.0, le=100.0)
    max_consecutive_training_days: int | None = Field(default=None, ge=1, le=7)
    auto_deload_on_fatigue: bool = Field(default=True)


class ContextualNote(BaseModel):
    """Qualitative injury, joint, medical, or contextual training note."""

    category: Literal["medical", "joint", "lifestyle", "schedule", "general"] = "general"
    note: str = Field(min_length=1, max_length=500)
    severity: Literal["info", "warning", "critical"] = "info"


class StructuredTrainingRules(BaseModel):
    """Versioned, user-owned structured training rules document."""

    version: int = Field(default=1, ge=1)
    prohibited_exercises: list[ProhibitedExerciseRule] = Field(default_factory=list)
    substitutions: list[ExerciseSubstitutionRule] = Field(default_factory=list)
    rep_intensity_bounds: RepIntensityBoundsRule = Field(
        default_factory=RepIntensityBoundsRule
    )
    frequency: FrequencyRule = Field(default_factory=FrequencyRule)
    volume: VolumeRule = Field(default_factory=VolumeRule)
    equipment: EquipmentRule = Field(default_factory=EquipmentRule)
    recovery: RecoveryRule = Field(default_factory=RecoveryRule)
    contextual_notes: list[ContextualNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_integrity(self) -> StructuredTrainingRules:
        prohibited_set = {
            p.exercise.strip().lower()
            for p in self.prohibited_exercises
            if p.exercise.strip()
        }
        for sub in self.substitutions:
            to_clean = sub.to_exercise.strip().lower()
            if to_clean in prohibited_set:
                raise ValueError(
                    f"Substitution target '{sub.to_exercise}' cannot be a prohibited exercise"
                )
        return self

    @classmethod
    def from_raw(cls, raw: Any) -> StructuredTrainingRules:
        """Parse from dict, model, or migrate legacy free-text list."""
        if isinstance(raw, StructuredTrainingRules):
            return raw
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        if isinstance(raw, list):
            # Legacy list of strings (e.g. constraints=["Bad knees", "No jumping"])
            notes: list[ContextualNote] = []
            for item in raw:
                text = str(item).strip()
                if not text:
                    continue
                lower = text.lower()
                is_joint = any(
                    part in lower
                    for part in (
                        "knee",
                        "shoulder",
                        "back",
                        "hip",
                        "joint",
                        "wrist",
                        "elbow",
                        "ankle",
                        "toe",
                        "spine",
                    )
                )
                category: Literal["medical", "joint", "general"] = (
                    "joint" if is_joint else "general"
                )
                severity: Literal["warning", "info"] = (
                    "warning"
                    if any(w in lower for w in ("no ", "avoid", "pain", "injury"))
                    else "info"
                )
                notes.append(
                    ContextualNote(category=category, note=text, severity=severity)
                )
            return cls(version=1, contextual_notes=notes)
        return cls()


def format_rules_for_prompt(
    rules_input: StructuredTrainingRules | dict[str, Any] | None,
) -> str:
    """Format structured rules into clear, unambiguous coaching directives for AI prompts."""
    if not rules_input:
        return "No specific structured training rules defined."

    rules = (
        rules_input
        if isinstance(rules_input, StructuredTrainingRules)
        else StructuredTrainingRules.from_raw(rules_input)
    )

    sections: list[str] = [
        f"MANDATORY TRAINING CONSTRAINTS (Deterministic Rule Schema v{rules.version}):"
    ]

    # Prohibited exercises
    if rules.prohibited_exercises:
        items = [
            f"{p.exercise}" + (f" (Reason: {p.reason})" if p.reason else "")
            for p in rules.prohibited_exercises
        ]
        sections.append(
            f"- PROHIBITED EXERCISES (Never prescribe under any circumstances): {'; '.join(items)}"
        )

    # Substitutions
    if rules.substitutions:
        items = [
            f"Replace '{s.from_exercise}' with '{s.to_exercise}'"
            + (f" (Reason: {s.reason})" if s.reason else "")
            for s in rules.substitutions
        ]
        sections.append(f"- MANDATORY EXERCISE SUBSTITUTIONS: {'; '.join(items)}")

    # Rep/Intensity
    b = rules.rep_intensity_bounds
    bounds_items: list[str] = []
    if b.min_reps is not None and b.max_reps is not None:
        bounds_items.append(f"Reps strictly between {b.min_reps} and {b.max_reps}")
    elif b.min_reps is not None:
        bounds_items.append(f"Minimum reps: {b.min_reps}")
    elif b.max_reps is not None:
        bounds_items.append(f"Maximum reps: {b.max_reps}")
    if b.max_rpe is not None:
        bounds_items.append(f"Max RPE: {b.max_rpe}")
    if b.min_rir is not None:
        bounds_items.append(f"Min RIR: {b.min_rir}")
    if b.max_intensity_pct is not None:
        bounds_items.append(f"Max intensity: {b.max_intensity_pct}%")
    if bounds_items:
        sections.append(f"- REP & INTENSITY BOUNDS: {', '.join(bounds_items)}")

    # Frequency
    f = rules.frequency
    freq_items: list[str] = []
    if f.max_sessions_per_week is not None:
        freq_items.append(f"Max {f.max_sessions_per_week} sessions/week")
    if f.min_rest_days_between_sessions is not None:
        freq_items.append(
            f"Min {f.min_rest_days_between_sessions} rest days between sessions"
        )
    if f.max_frequency_per_muscle is not None:
        freq_items.append(f"Max {f.max_frequency_per_muscle}x/week per muscle group")
    if freq_items:
        sections.append(f"- FREQUENCY CONSTRAINTS: {', '.join(freq_items)}")

    # Volume
    v = rules.volume
    vol_items: list[str] = []
    if v.max_sets_per_workout is not None:
        vol_items.append(f"Max {v.max_sets_per_workout} total sets/workout")
    if v.max_sets_per_exercise is not None:
        vol_items.append(f"Max {v.max_sets_per_exercise} sets/exercise")
    if v.max_weekly_sets_per_muscle is not None:
        vol_items.append(f"Max {v.max_weekly_sets_per_muscle} sets/muscle/week")
    if vol_items:
        sections.append(f"- VOLUME LIMITS: {', '.join(vol_items)}")

    # Equipment
    eq = rules.equipment
    eq_items: list[str] = []
    if eq.available_equipment:
        eq_items.append(f"Available equipment: {', '.join(eq.available_equipment)}")
    if eq.prohibited_equipment:
        eq_items.append(f"Prohibited equipment: {', '.join(eq.prohibited_equipment)}")
    if eq_items:
        sections.append(f"- EQUIPMENT: {'; '.join(eq_items)}")

    # Recovery
    rec = rules.recovery
    rec_items: list[str] = []
    if rec.min_sleep_hours is not None:
        rec_items.append(f"Min sleep: {rec.min_sleep_hours}h")
    if rec.min_readiness_score is not None:
        rec_items.append(
            f"Deload trigger below readiness score {rec.min_readiness_score}"
        )
    if rec.max_consecutive_training_days is not None:
        rec_items.append(
            f"Max consecutive training days: {rec.max_consecutive_training_days}"
        )
    if rec_items:
        sections.append(f"- RECOVERY & READINESS: {', '.join(rec_items)}")

    # Contextual notes
    if rules.contextual_notes:
        note_lines = [
            f"  * [{n.category.upper()}] ({n.severity.upper()}) {n.note}"
            for n in rules.contextual_notes
        ]
        sections.append("- CONTEXTUAL INJURY & MEDICAL NOTES:\n" + "\n".join(note_lines))

    return "\n".join(sections)


class RuleViolation(BaseModel):
    """An identified violation of a training rule, with deterministic remediation."""

    rule_type: str
    exercise: str | None = None
    message: str
    remediated: bool = False
    action_taken: str | None = None


class RuleEnforcementReport(BaseModel):
    """Result of deterministic rule enforcement."""

    valid: bool
    violations: list[RuleViolation] = Field(default_factory=list)
    remediations: list[str] = Field(default_factory=list)
    enforced_plan: str | None = None


# Regex matching: "Exercise Name: sets x reps" or "Exercise Name: sets x min-max" with optional note
_PLAN_LINE_RE = re.compile(
    r"^(?P<name>[^:\n]+):\s*(?P<sets>\d+)\s*[x×]\s*(?P<reps>\d+)(?:-(?P<reps_max>\d+))?(?P<rest>.*)$",
    re.IGNORECASE,
)


def enforce_rules_on_plan_text(
    plan_text: str,
    rules: StructuredTrainingRules,
    recovery: dict[str, Any] | None = None,
) -> tuple[str, list[RuleViolation]]:
    """Deterministically enforce structured rules on a generated or formatted plan text.

    Deterministic code remains the enforcement authority. Any prohibited exercise is
    replaced or eliminated; substitutions are applied; rep bounds and volume limits are
    strictly clamped; and deload interventions are enacted on poor recovery.
    """
    violations: list[RuleViolation] = []
    lines = plan_text.splitlines()
    if not lines:
        return plan_text, violations

    # Check recovery fatigue trigger
    needs_recovery_deload = False
    if rules.recovery.auto_deload_on_fatigue and recovery:
        sleep = recovery.get("sleep_hours")
        if (
            rules.recovery.min_sleep_hours is not None
            and sleep is not None
            and float(sleep) < rules.recovery.min_sleep_hours
        ):
            needs_recovery_deload = True
            violations.append(
                RuleViolation(
                    rule_type="recovery_fatigue",
                    message=(
                        f"Sleep ({sleep}h) is below required minimum "
                        f"({rules.recovery.min_sleep_hours}h). Applying volume reduction."
                    ),
                    remediated=True,
                    action_taken="Volume trimmed due to recovery deficit",
                )
            )
        readiness = recovery.get("readiness_score") or recovery.get("hrv_readiness")
        if (
            rules.recovery.min_readiness_score is not None
            and readiness is not None
            and float(readiness) < rules.recovery.min_readiness_score
        ):
            needs_recovery_deload = True
            violations.append(
                RuleViolation(
                    rule_type="readiness_deficit",
                    message=(
                        f"Readiness ({readiness}) is below threshold "
                        f"({rules.recovery.min_readiness_score}). Applying volume reduction."
                    ),
                    remediated=True,
                    action_taken="Volume trimmed due to readiness deficit",
                )
            )

    sub_map = {
        s.from_exercise.strip().lower(): s for s in rules.substitutions
    }
    prohib_map = {
        p.exercise.strip().lower(): p for p in rules.prohibited_exercises
    }

    enforced_lines: list[str] = []
    running_total_sets = 0
    max_workout_sets = rules.volume.max_sets_per_workout

    for idx, line in enumerate(lines):
        # Keep header line (line 0) unchanged unless deload note is added
        if idx == 0 and not _PLAN_LINE_RE.match(line):
            enforced_lines.append(line)
            continue

        match = _PLAN_LINE_RE.match(line)
        if not match:
            # Check if this non-exercise line mentions a prohibited exercise
            line_lower = line.lower()
            skip_line = False
            for p_name, p_rule in prohib_map.items():
                if p_name in line_lower:
                    if p_name in sub_map:
                        replacement = sub_map[p_name].to_exercise
                        line = re.sub(re.escape(p_rule.exercise), replacement, line, flags=re.IGNORECASE)
                        violations.append(
                            RuleViolation(
                                rule_type="substitution",
                                exercise=p_rule.exercise,
                                message=f"Substituted '{p_rule.exercise}' with '{replacement}'.",
                                remediated=True,
                                action_taken=f"Replaced with {replacement}",
                            )
                        )
                    else:
                        skip_line = True
                        violations.append(
                            RuleViolation(
                                rule_type="prohibited_exercise",
                                exercise=p_rule.exercise,
                                message=f"Prohibited exercise '{p_rule.exercise}' removed from plan.",
                                remediated=True,
                                action_taken="Removed line",
                            )
                        )
                    break
            if not skip_line:
                enforced_lines.append(line)
            continue

        ex_name = match.group("name").strip()
        sets = int(match.group("sets"))
        reps = int(match.group("reps"))
        reps_max_str = match.group("reps_max")
        reps_max = int(reps_max_str) if reps_max_str else None
        rest_note = match.group("rest") or ""

        # 1. Check Substitution
        ex_lower = ex_name.lower()
        if ex_lower in sub_map:
            sub = sub_map[ex_lower]
            ex_name = sub.to_exercise
            ex_lower = ex_name.lower()
            violations.append(
                RuleViolation(
                    rule_type="substitution",
                    exercise=sub.from_exercise,
                    message=f"Mandatory substitution applied: '{sub.from_exercise}' -> '{sub.to_exercise}'.",
                    remediated=True,
                    action_taken=f"Replaced with {sub.to_exercise}",
                )
            )

        # 2. Check Prohibited Exercise
        if ex_lower in prohib_map:
            p_rule = prohib_map[ex_lower]
            violations.append(
                RuleViolation(
                    rule_type="prohibited_exercise",
                    exercise=p_rule.exercise,
                    message=f"Exercise '{p_rule.exercise}' is prohibited ({p_rule.reason or 'User rule'}). Dropping from plan.",
                    remediated=True,
                    action_taken="Exercise omitted",
                )
            )
            continue

        # 3. Apply recovery deload (if triggered)
        if needs_recovery_deload and sets > 1:
            sets -= 1

        # 4. Enforce volume: max sets per exercise
        if (
            rules.volume.max_sets_per_exercise is not None
            and sets > rules.volume.max_sets_per_exercise
        ):
            violations.append(
                RuleViolation(
                    rule_type="volume_sets_per_exercise",
                    exercise=ex_name,
                    message=(
                        f"Exercise '{ex_name}' sets ({sets}) exceeded max allowed "
                        f"({rules.volume.max_sets_per_exercise}). Clamped."
                    ),
                    remediated=True,
                    action_taken=f"Clamped sets to {rules.volume.max_sets_per_exercise}",
                )
            )
            sets = rules.volume.max_sets_per_exercise

        # 5. Enforce volume: max sets per workout
        if max_workout_sets is not None:
            remaining = max_workout_sets - running_total_sets
            if remaining <= 0:
                violations.append(
                    RuleViolation(
                        rule_type="volume_max_workout_sets",
                        exercise=ex_name,
                        message=(
                            f"Total workout sets reached limit ({max_workout_sets}). "
                            f"Exercise '{ex_name}' dropped."
                        ),
                        remediated=True,
                        action_taken="Omitted to respect workout set limit",
                    )
                )
                continue
            if sets > remaining:
                violations.append(
                    RuleViolation(
                        rule_type="volume_max_workout_sets",
                        exercise=ex_name,
                        message=(
                            f"Sets for '{ex_name}' ({sets}) clamped to {remaining} "
                            f"to stay within workout cap ({max_workout_sets})."
                        ),
                        remediated=True,
                        action_taken=f"Clamped sets to {remaining}",
                    )
                )
                sets = remaining

        running_total_sets += sets

        # 6. Enforce rep bounds
        b = rules.rep_intensity_bounds
        if b.min_reps is not None:
            if reps < b.min_reps:
                violations.append(
                    RuleViolation(
                        rule_type="rep_bounds_min",
                        exercise=ex_name,
                        message=f"Reps for '{ex_name}' ({reps}) below min bound ({b.min_reps}). Clamped.",
                        remediated=True,
                        action_taken=f"Clamped reps to {b.min_reps}",
                    )
                )
                reps = b.min_reps
            if reps_max is not None and reps_max < b.min_reps:
                reps_max = b.min_reps

        if b.max_reps is not None:
            if reps > b.max_reps:
                violations.append(
                    RuleViolation(
                        rule_type="rep_bounds_max",
                        exercise=ex_name,
                        message=f"Reps for '{ex_name}' ({reps}) above max bound ({b.max_reps}). Clamped.",
                        remediated=True,
                        action_taken=f"Clamped reps to {b.max_reps}",
                    )
                )
                reps = b.max_reps
            if reps_max is not None and reps_max > b.max_reps:
                violations.append(
                    RuleViolation(
                        rule_type="rep_bounds_max",
                        exercise=ex_name,
                        message=f"Max rep bound for '{ex_name}' ({reps_max}) above max allowed ({b.max_reps}). Clamped.",
                        remediated=True,
                        action_taken=f"Clamped max reps to {b.max_reps}",
                    )
                )
                reps_max = b.max_reps

        # Ensure reps <= reps_max if a range is present
        if reps_max is not None and reps > reps_max:
            reps_max = reps

        rep_str = f"{reps}-{reps_max}" if reps_max is not None and reps_max != reps else f"{reps}"
        enforced_lines.append(f"{ex_name}: {sets} x {rep_str}{rest_note}")

    return "\n".join(enforced_lines), violations
