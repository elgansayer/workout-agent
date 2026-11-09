"""Deterministic programme validator enforcing profile constraints.

Runs across the programme lifecycle: create, import, edit, inference,
selection, and activation. Blocking violations prevent activation and
identify the exact exercise, prescription, rule, and permitted remediation.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field
from training_rules import StructuredTrainingRules

_STRUCTURED_RULE_KEYS = frozenset(
    {
        "prohibited_exercises",
        "substitutions",
        "rep_intensity_bounds",
        "frequency",
        "volume",
        "equipment",
        "recovery",
        "contextual_notes",
    }
)


class BlockingViolation(BaseModel):
    """A deterministic violation that blocks programme activation."""

    exercise: str
    prescription: str
    rule: str
    permitted_remediation: str
    remediation: str = ""
    block_number: int | None = None
    routine: str | None = None

    def model_post_init(self, __context: Any, /) -> None:
        if not self.remediation:
            self.remediation = self.permitted_remediation


class ValidationResult(BaseModel):
    """Aggregate result from deterministic programme constraint validation."""

    is_valid: bool
    blocking_violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExcludedExerciseRule(BaseModel):
    """A rule excluding a specific movement, variation, or exercise pattern."""

    pattern: str
    rule_text: str
    remediation: str
    replacement: str | None = None


class ProfileConstraints(BaseModel):
    """Structured constraints extracted from user preferences or request payload."""

    version: int | None = None
    min_reps: int | None = None
    min_reps_rule: str | None = None
    max_reps: int | None = None
    max_reps_rule: str | None = None
    min_rir: float | None = None
    min_rir_rule: str | None = None
    max_rir: float | None = None
    max_rir_rule: str | None = None
    max_rpe: float | None = None
    max_rpe_rule: str | None = None
    max_intensity_pct: float | None = None
    max_intensity_pct_rule: str | None = None
    max_sets_per_exercise: int | None = None
    max_sets_per_exercise_rule: str | None = None
    max_sets_per_workout: int | None = None
    max_sets_per_workout_rule: str | None = None
    max_sessions_per_week: int | None = None
    max_sessions_per_week_rule: str | None = None
    excluded_rules: list[ExcludedExerciseRule] = Field(default_factory=list)
    substitution_rules: list[ExcludedExerciseRule] = Field(default_factory=list)
    prohibited_equipment: list[str] = Field(default_factory=list)
    available_equipment: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_rules: list[str] = Field(default_factory=list)


def parse_rep_range(val: Any) -> tuple[int | None, int | None]:
    """Parse a lower and upper repetition bound from a prescription string."""
    if not val:
        return None, None
    text = str(val).strip().lower()
    if "singles to triples" in text:
        return 1, 3
    if "singles" in text:
        return 1, 1
    # Match range like "2-6", "1-4", "8 - 12", "2 to 5"
    range_match = re.search(r"(\d+)\s*(?:[-–]|to)\s*(\d+)", text)
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))
    # Match single number like "12" or "8 reps"
    num_match = re.search(r"(\d+)", text)
    if num_match:
        n = int(num_match.group(1))
        return n, n
    return None, None


def parse_rir_range(val: Any) -> tuple[float | None, float | None]:
    """Parse lower and upper RIR from a target RIR value or string."""
    if val is None:
        return None, None
    if isinstance(val, (int, float)):
        f = float(val)
        return f, f
    text = str(val).strip().lower()
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:[-–]|to)\s*(\d+(?:\.\d+)?)", text)
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))
    num_match = re.search(r"(\d+(?:\.\d+)?)", text)
    if num_match:
        n = float(num_match.group(1))
        return n, n
    return None, None


def _clean_exercise_token(text: str) -> str:
    cleaned = text.strip().lower()
    cleaned = re.sub(r"^(?:no|avoid|stop|without)\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _default_remediation_for(exercise_pattern: str) -> str:
    lowered = exercise_pattern.lower()
    if "bulgarian" in lowered or "split squat" in lowered:
        return "Use leg extensions or the flat-foot leg press instead."
    if "overhead" in lowered or "shoulder press" in lowered:
        return "Substitute with lateral raises or chest press, or remove from routine."
    if "jump" in lowered:
        return "Substitute with low-impact alternative or remove jumping."
    if "vacuum" in lowered:
        return "Train abs for mass with progressive overload."
    if "deadlift" in lowered:
        return "Substitute with trap bar deadlift, back extensions, or leg press."
    if "running" in lowered or "stair" in lowered:
        return "Use stationary bike, swim, or incline walking instead."
    return f"Substitute '{exercise_pattern}' with an approved alternative or remove it from the routine."


def _clean_equipment(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()


def _tighten_minimum(current: Any, value: Any) -> Any:
    if value is None:
        return current
    if current is None or value > current:
        return value
    return current


def _tighten_maximum(current: Any, value: Any) -> Any:
    if value is None:
        return current
    if current is None or value < current:
        return value
    return current


def _structured_payload(raw: Any) -> StructuredTrainingRules | None:
    if isinstance(raw, StructuredTrainingRules):
        return raw
    if isinstance(raw, dict) and _STRUCTURED_RULE_KEYS.intersection(raw):
        return StructuredTrainingRules.from_raw(raw)
    return None


def _add_excluded(result: ProfileConstraints, rule: ExcludedExerciseRule) -> None:
    if not any(existing.pattern == rule.pattern for existing in result.excluded_rules):
        result.excluded_rules.append(rule)


def _add_substitution(result: ProfileConstraints, rule: ExcludedExerciseRule) -> None:
    if not any(existing.pattern == rule.pattern for existing in result.substitution_rules):
        result.substitution_rules.append(rule)


def _apply_structured_rules(
    result: ProfileConstraints,
    rules: StructuredTrainingRules,
) -> ProfileConstraints:
    if result.version is None or rules.version > result.version:
        result.version = rules.version

    for prohibited in rules.prohibited_exercises:
        pattern = _clean_exercise_token(prohibited.exercise)
        if not pattern:
            continue
        reason = f" ({prohibited.reason})" if prohibited.reason else ""
        _add_excluded(
            result,
            ExcludedExerciseRule(
                pattern=pattern,
                rule_text=f"Prohibited exercise: {prohibited.exercise}{reason}",
                remediation=_default_remediation_for(pattern),
            ),
        )

    for substitution in rules.substitutions:
        pattern = _clean_exercise_token(substitution.from_exercise)
        if not pattern:
            continue
        reason = f" ({substitution.reason})" if substitution.reason else ""
        _add_substitution(
            result,
            ExcludedExerciseRule(
                pattern=pattern,
                rule_text=(
                    f"Substitute {substitution.from_exercise} with "
                    f"{substitution.to_exercise}{reason}"
                ),
                remediation=(
                    f"Replace {substitution.from_exercise} with "
                    f"{substitution.to_exercise}."
                ),
                replacement=substitution.to_exercise,
            ),
        )

    bounds = rules.rep_intensity_bounds
    if bounds.min_reps is not None:
        tightened = _tighten_minimum(result.min_reps, bounds.min_reps)
        if tightened != result.min_reps:
            result.min_reps = int(tightened)
            result.min_reps_rule = f"Structured rule: minimum {bounds.min_reps} reps"
    if bounds.max_reps is not None:
        tightened = _tighten_maximum(result.max_reps, bounds.max_reps)
        if tightened != result.max_reps:
            result.max_reps = int(tightened)
            result.max_reps_rule = f"Structured rule: maximum {bounds.max_reps} reps"
    if bounds.min_rir is not None:
        tightened = _tighten_minimum(result.min_rir, bounds.min_rir)
        if tightened != result.min_rir:
            result.min_rir = float(tightened)
            result.min_rir_rule = f"Structured rule: minimum {bounds.min_rir:g} RIR"
    if bounds.max_rpe is not None:
        tightened = _tighten_maximum(result.max_rpe, bounds.max_rpe)
        if tightened != result.max_rpe:
            result.max_rpe = float(tightened)
            result.max_rpe_rule = f"Structured rule: maximum RPE {bounds.max_rpe:g}"
    if bounds.max_intensity_pct is not None:
        tightened = _tighten_maximum(
            result.max_intensity_pct, bounds.max_intensity_pct
        )
        if tightened != result.max_intensity_pct:
            result.max_intensity_pct = float(tightened)
            result.max_intensity_pct_rule = (
                f"Structured rule: maximum {bounds.max_intensity_pct:g}% intensity"
            )

    volume = rules.volume
    if volume.max_sets_per_exercise is not None:
        tightened = _tighten_maximum(
            result.max_sets_per_exercise, volume.max_sets_per_exercise
        )
        if tightened != result.max_sets_per_exercise:
            result.max_sets_per_exercise = int(tightened)
            result.max_sets_per_exercise_rule = (
                f"Structured rule: maximum {volume.max_sets_per_exercise} sets per exercise"
            )
    if volume.max_sets_per_workout is not None:
        tightened = _tighten_maximum(
            result.max_sets_per_workout, volume.max_sets_per_workout
        )
        if tightened != result.max_sets_per_workout:
            result.max_sets_per_workout = int(tightened)
            result.max_sets_per_workout_rule = (
                f"Structured rule: maximum {volume.max_sets_per_workout} sets per workout"
            )

    frequency = rules.frequency
    if frequency.max_sessions_per_week is not None:
        tightened = _tighten_maximum(
            result.max_sessions_per_week, frequency.max_sessions_per_week
        )
        if tightened != result.max_sessions_per_week:
            result.max_sessions_per_week = int(tightened)
            result.max_sessions_per_week_rule = (
                "Structured rule: maximum "
                f"{frequency.max_sessions_per_week} sessions per week"
            )

    equipment = rules.equipment
    for item in equipment.prohibited_equipment:
        cleaned = _clean_equipment(item)
        if cleaned and cleaned not in result.prohibited_equipment:
            result.prohibited_equipment.append(cleaned)
    for item in equipment.available_equipment:
        cleaned = _clean_equipment(item)
        if cleaned and cleaned not in result.available_equipment:
            result.available_equipment.append(cleaned)

    for note in rules.contextual_notes:
        result.warnings.append(
            f"Profile note ({note.category}, {note.severity}): {note.note}. "
            "Review the programme against this note."
        )

    return result


def _merge_profile_constraints(
    base: ProfileConstraints,
    extra: ProfileConstraints,
) -> ProfileConstraints:
    if extra.version is not None and (base.version is None or extra.version > base.version):
        base.version = extra.version
    for attr in ("min_reps", "min_rir"):
        value = getattr(extra, attr)
        tightened = _tighten_minimum(getattr(base, attr), value)
        if tightened != getattr(base, attr):
            setattr(base, attr, tightened)
            setattr(base, f"{attr}_rule", getattr(extra, f"{attr}_rule"))
    for attr in (
        "max_reps",
        "max_rir",
        "max_rpe",
        "max_intensity_pct",
        "max_sets_per_exercise",
        "max_sets_per_workout",
        "max_sessions_per_week",
    ):
        value = getattr(extra, attr)
        tightened = _tighten_maximum(getattr(base, attr), value)
        if tightened != getattr(base, attr):
            setattr(base, attr, tightened)
            setattr(base, f"{attr}_rule", getattr(extra, f"{attr}_rule"))
    for rule in extra.excluded_rules:
        _add_excluded(base, rule)
    for rule in extra.substitution_rules:
        _add_substitution(base, rule)
    for attr in ("prohibited_equipment", "available_equipment"):
        target = getattr(base, attr)
        for item in getattr(extra, attr):
            if item not in target:
                target.append(item)
    for warning in extra.warnings:
        if warning not in base.warnings:
            base.warnings.append(warning)
    for raw in extra.raw_rules:
        if raw not in base.raw_rules:
            base.raw_rules.append(raw)
    return base


def parse_profile_rules(
    constraints: Any,
    custom_rules: Any = None,
) -> ProfileConstraints:
    """Parse structured constraints or free-text profile rules."""
    result = ProfileConstraints()
    raw_list: list[Any] = []

    structured = _structured_payload(constraints)
    if structured is not None:
        _apply_structured_rules(result, structured)

    if isinstance(constraints, dict):
        if constraints.get("min_reps") is not None:
            result.min_reps = int(constraints["min_reps"])
            result.min_reps_rule = f"min_reps: {result.min_reps}"
        if constraints.get("max_reps") is not None:
            result.max_reps = int(constraints["max_reps"])
            result.max_reps_rule = f"max_reps: {result.max_reps}"
        if constraints.get("min_rir") is not None:
            result.min_rir = float(constraints["min_rir"])
            result.min_rir_rule = f"min_rir: {result.min_rir:g}"
        if constraints.get("max_rir") is not None:
            result.max_rir = float(constraints["max_rir"])
            result.max_rir_rule = f"max_rir: {result.max_rir:g}"
        for exc in constraints.get("excluded_exercises") or constraints.get("exclude") or []:
            pat = _clean_exercise_token(str(exc))
            if pat:
                _add_excluded(
                    result,
                    ExcludedExerciseRule(
                        pattern=pat,
                        rule_text=f"Exclude {pat}",
                        remediation=_default_remediation_for(pat),
                    ),
                )
        items = constraints.get("rules") or constraints.get("constraints") or []
        if isinstance(items, list):
            raw_list.extend(items)
    elif isinstance(constraints, list):
        raw_list.extend(constraints)

    if isinstance(custom_rules, list):
        raw_list.extend(custom_rules)

    for item in raw_list:
        if isinstance(item, dict):
            # Parse dict items inside a list
            _merge_profile_constraints(result, parse_profile_rules(item))
            continue

        text = str(item).strip()
        if not text:
            continue
        result.raw_rules.append(text)

        # 1. Rep range constraints
        min_rep_m = re.search(
            r"(?:min(?:imum)?_?reps?|reps?\s*>=\s*|no\s+(?:reps|sets)\s+under\s+|keep\s+reps\s+(?:above|at\s+least)\s+|at\s+least\s+)(?:\s*[:=]\s*|\s*)(\d+)",
            text,
            re.IGNORECASE,
        )
        if not min_rep_m:
            min_rep_m = re.search(r"min(?:imum)?\s+(\d+)\s+reps?", text, re.IGNORECASE)
        if min_rep_m and result.min_reps is None:
            result.min_reps = int(min_rep_m.group(1))
            result.min_reps_rule = text
        elif re.search(r"no\s+heavy\s+singles", text, re.IGNORECASE) and result.min_reps is None:
            result.min_reps = 2
            result.min_reps_rule = text

        max_rep_m = re.search(
            r"(?:max(?:imum)?_?reps?|reps?\s*<=\s*|no\s+reps\s+over\s+|at\s+most\s+)(?:\s*[:=]\s*|\s*)(\d+)",
            text,
            re.IGNORECASE,
        )
        if not max_rep_m:
            max_rep_m = re.search(r"max(?:imum)?\s+(\d+)\s+reps?", text, re.IGNORECASE)
        if max_rep_m and result.max_reps is None:
            result.max_reps = int(max_rep_m.group(1))
            result.max_reps_rule = text

        # 2. Intensity / RIR constraints
        min_rir_m = re.search(
            r"(?:min(?:imum)?_?rir|rir\s*>=\s*|no\s+sets\s+under\s+|leave\s+at\s+least\s+)(?:\s*[:=]\s*|\s*)(\d+(?:\.\d+)?)\s*(?:rir|reps?\s+in\s+reserve)?",
            text,
            re.IGNORECASE,
        )
        if min_rir_m and result.min_rir is None:
            result.min_rir = float(min_rir_m.group(1))
            result.min_rir_rule = text
        elif re.search(r"no\s+(?:training\s+to\s+)?failure", text, re.IGNORECASE) and result.min_rir is None:
            result.min_rir = 1.0
            result.min_rir_rule = text
        elif re.search(r"avoid\s+high\s+intensity", text, re.IGNORECASE) and result.min_rir is None:
            result.min_rir = 2.0
            result.min_rir_rule = text

        # 3. Excluded exercise / movement constraints
        exc_match = re.search(
            r"(?:(?:bad\s+\w+|injury|rehab)\s*[-—–:]\s*)?no\s+([a-zA-Z\s\-]+?)(?:\s*\((?:bad|protect|use|toe)[^\)]*\)|\s*[-—–]|\.|$)",
            text,
            re.IGNORECASE,
        )
        if not exc_match:
            exc_match = re.search(
                r"avoid\s+([a-zA-Z\s\-]+?)(?:\s*\([^\)]*\)|\s*[-—–]|\.|$)",
                text,
                re.IGNORECASE,
            )
        if exc_match:
            cand = _clean_exercise_token(exc_match.group(1))
            # Ignore false positives like "no reps under 5", "no sets to failure"
            if cand and not any(k in cand for k in ("rep", "set", "failure", "fatigue", "momentum", "stomach")):
                rem_match = re.search(
                    r"(?:use|substitute\s+with|replace\s+with)\s+([^.]+\.?)",
                    text,
                    re.IGNORECASE,
                )
                custom_rem = rem_match.group(0).strip() if rem_match else _default_remediation_for(cand)
                _add_excluded(
                    result,
                    ExcludedExerciseRule(
                        pattern=cand,
                        rule_text=text,
                        remediation=custom_rem,
                    ),
                )

        if "stomach vacuum" in text.lower():
            _add_excluded(
                result,
                ExcludedExerciseRule(
                    pattern="stomach vacuum",
                    rule_text=text,
                    remediation="Train abs for mass with progressive overload.",
                ),
            )

    return result


def _exercise_matches_exclusion(exercise_name: str, pattern: str) -> bool:
    """Check whether an exercise title matches a banned exercise pattern."""
    ename = exercise_name.lower().strip()
    pat = pattern.lower().strip()

    # Direct substring matches
    if pat in ename:
        return True

    # Normalize plurals: e.g. "squats" vs "squat", "presses" vs "press", "curls" vs "curl"
    pat_singular = re.sub(r"(?:es|s)$", "", pat)
    ename_singular = re.sub(r"(?:es|s)$", "", ename)
    if pat_singular and pat_singular in ename_singular:
        return True

    # Word stem match: e.g. "overhead pressing" vs "overhead press"
    pat_stem = pat.replace("pressing", "press").replace("jumping", "jump")
    ename_stem = ename.replace("pressing", "press").replace("jumping", "jump")
    return pat_stem in ename_stem


def validate_programme(
    programme: Any,
    profile_rules: Any = None,
    training_rules: Any = None,
) -> ValidationResult:
    """Validate a programme definition, preview, or routines against profile constraints."""
    parsed = parse_profile_rules(profile_rules)
    if training_rules is not None:
        parsed = _merge_profile_constraints(parsed, parse_profile_rules(training_rules))
    violations: list[BlockingViolation] = []
    warnings: list[str] = list(parsed.warnings)

    has_constraint_checks = any(
        value is not None
        for value in (
            parsed.min_reps,
            parsed.max_reps,
            parsed.min_rir,
            parsed.max_rir,
            parsed.max_rpe,
            parsed.max_intensity_pct,
            parsed.max_sets_per_exercise,
            parsed.max_sets_per_workout,
            parsed.max_sessions_per_week,
        )
    ) or bool(
        parsed.excluded_rules
        or parsed.substitution_rules
        or parsed.prohibited_equipment
        or parsed.available_equipment
    )

    if not has_constraint_checks:
        return ValidationResult(
            is_valid=True,
            blocking_violations=[],
            warnings=warnings,
        )

    def check_exercise(
        name: str,
        prescriptions: list[dict[str, Any]],
        routine_name: str | None = None,
        equipment: Any = None,
    ) -> None:
        # 1. Check excluded exercises
        for rule in parsed.excluded_rules:
            if _exercise_matches_exclusion(name, rule.pattern):
                violations.append(
                    BlockingViolation(
                        exercise=name,
                        prescription=f"Prescribed in routine '{routine_name or 'Current routine'}'",
                        rule=rule.rule_text,
                        permitted_remediation=rule.remediation,
                        routine=routine_name,
                    )
                )

        # 2. Mandatory substitutions must be applied before activation.
        for rule in parsed.substitution_rules:
            if _exercise_matches_exclusion(name, rule.pattern):
                violations.append(
                    BlockingViolation(
                        exercise=name,
                        prescription=(
                            f"Prescribed in routine '{routine_name or 'Current routine'}'"
                        ),
                        rule=rule.rule_text,
                        permitted_remediation=rule.remediation,
                        routine=routine_name,
                    )
                )
                warnings.append(
                    f"{rule.rule_text}. Permitted remediation: {rule.remediation}"
                )

        # 3. Equipment availability/prohibition
        if equipment:
            cleaned_equipment = _clean_equipment(equipment)
            if cleaned_equipment and cleaned_equipment in parsed.prohibited_equipment:
                violations.append(
                    BlockingViolation(
                        exercise=name,
                        prescription=f"Equipment: {equipment}",
                        rule=f"Prohibited equipment: {equipment}",
                        permitted_remediation=(
                            f"Replace '{name}' with an exercise that does not require "
                            f"{equipment}."
                        ),
                        routine=routine_name,
                    )
                )
            elif (
                cleaned_equipment
                and parsed.available_equipment
                and cleaned_equipment not in parsed.available_equipment
            ):
                warnings.append(
                    f"{name} uses equipment '{equipment}', which is not in the "
                    "profile's available equipment list. Review before activation."
                )

        # 4. Check rep ranges and intensity for each prescription
        for p in prescriptions:
            block_num = p.get("block_number")
            block_prefix = f"Block {block_num}: " if block_num else ""

            # Rep range validation
            rep_str = str(p.get("rep_range") or "").strip()
            if rep_str:
                rep_min, rep_max = parse_rep_range(rep_str)
                if rep_min is not None and parsed.min_reps is not None and rep_min < parsed.min_reps:
                    rule_desc = parsed.min_reps_rule or f"min_reps: {parsed.min_reps}"
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}{rep_str} reps",
                            rule=rule_desc,
                            permitted_remediation=(
                                f"Increase prescribed rep range to at least {parsed.min_reps} reps, "
                                "or select a goal with higher repetition ranges (such as Hypertrophy or General Fitness)."
                            ),
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )
                if rep_max is not None and parsed.max_reps is not None and rep_max > parsed.max_reps:
                    rule_desc = parsed.max_reps_rule or f"max_reps: {parsed.max_reps}"
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}{rep_str} reps",
                            rule=rule_desc,
                            permitted_remediation=f"Reduce prescribed rep range to at most {parsed.max_reps} reps.",
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )

            # Intensity (target RIR) validation
            rir_val = p.get("target_rir")
            if rir_val is not None and parsed.min_rir is not None:
                rir_min, _ = parse_rir_range(rir_val)
                if rir_min is not None and rir_min < parsed.min_rir:
                    rule_desc = parsed.min_rir_rule or f"min_rir: {parsed.min_rir:g}"
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}Target RIR {rir_val}",
                            rule=rule_desc,
                            permitted_remediation=(
                                f"Adjust target RIR to at least {parsed.min_rir:g} RIR "
                                "or select a goal with moderate intensity."
                            ),
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )

            # Intensity (target RPE) validation
            rpe_val = p.get("target_rpe")
            if rpe_val is not None and parsed.max_rpe is not None:
                rpe_min, rpe_max = parse_rir_range(rpe_val)
                rpe_value = rpe_max if rpe_max is not None else rpe_min
                if rpe_value is not None and rpe_value > parsed.max_rpe:
                    rule_desc = parsed.max_rpe_rule or f"max_rpe: {parsed.max_rpe:g}"
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}Target RPE {rpe_val}",
                            rule=rule_desc,
                            permitted_remediation=(
                                f"Reduce target RPE to at most {parsed.max_rpe:g}."
                            ),
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )

            # Intensity (percent of max) validation
            intensity_val = p.get("target_intensity_pct") or p.get("intensity_pct")
            if intensity_val is not None and parsed.max_intensity_pct is not None:
                try:
                    intensity = float(str(intensity_val).rstrip("%").strip())
                except (TypeError, ValueError):
                    intensity = None
                if intensity is not None and intensity > parsed.max_intensity_pct:
                    rule_desc = parsed.max_intensity_pct_rule or (
                        f"max_intensity_pct: {parsed.max_intensity_pct:g}"
                    )
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}Target intensity {intensity_val}%",
                            rule=rule_desc,
                            permitted_remediation=(
                                "Reduce prescribed intensity to at most "
                                f"{parsed.max_intensity_pct:g}% of maximum."
                            ),
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )

            # Volume (sets per exercise) validation
            if parsed.max_sets_per_exercise is not None:
                raw_sets = p.get("sets")
                try:
                    sets = int(raw_sets) if raw_sets is not None else None
                except (TypeError, ValueError):
                    sets = None
                if sets is not None and sets > parsed.max_sets_per_exercise:
                    rule_desc = parsed.max_sets_per_exercise_rule or (
                        f"max_sets_per_exercise: {parsed.max_sets_per_exercise}"
                    )
                    violations.append(
                        BlockingViolation(
                            exercise=name,
                            prescription=f"{block_prefix}{sets} sets",
                            rule=rule_desc,
                            permitted_remediation=(
                                "Reduce prescribed sets to at most "
                                f"{parsed.max_sets_per_exercise} per exercise."
                            ),
                            block_number=block_num,
                            routine=routine_name,
                        )
                    )

    def check_session_count(session_count: int, routine_name: str | None = None) -> None:
        if (
            parsed.max_sessions_per_week is not None
            and session_count > parsed.max_sessions_per_week
        ):
            rule_desc = parsed.max_sessions_per_week_rule or (
                f"max_sessions_per_week: {parsed.max_sessions_per_week}"
            )
            violations.append(
                BlockingViolation(
                    exercise="Programme",
                    prescription=f"{session_count} sessions per week",
                    rule=rule_desc,
                    permitted_remediation=(
                        "Reduce planned sessions to at most "
                        f"{parsed.max_sessions_per_week} per week."
                    ),
                    routine=routine_name,
                )
            )

    def check_workout_volume(total_sets: int, routine_name: str | None) -> None:
        if (
            parsed.max_sets_per_workout is not None
            and total_sets > parsed.max_sets_per_workout
        ):
            rule_desc = parsed.max_sets_per_workout_rule or (
                f"max_sets_per_workout: {parsed.max_sets_per_workout}"
            )
            violations.append(
                BlockingViolation(
                    exercise="Programme day",
                    prescription=f"{routine_name or 'Workout'}: {total_sets} total sets",
                    rule=rule_desc,
                    permitted_remediation=(
                        "Reduce total prescribed sets for this workout to at most "
                        f"{parsed.max_sets_per_workout}."
                    ),
                    routine=routine_name,
                )
            )

    def prescription_sets(prescriptions: list[dict[str, Any]]) -> int:
        total = 0
        for prescription in prescriptions:
            try:
                total += int(prescription.get("sets") or 0)
            except (TypeError, ValueError):
                continue
        return total

    # Dispatch based on structure of `programme`
    if isinstance(programme, dict):
        # Case A: Preview or Active Programme with "days"
        days = programme.get("days") or []
        for day in days:
            routine_focus = day.get("focus") or day.get("title") or f"Day {day.get('number', '')}"
            day_sets = 0
            for ex in day.get("exercises") or []:
                ex_name = ex.get("name") or ex.get("title") or "Exercise"
                ex_prescriptions = ex.get("prescriptions") or []
                if not ex_prescriptions:
                    ex_prescriptions = [{
                        "rep_range": ex.get("rep_range"),
                        "target_rir": ex.get("target_rir"),
                        "sets": ex.get("sets"),
                    }]
                day_sets += prescription_sets(ex_prescriptions)
                check_exercise(
                    ex_name,
                    ex_prescriptions,
                    routine_focus,
                    ex.get("equipment"),
                )
            check_workout_volume(day_sets, routine_focus)
        if days:
            check_session_count(len(days))

        # Also inspect source_snapshots if present and days were empty
        if not days and programme.get("source_snapshots"):
            snapshots = programme.get("source_snapshots") or []
            for snapshot in snapshots:
                s_focus = snapshot.get("title") or snapshot.get("id")
                for ex in snapshot.get("exercises") or []:
                    ex_name = ex.get("title") or ex.get("name") or "Exercise"
                    check_exercise(ex_name, [], s_focus, ex.get("equipment"))
            check_session_count(len(snapshots))

        # Inspect routines if present (e.g. Hevy source payload)
        if not days and programme.get("routines"):
            routines = programme.get("routines") or []
            for routine in routines:
                r_name = routine.get("title") or routine.get("id")
                for ex in routine.get("exercises") or []:
                    ex_name = ex.get("title") or ex.get("name") or "Exercise"
                    ex_p: list[dict[str, Any]] = []
                    if ex.get("target_reps"):
                        ex_p.append({"rep_range": str(ex["target_reps"])})
                    elif ex.get("target_rep_range"):
                        rng = ex["target_rep_range"]
                        if isinstance(rng, list) and len(rng) == 2:
                            ex_p.append({"rep_range": f"{rng[0]}-{rng[1]}"})
                    check_exercise(ex_name, ex_p, r_name, ex.get("equipment"))
            check_session_count(len(routines))

    elif hasattr(programme, "days"):
        # Case B: InferredProgramme or object with days attribute
        for day in programme.days:
            r_title = getattr(day, "routine_title", "") or getattr(day, "focus", "")
            exercises = getattr(day, "exercises", [])
            for ex in exercises:
                ex_name = ex.name if hasattr(ex, "name") else str(ex)
                prescriptions: list[dict[str, Any]] = []
                if hasattr(ex, "rep_range"):
                    prescriptions.append({"rep_range": ex.rep_range})
                check_exercise(
                    ex_name,
                    prescriptions,
                    r_title,
                    getattr(ex, "equipment", None),
                )

    elif isinstance(programme, list):
        # Case C: list of routines or exercises
        for item in programme:
            if hasattr(item, "exercises"):
                r_title = getattr(item, "title", "Routine")
                for ex in item.exercises:
                    ex_name = getattr(ex, "title", getattr(ex, "name", str(ex)))
                    ex_p = []
                    if hasattr(ex, "rep_range"):
                        ex_p.append({"rep_range": ex.rep_range})
                    elif hasattr(ex, "target_reps") and ex.target_reps:
                        ex_p.append({"rep_range": str(ex.target_reps)})
                    check_exercise(
                        ex_name,
                        ex_p,
                        r_title,
                        getattr(ex, "equipment", None),
                    )
            elif hasattr(item, "name"):
                ex_name = item.name
                ex_p = []
                if hasattr(item, "rep_range"):
                    ex_p.append({"rep_range": item.rep_range})
                check_exercise(
                    ex_name,
                    ex_p,
                    None,
                    getattr(item, "equipment", None),
                )

    serialized_violations = [v.model_dump() for v in violations]
    if serialized_violations:
        warnings.append(
            f"{len(serialized_violations)} blocking constraint violation(s) prevent activation."
        )

    deduped_warnings = list(dict.fromkeys(warnings))
    return ValidationResult(
        is_valid=len(serialized_violations) == 0,
        blocking_violations=serialized_violations,
        warnings=deduped_warnings,
    )


def assert_programme_activatable(
    programme: Any,
    profile_rules: Any = None,
    training_rules: Any = None,
) -> None:
    """Raise ValueError if the programme violates declared profile constraints."""
    result = validate_programme(
        programme,
        profile_rules=profile_rules,
        training_rules=training_rules,
    )
    if not result.is_valid:
        first = result.blocking_violations[0]
        raise ValueError(
            f"Activation blocked by profile constraint: {first['exercise']} "
            f"({first['prescription']}) violates '{first['rule']}'. "
            f"Permitted remediation: {first['permitted_remediation']}"
        )
