"""Periodic programme check-ins.

Review points come from the active programme's own blocks and cadence.  The
engine compares each materialised prescription with logged Hevy data and never
assumes a fixed four-week block, named lifts, or a twelve-week cycle.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ai_resolver import resolve_provider
from config import Config
from database import (
    get_meta,
    save_checkin,
    save_notification,
    set_meta,
)
from gemini_engine import generate_checkin_message
from hevy_client import get_exercise_history, get_workout_count
from programme_runtime import (
    ACTIVE,
    ProgrammeRuntime,
    load_programme_runtime,
)

logger = logging.getLogger(__name__)

# Fallback cadence when a programme defines no explicit review points.
CHECKIN_MIN_WEEKS = 4
CHECKIN_MAX_WEEKS = 6

_KEY_NUMBER = "checkin_number"
_KEY_LAST_DATE = "last_checkin_date"
_KEY_LAST_COUNT = "last_checkin_workout_count"
_KEY_LAST_WEEK = "last_checkin_week"


@dataclass(frozen=True)
class CheckinDue:
    number: int  # this check-in's sequence number (1, 2, 3, ...)
    workouts_done: int  # sessions logged since the last check-in
    weeks_elapsed: int
    total_count: int | None


@dataclass(frozen=True)
class LiftReview:
    name: str
    planned: str  # e.g. "4 x 5-8"
    sessions: int
    latest: str  # e.g. "120 kg x 6" or "8 reps"
    change_kg: float | None  # weight delta across the window
    hit_top: bool | None
    stalled: bool
    role: str = ""


def _top_rep(rep_range: str) -> int | None:
    numbers = re.findall(r"\d+", rep_range)
    return int(numbers[-1]) if numbers else None


def _weeks_between(start: date, today: date) -> int:
    return max((today - start).days // 7, 0)


def _last_checkin_date(
    config: Config,
    *,
    user_id: str | None = None,
    runtime: ProgrammeRuntime | None = None,
) -> date:
    value = get_meta(_KEY_LAST_DATE, config.database_path, user_id=user_id)
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    if runtime is not None and runtime.start_date:
        return runtime.start_date
    return datetime.now(tz=timezone.utc).date()


def _seed_baseline_if_missing(
    config: Config,
    total_count: int | None,
    *,
    user_id: str | None = None,
) -> None:
    """Initialise check-in tracking for only the acting user."""
    if get_meta(_KEY_NUMBER, config.database_path, user_id=user_id) is None:
        set_meta(_KEY_NUMBER, "0", config.database_path, user_id=user_id)
        set_meta(
            _KEY_LAST_DATE,
            datetime.now(tz=timezone.utc).date().isoformat(),
            config.database_path,
            user_id=user_id,
        )
    if (
        total_count is not None
        and get_meta(_KEY_LAST_COUNT, config.database_path, user_id=user_id) is None
    ):
        set_meta(
            _KEY_LAST_COUNT,
            str(total_count),
            config.database_path,
            user_id=user_id,
        )


def _next_review_week(runtime: ProgrammeRuntime, last_week: int) -> int | None:
    for week in runtime.review_weeks:
        if week > last_week:
            return week
    return None


def _due_workout_target(runtime: ProgrammeRuntime) -> int:
    sessions = max(1, len(runtime.days) or 1)
    interval = CHECKIN_MIN_WEEKS
    if runtime.review_weeks:
        interval = max(1, runtime.review_weeks[0])
    return sessions * interval


def due(
    config: Config,
    today: date | None = None,
    *,
    user_id: str | None = None,
    runtime: ProgrammeRuntime | None = None,
) -> CheckinDue | None:
    """Return check-in details if a programme review point is due."""
    if today is None:
        today = datetime.now(tz=timezone.utc).date()

    if runtime is None:
        if not user_id:
            return None
        runtime = load_programme_runtime(user_id, config.database_path, today=today)
    if runtime.status != ACTIVE or runtime.start_date is None:
        return None

    total_count = (
        get_workout_count(config.hevy_api_key) if config.hevy_api_key else None
    )
    _seed_baseline_if_missing(config, total_count, user_id=user_id)

    last_date = _last_checkin_date(config, user_id=user_id, runtime=runtime)
    weeks = _weeks_between(last_date, today)
    number = (
        int(get_meta(_KEY_NUMBER, config.database_path, user_id=user_id) or "0") + 1
    )

    last_week_raw = get_meta(_KEY_LAST_WEEK, config.database_path, user_id=user_id)
    if last_week_raw is not None:
        try:
            last_week = int(last_week_raw)
        except ValueError:
            last_week = 0
    else:
        last_week = max(0, _weeks_between(runtime.start_date, last_date))

    next_review = _next_review_week(runtime, last_week)
    review_due = next_review is not None and runtime.week >= next_review

    last_count_raw = get_meta(_KEY_LAST_COUNT, config.database_path, user_id=user_id)
    if total_count is not None and last_count_raw is not None:
        workouts_done = max(total_count - int(last_count_raw), 0)
        target = _due_workout_target(runtime)
        if review_due or workouts_done >= target or weeks >= CHECKIN_MAX_WEEKS:
            return CheckinDue(number, workouts_done, weeks, total_count)
        return None

    # No provider count: keep a calendar cadence anchored to the programme.
    if review_due or (next_review is None and weeks >= CHECKIN_MIN_WEEKS):
        return CheckinDue(number, 0, weeks, total_count)
    return None


def _session_top_sets(
    history: list[dict[str, Any]],
) -> list[tuple[float | None, int | None]]:
    """Return each session's top set (heaviest, reps tie-break), oldest first."""
    best: dict[str, tuple[float | None, int | None]] = {}
    for entry in history:
        when = entry.get("workout_start_time") or ""
        weight = entry.get("weight_kg")
        reps = entry.get("reps")
        key = (
            weight if weight is not None else -1.0,
            reps if reps is not None else -1,
        )
        current = best.get(when)
        current_key = (
            (
                current[0] if current and current[0] is not None else -1.0,
                current[1] if current and current[1] is not None else -1,
            )
            if current is not None
            else None
        )
        if current_key is None or key > current_key:
            best[when] = (weight, reps)
    return [best[when] for when in sorted(best)]


def _review_exercise(
    name: str, planned: str, rep_range: str, history: list[dict]
) -> LiftReview:
    tops = _session_top_sets(history)
    sessions = len(tops)
    latest_w, latest_r = tops[-1] if tops else (None, None)
    first_w, first_r = tops[0] if tops else (None, None)

    change = (
        round(latest_w - first_w, 2)
        if latest_w is not None and first_w is not None
        else None
    )
    rep_change = (
        latest_r - first_r if latest_r is not None and first_r is not None else None
    )
    top_rep = _top_rep(rep_range)
    hit_top = latest_r is not None and top_rep is not None and latest_r >= top_rep
    stalled = (
        sessions >= 3
        and change is not None
        and change <= 0
        and rep_change is not None
        and rep_change <= 0
    )

    if latest_w is not None and latest_r is not None:
        latest = f"{latest_w:g} kg x {latest_r}"
    elif latest_r is not None:
        latest = f"{latest_r} reps"
    else:
        latest = "no data"

    return LiftReview(name, planned, sessions, latest, change, hit_top, stalled)


def _analyse(
    config: Config,
    runtime: ProgrammeRuntime,
    *,
    user_id: str | None = None,
) -> list[LiftReview]:
    """Compare the current materialised prescription to logged data."""
    if not config.hevy_api_key:
        return []
    start_iso = _last_checkin_date(
        config, user_id=user_id, runtime=runtime
    ).isoformat()
    reviews: list[LiftReview] = []
    seen: set[str] = set()
    for day in runtime.days:
        for exercise in day.get("exercises") or []:
            template_id = exercise.get("template_id")
            name = str(exercise.get("name") or "")
            if not name or not template_id or name in seen:
                continue
            seen.add(name)
            history = (
                get_exercise_history(config.hevy_api_key, str(template_id), start_iso)
                or []
            )
            planned = str(
                exercise.get("scheme")
                or f"{exercise.get('sets', '')} x {exercise.get('rep_range', '')}"
            )
            rep_range = str(exercise.get("rep_range") or "")
            review = _review_exercise(name, planned, rep_range, history)
            reviews.append(
                LiftReview(
                    review.name,
                    review.planned,
                    review.sessions,
                    review.latest,
                    review.change_kg,
                    review.hit_top,
                    review.stalled,
                    role=str(exercise.get("role") or ""),
                )
            )
    return reviews


def _analysis_text(reviews: list[LiftReview]) -> str:
    if not reviews:
        return "No logged Hevy data is available for this period."
    lines = []
    for r in reviews:
        line = (
            f"- {r.name}: planned {r.planned}; latest {r.latest}; {r.sessions} sessions"
        )
        if r.role:
            line += f"; role {r.role}"
        if r.change_kg is not None and r.change_kg != 0:
            line += f"; weight change {r.change_kg:+g} kg"
        if r.stalled:
            line += "; STALLED (hitting top reps but load not moving)"
        elif r.hit_top:
            line += "; hitting the top of the rep range"
        lines.append(line)
    return "\n".join(lines)


def _fallback_message(
    due_info: CheckinDue,
    runtime: ProgrammeRuntime,
    reviews: list[LiftReview],
) -> str:
    block_name = str((runtime.block or {}).get("name") or "Programme review")
    lines = [
        f"Check-in {due_info.number}: {block_name}",
        (
            f"{due_info.workouts_done} sessions logged over "
            f"{due_info.weeks_elapsed} weeks."
        ),
    ]
    if reviews:
        lines.append("")
        for r in reviews:
            note = ""
            if r.stalled:
                note = " - stalled, consider a small deload then rebuild"
            elif r.change_kg is not None and r.change_kg > 0:
                note = f" - up {r.change_kg:+g} kg, keep pushing"
            lines.append(f"{r.name}: {r.planned}, now {r.latest}{note}")
    lines.append("")
    lines.append("Keep logging every set and following the prescribed programme.")
    return "\n".join(lines)


def run_checkin(
    config: Config,
    due_info: CheckinDue,
    runtime: ProgrammeRuntime,
    *,
    user_id: str | None = None,
) -> str:
    """Build the acting user's check-in with their selected AI provider."""
    reviews = _analyse(config, runtime, user_id=user_id)
    fallback = _fallback_message(due_info, runtime, reviews)
    provider = resolve_provider(
        user_id=user_id,
        server_gemini_key=config.gemini_api_key,
        server_gemini_model=config.gemini_model,
        db_path=config.database_path,
    )
    return generate_checkin_message(
        provider,
        runtime=runtime,
        number=due_info.number,
        workouts_done=due_info.workouts_done,
        weeks=due_info.weeks_elapsed,
        analysis_text=_analysis_text(reviews),
        fallback=fallback,
    )


def record(
    config: Config,
    due_info: CheckinDue,
    message: str,
    today: date | None = None,
    *,
    user_id: str | None = None,
    runtime: ProgrammeRuntime | None = None,
) -> None:
    """Persist the completed check-in and reset this user's tracking baseline."""
    if today is None:
        today = datetime.now(tz=timezone.utc).date()
    set_meta(
        _KEY_NUMBER,
        str(due_info.number),
        config.database_path,
        user_id=user_id,
    )
    set_meta(
        _KEY_LAST_DATE,
        today.isoformat(),
        config.database_path,
        user_id=user_id,
    )
    if runtime is not None:
        set_meta(
            _KEY_LAST_WEEK,
            str(runtime.week),
            config.database_path,
            user_id=user_id,
        )
    if due_info.total_count is not None:
        set_meta(
            _KEY_LAST_COUNT,
            str(due_info.total_count),
            config.database_path,
            user_id=user_id,
        )
    save_checkin(
        due_info.number,
        today.isoformat(),
        due_info.workouts_done,
        due_info.weeks_elapsed,
        message,
        config.database_path,
        user_id=user_id,
    )
    if user_id:
        save_notification(
            user_id,
            title=f"✨ Coach Check-in #{due_info.number} Ready",
            message=f"Reviewed {due_info.workouts_done} workouts across {due_info.weeks_elapsed} weeks.",
            type="coach",
            link="/checkins",
            db_path=config.database_path,
        )
