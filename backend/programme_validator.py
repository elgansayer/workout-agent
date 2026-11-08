"""Deterministic programme validator enforcing profile constraints.

Runs across the programme lifecycle: create, import, edit, inference,
selection, and activation. Blocking violations prevent activation and
identify the exact exercise, prescription, rule, and permitted remediation.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


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


class ProfileConstraints(BaseModel):
    """Structured constraints extracted from user preferences or request payload."""

    min_reps: int | None = None
    min_reps_rule: str | None = None
    max_reps: int | None = None
    max_reps_rule: str | None = None
    min_rir: float | None = None
    min_rir_rule: str | None = None
    max_rir: float | None = None
    max_rir_rule: str | None = None
    excluded_rules: list[ExcludedExerciseRule] = Field(default_factory=list)
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


def parse_profile_rules(
    constraints: Any,
    custom_rules: Any = None,
) -> ProfileConstraints:
    """Parse structured constraints or free-text profile rules."""
    result = ProfileConstraints()
    raw_list: list[Any] = []

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
                result.excluded_rules.append(
                    ExcludedExerciseRule(
                        pattern=pat,
                        rule_text=f"Exclude {pat}",
                        remediation=_default_remediation_for(pat),
                    )
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
            sub = parse_profile_rules(item)
            if sub.min_reps is not None and result.min_reps is None:
                result.min_reps = sub.min_reps
                result.min_reps_rule = sub.min_reps_rule
            if sub.max_reps is not None and result.max_reps is None:
                result.max_reps = sub.max_reps
                result.max_reps_rule = sub.max_reps_rule
            if sub.min_rir is not None and result.min_rir is None:
                result.min_rir = sub.min_rir
                result.min_rir_rule = sub.min_rir_rule
            result.excluded_rules.extend(sub.excluded_rules)
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
                result.excluded_rules.append(
                    ExcludedExerciseRule(
                        pattern=cand,
                        rule_text=text,
                        remediation=custom_rem,
                    )
                )

        if "stomach vacuum" in text.lower():
            result.excluded_rules.append(
                ExcludedExerciseRule(
                    pattern="stomach vacuum",
                    rule_text=text,
                    remediation="Train abs for mass with progressive overload.",
                )
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
) -> ValidationResult:
    """Validate a programme definition, preview, or routines against profile constraints."""
    parsed = parse_profile_rules(profile_rules)
    violations: list[BlockingViolation] = []

    # If no constraints are declared, the programme trivially passes.
    if (
        parsed.min_reps is None
        and parsed.max_reps is None
        and parsed.min_rir is None
        and parsed.max_rir is None
        and not parsed.excluded_rules
    ):
        return ValidationResult(is_valid=True, blocking_violations=[], warnings=[])

    def check_exercise(
        name: str,
        prescriptions: list[dict[str, Any]],
        routine_name: str | None = None,
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

        # 2. Check rep ranges and intensity for each prescription
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

    # Dispatch based on structure of `programme`
    if isinstance(programme, dict):
        # Case A: Preview or Active Programme with "days"
        days = programme.get("days") or []
        for day in days:
            routine_focus = day.get("focus") or day.get("title") or f"Day {day.get('number', '')}"
            for ex in day.get("exercises") or []:
                ex_name = ex.get("name") or ex.get("title") or "Exercise"
                ex_prescriptions = ex.get("prescriptions") or []
                if not ex_prescriptions:
                    ex_prescriptions = [{
                        "rep_range": ex.get("rep_range"),
                        "target_rir": ex.get("target_rir"),
                        "sets": ex.get("sets"),
                    }]
                check_exercise(ex_name, ex_prescriptions, routine_focus)

        # Also inspect source_snapshots if present and days were empty
        if not days and programme.get("source_snapshots"):
            for snapshot in programme.get("source_snapshots") or []:
                s_focus = snapshot.get("title") or snapshot.get("id")
                for ex in snapshot.get("exercises") or []:
                    ex_name = ex.get("title") or ex.get("name") or "Exercise"
                    check_exercise(ex_name, [], s_focus)

        # Inspect routines if present (e.g. Hevy source payload)
        if not days and programme.get("routines"):
            for routine in programme.get("routines") or []:
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
                    check_exercise(ex_name, ex_p, r_name)

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
                check_exercise(ex_name, prescriptions, r_title)

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
                    check_exercise(ex_name, ex_p, r_title)
            elif hasattr(item, "name"):
                ex_name = item.name
                ex_p = []
                if hasattr(item, "rep_range"):
                    ex_p.append({"rep_range": item.rep_range})
                check_exercise(ex_name, ex_p, None)

    serialized_violations = [v.model_dump() for v in violations]
    warnings = []
    if serialized_violations:
        warnings.append(
            f"{len(serialized_violations)} blocking constraint violation(s) prevent activation."
        )

    return ValidationResult(
        is_valid=len(serialized_violations) == 0,
        blocking_violations=serialized_violations,
        warnings=warnings,
    )


def assert_programme_activatable(
    programme: Any,
    profile_rules: Any = None,
) -> None:
    """Raise ValueError if the programme violates declared profile constraints."""
    result = validate_programme(programme, profile_rules=profile_rules)
    if not result.is_valid:
        first = result.blocking_violations[0]
        raise ValueError(
            f"Activation blocked by profile constraint: {first['exercise']} "
            f"({first['prescription']}) violates '{first['rule']}'. "
            f"Permitted remediation: {first['permitted_remediation']}"
        )
