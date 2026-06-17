"""Stage-prep lifestyle guidance: the pillars around the training.

The gym is only part of the battle. This module turns the day's training focus
into concrete daily actions across the supporting pillars so the agent can
deliver, and log, a complete "what to do today" plan:

1. Strategic nutrition (carb cycling around training load, static protein).
2. Joint-friendly cardio and NEAT (steps plus low-impact Zone 2).
3. CNS recovery (sleep and targeted supplementation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from program import day_focus

# Pillar 1: nutrition. Protein is held static; carbohydrates cycle with load.
PROTEIN_G_PER_KG = 2.2

# Carb tiers by cycle day. Heavy pulling days are fully fuelled, leg days run
# moderate, and the lighter upper days and rest days run low carb / higher fats.
_HIGH_CARB_DAYS = {1, 4}      # Back & Deadlift: glycogen for heavy pulls
_MODERATE_CARB_DAYS = {3, 6}  # Legs & Abs: the leg press still needs fuel

# Pillar 2: cardio and NEAT.
STEP_TARGET = "10,000 to 12,000 steps"
# Joint-friendly Zone 2 only on the lighter upper days and rest days, to spare
# the heavy lower-body and pulling sessions. Bad toes rule out the stair-master
# and running, so steady-state means the stationary bike or swimming.
_LISS_DAYS = {2, 5}

# Pillar 3: CNS recovery. Heavy deadlifts tax the nervous system.
SLEEP_TARGET_HOURS = 8


@dataclass(frozen=True)
class DailyGuidance:
    """The lifestyle pillars resolved for a single day."""

    training: str                # which session to train today
    carb_tier: str               # "high" | "moderate" | "low"
    nutrition: str
    cardio: str
    recovery: str
    protein_target: str | None   # set only when bodyweight is known

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


def _carb_tier(day: int | None) -> str:
    if day in _HIGH_CARB_DAYS:
        return "high"
    if day in _MODERATE_CARB_DAYS:
        return "moderate"
    return "low"


def _nutrition(tier: str) -> str:
    if tier == "high":
        return (
            "Heavy pulling day. Put about 70% of your carbs in the pre- and "
            "post-workout meals to fuel the pulls and refill glycogen."
        )
    if tier == "moderate":
        return (
            "Leg day. Keep carbs moderate and timed around the session so the "
            "leg press has fuel, leaner the rest of the day."
        )
    return (
        "Lower carb today. Drop the carbs and lean on healthy fats (avocado, "
        "nuts, salmon) to support the joints and hormones."
    )


def _cardio(day: int | None, is_rest: bool) -> str:
    base = f"Hit {STEP_TARGET} (NEAT)."
    if is_rest or day in _LISS_DAYS:
        return (
            base + " Add 20 to 30 min Zone 2 on the stationary bike or in the "
            "pool. No stair-master or running (toes and leg recovery)."
        )
    return base + " Skip steady-state cardio today and protect leg and pull recovery."


def _recovery() -> str:
    return (
        f"{SLEEP_TARGET_HOURS} h sleep minimum. Omega-3 with a meal for the "
        "joints, Magnesium Glycinate before bed to settle the nervous system."
    )


def _protein_target(recovery: dict[str, Any] | None) -> str | None:
    if not recovery:
        return None
    try:
        grams = round(float(recovery.get("weight_kg")) * PROTEIN_G_PER_KG)
    except (TypeError, ValueError):
        return None
    return f"Protein: about {grams} g today ({PROTEIN_G_PER_KG:g} g per kg)."


def daily_guidance(
    day: int | None, is_rest: bool, recovery: dict[str, Any] | None = None
) -> DailyGuidance:
    """Resolve the lifestyle pillars for the given cycle day.

    `day` is the cycle day (1-6) or None on a rest day. `recovery` may carry a
    `weight_kg` reading used to compute the daily protein target.
    """
    rest = is_rest or day is None
    tier = _carb_tier(None if rest else day)
    training = "Rest and recovery, no lifting today" if rest else day_focus(day)
    return DailyGuidance(
        training=training,
        carb_tier=tier,
        nutrition=_nutrition(tier),
        cardio=_cardio(day, is_rest),
        recovery=_recovery(),
        protein_target=_protein_target(recovery),
    )
