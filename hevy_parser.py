"""Parses a raw Hevy API payload into a compact, model-friendly summary.

Instead of dumping the full Hevy JSON into the Gemini prompt, we distil each
exercise down to its top set (heaviest weight for reps) and whether the top of
the target rep range was reached. This keeps prompts small and focused.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExerciseSummary:
    name: str
    top_weight_kg: float | None
    top_reps: int | None
    sets: int
    hit_top_of_range: bool | None = None

    def as_line(self) -> str:
        parts = [f"{self.name}:"]
        if self.top_weight_kg is not None and self.top_reps is not None:
            parts.append(f"{self.top_weight_kg:g} kg x {self.top_reps}")
        elif self.top_reps is not None:
            parts.append(f"{self.top_reps} reps (bodyweight)")
        else:
            parts.append("no sets logged")
        parts.append(f"({self.sets} sets)")
        if self.hit_top_of_range is True:
            parts.append("- hit the top of the rep range")
        elif self.hit_top_of_range is False:
            parts.append("- below the top of the rep range")
        return " ".join(parts)


@dataclass(frozen=True)
class WorkoutSummary:
    title: str
    date: str | None
    exercises: list[ExerciseSummary]

    def as_text(self) -> str:
        header = self.title
        if self.date:
            header += f" ({self.date})"
        lines = [header]
        lines.extend(f"- {ex.as_line()}" for ex in self.exercises)
        return "\n".join(lines)


def normalise_name(name: str) -> str:
    """Lower-case and collapse whitespace so names match across sources."""
    return " ".join(name.lower().split())


def _first_workout(payload: Any) -> dict[str, Any] | None:
    """Extract the single most recent workout from various payload shapes."""
    if not payload:
        return None
    if isinstance(payload, dict):
        workouts = payload.get("workouts")
        if isinstance(workouts, list) and workouts:
            first = workouts[0]
            return first if isinstance(first, dict) else None
        if "exercises" in payload:
            return payload
        return None
    if isinstance(payload, list) and payload:
        first = payload[0]
        return first if isinstance(first, dict) else None
    return None


def _top_set(sets: Any) -> tuple[float | None, int | None, int]:
    """Return (top_weight, top_reps, valid_set_count) for an exercise.

    The top set is the one with the greatest weight, with reps as a tie-break.
    Bodyweight sets (no weight) are still counted via their reps.
    """
    best_key: tuple[float, int] | None = None
    best_weight: float | None = None
    best_reps: int | None = None
    count = 0

    for entry in sets if isinstance(sets, list) else []:
        if not isinstance(entry, dict):
            continue
        count += 1
        weight = entry.get("weight_kg")
        reps = entry.get("reps")
        key = (
            float(weight) if weight is not None else -1.0,
            int(reps) if reps is not None else -1,
        )
        if best_key is None or key > best_key:
            best_key = key
            best_weight = float(weight) if weight is not None else None
            best_reps = int(reps) if reps is not None else None

    return best_weight, best_reps, count


def parse_workout(
    payload: Any,
    rep_targets: dict[str, int] | None = None,
) -> WorkoutSummary | None:
    """Parse a raw Hevy payload into a compact WorkoutSummary, or None.

    ``rep_targets`` maps a normalised exercise name to the top of its target
    rep range, used to flag whether that range was reached in the top set.
    """
    targets = rep_targets or {}
    workout = _first_workout(payload)
    if workout is None:
        return None

    raw_exercises = workout.get("exercises")
    if not isinstance(raw_exercises, list):
        return None

    summaries: list[ExerciseSummary] = []
    for ex in raw_exercises:
        if not isinstance(ex, dict):
            continue
        name = (ex.get("title") or ex.get("name") or "Unknown exercise").strip()
        top_weight, top_reps, count = _top_set(ex.get("sets"))

        target = targets.get(normalise_name(name))
        hit_top: bool | None = None
        if target is not None and top_reps is not None:
            hit_top = top_reps >= target

        summaries.append(
            ExerciseSummary(
                name=name,
                top_weight_kg=top_weight,
                top_reps=top_reps,
                sets=count,
                hit_top_of_range=hit_top,
            )
        )

    if not summaries:
        return None

    title = (workout.get("title") or "Workout").strip()
    when = workout.get("start_time") or workout.get("end_time")
    return WorkoutSummary(title=title, date=when, exercises=summaries)
