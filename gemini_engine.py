"""Uses Google Gemini to apply progressive overload and write tomorrow's plan."""

from __future__ import annotations

import json
import logging
from typing import Any

import google.generativeai as genai

from program import COACHING_RULES, SPLIT_NAME, format_day

logger = logging.getLogger(__name__)


def _build_prompt(
    day: int,
    recent_workout: Any,
    recovery: dict[str, Any] | None,
) -> str:
    baseline = format_day(day)
    rules = "\n".join(f"- {rule}" for rule in COACHING_RULES)
    workout_json = json.dumps(recent_workout, indent=2) if recent_workout else "None available."
    recovery_json = json.dumps(recovery, indent=2) if recovery else "None available."

    return f"""You are an elite bodybuilding and stage-prep coach for Elgan.

He is on Day {day} of a 6-day "{SPLIT_NAME}" focused on hypertrophy and fat loss.

Coaching rules you must always respect:
{rules}

The baseline plan for today's slot in the cycle is:
{baseline}

Recent workout performance pulled from the Hevy API:
{workout_json}

Recovery metrics from Health Connect (sleep, bodyweight, resting heart rate):
{recovery_json}

Your task:
1. Briefly assess the recent performance and recovery in two or three sentences.
2. Apply progressive overload: where the last session hit the top of a rep
   range with good form, suggest adding one rep or a small load increase. Where
   recovery looks poor (short sleep or elevated resting heart rate), dial back
   volume slightly and emphasise technique and recovery.
3. Output today's exact workout as a clean list, keeping the structure of the
   baseline plan but with your adjustments and target weights or rep targets.
4. Finish with exactly one specific daily improvement tip (tempo, breathing,
   mind-muscle connection, or mobility).

Keep it concise and motivating. Use British English. Never use the em dash."""


def generate_next_workout(
    api_key: str,
    model_name: str,
    day: int,
    recent_workout: Any,
    recovery: dict[str, Any] | None = None,
) -> str:
    """Generate tomorrow's plan, falling back to the baseline plan on error."""
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        prompt = _build_prompt(day, recent_workout, recovery)
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text:
            return text
        logger.warning("Gemini returned an empty response; using baseline plan.")
    except Exception as exc:  # the SDK raises a variety of exception types
        logger.warning("Gemini generation failed (%s); using baseline plan.", exc)

    return _fallback_plan(day)


def _fallback_plan(day: int) -> str:
    return (
        "Coach unavailable this morning, so here is your baseline plan.\n\n"
        f"{format_day(day)}\n\n"
        "Daily improvement: focus on a strict 3-second negative on every rep."
    )
