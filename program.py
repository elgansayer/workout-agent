"""The perfected, joint-friendly 6-day Stage-Prep Arnold Split.

Stored as structured data so the agent (and Gemini) can reason about it and
apply progressive overload, rather than treating it as opaque text.
"""

from __future__ import annotations

from dataclasses import dataclass

SPLIT_NAME = "Stage-Prep Arnold Split"

# Non-negotiable coaching rules the agent must always honour.
COACHING_RULES = [
    "Train for hypertrophy, not maximal strength. No heavy barbell maxes.",
    "Use a strict 3-second negative (eccentric) on every rep. No momentum.",
    "Work in the 10-20 rep range, taking sets close to failure.",
    "Pre-exhaust with an isolation movement before any compound movement.",
    "No Bulgarian split squats (bad toes). Use the flat-foot leg press instead.",
    "No stomach vacuums. Train abs for mass with progressive overload.",
    "Favour lateral and rear-delt isolation over heavy overhead pressing to "
    "protect the shoulders and elbows (Thai boxing and bouldering add load).",
    "Keep protein around 2 g per kg of bodyweight to preserve muscle in a deficit.",
    "Use British English spelling (e.g. programme). Never use the em dash.",
]


@dataclass(frozen=True)
class Exercise:
    name: str
    sets: int
    rep_range: str
    note: str = ""

    def as_line(self) -> str:
        line = f"{self.name}: {self.sets} sets x {self.rep_range} reps"
        if self.note:
            line += f" ({self.note})"
        return line


# Three distinct training days. The 6-day cycle repeats each block twice.
CHEST_BACK = [
    Exercise("Incline Dumbbell Flyes", 4, "12-15", "pre-exhaust upper chest"),
    Exercise("Incline Smith Machine Press", 3, "10-12", "focus on the top half"),
    Exercise("Chest-Supported T-Bar Rows", 3, "10-12", "mid-back thickness"),
    Exercise("Wide-Grip Lat Pulldowns", 4, "12", "build width"),
    Exercise("Straight-Arm Cable Pull-Downs", 3, "15", "constant lat tension"),
]

SHOULDERS_ARMS = [
    Exercise("Cable Lateral Raises", 5, "15-20", "maximum shoulder width"),
    Exercise("Reverse Pec Deck Flyes", 4, "15", "rear delts"),
    Exercise("Incline Dumbbell Curls", 4, "12", "deep stretch, bicep long head"),
    Exercise("Tricep Overhead Cable Extensions", 4, "12-15", "triceps long head"),
    Exercise("Reverse-Grip Cable Curls", 3, "15", "brachialis and forearm"),
]

LEGS_ABS = [
    Exercise("Lying Leg Curls", 4, "12", "hamstring isolation"),
    Exercise("Leg Extensions", 4, "15", "pre-exhaust quads"),
    Exercise("Leg Press", 3, "10-12", "feet flat, 3-second negative"),
    Exercise("Romanian Deadlifts (Dumbbells)", 4, "12", "constant hamstring tension"),
    Exercise("Leg Press Calf Raises", 4, "15-20", "feet flat, gastrocnemius"),
    Exercise("Hanging Leg Raises", 4, "12-15", "lower abs"),
    Exercise("Kneeling Cable Crunches", 4, "10-12", "upper abs, heavy resistance"),
]

# Maps the current day (1-6) to its focus and exercises.
_SCHEDULE = {
    1: ("Chest & Back", CHEST_BACK),
    2: ("Shoulders & Arms", SHOULDERS_ARMS),
    3: ("Legs & Abs", LEGS_ABS),
    4: ("Chest & Back", CHEST_BACK),
    5: ("Shoulders & Arms", SHOULDERS_ARMS),
    6: ("Legs & Abs", LEGS_ABS),
}

TOTAL_DAYS = 6


def day_focus(day: int) -> str:
    """Return the muscle-group focus for a given day (1-6)."""
    return _SCHEDULE[day][0]


def day_exercises(day: int) -> list[Exercise]:
    """Return the exercise list for a given day (1-6)."""
    return list(_SCHEDULE[day][1])


def format_day(day: int) -> str:
    """Render a readable plan for the given day."""
    focus, exercises = _SCHEDULE[day]
    lines = [f"Day {day}: {focus}"]
    lines.extend(f"  - {ex.as_line()}" for ex in exercises)
    return "\n".join(lines)
