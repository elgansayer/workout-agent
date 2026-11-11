"""Uses the user's preferred AI provider to explain a validated prescription.

The deterministic programme runtime is the source of truth.  The model may only
explain, summarise, format, or answer questions about that validated output; it
must never invent or replace a set, rep, load, exercise, or constraint.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ai_provider import AIProvider
from hevy_parser import WorkoutSummary
from insights import TrainingInsights
from localisation import format_weight, load_step, prompt_context, spelling_for
from programme_runtime import ProgrammeRuntime
from training_rules import (
    StructuredTrainingRules,
    enforce_rules_on_plan_text,
    format_rules_for_prompt,
)
from provenance import GeneratedText
from user_settings import UserSettings

logger = logging.getLogger(__name__)


def _regional_context(settings: UserSettings) -> str:
    """The user's time zone, locale and unit instructions for any prompt."""
    return prompt_context(settings.timezone, settings.locale, settings.unit_system)


def _format_history(
    history: dict[str, dict[str, Any]] | None,
    unit_system: str = UserSettings().unit_system,
) -> str:
    if not history:
        return "None on record yet."
    lines = []
    for name in sorted(history):
        record = history[name]
        weight = record.get("top_weight_kg")
        reps = record.get("top_reps")
        if weight is not None and reps is not None:
            lines.append(f"- {name}: {format_weight(weight, unit_system)} x {reps}")
        elif reps is not None:
            lines.append(f"- {name}: {reps} reps (bodyweight)")
    return "\n".join(lines) if lines else "None on record yet."


def _programme_header(runtime: ProgrammeRuntime) -> str:
    name = runtime.name or "the active programme"
    goal = f" Goal: {runtime.goal}." if runtime.goal else ""
    dates = ""
    if runtime.start_date and runtime.end_date:
        dates = (
            f" Dates: {runtime.start_date.isoformat()} to "
            f"{runtime.end_date.isoformat()}."
        )
    block = ""
    if runtime.block:
        block = (
            f" Current block: {runtime.block.get('name', '')}"
            f" (weeks {runtime.block.get('start_week')}-"
            f"{runtime.block.get('end_week')})."
        )
    return (
        f"Programme: {name}.{goal} Week {runtime.week} of "
        f"{runtime.cycle_weeks}.{block}{dates}"
    )


def _exercise_lines(day: dict[str, Any]) -> list[str]:
    lines = []
    for exercise in day.get("exercises") or []:
        name = str(exercise.get("name") or "Exercise")
        scheme = str(
            exercise.get("scheme")
            or f"{exercise.get('sets', '')} x {exercise.get('rep_range', '')}"
        ).strip()
        note = str(exercise.get("note") or "").strip()
        lines.append(f"{name}: {scheme}" + (f" ({note})" if note else ""))
    return lines


def _baseline_plan(runtime: ProgrammeRuntime, day: dict[str, Any]) -> str:
    focus = str(day.get("focus") or "Training")
    lines = [f"{focus} - Week {runtime.week}"]
    lines.extend(_exercise_lines(day))
    return "\n".join(lines)


def _build_prompt(
    runtime: ProgrammeRuntime,
    day: dict[str, Any],
    workout_summary: WorkoutSummary | None,
    recovery: dict[str, Any] | None,
    history: dict[str, dict[str, Any]] | None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
) -> str:
    settings = settings or UserSettings()
    focus = str(day.get("focus") or "Training")
    baseline = _baseline_plan(runtime, day)
    structured_rules_str = format_rules_for_prompt(rules)
    workout_text = (
        workout_summary.as_text(settings.unit_system)
        if workout_summary
        else "None available."
    )
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."
    history_text = _format_history(history, settings.unit_system)
    insights_text = insights.as_text() if insights else "Not enough history yet."
    last_plan_text = last_plan if last_plan else "None available."

    return f"""You are the user's training coach. Explain and format the validated prescription below.

{_programme_header(runtime)}

Today's routine: {focus}.

The deterministic prescription is the source of truth. Do NOT add, remove, swap,
re-order, or change any exercise, set, rep target, load, tempo, or rest period.
Do not infer a load that is not in the prescription. If you mention an
adjustment, it must already be present in the prescription or the structured
rules.

{_regional_context(settings)}

{structured_rules_str}

Validated prescription:
{baseline}

The plan shown last session:
{last_plan_text}

Most recent logged session (actual execution, for context only):
{workout_text}

Per-exercise bests on record:
{history_text}

Recovery metrics (optional; may be absent or stale):
{recovery_json}

Recent trend context:
{insights_text}

Your task:
- Output the validated prescription in the same order, with the same exercises,
  sets, and rep targets.
- You may add a short parenthetical note only when it explains an existing
  prescribed adjustment or a structured-rule substitution.
- Act on the recovery and trend context only to the extent allowed by the
  structured rules; never invent extra volume or load.

Strict output format:
- Plain text only. No markdown, no asterisks, no bold, no headings.
- First line exactly: "{focus} - Week {runtime.week}".
- Then one exercise per line as "Name: sets x reps".
- Add a short note in round brackets ONLY for an already prescribed adjustment,
  e.g. "(+{load_step(settings.unit_system)})" or "(+1 rep)".
- No intro, no assessment, no motivation, no tips, no closing line.

Use {spelling_for(settings.locale)}. Never use the em dash."""


def generate_next_workout(
    provider: AIProvider,
    *,
    runtime: ProgrammeRuntime,
    day: dict[str, Any],
    workout_summary: WorkoutSummary | None = None,
    recovery: dict[str, Any] | None = None,
    history: dict[str, dict[str, Any]] | None = None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
    **_kwargs: Any,
) -> str:
    """Format the deterministic prescription, falling back to the baseline."""
    effective_rules = rules or _kwargs.get("rules")
    rules_obj = (
        StructuredTrainingRules.from_raw(effective_rules)
        if effective_rules
        else None
    )

    try:
        prompt = _build_prompt(
            runtime,
            day,
            workout_summary,
            recovery,
            history,
            insights,
            last_plan,
            rules=rules_obj,
            settings=settings,
        )
        text = str(provider.generate(prompt)).strip()
        if text:
            baseline = _baseline_plan(runtime, day)
            if _prescription_preserved(baseline, text) and rules_obj:
                enforced, _ = enforce_rules_on_plan_text(
                    text, rules_obj, recovery=recovery
                )
                return GeneratedText.from_provider(enforced, provider)
            if _prescription_preserved(baseline, text):
                return GeneratedText.from_provider(text, provider)
            logger.warning(
                "AI output changed the validated prescription; using baseline."
            )
        else:
            logger.warning("AI provider returned an empty response; using baseline plan.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI generation failed (%s); using baseline plan.", exc)

    fallback = _baseline_plan(runtime, day)
    if rules_obj:
        fallback, _ = enforce_rules_on_plan_text(
            fallback, rules_obj, recovery=recovery
        )
    return GeneratedText.fallback(fallback)


def _prescription_preserved(baseline: str, candidate: str) -> bool:
    """Reject generated prose that drops or rewrites a prescribed exercise/scheme."""

    def parse(text: str) -> list[tuple[str, str]]:
        parsed: list[tuple[str, str]] = []
        for line in text.splitlines()[1:]:
            if ":" not in line:
                continue
            name, scheme = line.split(":", 1)
            scheme = scheme.split("(", 1)[0].strip()
            scheme = " ".join(scheme.replace("×", "x").lower().split())
            parsed.append((name.strip().lower(), scheme))
        return parsed

    expected = parse(baseline)
    actual = parse(candidate)
    return bool(expected) and actual == expected


def _fallback_plan(runtime: ProgrammeRuntime, day: dict[str, Any]) -> str:
    return _baseline_plan(runtime, day)


baseline_plan = _baseline_plan


def _build_rest_prompt(
    runtime: ProgrammeRuntime,
    recovery: dict[str, Any] | None,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
) -> str:
    settings = settings or UserSettings()
    structured_rules_str = format_rules_for_prompt(rules)
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."

    return f"""You are the user's training coach.

{_programme_header(runtime)}

{_regional_context(settings)}

Today has no prescribed training exposure in the active programme, so this is a
recovery day. Do not invent a workout.

{structured_rules_str}

Recovery metrics (optional; may be absent or stale):
{recovery_json}

Your task:
1. Briefly affirm that recovery supports the programme.
2. Give two or three short, specific recovery actions, drawing on the recovery
   metrics only where they are present and fresh.
3. Keep it short and practical.

Use {spelling_for(settings.locale)}. Never use the em dash."""


def generate_rest_day_message(
    provider: AIProvider,
    runtime: ProgrammeRuntime | None = None,
    recovery: dict[str, Any] | None = None,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
    **_kwargs: Any,
) -> str:
    """Generate a short recovery message, falling back on error."""
    effective_rules = rules or _kwargs.get("rules")
    try:
        runtime = runtime or _kwargs.get("runtime")
        if runtime is None:
            return GeneratedText.fallback(_fallback_rest_message())
        prompt = _build_rest_prompt(
            runtime, recovery, rules=effective_rules, settings=settings
        )
        text = str(provider.generate(prompt)).strip()
        if text:
            return GeneratedText.from_provider(text, provider)
        logger.warning(
            "AI provider returned an empty rest-day response; using fallback."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI rest-day generation failed (%s); using fallback.", exc)

    return GeneratedText.fallback(_fallback_rest_message())


def _fallback_rest_message() -> str:
    return (
        "Today is a rest day with no prescribed training. Recovery supports the programme.\n\n"
        "- Prioritise sleep tonight.\n"
        "- Stay hydrated and eat enough protein.\n"
        "- Keep movement gentle: light mobility or an easy walk.\n"
    )


def _build_checkin_prompt(
    runtime: ProgrammeRuntime,
    number: int,
    workouts_done: int,
    weeks: int,
    analysis_text: str,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
) -> str:
    settings = settings or UserSettings()
    structured_rules_str = format_rules_for_prompt(rules)
    return f"""You are the user's training coach.

{_programme_header(runtime)}

{_regional_context(settings)}

This is programme check-in number {number}. The user logged {workouts_done}
sessions over the last {weeks} weeks.

{structured_rules_str}

Planned versus actual materialised prescriptions:
{analysis_text}

Your task: write a short, factual check-in.
- Plain text only. No markdown, no asterisks, no headings.
- First line exactly: "Check-in {number}".
- Then 3 to 5 short lines: what is progressing, what has stalled, and where the
  user is versus the plan. Reference the real numbers.
- Then 1 to 3 concrete adjustments for the next stretch that are already
  consistent with the structured programme and adaptation decisions.
- Never invent exercises, loads, or programme changes.
- End with one short line of encouragement.

Use {spelling_for(settings.locale)}. Never use the em dash."""


def generate_checkin_message(
    provider: AIProvider,
    *,
    runtime: ProgrammeRuntime,
    number: int,
    workouts_done: int,
    weeks: int,
    analysis_text: str,
    fallback: str,
    rules: StructuredTrainingRules | dict[str, Any] | None = None,
    settings: UserSettings | None = None,
    **_kwargs: Any,
) -> str:
    """Generate a periodic check-in message, falling back on error."""
    effective_rules = rules or _kwargs.get("rules")
    try:
        prompt = _build_checkin_prompt(
            runtime,
            number,
            workouts_done,
            weeks,
            analysis_text,
            rules=effective_rules,
            settings=settings,
        )
        text = str(provider.generate(prompt)).strip()
        if text:
            return GeneratedText.from_provider(text, provider)
        logger.warning("AI provider returned an empty check-in; using fallback.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI check-in generation failed (%s); using fallback.", exc)

    return GeneratedText.fallback(fallback)


__all__ = [
    "_build_checkin_prompt",
    "_build_prompt",
    "_build_rest_prompt",
    "_fallback_plan",
    "_fallback_rest_message",
    "_format_history",
    "baseline_plan",
    "generate_checkin_message",
    "generate_next_workout",
    "generate_rest_day_message",
]
