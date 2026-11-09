"""Read-only, deterministic runtime for an activated Hevy programme.

This module is the single tenant-scoped read facade used by web routes, the
scheduled worker and AI explanation code.  It prefers the normalised programme
domain (#970) and falls back to the legacy ``programmes.definition`` row only
while activation flows are still migrating.  It never reads the retired static
``program`` module.

Rotation follows elapsed local calendar days from the programme start date.
Prescriptions are materialised from the current block without changing the
stored snapshot.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SETUP_REQUIRED = "setup_required"
NOT_STARTED = "not_started"
ACTIVE = "active"
PAUSED = "paused"
COMPLETED = "completed"
NEEDS_REVIEW = "needs_review"


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

    status: str = SETUP_REQUIRED
    message: str = "Select routines from Hevy to generate your programme."
    name: str = ""
    goal: str = ""
    source: str = ""
    start_date: date | None = None
    end_date: date | None = None
    week: int = 0
    cycle_weeks: int = 0
    blocks: list[dict[str, Any]] = field(default_factory=list)
    block: dict[str, Any] = field(default_factory=dict)
    days: list[dict[str, Any]] = field(default_factory=list)
    next_day: dict[str, Any] | None = None
    cadence: str = "calendar"
    last_completed_at: str | None = None
    review_weeks: list[int] = field(default_factory=list)
    programme_id: int | None = None
    revision_id: int | None = None
    revision_number: int | None = None

    @property
    def setup_required(self) -> bool:
        return self.status == SETUP_REQUIRED

    @property
    def has_training_today(self) -> bool:
        return self.status == ACTIVE and self.next_day is not None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _review_weeks(blocks: list[dict[str, Any]], cycle_weeks: int) -> list[int]:
    """Review points are block boundaries, not fixed four-week constants."""
    points: set[int] = set()
    for block in blocks:
        try:
            end = int(block.get("end_week") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= end < cycle_weeks:
            points.add(end)
    return sorted(points)


def _definition_from_domain(
    programme: Any,
    revision: Any,
    *,
    timezone_name: str,
) -> dict[str, Any]:
    """Translate a normalised domain revision into the runtime definition shape.

    Newly activated revisions may already contain a full legacy-compatible
    definition in ``revision.settings`` (the activation bridge).  Otherwise the
    normalised routines, blocks and prescriptions are translated here.
    """

    settings = getattr(revision, "settings", None) or {}
    if isinstance(settings, dict) and isinstance(settings.get("days"), list):
        definition = deepcopy(settings)
        definition.setdefault("name", programme.name)
        definition.setdefault(
            "programme_spec", {}
        ).setdefault("start_date", _start_iso(programme, timezone_name))
        definition["programme_spec"].setdefault(
            "duration_weeks", programme.duration_weeks or 1
        )
        definition["programme_spec"].setdefault("goal", programme.goal or "")
        return definition

    routines = list(getattr(revision, "routines", None) or [])
    blocks = list(getattr(revision, "blocks", None) or [])
    prescriptions = list(getattr(revision, "prescriptions", None) or [])

    runtime_blocks = [
        {
            "number": block.position,
            "key": block.key,
            "name": block.name,
            "focus": block.objective,
            "start_week": block.start_week,
            "end_week": block.end_week,
            "weeks": f"{block.start_week}-{block.end_week}",
            "targets": dict(block.targets),
            "target_rir": (block.targets or {}).get("target_rir"),
            "rep_emphasis": (block.targets or {}).get("rep_emphasis", ""),
            "volume_multiplier": (block.targets or {}).get("volume_multiplier", 1.0),
        }
        for block in sorted(blocks, key=lambda item: item.position)
    ]

    by_routine: dict[int, list[Any]] = {}
    for prescription in prescriptions:
        if prescription.routine_position is not None:
            by_routine.setdefault(prescription.routine_position, []).append(
                prescription
            )

    days: list[dict[str, Any]] = []
    for routine in sorted(routines, key=lambda item: item.rotation_position):
        snapshot = routine.snapshot or {}
        source_exercises = snapshot.get("exercises") or []
        exercises: list[dict[str, Any]] = []
        for exercise in source_exercises:
            template_id = str(exercise.get("template_id") or "")
            title = str(exercise.get("title") or template_id or "Exercise")
            matching = [
                item
                for item in by_routine.get(routine.rotation_position, [])
                if item.exercise_template_id == template_id
            ]
            exercise_prescriptions: list[dict[str, Any]] = []
            for item in matching:
                sets = int(
                    (item.set_target or {}).get("sets")
                    or exercise.get("sets")
                    or 1
                )
                rep_range = _rep_range(item, exercise)
                exercise_prescriptions.append(
                    {
                        "block_number": item.block_position,
                        "sets": sets,
                        "rep_range": rep_range,
                        "scheme": f"{sets} × {rep_range}",
                        "week_number": item.week_number,
                    }
                )
            opening = exercise_prescriptions[0] if exercise_prescriptions else {}
            exercises.append(
                {
                    "name": title,
                    "template_id": template_id,
                    "exercise_type": exercise.get("exercise_type"),
                    "role": (
                        matching[0].role if matching else "hypertrophy_accessory"
                    ),
                    "sets": opening.get("sets", exercise.get("sets") or 1),
                    "rep_range": opening.get("rep_range", ""),
                    "scheme": opening.get(
                        "scheme",
                        f"{exercise.get('sets') or 1} × source target",
                    ),
                    "note": exercise.get("notes") or "",
                    "rest_seconds": exercise.get("rest_seconds"),
                    "source_sets": exercise.get("sets"),
                    "source_targets": exercise.get("set_targets") or [],
                    "prescriptions": exercise_prescriptions,
                }
            )
        days.append(
            {
                "number": routine.rotation_position,
                "routine_id": routine.source_routine_id,
                "source_hash": routine.snapshot_hash,
                "focus": routine.title,
                "muscles": snapshot.get("muscle_summary") or [],
                "estimated_duration_minutes": snapshot.get(
                    "estimated_duration_minutes"
                ),
                "cadence": routine.cadence,
                "exercises": exercises,
            }
        )

    return {
        "name": programme.name,
        "source": "hevy",
        "cycle_weeks": programme.duration_weeks or 1,
        "total_days": len(days),
        "programme_spec": {
            "start_date": _start_iso(programme, timezone_name),
            "duration_weeks": programme.duration_weeks or 1,
            "goal": programme.goal or "",
            "sessions_per_week": len(days),
        },
        "blocks": runtime_blocks,
        "days": days,
    }


def _start_iso(programme: Any, timezone_name: str) -> str | None:
    if getattr(programme, "start_date", None):
        return programme.start_date.isoformat()
    activated = _timestamp(getattr(programme, "activated_at", None))
    if activated is None:
        activated = _timestamp(getattr(programme, "created_at", None))
    return (
        activated.astimezone(user_timezone(timezone_name)).date().isoformat()
        if activated
        else None
    )


def _rep_range(prescription: Any, exercise: dict[str, Any]) -> str:
    target = prescription.rep_target or {}
    if target.get("range"):
        value = target["range"]
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return f"{value[0]}-{value[1]}"
        return str(value)
    if target.get("min") or target.get("max"):
        return f"{target.get('min')}-{target.get('max')}"
    source_range = exercise.get("target_rep_range")
    if isinstance(source_range, (list, tuple)) and len(source_range) == 2:
        return f"{source_range[0]}-{source_range[1]}"
    if exercise.get("target_reps"):
        return str(exercise["target_reps"])
    return "source target"


def _normalise_cadence(value: Any) -> str | None:
    """Return ``rolling`` or ``calendar`` for known cadence labels."""
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"rolling", "rolling_order", "rotation", "rotate", "sequence"}:
        return "rolling"
    if text in {"calendar", "scheduled", "schedule", "weekday", "weekdays"}:
        return "calendar"
    return None


def _cadence_for(
    definition: dict[str, Any], days: list[dict[str, Any]]
) -> str:
    """Resolve the programme's rotation mode.

    Explicit ``cadence``/``rotation_mode`` fields win.  Normalised domain
    routines default to ``rotation`` (rolling), while legacy definitions without
    any cadence metadata keep the historical elapsed-day calendar rotation.
    """

    spec = definition.get("programme_spec") or {}
    for value in (
        definition.get("cadence"),
        definition.get("rotation_mode"),
        spec.get("cadence") if isinstance(spec, dict) else None,
        spec.get("rotation_mode") if isinstance(spec, dict) else None,
    ):
        mode = _normalise_cadence(value)
        if mode:
            return mode

    modes = {
        mode
        for day in days
        if (mode := _normalise_cadence(day.get("cadence"))) is not None
    }
    if "rolling" in modes:
        return "rolling"
    if modes == {"calendar"}:
        return "calendar"
    return "calendar"


def _routine_index(
    days: list[dict[str, Any]], routine_id: str, title: str
) -> int | None:
    if routine_id:
        for index, day in enumerate(days):
            if str(day.get("routine_id") or "") == routine_id:
                return index
    if title:
        wanted = title.strip().lower()
        for index, day in enumerate(days):
            if str(day.get("focus") or "").strip().lower() == wanted:
                return index
    return None


def _rolling_position(
    days: list[dict[str, Any]],
    completed: list[tuple[datetime, str, str]],
) -> int:
    """Return the routine after the most recently completed known routine.

    Missed calendar days do not advance the rotation; only a completed session
    moves it to the next routine.
    """

    if not completed:
        return 0
    for _, routine_id, title in sorted(completed, key=lambda item: item[0], reverse=True):
        index = _routine_index(days, routine_id, title)
        if index is not None:
            return (index + 1) % len(days)
    return 0


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
    result.name = str(definition.get("name") or active.get("name") or "")
    result.goal = str(spec.get("goal") or definition.get("goal") or "")
    result.source = "hevy"
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
        result.status = NEEDS_REVIEW
        result.message = (
            "The programme start date or duration is missing. Review your programme."
        )
        return result

    result.end_date = result.start_date + _days_for_weeks(result.cycle_weeks)
    elapsed = (today - result.start_date).days
    result.week = min(result.cycle_weeks, max(0, elapsed) // 7 + 1)
    result.blocks = deepcopy(definition.get("blocks") or [])
    result.review_weeks = _review_weeks(result.blocks, result.cycle_weeks)
    for block in result.blocks:
        start_week = int(block.get("start_week") or 1)
        end_week = int(block.get("end_week") or start_week)
        block.setdefault(
            "start_date",
            (result.start_date + _days_for_weeks(start_week - 1) + _one_day()).isoformat(),
        )
        block.setdefault(
            "end_date",
            (result.start_date + _days_for_weeks(end_week)).isoformat(),
        )
        block["is_current"] = start_week <= result.week <= end_week
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
        result.status = NOT_STARTED
        result.message = f"Your programme starts on {result.start_date.isoformat()}."
        return result
    if elapsed >= result.cycle_weeks * 7:
        result.status = COMPLETED
        result.message = (
            "This programme is complete. Review it before starting a new programme."
        )
        return result
    if not result.days:
        result.status = NEEDS_REVIEW
        result.message = "No routine rotation is available. Review your programme."
        return result

    eligible: list[datetime] = []
    completed: list[tuple[datetime, str, str]] = []
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
            eligible.append(timestamp)
            completed.append(
                (
                    timestamp,
                    str(workout.get("routine_id") or workout.get("routineId") or ""),
                    str(workout.get("title") or ""),
                )
            )

    if eligible:
        result.last_completed_at = max(eligible).isoformat()

    result.cadence = _cadence_for(definition, result.days)
    if result.cadence == "rolling":
        position = _rolling_position(result.days, completed)
    else:
        position = elapsed % len(result.days)

    result.message = "Your workout for today."
    result.status = ACTIVE
    result.next_day = result.days[position]
    return result


def _days_for_weeks(weeks: int):
    from datetime import timedelta

    return timedelta(weeks=max(0, weeks)) - timedelta(days=1)


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def _workouts_for(user_id: str, db_path: str, limit: int = 56) -> list[dict[str, Any]]:
    from database import get_recent_hevy_logs

    try:
        return get_recent_hevy_logs(
            limit=limit, db_path=db_path, user_id=user_id
        )
    except Exception:  # noqa: BLE001 - optional history must not block the plan
        return []


def _timezone_for(user_id: str, db_path: str) -> str:
    from database import get_user_by_id

    try:
        user = get_user_by_id(user_id, db_path)
    except Exception:  # noqa: BLE001 - legacy settings may be unreadable
        user = None
    values: dict[str, Any] = dict(user) if user else {}
    return str(values.get("timezone") or "UTC")


def load_programme_runtime(
    user_id: str,
    db_path: str,
    *,
    today: date | None = None,
    timezone_name: str | None = None,
    workouts: list[dict[str, Any]] | None = None,
) -> ProgrammeRuntime:
    """Load the tenant's active programme through the canonical runtime facade.

    Resolution order:

    1. the normalised programme domain (immutable revision);
    2. the activated Hevy definition while the activation bridge is pending;
    3. an explicit ``setup_required`` state.

    Paused and completed programmes are surfaced as such; they never fall back
    to setup or static programme data.
    """

    timezone_name = timezone_name or _timezone_for(user_id, db_path)
    today = today or local_today(timezone_name)
    history = workouts if workouts is not None else _workouts_for(user_id, db_path)

    domain_programme = None
    domain_revision = None
    try:
        from programme_domain import ProgrammeService

        service = ProgrammeService(db_path)
        domain_programme = service.get_active_programme(user_id)
        if domain_programme is not None:
            domain_revision = domain_programme.current_revision
    except Exception:  # noqa: BLE001 - missing domain rows are a setup state
        domain_programme = None

    if domain_programme is not None and domain_revision is not None:
        definition = _definition_from_domain(
            domain_programme, domain_revision, timezone_name=timezone_name
        )
        runtime = resolve_programme_runtime(
            {
                "source": "hevy",
                "definition": definition,
                "updated_at": domain_programme.activated_at
                or domain_programme.updated_at,
                "name": domain_programme.name,
            },
            history,
            today=today,
            timezone_name=timezone_name,
        )
        runtime.programme_id = domain_programme.id
        runtime.revision_id = domain_revision.id
        runtime.revision_number = domain_revision.revision_number
        return runtime

    from database import get_active_programme

    active = get_active_programme(user_id, db_path=db_path)
    if active is not None:
        runtime = resolve_programme_runtime(
            active, history, today=today, timezone_name=timezone_name
        )
        runtime.programme_id = int(active["id"])
        return runtime

    return _lifecycle_runtime(user_id, db_path, today=today, timezone_name=timezone_name)


def _lifecycle_runtime(
    user_id: str,
    db_path: str,
    *,
    today: date,
    timezone_name: str,
) -> ProgrammeRuntime:
    """Return an explicit paused/completed/setup state for the newest programme."""

    import sqlite3
    from contextlib import closing

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, name, status, goal, duration_weeks, start_date,
                       activated_at, created_at, updated_at
                FROM programmes
                WHERE user_id = ? AND status IN ('paused', 'completed')
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
    except sqlite3.Error:
        row = None

    if row is None:
        return ProgrammeRuntime()

    runtime = ProgrammeRuntime(
        status=str(row["status"]),
        name=str(row["name"] or "Programme"),
        goal=str(row["goal"] or ""),
        source="hevy",
        programme_id=int(row["id"]),
    )
    if row["start_date"]:
        try:
            runtime.start_date = date.fromisoformat(str(row["start_date"]))
        except ValueError:
            runtime.start_date = None
    if runtime.start_date and row["duration_weeks"]:
        runtime.cycle_weeks = int(row["duration_weeks"])
        runtime.end_date = runtime.start_date + _days_for_weeks(runtime.cycle_weeks)
    if runtime.status == PAUSED:
        runtime.message = "This programme is paused. Resume it when you are ready."
    else:
        runtime.message = (
            "This programme is complete. Review it before starting another."
        )
    return runtime


__all__ = [
    "ACTIVE",
    "COMPLETED",
    "NEEDS_REVIEW",
    "NOT_STARTED",
    "PAUSED",
    "SETUP_REQUIRED",
    "ProgrammeRuntime",
    "load_programme_runtime",
    "local_today",
    "resolve_programme_runtime",
    "user_timezone",
]
