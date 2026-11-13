"""Deterministic week materialisation, cadence and exposure tracking.

Everything here is a pure function of a stored revision definition, the
programme start date, the user's explicit time zone and the completed-exposure
records.  Nothing reads the clock or the database, so the first and every later
week state can be re-derived from the immutable revision and compared with what
was stored.

Two cadence modes are first-class:

``rolling``
    Routines follow the user's rotation order.  Only a *completed* exposure
    advances the rotation; a passed weekday never skips a routine.  Dates in a
    rolling week are nominal guidance and are reported as ``carried_over`` once
    they pass without a session.

``calendar``
    Routines are placed on explicit dates by the preview's weekly cadence.  A
    scheduled session whose date passed without a workout is reported as
    ``missed``; it is never silently rescheduled.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CADENCE_ROLLING = "rolling"
CADENCE_CALENDAR = "calendar"

STATUS_PLANNED = "planned"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_SUBSTITUTED = "substituted"
STATUS_MISSED = "missed"
STATUS_CARRIED_OVER = "carried_over"

#: A session below this share of its planned working sets is ``partial``.
PARTIAL_SET_SHARE = 0.8


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return the user's zone; an unknown legacy value falls back to UTC."""

    try:
        return ZoneInfo(str(name or "UTC"))
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def local_date_of(value: Any, timezone_name: str | None) -> date | None:
    """The user's local calendar date for a provider timestamp."""

    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(resolve_timezone(timezone_name)).date()


def week_bounds(start_date: date, week_number: int) -> tuple[date, date]:
    first = start_date + timedelta(weeks=week_number - 1)
    return first, first + timedelta(days=6)


def week_for_date(start_date: date, duration_weeks: int, day: date) -> int | None:
    """The programme week containing *day*, or ``None`` outside the programme."""

    elapsed = (day - start_date).days
    if elapsed < 0 or elapsed >= duration_weeks * 7:
        return None
    return elapsed // 7 + 1


def cadence_mode(definition: Mapping[str, Any]) -> str:
    """Resolve ``rolling`` or ``calendar`` from a stored definition.

    The generated preview stores the cadence as ``{"mode": "rolling"|"weekly"}``;
    ``weekly`` is the explicit calendar cadence.  A definition without any
    cadence metadata keeps the historical calendar behaviour.
    """

    spec = definition.get("programme_spec")
    candidates: list[Any] = [
        definition.get("cadence"),
        definition.get("rotation_mode"),
        spec.get("cadence") if isinstance(spec, Mapping) else None,
        spec.get("rotation_mode") if isinstance(spec, Mapping) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            candidate = candidate.get("mode")
        text = str(candidate or "").strip().lower().replace("-", "_")
        if text in {"rolling", "rolling_order", "rotation", "rotate", "sequence"}:
            return CADENCE_ROLLING
        if text in {"weekly", "calendar", "scheduled", "schedule", "weekday", "weekdays"}:
            return CADENCE_CALENDAR
    return CADENCE_CALENDAR


def sessions_per_week(definition: Mapping[str, Any]) -> int:
    cadence = definition.get("cadence")
    if isinstance(cadence, Mapping):
        days = cadence.get("training_days")
        if isinstance(days, list) and days:
            return len(days)
        value = cadence.get("sessions_per_week")
        if isinstance(value, int) and value > 0:
            return value
    spec = definition.get("programme_spec")
    if isinstance(spec, Mapping):
        value = spec.get("sessions_per_week")
        if isinstance(value, int) and value > 0:
            return value
    return max(1, len(definition.get("days") or []))


def rolling_pointer(
    days: Sequence[Mapping[str, Any]],
    completed_positions: Iterable[int | None],
) -> int:
    """Index of the routine that follows the most recent completed one.

    ``completed_positions`` are rotation positions (``days[].number``) in
    chronological order.  Unknown positions are ignored, so unmatched workouts
    never move the rotation.
    """

    numbers = [int(day.get("number") or index + 1) for index, day in enumerate(days)]
    last_index: int | None = None
    for position in completed_positions:
        if position in numbers:
            last_index = numbers.index(int(position))
    if last_index is None or not numbers:
        return 0
    return (last_index + 1) % len(numbers)


@dataclass(frozen=True)
class PlannedExposure:
    """One planned routine exposure within a materialised week."""

    slot: int
    routine_position: int
    source_routine_id: str
    title: str
    scheduled_date: str | None
    status: str = STATUS_PLANNED
    workout_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "routine_position": self.routine_position,
            "source_routine_id": self.source_routine_id,
            "title": self.title,
            "scheduled_date": self.scheduled_date,
            "status": self.status,
            "workout_id": self.workout_id,
        }


@dataclass(frozen=True)
class WeekPlan:
    """A dated, reproducible plan for one programme week."""

    week_number: int
    start_date: str
    end_date: str
    cadence: str
    timezone: str
    block: dict[str, Any]
    exposures: tuple[PlannedExposure, ...]

    @property
    def next_exposure(self) -> PlannedExposure | None:
        for exposure in self.exposures:
            if exposure.status in {STATUS_PLANNED, STATUS_CARRIED_OVER}:
                return exposure
        return None

    def to_dict(self) -> dict[str, Any]:
        next_exposure = self.next_exposure
        return {
            "week_number": self.week_number,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "cadence": self.cadence,
            "timezone": self.timezone,
            "block": dict(self.block),
            "exposures": [item.to_dict() for item in self.exposures],
            "next_exposure": next_exposure.to_dict() if next_exposure else None,
        }


def _block_for_week(definition: Mapping[str, Any], week_number: int) -> dict[str, Any]:
    for block in definition.get("blocks") or []:
        try:
            if int(block["start_week"]) <= week_number <= int(block["end_week"]):
                return {
                    "position": int(block.get("number") or block.get("position") or 0),
                    "key": str(block.get("key") or ""),
                    "name": str(block.get("name") or ""),
                }
        except (KeyError, TypeError, ValueError):
            continue
    return {}


def _day_fields(day: Mapping[str, Any], index: int) -> tuple[int, str, str]:
    return (
        int(day.get("number") or index + 1),
        str(day.get("routine_id") or ""),
        str(day.get("focus") or day.get("title") or ""),
    )


def plan_week(
    definition: Mapping[str, Any],
    *,
    start_date: date,
    duration_weeks: int,
    week_number: int,
    timezone_name: str,
    rotation_pointer: int = 0,
) -> WeekPlan:
    """Materialise the dated exposures of one week from a stored definition.

    ``rotation_pointer`` is the index (into ``definition["days"]``) of the next
    routine at the start of a rolling week.  It is ignored for the calendar
    cadence, where the stored schedule decides the routine for every date.
    """

    if not 1 <= week_number <= duration_weeks:
        raise ValueError(f"Week {week_number} is outside the programme duration.")
    days = list(definition.get("days") or [])
    if not days:
        raise ValueError("The programme has no routines to materialise.")
    first, last = week_bounds(start_date, week_number)
    mode = cadence_mode(definition)
    schedule = [
        entry
        for entry in definition.get("schedule") or []
        if isinstance(entry, Mapping) and int(entry.get("week") or 0) == week_number
    ]
    schedule.sort(key=lambda entry: (str(entry.get("date")), int(entry.get("rotation_position") or 0)))
    exposures: list[PlannedExposure] = []

    if mode == CADENCE_CALENDAR and schedule:
        by_position = {
            _day_fields(day, index)[0]: _day_fields(day, index)
            for index, day in enumerate(days)
        }
        for slot, entry in enumerate(schedule, start=1):
            position = int(entry.get("rotation_position") or 0)
            fields = by_position.get(position)
            if fields is None:
                continue
            exposures.append(
                PlannedExposure(
                    slot=slot,
                    routine_position=position,
                    source_routine_id=fields[1],
                    title=fields[2],
                    scheduled_date=str(entry.get("date")),
                )
            )
    else:
        count = sessions_per_week(definition)
        for period in definition.get("fatigue_periods") or []:
            reduced = period.get("sessions_per_week") if isinstance(period, Mapping) else None
            if isinstance(reduced, int) and week_number in (period.get("weeks") or []):
                count = max(1, min(count, reduced))
        pointer = rotation_pointer % len(days)
        for slot in range(count):
            position, routine_id, title = _day_fields(
                days[(pointer + slot) % len(days)], (pointer + slot) % len(days)
            )
            nominal = schedule[slot].get("date") if slot < len(schedule) else None
            exposures.append(
                PlannedExposure(
                    slot=slot + 1,
                    routine_position=position,
                    source_routine_id=routine_id,
                    title=title,
                    scheduled_date=str(nominal) if nominal else None,
                )
            )

    return WeekPlan(
        week_number=week_number,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        cadence=mode,
        timezone=resolve_timezone(timezone_name).key,
        block=_block_for_week(definition, week_number),
        exposures=tuple(exposures),
    )


# ---------------------------------------------------------------------------
# Exposure classification and week summary
# ---------------------------------------------------------------------------


def classify_session(
    planned: Sequence[tuple[str, int]],
    performed: Mapping[str, int],
    substitutions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Classify one session against its planned exercises.

    ``planned`` is ``(template_id, planned_working_sets)``; ``performed`` maps a
    template ID to the working sets actually logged; ``substitutions`` maps a
    planned template to the exercise the user intentionally performed instead
    (a recorded user decision, never inferred).
    """

    substitution_map = dict(substitutions or {})
    planned_sets = 0
    completed_sets = 0
    skipped: list[str] = []
    used: list[dict[str, str]] = []
    covered: set[str] = set()
    for template_id, sets in planned:
        wanted = max(1, int(sets))
        planned_sets += wanted
        done = int(performed.get(template_id, 0))
        covered.add(template_id)
        replacement = substitution_map.get(template_id)
        if done == 0 and replacement and int(performed.get(replacement, 0)) > 0:
            done = int(performed[replacement])
            covered.add(replacement)
            used.append({"planned": template_id, "performed": replacement})
        completed_sets += min(done, wanted)
        if done == 0:
            skipped.append(template_id)
    extra = sorted(
        template_id for template_id, count in performed.items() if count and template_id not in covered
    )
    if skipped or (planned_sets and completed_sets < PARTIAL_SET_SHARE * planned_sets):
        status = STATUS_PARTIAL
    elif used:
        status = STATUS_SUBSTITUTED
    else:
        status = STATUS_COMPLETED
    return {
        "status": status,
        "planned_sets": planned_sets,
        "completed_sets": completed_sets,
        "skipped_exercises": skipped,
        "substitutions": used,
        "extra_exercises": extra,
    }


def summarise_week(
    plan: WeekPlan,
    exposures: Sequence[Mapping[str, Any]],
    *,
    today: date,
    definition: Mapping[str, Any],
) -> tuple[WeekPlan, dict[str, Any]]:
    """Fold recorded exposures into a plan and compute the ``actual`` summary.

    Returns the plan with slot statuses applied and a JSON-safe summary.  Only
    matched exposures (``routine_position`` set) fill slots; unmatched and
    ambiguous workouts are listed but never advance the rotation.
    """

    matched = [item for item in exposures if item.get("routine_position") is not None]
    unmatched = [
        str(item.get("workout_id"))
        for item in exposures
        if item.get("routine_position") is None
        and item.get("match_confidence") != "ambiguous"
    ]
    ambiguous = [
        str(item.get("workout_id"))
        for item in exposures
        if item.get("match_confidence") == "ambiguous"
    ]
    matched.sort(key=lambda item: (str(item.get("performed_at")), str(item.get("workout_id"))))
    days = list(definition.get("days") or [])
    slots = list(plan.exposures)
    result: list[PlannedExposure] = []
    used: set[int] = set()

    if plan.cadence == CADENCE_ROLLING:
        count = len(slots)
        for index, item in enumerate(matched[:count]):
            position = int(item["routine_position"])
            fields = next(
                (
                    _day_fields(day, i)
                    for i, day in enumerate(days)
                    if _day_fields(day, i)[0] == position
                ),
                (position, "", ""),
            )
            result.append(
                PlannedExposure(
                    slot=index + 1,
                    routine_position=position,
                    source_routine_id=fields[1],
                    title=fields[2],
                    scheduled_date=slots[index].scheduled_date,
                    status=str(item.get("status") or STATUS_COMPLETED),
                    workout_id=str(item.get("workout_id")),
                )
            )
        remaining = max(0, count - len(result))
        pointer = rolling_pointer(days, [item.get("routine_position") for item in matched]) if matched else None
        base = pointer if pointer is not None else (
            next((i for i, day in enumerate(days) if _day_fields(day, i)[0] == slots[0].routine_position), 0)
            if slots
            else 0
        )
        for offset in range(remaining):
            index = (base + offset) % len(days) if days else 0
            position, routine_id, title = _day_fields(days[index], index)
            nominal = slots[len(result)].scheduled_date if len(result) < len(slots) else None
            carried = bool(nominal and date.fromisoformat(nominal) < today)
            result.append(
                PlannedExposure(
                    slot=len(result) + 1,
                    routine_position=position,
                    source_routine_id=routine_id,
                    title=title,
                    scheduled_date=nominal,
                    status=STATUS_CARRIED_OVER if carried else STATUS_PLANNED,
                )
            )
    else:
        for slot in slots:
            match_index = next(
                (
                    index
                    for index, item in enumerate(matched)
                    if index not in used
                    and int(item["routine_position"]) == slot.routine_position
                ),
                None,
            )
            if match_index is not None:
                used.add(match_index)
                item = matched[match_index]
                result.append(
                    PlannedExposure(
                        slot=slot.slot,
                        routine_position=slot.routine_position,
                        source_routine_id=slot.source_routine_id,
                        title=slot.title,
                        scheduled_date=slot.scheduled_date,
                        status=str(item.get("status") or STATUS_COMPLETED),
                        workout_id=str(item.get("workout_id")),
                    )
                )
                continue
            passed = bool(slot.scheduled_date and date.fromisoformat(slot.scheduled_date) < today)
            result.append(
                PlannedExposure(
                    slot=slot.slot,
                    routine_position=slot.routine_position,
                    source_routine_id=slot.source_routine_id,
                    title=slot.title,
                    scheduled_date=slot.scheduled_date,
                    status=STATUS_MISSED if passed else STATUS_PLANNED,
                )
            )

    planned_count = len(result)
    counts = {
        key: sum(1 for item in matched if item.get("status") == key)
        for key in (STATUS_COMPLETED, STATUS_PARTIAL, STATUS_SUBSTITUTED)
    }
    credit = counts[STATUS_COMPLETED] + counts[STATUS_SUBSTITUTED] + 0.5 * counts[STATUS_PARTIAL]
    adherence = round(min(1.0, credit / planned_count), 3) if planned_count else None
    skipped = sorted(
        {
            str(template)
            for item in matched
            for template in item.get("skipped_exercises") or []
        }
    )
    substitutions = [
        dict(entry)
        for item in matched
        for entry in item.get("substitutions") or []
    ]
    actual = {
        "planned_exposures": planned_count,
        "completed_exposures": counts[STATUS_COMPLETED],
        "partial_exposures": counts[STATUS_PARTIAL],
        "substituted_exposures": counts[STATUS_SUBSTITUTED],
        "skipped_exercises": skipped,
        "substitutions": substitutions,
        "unmatched_workouts": sorted(set(unmatched)),
        "ambiguous_workouts": sorted(set(ambiguous)),
        "adherence": adherence,
        "exposures": [dict(item) for item in exposures],
    }
    return (
        WeekPlan(
            week_number=plan.week_number,
            start_date=plan.start_date,
            end_date=plan.end_date,
            cadence=plan.cadence,
            timezone=plan.timezone,
            block=plan.block,
            exposures=tuple(result),
        ),
        actual,
    )


def paused_weeks(paused_on: date, resumed_on: date) -> int:
    """Whole programme weeks lost to a pause (rounded down, never negative)."""

    return max(0, (resumed_on - paused_on).days // 7)


__all__ = [
    "CADENCE_CALENDAR",
    "CADENCE_ROLLING",
    "PlannedExposure",
    "WeekPlan",
    "cadence_mode",
    "classify_session",
    "local_date_of",
    "parse_timestamp",
    "paused_weeks",
    "plan_week",
    "resolve_timezone",
    "rolling_pointer",
    "sessions_per_week",
    "summarise_week",
    "week_bounds",
    "week_for_date",
]
