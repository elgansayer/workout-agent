"""Uses the user's preferred AI provider to apply progressive overload and
write today's plan from the active Hevy-native programme."""

from __future__ import annotations

import json
import logging
from typing import Any

from ai_provider import AIProvider
from hevy_parser import WorkoutSummary
from insights import TrainingInsights

logger = logging.getLogger(__name__)


def _format_history(history: dict[str, dict[str, Any]] | None) -> str:
    if not history:
        return "None on record yet."
    lines = []
    for name in sorted(history):
        record = history[name]
        weight = record.get("top_weight_kg")
        reps = record.get("top_reps")
        if weight is not None and reps is not None:
            lines.append(f"- {name}: {weight:g} kg x {reps}")
        elif reps is not None:
            lines.append(f"- {name}: {reps} reps (bodyweight)")
    return "\n".join(lines) if lines else "None on record yet."


def _day_focus(day: dict[str, Any]) -> str:
    return str(day.get("focus") or "Training")


def _day_exercises(day: dict[str, Any]) -> list[dict[str, Any]]:
    exercises = day.get("exercises")
    return exercises if isinstance(exercises, list) else []


def _baseline_lines(day: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for exercise in _day_exercises(day):
        name = exercise.get("name")
        if not name:
            continue
        scheme = exercise.get("scheme")
        if not scheme:
            scheme = f"{exercise.get('sets', '?')} x {exercise.get('rep_range', '?')}"
        lines.append(f"{name}: {scheme}")
    return lines


def _build_prompt(
    day: dict[str, Any],
    week: int,
    cycle_weeks: int,
    programme_name: str,
    rules: list[str],
    workout_summary: WorkoutSummary | None,
    recovery: dict[str, Any] | None,
    history: dict[str, dict[str, Any]] | None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
) -> str:
    focus = _day_focus(day)
    baseline = "\n".join(_baseline_lines(day)) or "No exercises are prescribed."
    rules_text = "\n".join(f"- {rule}" for rule in rules) or "- None recorded."
    workout_text = workout_summary.as_text() if workout_summary else "None available."
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."
    history_text = _format_history(history)
    insights_text = insights.as_text() if insights else "Not enough history yet."
    last_plan_text = last_plan if last_plan else "None available."

    return f"""You are an experienced strength and conditioning coach.

The active programme is "{programme_name}". Today is Week {week} of
{cycle_weeks}, and the selected routine for today is "{focus}".

Programme rules you must always respect:
{rules_text}

The baseline prescription for today (already block-adjusted) is:
{baseline}

The PLANNED routine you generated for the last session:
{last_plan_text}

Compact summary of the most recent logged Hevy session (EXECUTED routine):
{workout_text}

Per-exercise bests on record (most recent logged top set for each lift):
{history_text}

Recovery metrics from Health Connect/Fitbit (sleep, bodyweight, resting heart rate, hrv):
{recovery_json}

Self-review of how the coaching is actually working (trends across recent
sessions, stalls, and recovery direction). Treat this as your memory and act on
it:
{insights_text}

Your task:
Compare the EXECUTED routine against the PLANNED routine and identify any manual adjustments the user made. Apply the following rules when generating the next workout:
- Exercise Swaps: If an exercise was substituted, assume this was intentional (due to equipment availability, injury, or preference). Update the master template to use this new exercise for the remainder of the current block. Do not revert to the original movement.
- Volume and RPE Overrides: If the user completed fewer sets or dropped the load significantly below the plan, treat this as a manual auto-regulation for fatigue. Maintain this lower volume baseline for the next session before attempting progressive overload again.
- Exercise Reordering: If the order of movements was changed, respect the new sequence for future routines.
- Added Exercises: If the user added new accessory work, append it to the routine template for this specific training day.
- Recovery Metrics: If recovery metrics (e.g., HRV or resting HR) drop below baseline average, auto-suggest a lighter variation or force a deload day.

Other rules:
- Keep the routines and exercises that the user selected in Hevy in today's plan unless they were overridden or recovery is poor.
- Apply progressive overload from the Hevy data: where the last session hit the
  top of a rep range with good form, add a rep or a small load increase; where
  recovery looks poor (short sleep, elevated resting heart rate, dropped HRV), trim volume
  slightly.
- Act on the self-review: for any lift flagged as stalling or regressing, apply
  its suggested intervention (deload, change rep range, or swap the movement)
  rather than blindly adding load. Follow the recovery directive for overall
  volume today.
- Output ONLY today's workout, nothing else.

Strict output format (this is read on a phone push notification, keep it minimal):
- Plain text only. No markdown, no asterisks, no bold, no headings.
- First line exactly: "{focus} - Week {week}".
- Then one exercise per line, in the form "Name: sets x reps".
- Add a short note in round brackets ONLY when you change something from the
  baseline, e.g. "(+2.5 kg)" or "(+1 rep)". Otherwise add no note.
- No intro, no assessment, no motivation, no tips, no closing line.

Use British English. Never use the em dash."""


def generate_next_workout(
    provider: AIProvider,
    day: dict[str, Any],
    week: int,
    cycle_weeks: int,
    programme_name: str,
    rules: list[str],
    *,
    workout_summary: WorkoutSummary | None = None,
    recovery: dict[str, Any] | None = None,
    history: dict[str, dict[str, Any]] | None = None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
    **_kwargs: Any,
) -> str:
    """Generate today's plan, falling back to the baseline plan on error.

    Args:
        provider: A resolved AI provider instance from
            :func:`ai_resolver.resolve_provider`.
    """
    try:
        prompt = _build_prompt(
            day,
            week,
            cycle_weeks,
            programme_name,
            rules,
            workout_summary,
            recovery,
            history,
            insights,
            last_plan,
        )
        text = str(provider.generate(prompt)).strip()
        if text:
            return text
        logger.warning("AI provider returned an empty response; using baseline plan.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI generation failed (%s); using baseline plan.", exc)

    return _fallback_plan(day, week)


def _fallback_plan(day: dict[str, Any], week: int) -> str:
    lines = [f"{_day_focus(day)} - Week {week}"]
    lines.extend(_baseline_lines(day))
    return "\n".join(lines)


def _build_rest_prompt(recovery: dict[str, Any] | None) -> str:
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."

    return f"""You are an experienced strength and conditioning coach.

Today is a scheduled rest day.

Recovery metrics from Health Connect (sleep, bodyweight, resting heart rate):
{recovery_json}

Your task:
1. Briefly reassure the user that recovery is where adaptation happens.
2. Give two or three short, specific recovery actions for today, drawing on the
   recovery metrics if available (sleep, light mobility, hydration, hitting the
   protein target, gentle movement only).
3. Keep it short and motivating.

Use British English. Never use the em dash."""


def generate_rest_day_message(
    provider: AIProvider,
    recovery: dict[str, Any] | None = None,
    **_kwargs: Any,
) -> str:
    """Generate a short rest-day recovery message, falling back on error.

    Args:
        provider: A resolved AI provider instance from
            :func:`ai_resolver.resolve_provider`.
    """
    try:
        prompt = _build_rest_prompt(recovery)
        text = str(provider.generate(prompt)).strip()
        if text:
            return text
        logger.warning(
            "AI provider returned an empty rest-day response; using fallback."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI rest-day generation failed (%s); using fallback.", exc)

    return _fallback_rest_message()


def _fallback_rest_message() -> str:
    return (
        "Today is a scheduled rest day. Recovery is where adaptation happens.\n\n"
        "- Aim for around 8 hours of sleep tonight.\n"
        "- Hit your protein target.\n"
        "- Keep it gentle: light mobility or an easy walk, nothing heavy.\n"
        "- Stay hydrated and arrive fresh for the next session."
    )


def _build_checkin_prompt(
    number: int,
    week: int,
    cycle_weeks: int,
    block: dict[str, Any],
    rules: list[str],
    workouts_done: int,
    weeks: int,
    analysis_text: str,
) -> str:
    block_number = block.get("number") or 0
    block_name = block.get("name") or "Programme"
    rules_text = "\n".join(f"- {rule}" for rule in rules) or "- None recorded."
    return f"""You are an experienced strength and conditioning coach.

This is programme check-in number {number}. The user has logged {workouts_done}
sessions over the last {weeks} weeks, and is in Week {week} of {cycle_weeks},
Block {block_number}: {block_name}.

Programme rules you must always respect:
{rules_text}

Here is the data: planned targets versus what was actually logged this block.
{analysis_text}

Your task: write a short, motivating check-in the user reads on their phone.
- Plain text only. No markdown, no asterisks, no headings.
- First line exactly: "Check-in {number}: Block {block_number} ({block_name})".
- Then 3 to 5 short lines: what is progressing, what has stalled, and where the
  user is versus the plan. Reference the real numbers.
- Then 1 to 3 concrete adjustments for the next stretch (load, reps, or swapping
  a stalled lift), consistent with the programme rules.
- End with one short line of genuine encouragement.

Use British English. Never use the em dash."""


def generate_checkin_message(
    provider: AIProvider,
    number: int,
    week: int,
    cycle_weeks: int,
    block: dict[str, Any],
    rules: list[str],
    workouts_done: int,
    weeks: int,
    analysis_text: str,
    fallback: str,
) -> str:
    """Generate a periodic check-in message, falling back on error.

    Args:
        provider: A resolved AI provider instance from
            :func:`ai_resolver.resolve_provider`.
    """
    try:
        prompt = _build_checkin_prompt(
            number,
            week,
            cycle_weeks,
            block,
            rules,
            workouts_done,
            weeks,
            analysis_text,
        )
        text = str(provider.generate(prompt)).strip()
        if text:
            return text
        logger.warning("AI provider returned an empty check-in; using fallback.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI check-in generation failed (%s); using fallback.", exc)

    return fallback
