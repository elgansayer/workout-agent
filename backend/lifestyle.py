"""Daily lifestyle guidance: the pillars around the training.

The gym is only part of the battle. This module turns the active programme's
training focus into concrete daily actions across the supporting pillars so the
agent can deliver, and log, a complete "what to do today" plan:

1. Strategic nutrition (protein held static, carbohydrates timed around load).
2. Joint-friendly cardio and NEAT.
3. Recovery (sleep and general recovery habits).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Protein is held static; carbohydrates are adjusted around training load.
PROTEIN_G_PER_KG = 2.2

# Cardio and NEAT.
STEP_TARGET = "10,000 to 12,000 steps"

# Recovery.
SLEEP_TARGET_HOURS = 8


@dataclass(frozen=True)
class DailyGuidance:
    """The lifestyle pillars resolved for a single day."""

    training: str  # which session to train today
    carb_tier: str  # "moderate" | "low"
    nutrition: str
    cardio: str
    recovery: str
    protein_target: str | None  # set only when bodyweight is known

    def as_lines(self) -> list[str]:
        lines = [
            f"Train: {self.training}",
            f"Nutrition ({self.carb_tier} carb): {self.nutrition}",
        ]
        if self.protein_target:
            lines.append(self.protein_target)
        lines.append(f"Cardio and steps: {self.cardio}")
        lines.append(f"Recovery: {self.recovery}")
        return lines

    def as_text(self) -> str:
        body = "\n".join(f"- {line}" for line in self.as_lines())
        return "Today's lifestyle:\n" + body


def _nutrition(tier: str) -> str:
    if tier == "moderate":
        return (
            "Time most of your carbs around today's session to fuel the work "
            "and refill glycogen, keeping the rest of the day leaner."
        )
    return (
        "Lower carb today. Lean on healthy fats and protein to support "
        "recovery without overshooting energy intake."
    )


def _cardio(is_rest: bool) -> str:
    base = f"Hit {STEP_TARGET} (NEAT)."
    if is_rest:
        return base + " Add 20 to 30 min of easy Zone 2 if it feels good."
    return base + " Add optional easy Zone 2 if it does not compromise recovery."


def _recovery() -> str:
    return (
        f"{SLEEP_TARGET_HOURS} h sleep minimum. Prioritise hydration, a protein "
        "target, and light mobility on rest days."
    )


def _protein_target(recovery: dict[str, Any] | None) -> str | None:
    if not recovery:
        return None
    weight = recovery.get("weight_kg")
    if weight is None:
        return None
    try:
        grams = round(float(weight) * PROTEIN_G_PER_KG)
    except (TypeError, ValueError):
        return None
    return f"Protein: about {grams} g today ({PROTEIN_G_PER_KG:g} g per kg)."


def daily_guidance(
    focus: str | None,
    is_rest: bool,
    recovery: dict[str, Any] | None = None,
) -> DailyGuidance:
    """Resolve the lifestyle pillars for the active programme's training focus.

    ``focus`` is the selected Hevy routine title, or ``None`` on a rest day.
    ``recovery`` may carry a ``weight_kg`` reading used to compute the daily
    protein target.
    """
    rest = is_rest or not focus
    tier = "low" if rest else "moderate"
    training = "Rest and recovery, no lifting today" if rest else str(focus)
    return DailyGuidance(
        training=training,
        carb_tier=tier,
        nutrition=_nutrition(tier),
        cardio=_cardio(rest),
        recovery=_recovery(),
        protein_target=_protein_target(recovery),
    )
