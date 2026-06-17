"""The 6-day Hybrid Powerbuilding split, periodised into 4-week blocks.

Stored as structured data so the agent (and Gemini) can reason about it, apply
progressive overload, and adapt the main-lift intensity to the current block,
rather than treating the programme as opaque text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

SPLIT_NAME = "Hybrid Powerbuilding (12-week periodised)"
REST_DAY_FOCUS = "Rest & Recovery"

# The programme runs in three 4-week blocks (a 12-week mesocycle), then repeats.
CYCLE_WEEKS = 12
BLOCK_WEEKS = 4

# Non-negotiable coaching rules the agent must always honour.
COACHING_RULES = [
    "Hybrid powerbuilding: build the deadlift and pull-up for raw strength while "
    "training everything else for hypertrophy.",
    "Periodise in 4-week blocks (Accumulation, Intensification, Peaking). Match the "
    "intensity of the main lifts to the current block.",
    "Main lifts (deadlift, pull-up): brace hard, keep a neutral spine, and stop a "
    "set the moment form or bar speed breaks down. Trap bar is a valid joint-friendly "
    "deadlift swap.",
    "Accessories: strict 3-second negative on every rep, taken close to failure. No "
    "momentum.",
    "No Bulgarian split squats (bad toes). Use leg extensions or the flat-foot leg "
    "press instead.",
    "No stomach vacuums. Train abs for mass with progressive overload.",
    "Favour lateral and rear-delt isolation over heavy overhead pressing to protect "
    "the shoulders and elbows (Thai boxing and bouldering add load).",
    "Keep protein static at roughly 2.2 g per kg of bodyweight every day; a visible "
    "six-pack needs a sustained caloric deficit.",
    "Cycle carbohydrates with training load: high carbs on heavy deadlift and back "
    "days (about 70% around the workout), moderate on leg days, low carb with higher "
    "healthy fats on the lighter upper days and rest days.",
    "Burn fat with movement, not by frying recovery: 10-12k steps a day (NEAT) plus "
    "20-30 min of joint-friendly Zone 2 (stationary bike or swim) three to four times "
    "a week. No stair-master or running (bad toes).",
    "Protect the central nervous system: 8 hours of sleep minimum, Omega-3 for the "
    "joints, and Magnesium Glycinate before bed.",
    "Log every set in Hevy, take a morning weigh-in and body-fat reading, and film the "
    "top deadlift and pull-up set each week.",
    "Use British English spelling (e.g. programme). Never use the em dash.",
]


@dataclass(frozen=True)
class Exercise:
    name: str
    sets: int
    rep_range: str
    note: str = ""
    template_id: str | None = None

    def as_line(self) -> str:
        line = f"{self.name}: {self.sets} sets x {self.rep_range} reps"
        if self.note:
            line += f" ({self.note})"
        return line


@dataclass(frozen=True)
class LiftScheme:
    """A block-specific set/rep/intensity prescription for a main lift."""

    sets: int
    rep_range: str
    note: str
    template_id: str


@dataclass(frozen=True)
class Block:
    number: int
    name: str
    weeks: str
    focus: str
    deadlift: LiftScheme
    pullups: LiftScheme
    accessory_emphasis: str


# Hevy exercise-template ids for the two periodised main lifts.
_DEADLIFT_BARBELL = "C6272009"
_PULL_UP = "1B2B1E7C"
_PULL_UP_WEIGHTED = "729237D1"

BLOCKS: dict[int, Block] = {
    1: Block(
        number=1,
        name="Accumulation",
        weeks="1-4",
        focus="Rebuild strength capacity and high-volume hypertrophy.",
        deadlift=LiftScheme(
            4, "5-8",
            "Moderate-heavy, leave about 2 reps in the tank. Trap bar is a fine "
            "joint-friendly swap.",
            _DEADLIFT_BARBELL,
        ),
        pullups=LiftScheme(
            4, "6-10",
            "Bodyweight, stop 1 rep shy of failure. Use a band only if you cannot "
            "hit 6 clean reps.",
            _PULL_UP,
        ),
        accessory_emphasis="10-15 reps, strict 3-second negatives.",
    ),
    2: Block(
        number=2,
        name="Intensification",
        weeks="5-8",
        focus="Peak strength development.",
        deadlift=LiftScheme(
            5, "3-5",
            "Heavy, push the last set close to failure with a hard brace.",
            _DEADLIFT_BARBELL,
        ),
        pullups=LiftScheme(
            4, "4-6",
            "Add a weight belt, leave about 1 rep in reserve.",
            _PULL_UP_WEIGHTED,
        ),
        accessory_emphasis="8-12 reps, slightly heavier with controlled negatives.",
    ),
    3: Block(
        number=3,
        name="Peaking & Shredding",
        weeks="9-12",
        focus="Competition prep, fat loss, and a strength display.",
        deadlift=LiftScheme(
            5, "1-2",
            "Ramp over the first sets to a heavy 1-2 rep max. Stop if bar speed or "
            "form breaks down.",
            _DEADLIFT_BARBELL,
        ),
        pullups=LiftScheme(
            4, "3",
            "Weighted 3-rep max, ramp up over the first two sets.",
            _PULL_UP_WEIGHTED,
        ),
        accessory_emphasis="15-20 reps, supersets, maximum metabolic stress for fat loss.",
    ),
}


def block_for_week(week: int) -> Block:
    """Return the training block for a week number (1-12, wrapping after 12)."""
    index = ((week - 1) % CYCLE_WEEKS) // BLOCK_WEEKS + 1
    return BLOCKS[index]


def week_in_cycle(start: date, today: date | None = None) -> int:
    """Return the current week (1-12) given the programme start date."""
    if today is None:
        today = date.today()
    weeks_elapsed = max((today - start).days // 7, 0)
    return weeks_elapsed % CYCLE_WEEKS + 1


# Accessory pools. Main lifts are added on top of the back day per block.
_BACK_DL_CHEST = [
    Exercise("Incline Dumbbell Flyes", 4, "12-15", "pre-exhaust upper chest", "D3E2AB55"),
    Exercise("Incline Smith Machine Press", 3, "10-12", "focus on the top half", "3A6FA3D1"),
    Exercise("Chest-Supported T-Bar Rows", 3, "10-12", "mid-back thickness", "08A2974E"),
]

_SHOULDERS_ARMS = [
    Exercise("Cable Lateral Raises", 5, "15-20", "maximum shoulder width", "BE289E45"),
    Exercise("Reverse Pec Deck Flyes", 4, "15", "rear delts", "D8281C62"),
    Exercise("Incline Dumbbell Curls", 4, "12", "deep stretch, bicep long head", "8BAB2735"),
    Exercise("Tricep Overhead Cable Extensions", 4, "12-15", "triceps long head", "B5EFBF9C"),
    Exercise("Reverse-Grip Cable Curls", 3, "15", "brachialis and forearm", "9F48F858"),
]

_LEGS_ABS = [
    Exercise("Lying Leg Curls", 4, "12", "hamstring isolation", "B8127AD1"),
    Exercise("Leg Press", 4, "10-12", "feet flat, 3-second negative", "C7973E0E"),
    Exercise("Leg Extensions", 3, "12", "quad sweep; toe-friendly split-squat swap", "75A4F6C4"),
    Exercise("Leg Press Calf Raises", 4, "15-20", "feet flat, gastrocnemius", "91237BDD"),
    Exercise("Hanging Leg Raises", 4, "12-15", "lower abs", "F8356514"),
    Exercise("Kneeling Cable Crunches", 4, "10-12", "upper abs, heavy resistance", "23A48484"),
]

# Day-of-cycle (1-6) to session focus. Days 1-3 repeat as 4-6.
_FOCUS = {
    1: "Back, Deadlifts & Chest",
    2: "Shoulders & Arms",
    3: "Legs & Abs",
    4: "Back, Deadlifts & Chest",
    5: "Shoulders & Arms",
    6: "Legs & Abs",
}

TOTAL_DAYS = 6


def day_focus(day: int) -> str:
    """Return the session focus for a given day (1-6)."""
    return _FOCUS[day]


def day_exercises(day: int, block: Block) -> list[Exercise]:
    """Return the exercise list for a given day (1-6) within a block."""
    focus = _FOCUS[day]
    if focus == "Back, Deadlifts & Chest":
        mains = [
            Exercise(
                "Deadlift (Barbell)",
                block.deadlift.sets,
                block.deadlift.rep_range,
                block.deadlift.note,
                block.deadlift.template_id,
            ),
            Exercise(
                "Strict Pull-Ups",
                block.pullups.sets,
                block.pullups.rep_range,
                block.pullups.note,
                block.pullups.template_id,
            ),
        ]
        return mains + list(_BACK_DL_CHEST)
    if focus == "Shoulders & Arms":
        return list(_SHOULDERS_ARMS)
    return list(_LEGS_ABS)


def format_day(day: int, block: Block) -> str:
    """Render a readable plan for the given day within a block."""
    focus = _FOCUS[day]
    lines = [f"{focus}"]
    lines.extend(
        f"{ex.name}: {ex.sets} x {ex.rep_range}" for ex in day_exercises(day, block)
    )
    return "\n".join(lines)


# The 6-day split maps directly onto Monday to Saturday; Sunday is a rest day.
# Python's date.weekday() is Monday=0 ... Sunday=6.
_WEEKDAY_TO_DAY = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6}


def day_for_weekday(weekday: int) -> int | None:
    """Map a weekday (Monday=0 ... Sunday=6) to a cycle day (1-6).

    Returns None for Sunday, which is a rest day.
    """
    return _WEEKDAY_TO_DAY.get(weekday)


def is_rest_day(weekday: int) -> bool:
    """Return True if the given weekday (Monday=0 ... Sunday=6) is a rest day."""
    return weekday not in _WEEKDAY_TO_DAY


def today_day(today: date | None = None) -> int | None:
    """Return today's cycle day (1-6), or None if today is a rest day."""
    if today is None:
        today = date.today()
    return day_for_weekday(today.weekday())


def _top_rep(rep_range: str) -> int | None:
    """Return the top of a rep range string such as '12-15' or '12'."""
    numbers = re.findall(r"\d+", rep_range)
    return int(numbers[-1]) if numbers else None


def rep_targets(block: Block | None = None) -> dict[str, int]:
    """Map each exercise (normalised name) to the top of its target rep range."""
    if block is None:
        block = BLOCKS[1]
    targets: dict[str, int] = {}
    for day in (1, 2, 3):
        for exercise in day_exercises(day, block):
            top = _top_rep(exercise.rep_range)
            if top is not None:
                targets[" ".join(exercise.name.lower().split())] = top
    return targets
