"""Read-only, deterministic web runtime for an activated Hevy programme.

Rotation follows completed Hevy sessions, so missed calendar days do not skip
workouts. Prescriptions are materialised from the current block without
changing the stored snapshot.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def user_timezone(name: str) -> ZoneInfo:
    """Resolve a persisted timezone, with UTC for invalid legacy settings."""
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def local_today(name: str, now: datetime | None = None) -> date:
    """Return the user's local calendar date, including UTC rollover."""
    return (now or datetime.now(timezone.utc)).astimezone(user_timezone(name)).date()


@dataclass
class ProgrammeRuntime:
    """Materialised web view; no writes or external requests are performed."""

    status: str = "setup_required"
    message: str = "Select routines from Hevy to generate your programme."
    start_date: date | None = None
    week: int = 0
    cycle_weeks: int = 0
    blocks: list[dict[str, Any]] = field(default_factory=list)
    block: dict[str, Any] = field(default_factory=dict)
    days: list[dict[str, Any]] = field(default_factory=list)
    next_day: dict[str, Any] | None = None
    last_completed_at: str | None = None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _match_day(workout: dict[str, Any], days: list[dict[str, Any]]) -> int | None:
    routine_id = workout.get("routine_id")
    if routine_id:
        matches = [
            i for i, day in enumerate(days) if day.get("routine_id") == routine_id
        ]
    else:
        # Historical Hevy payloads may omit a routine relation. Only an exact,
        # unique exercise-template signature may resolve those records. Titles
        # are mutable and never establish identity.
        exercises = workout.get("exercises") or []
        if not exercises or any(
            not isinstance(item, dict) or not item.get("exercise_template_id")
            for item in exercises
        ):
            return None
        signature = Counter(item["exercise_template_id"] for item in exercises)
        matches = [
            i
            for i, day in enumerate(days)
            if signature
            == Counter(item.get("template_id") for item in day.get("exercises", []))
        ]
    return matches[0] if len(matches) == 1 else None


def resolve_programme_runtime(
    active: dict[str, Any] | None,
    workouts: list[dict[str, Any]],
    *,
    today: date,
    timezone_name: str = "UTC",
) -> ProgrammeRuntime:
    """Resolve the current block and next exposure from this tenant's data."""
    result = ProgrammeRuntime()
    if not active or active.get("source") != "hevy" or not active.get("definition"):
        return result
    definition = active["definition"]
    spec = definition.get("programme_spec") or {}
    start_value = spec.get("start_date")
    if not start_value:
        activated = _timestamp(active.get("updated_at") or active.get("created_at"))
        start_value = (
            activated.astimezone(user_timezone(timezone_name)).date()
            if activated
            else None
        )
    try:
        result.start_date = date.fromisoformat(str(start_value))
        result.cycle_weeks = max(
            1, int(spec.get("duration_weeks") or definition.get("cycle_weeks") or 1)
        )
    except (ValueError, TypeError):
        result.status = "needs_review"
        result.message = (
            "The programme start date or duration is missing. Review your programme."
        )
        return result

    elapsed = (today - result.start_date).days
    result.week = min(result.cycle_weeks, max(0, elapsed) // 7 + 1)
    result.blocks = deepcopy(definition.get("blocks") or [])
    for block in result.blocks:
        block["is_current"] = (
            int(block.get("start_week") or 1)
            <= result.week
            <= int(block.get("end_week") or 1)
        )
        if block["is_current"]:
            result.block = block

    result.days = deepcopy(definition.get("days") or [])
    for day in result.days:
        for exercise in day.get("exercises") or []:
            for prescription in exercise.get("prescriptions") or []:
                if prescription.get("block_number") == result.block.get("number"):
                    exercise.update(prescription)
                    exercise["scheme"] = (
                        f"{prescription['sets']} × {prescription['rep_range']}"
                    )
                    break

    if elapsed < 0:
        result.status = "not_started"
        result.message = f"Your programme starts on {result.start_date.isoformat()}."
        return result
    if elapsed >= result.cycle_weeks * 7:
        result.status = "completed"
        result.message = (
            "This programme is complete. Review it before starting a new programme."
        )
        return result
    if not result.days:
        result.status = "needs_review"
        result.message = "No routine rotation is available. Review your programme."
        return result

    eligible: list[tuple[datetime, dict[str, Any]]] = []
    seen: set[str] = set()
    for workout in workouts:
        if not isinstance(workout, dict):
            continue
        identifier = str(workout.get("id") or "")
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        timestamp = _timestamp(workout.get("end_time") or workout.get("start_time"))
        if (
            timestamp
            and result.start_date
            <= timestamp.astimezone(user_timezone(timezone_name)).date()
            <= today
        ):
            eligible.append((timestamp, workout))

    if eligible:
        completed_at, latest = max(eligible, key=lambda entry: entry[0])
        result.last_completed_at = completed_at.isoformat()
        completed_position = _match_day(latest, result.days)
        if completed_position is None:
            result.status = "needs_review"
            result.message = (
                "The latest workout could not be matched to this programme. "
                "Review your routine mapping."
            )
            return result
        position = (completed_position + 1) % len(result.days)
    else:
        position = 0

    result.message = "Your next planned workout."
    result.status = "active"
    result.next_day = result.days[position]
    return result
