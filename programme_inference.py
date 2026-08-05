"""Infer a user's training programme from their Hevy data.

Instead of a static ``program.py`` that hardcodes a 6-day powerbuilding split,
this module analyses the user's actual Hevy routines and workout history to
understand:

- What split they run (push/pull/legs, upper/lower, full body, bro split, etc.)
- How many training days per week
- Which muscle groups are emphasised
- What periodisation structure they follow (if any)
- What their next workout should be

The output is an ``InferredProgramme`` that the AI engine and dashboard use
to build plans, show progress, and render the dynamic UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from hevy_reader import (
    CompletedWorkout,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TrainingDay:
    """One session in the user's split, derived from a Hevy routine."""

    title: str
    routine_id: str
    primary_muscles: list[str] = field(default_factory=list)
    exercises: list[RoutineExercise] = field(default_factory=list)
    exercise_count: int = 0

    def focus_summary(self) -> str:
        """Readable summary of this day's muscle focus."""
        if not self.primary_muscles:
            return self.title
        # Show top 2-3 muscle groups.
        top = self.primary_muscles[:3]
        return ", ".join(g.replace("_", " ").title() for g in top)


@dataclass
class InferredProgramme:
    """What we understand about the user's training from their Hevy data.

    This replaces the static ``program.py`` split definition. Everything here
    is inferred from real data, not hardcoded.
    """

    # The user's training days (derived from routines).
    training_days: list[TrainingDay] = field(default_factory=list)

    # Split classification.
    split_type: str = "unknown"  # ppl, upper_lower, full_body, bro_split, custom

    # Frequency analysis.
    sessions_per_week: float = 0.0
    muscle_frequency: dict[str, float] = field(
        default_factory=dict
    )  # muscle -> sessions/week

    # Training history stats.
    total_workouts: int = 0
    recent_workout_days: list[str] = field(default_factory=list)  # ISO dates

    # The next routine the user should do (round-robin from their routines).
    next_routine: TrainingDay | None = None

    def day_titles(self) -> list[str]:
        return [d.title for d in self.training_days]

    def is_rest_day_today(self) -> bool:
        """Heuristic: if the user trained yesterday but not today, it's rest."""
        if not self.recent_workout_days:
            return False
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return today not in self.recent_workout_days


# ---------------------------------------------------------------------------
# Inference logic
# ---------------------------------------------------------------------------

_MUSCLE_GROUP_CATEGORIES = {
    # Push muscles
    "chest": "push",
    "shoulders": "push",
    "triceps": "push",
    # Pull muscles
    "lats": "pull",
    "upper_back": "pull",
    "traps": "pull",
    "biceps": "pull",
    "forearms": "pull",
    # Leg muscles
    "quads": "legs",
    "hamstrings": "legs",
    "glutes": "legs",
    "calves": "legs",
    "adductors": "legs",
    "abductors": "legs",
    # Core
    "abdominals": "core",
    "obliques": "core",
    "lower_back": "core",
    # Other
    "neck": "other",
    "full_body": "full_body",
    "cardio": "cardio",
    "other": "other",
}


def _classify_routine_muscles(
    routine: Routine,
    templates: dict,
) -> list[str]:
    """Return a ranked list of primary muscle groups hit by this routine."""
    counts: dict[str, float] = {}
    for ex in routine.exercises:
        tmpl = templates.get(ex.template_id)
        if tmpl:
            counts[tmpl.primary_muscle_group] = (
                counts.get(tmpl.primary_muscle_group, 0.0) + 1.0
            )
            for sec in tmpl.secondary_muscle_groups:
                counts[sec] = counts.get(sec, 0.0) + 0.5
    return sorted(counts, key=lambda k: counts[k], reverse=True)


def _classify_split(days: list[TrainingDay]) -> str:
    """Classify the split type from the training days' muscle focus."""
    if not days:
        return "unknown"

    # Check if any day hits almost every muscle group -> full body.
    all_muscles = set()
    for d in days:
        all_muscles.update(d.primary_muscles)

    day_categories: list[set[str]] = []
    for d in days:
        cats = set()
        for m in d.primary_muscles[:3]:
            cat = _MUSCLE_GROUP_CATEGORIES.get(m, "other")
            cats.add(cat)
        day_categories.append(cats)

    # Full body: most days hit push + pull + legs.
    full_body_days = sum(
        1 for cats in day_categories if len(cats & {"push", "pull", "legs"}) >= 3
    )
    if full_body_days >= len(days) * 0.6:
        return "full_body"

    # PPL: distinct push, pull, and legs days.
    has_push = any(
        "push" == max(cats, key=lambda c: c, default="")
        for cats in day_categories
        if "push" in cats
    )
    has_pull = any("pull" in cats for cats in day_categories)
    has_legs = any("legs" in cats for cats in day_categories)
    if has_push and has_pull and has_legs and len(days) >= 3:
        return "push_pull_legs"

    # Upper/Lower: days are either upper or lower focused.
    upper_count = sum(
        1 for cats in day_categories if cats & {"push", "pull"} and not cats & {"legs"}
    )
    lower_count = sum(
        1 for cats in day_categories if cats & {"legs"} and not cats & {"push", "pull"}
    )
    if (
        upper_count > 0
        and lower_count > 0
        and upper_count + lower_count >= len(days) * 0.7
    ):
        return "upper_lower"

    # Bro split: each day focuses on 1-2 specific muscle groups.
    specific_days = sum(1 for d in days if len(d.primary_muscles) <= 3)
    if specific_days >= len(days) * 0.7 and len(days) >= 4:
        return "bro_split"

    return "custom"


def _compute_frequency(
    workouts: list[CompletedWorkout],
    templates: dict,
    window_days: int = 28,
) -> tuple[float, dict[str, float]]:
    """Compute sessions/week and per-muscle frequency from recent workouts."""
    if not workouts:
        return 0.0, {}

    # Filter to the window.
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    recent = []
    for w in workouts:
        if w.start_time:
            try:
                when = datetime.fromisoformat(w.start_time.replace("Z", "+00:00"))
                if when.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
                    recent.append(w)
            except (ValueError, TypeError):
                recent.append(w)
        else:
            recent.append(w)

    weeks = window_days / 7
    sessions_per_week = len(recent) / weeks if weeks > 0 else 0

    muscle_hits: dict[str, int] = {}
    for w in recent:
        session_muscles: set[str] = set()
        for ex in w.exercises:
            tmpl = templates.get(ex.template_id)
            if tmpl:
                session_muscles.add(tmpl.primary_muscle_group)
        for m in session_muscles:
            muscle_hits[m] = muscle_hits.get(m, 0) + 1

    muscle_freq = {m: round(count / weeks, 1) for m, count in muscle_hits.items()}
    return round(sessions_per_week, 1), muscle_freq


def _determine_next_routine(
    training_days: list[TrainingDay],
    recent_workouts: list[CompletedWorkout],
) -> TrainingDay | None:
    """Round-robin: suggest the routine the user hasn't done most recently."""
    if not training_days:
        return None
    if not recent_workouts:
        return training_days[0]

    # Map routine titles to their last-done time.
    last_done: dict[str, str] = {}
    for w in recent_workouts:
        title = w.title
        if title not in last_done:
            last_done[title] = w.start_time or ""

    # Find the training day least recently done.
    best: TrainingDay | None = None
    best_time = None
    for day in training_days:
        done_time = last_done.get(day.title)
        if done_time is None:
            # Never done this routine -> highest priority.
            return day
        if best_time is None or done_time < best_time:
            best_time = done_time
            best = day

    return best


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def infer_programme(hevy_data: HevyTrainingData) -> InferredProgramme:
    """Build an InferredProgramme from the user's Hevy training data.

    This is the replacement for the static ``program.py``. Everything is
    derived from what the user actually has in their Hevy account.
    """
    programme = InferredProgramme()

    # Build training days from routines.
    for routine in hevy_data.routines:
        muscles = _classify_routine_muscles(routine, hevy_data.exercise_templates)
        day = TrainingDay(
            title=routine.title,
            routine_id=routine.id,
            primary_muscles=muscles,
            exercises=routine.exercises,
            exercise_count=len(routine.exercises),
        )
        programme.training_days.append(day)

    # Classify the split.
    programme.split_type = _classify_split(programme.training_days)

    # Frequency from recent workouts.
    sessions_per_week, muscle_freq = _compute_frequency(
        hevy_data.recent_workouts,
        hevy_data.exercise_templates,
    )
    programme.sessions_per_week = sessions_per_week
    programme.muscle_frequency = muscle_freq

    # Total workout count.
    programme.total_workouts = hevy_data.workout_count or len(hevy_data.recent_workouts)

    # Recent workout dates.
    for w in hevy_data.recent_workouts:
        if w.start_time:
            try:
                d = datetime.fromisoformat(w.start_time.replace("Z", "+00:00"))
                programme.recent_workout_days.append(d.strftime("%Y-%m-%d"))
            except (ValueError, TypeError):
                pass

    # Next routine suggestion.
    programme.next_routine = _determine_next_routine(
        programme.training_days, hevy_data.recent_workouts
    )

    logger.info(
        "Inferred programme: %s split, %.1f sessions/week, %d routines, next: %s",
        programme.split_type,
        programme.sessions_per_week,
        len(programme.training_days),
        programme.next_routine.title if programme.next_routine else "none",
    )

    return programme
