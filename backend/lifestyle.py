"""Optional lifestyle guidance around the active programme.

The module makes no assumptions about a specific split, named lifts, personal
medical conditions, or a fixed cycle.  Callers supply the day's programme focus
and optional recovery data; everything else degrades to generic guidance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Pillar 1: nutrition. Protein is held static; carbohydrates can cycle with load.
PROTEIN_G_PER_KG = 2.2


@dataclass(frozen=True)
class DailyGuidance:
    """The lifestyle pillars resolved for a single day."""

    training: str  # which session to train today
    carb_tier: str  # "high" | "moderate" | "low"
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


def _focus_text(focus: str | int | None) -> str:
    if isinstance(focus, int):
        return ""
    return " ".join(str(focus or "").lower().split())


def _carb_tier(focus: str | int | None, is_rest: bool) -> str:
    if is_rest:
        return "low"
    text = _focus_text(focus)
    if any(key in text for key in ("leg", "squat", "lower")):
        return "moderate"
    if any(key in text for key in ("pull", "back", "posterior", "hinge")):
        return "high"
    return "low"


def _nutrition(tier: str) -> str:
    if tier == "high":
        return (
            "Fuel the session with most of today's carbohydrates around training, "
            "then return to your usual balance."
        )
    if tier == "moderate":
        return (
            "Keep carbohydrates moderate and timed around the session; keep the "
            "rest of the day balanced."
        )
    return "Keep carbohydrates lower today and include healthy fats with meals."


def _cardio(focus: str | int | None, is_rest: bool) -> str:
    base = "Hit your usual daily step target (NEAT)."
    text = _focus_text(focus)
    lighter = is_rest or any(
        key in text for key in ("upper", "arm", "shoulder", "light", "accessory")
    )
    if lighter:
        return (
            base
            + " Add a short, low-impact Zone 2 session if it does not compromise recovery."
        )
    return base + " Skip steady-state cardio today and protect recovery."


def _recovery() -> str:
    return (
        "Prioritise sleep, hydration, and a balanced intake. Adjust recovery "
        "actions to how you actually feel rather than a single wearable value."
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
    focus: str | int | None,
    is_rest: bool,
    recovery: dict[str, Any] | None = None,
) -> DailyGuidance:
    """Resolve optional lifestyle pillars for the programme's current focus."""

    rest = is_rest or focus is None or focus == ""
    tier = _carb_tier(focus, rest)
    if rest:
        training = "Rest and recovery, no lifting today"
    else:
        training = str(focus)
    return DailyGuidance(
        training=training,
        carb_tier=tier,
        nutrition=_nutrition(tier),
        cardio=_cardio(focus, rest),
        recovery=_recovery(),
        protein_target=_protein_target(recovery),
    )
