"""Uses Google Gemini to apply progressive overload and write today's plan."""

from __future__ import annotations

import json
import logging
from typing import Any

import google.generativeai as genai

from hevy_parser import WorkoutSummary
from insights import TrainingInsights
from program import COACHING_RULES, Block, SPLIT_NAME, day_exercises, day_focus, format_day

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


def _build_prompt(
    day: int,
    week: int,
    block: Block,
    workout_summary: WorkoutSummary | None,
    recovery: dict[str, Any] | None,
    history: dict[str, dict[str, Any]] | None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
) -> str:
    baseline = format_day(day, block)
    focus = day_focus(day)
    rules = "\n".join(f"- {rule}" for rule in COACHING_RULES)
    workout_text = (
        workout_summary.as_text() if workout_summary else "None available."
    )
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."
    history_text = _format_history(history)
    insights_text = insights.as_text() if insights else "Not enough history yet."
    last_plan_text = last_plan if last_plan else "None available."

    return f"""You are an elite powerbuilding and stage-prep coach for Elgan.

His goals are a competition-level physique, a visible six-pack, and rising
strength on the deadlift and weighted pull-up, without wrecking his joints.

The programme is the periodised "{SPLIT_NAME}". Today is Week {week} of 12,
in Block {block.number}: {block.name} ({block.focus}).
- Deadlift this block: {block.deadlift.sets} sets x {block.deadlift.rep_range} reps. {block.deadlift.note}
- Pull-ups this block: {block.pullups.sets} sets x {block.pullups.rep_range} reps. {block.pullups.note}
- Accessory emphasis this block: {block.accessory_emphasis}

Today is Day {day}: {focus}.

Coaching rules you must always respect:
{rules}

The baseline plan for today (already block-adjusted) is:
{baseline}

The PLANNED routine you generated for the last session:
{last_plan_text}

Compact summary of his most recent logged Hevy session (EXECUTED routine):
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
- Keep the main lifts (deadlift, pull-up) in this block's prescribed scheme unless overriden or recovery is poor.
- Apply progressive overload from the Hevy data: where the last session hit the
  top of a rep range with good form, add a rep or a small load increase; where
  recovery looks poor (short sleep, elevated resting heart rate, dropped HRV), trim volume
  slightly.
- Act on the self-review: for any lift flagged as stalling or regressing, apply
  its suggested intervention (deload, change rep range, or swap the movement)
  rather than blindly adding load. Follow the recovery directive for overall
  volume today.
- Output ONLY today's workout, nothing else.

Strict output format (this is read on a phone in Telegram, keep it minimal):
- Plain text only. No markdown, no asterisks, no bold, no headings.
- First line exactly: "{focus} - Week {week} ({block.name})".
- Then one exercise per line, in the form "Name: sets x reps".
- Add a short note in round brackets ONLY when you change something from the
  baseline, e.g. "(+2.5 kg)" or "(+1 rep)". Otherwise add no note.
- No intro, no assessment, no motivation, no tips, no closing line.

Use British English. Never use the em dash."""


def generate_next_workout(
    api_key: str,
    model_name: str,
    day: int,
    week: int,
    block: Block,
    workout_summary: WorkoutSummary | None = None,
    recovery: dict[str, Any] | None = None,
    history: dict[str, dict[str, Any]] | None = None,
    insights: TrainingInsights | None = None,
    last_plan: str | None = None,
) -> str:
    """Generate today's plan, falling back to the baseline plan on error."""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        prompt = _build_prompt(
            day, week, block, workout_summary, recovery, history, insights, last_plan
        )
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text:
            return text
        logger.warning("Gemini returned an empty response; using baseline plan.")
    except Exception as exc:  # the SDK raises a variety of exception types
        logger.warning("Gemini generation failed (%s); using baseline plan.", exc)

    return _fallback_plan(day, week, block)


def _fallback_plan(day: int, week: int, block: Block) -> str:
    lines = [f"{day_focus(day)} - Week {week} ({block.name})"]
    lines.extend(
        f"{ex.name}: {ex.sets} x {ex.rep_range}" for ex in day_exercises(day, block)
    )
    return "\n".join(lines)


def _build_rest_prompt(recovery: dict[str, Any] | None) -> str:
    rules = "\n".join(f"- {rule}" for rule in COACHING_RULES)
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."

    return f"""You are an elite bodybuilding and stage-prep coach for Elgan.

Today is a scheduled rest day in his 6-day "{SPLIT_NAME}".

Coaching rules you must always respect:
{rules}

Recovery metrics from Health Connect (sleep, bodyweight, resting heart rate):
{recovery_json}

Your task:
1. Briefly reassure him that recovery is where the muscle is built.
2. Give two or three short, specific recovery actions for today, drawing on the
   recovery metrics if available (sleep, light mobility, hydration, hitting his
   protein target of around 2 g per kg of bodyweight, gentle movement only).
3. Keep it short and motivating.

Use British English. Never use the em dash."""


def generate_rest_day_message(
    api_key: str,
    model_name: str,
    recovery: dict[str, Any] | None = None,
) -> str:
    """Generate a short rest-day recovery message, falling back on error."""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        prompt = _build_rest_prompt(recovery)
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text:
            return text
        logger.warning("Gemini returned an empty rest-day response; using fallback.")
    except Exception as exc:  # the SDK raises a variety of exception types
        logger.warning("Gemini rest-day generation failed (%s); using fallback.", exc)

    return _fallback_rest_message()


def _fallback_rest_message() -> str:
    return (
        "Today is your scheduled rest day. Recovery is where the muscle is built.\n\n"
        "- Aim for around 8 hours of sleep tonight.\n"
        "- Hit your protein target (around 2 g per kg of bodyweight).\n"
        "- Keep it gentle: light mobility or an easy walk, nothing heavy.\n"
        "- Stay hydrated and let the joints recover for tomorrow."
    )


def _build_checkin_prompt(
    number: int,
    week: int,
    block: Block,
    workouts_done: int,
    weeks: int,
    analysis_text: str,
) -> str:
    rules = "\n".join(f"- {rule}" for rule in COACHING_RULES)
    return f"""You are an elite powerbuilding and stage-prep coach for Elgan.

This is programme check-in number {number}. He has logged {workouts_done}
sessions over the last {weeks} weeks, and is in Week {week} of 12, Block
{block.number}: {block.name} ({block.focus}).

Coaching rules you must always respect:
{rules}

Here is the data: planned targets versus what he actually logged this block.
{analysis_text}

Your task: write a short, motivating check-in he reads on his phone.
- Plain text only. No markdown, no asterisks, no headings.
- First line exactly: "Check-in {number}: Block {block.number} ({block.name})".
- Then 3 to 5 short lines: what is progressing, what has stalled, and where he
  is versus the plan. Reference the real numbers.
- Then 1 to 3 concrete adjustments for the next stretch (load, reps, or swapping
  a stalled lift), consistent with the coaching rules.
- End with one short line of genuine encouragement.

Use British English. Never use the em dash."""


def generate_checkin_message(
    api_key: str,
    model_name: str,
    number: int,
    week: int,
    block: Block,
    workouts_done: int,
    weeks: int,
    analysis_text: str,
    fallback: str,
) -> str:
    """Generate a periodic check-in message, falling back on error."""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        prompt = _build_checkin_prompt(
            number, week, block, workouts_done, weeks, analysis_text
        )
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text:
            return text
        logger.warning("Gemini returned an empty check-in; using fallback.")
    except Exception as exc:  # the SDK raises a variety of exception types
        logger.warning("Gemini check-in generation failed (%s); using fallback.", exc)

    return fallback
