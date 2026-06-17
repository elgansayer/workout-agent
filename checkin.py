"""Periodic programme check-ins.

Every four to six weeks (driven by how quickly a block's worth of sessions is
logged, with a calendar cap), the agent reviews the actual logged data against
the planned targets and sends a "Check-in N" summary with concrete adjustments.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from config import Config
from database import (
    get_meta,
    get_programme_start_date,
    save_checkin,
    set_meta,
)
from gemini_engine import generate_checkin_message
from hevy_client import get_exercise_history, get_workout_count
from program import Block, day_exercises

logger = logging.getLogger(__name__)

# One training block is roughly four weeks at six sessions a week.
CHECKIN_WORKOUT_TARGET = 24
# Never let more than six weeks pass without a review.
CHECKIN_MAX_WEEKS = 6
# With no Hevy count available, fall back to a purely calendar cadence.
CHECKIN_MIN_WEEKS = 4

_KEY_NUMBER = "checkin_number"
_KEY_LAST_DATE = "last_checkin_date"
_KEY_LAST_COUNT = "last_checkin_workout_count"


@dataclass(frozen=True)
class CheckinDue:
    number: int          # this check-in's sequence number (1, 2, 3, ...)
    workouts_done: int   # sessions logged since the last check-in
    weeks_elapsed: int
    total_count: int | None


@dataclass(frozen=True)
class LiftReview:
    name: str
    planned: str            # e.g. "4 x 5-8"
    sessions: int
    latest: str             # e.g. "120 kg x 6" or "8 reps"
    change_kg: float | None  # weight delta across the window
    hit_top: bool | None
    stalled: bool


def _top_rep(rep_range: str) -> int | None:
    numbers = re.findall(r"\d+", rep_range)
    return int(numbers[-1]) if numbers else None


def _weeks_between(start: date, today: date) -> int:
    return max((today - start).days // 7, 0)


def _last_checkin_date(config: Config) -> date:
    value = get_meta(_KEY_LAST_DATE, config.database_path)
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return get_programme_start_date(config.database_path)


def _seed_baseline_if_missing(config: Config, total_count: int | None) -> None:
    """Initialise check-in tracking the first time we ever see this account."""
    if get_meta(_KEY_NUMBER, config.database_path) is None:
        set_meta(_KEY_NUMBER, "0", config.database_path)
        set_meta(_KEY_LAST_DATE, date.today().isoformat(), config.database_path)
    if total_count is not None and get_meta(_KEY_LAST_COUNT, config.database_path) is None:
        set_meta(_KEY_LAST_COUNT, str(total_count), config.database_path)


def due(config: Config, today: date | None = None) -> CheckinDue | None:
    """Return check-in details if one is due, otherwise None."""
    if today is None:
        today = date.today()

    total_count = (
        get_workout_count(config.hevy_api_key) if config.hevy_api_key else None
    )
    _seed_baseline_if_missing(config, total_count)

    last_date = _last_checkin_date(config)
    weeks = _weeks_between(last_date, today)
    number = int(get_meta(_KEY_NUMBER, config.database_path) or "0") + 1

    last_count_raw = get_meta(_KEY_LAST_COUNT, config.database_path)
    if total_count is not None and last_count_raw is not None:
        workouts_done = max(total_count - int(last_count_raw), 0)
        if workouts_done >= CHECKIN_WORKOUT_TARGET or weeks >= CHECKIN_MAX_WEEKS:
            return CheckinDue(number, workouts_done, weeks, total_count)
        return None

    # No Hevy count: fall back to a calendar cadence so check-ins still happen.
    if weeks >= CHECKIN_MIN_WEEKS:
        return CheckinDue(number, 0, weeks, total_count)
    return None


def _session_top_sets(
    history: list[dict],
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
            current[0] if current and current[0] is not None else -1.0,
            current[1] if current and current[1] is not None else -1,
        ) if current is not None else None
        if current_key is None or key > current_key:
            best[when] = (weight, reps)
    return [best[when] for when in sorted(best)]


def _review_exercise(name: str, planned: str, rep_range: str,
                     history: list[dict]) -> LiftReview:
    tops = _session_top_sets(history)
    sessions = len(tops)
    latest_w, latest_r = tops[-1] if tops else (None, None)
    first_w, _ = tops[0] if tops else (None, None)

    change = (
        round(latest_w - first_w, 2)
        if latest_w is not None and first_w is not None
        else None
    )
    top_rep = _top_rep(rep_range)
    hit_top = (
        latest_r is not None and top_rep is not None and latest_r >= top_rep
    )
    stalled = sessions >= 3 and change is not None and change <= 0 and hit_top

    if latest_w is not None and latest_r is not None:
        latest = f"{latest_w:g} kg x {latest_r}"
    elif latest_r is not None:
        latest = f"{latest_r} reps"
    else:
        latest = "no data"

    return LiftReview(name, planned, sessions, latest, change, hit_top, stalled)


def _analyse(config: Config, block: Block) -> list[LiftReview]:
    """Compare planned targets to logged data for each lift in the block."""
    if not config.hevy_api_key:
        return []
    start_iso = _last_checkin_date(config).isoformat()
    reviews: list[LiftReview] = []
    seen: set[str] = set()
    for day in (1, 2, 3):
        for ex in day_exercises(day, block):
            if ex.name in seen or ex.template_id is None:
                continue
            seen.add(ex.name)
            history = (
                get_exercise_history(config.hevy_api_key, ex.template_id, start_iso)
                or []
            )
            planned = f"{ex.sets} x {ex.rep_range}"
            reviews.append(
                _review_exercise(ex.name, planned, ex.rep_range, history)
            )
    return reviews


def _analysis_text(reviews: list[LiftReview]) -> str:
    if not reviews:
        return "No logged Hevy data is available for this period."
    lines = []
    for r in reviews:
        line = f"- {r.name}: planned {r.planned}; latest {r.latest}; {r.sessions} sessions"
        if r.change_kg is not None and r.change_kg != 0:
            line += f"; weight change {r.change_kg:+g} kg"
        if r.stalled:
            line += "; STALLED (hitting top reps but load not moving)"
        elif r.hit_top:
            line += "; hitting the top of the rep range"
        lines.append(line)
    return "\n".join(lines)


def _fallback_message(due_info: CheckinDue, block: Block,
                      reviews: list[LiftReview]) -> str:
    lines = [
        f"Check-in {due_info.number}: Block {block.number} ({block.name})",
        f"{due_info.workouts_done} sessions logged over "
        f"{due_info.weeks_elapsed} weeks.",
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
    lines.append("Keep logging every set and filming your top deadlift and pull-up.")
    return "\n".join(lines)


def run_checkin(config: Config, due_info: CheckinDue, week: int,
                block: Block) -> str:
    """Build the check-in message from logged data versus the plan."""
    reviews = _analyse(config, block)
    fallback = _fallback_message(due_info, block, reviews)
    return generate_checkin_message(
        api_key=config.gemini_api_key,
        model_name=config.gemini_model,
        number=due_info.number,
        week=week,
        block=block,
        workouts_done=due_info.workouts_done,
        weeks=due_info.weeks_elapsed,
        analysis_text=_analysis_text(reviews),
        fallback=fallback,
    )


def record(config: Config, due_info: CheckinDue, message: str,
           today: date | None = None) -> None:
    """Persist the completed check-in and reset the tracking baseline."""
    if today is None:
        today = date.today()
    set_meta(_KEY_NUMBER, str(due_info.number), config.database_path)
    set_meta(_KEY_LAST_DATE, today.isoformat(), config.database_path)
    if due_info.total_count is not None:
        set_meta(_KEY_LAST_COUNT, str(due_info.total_count), config.database_path)
    save_checkin(
        due_info.number,
        today.isoformat(),
        due_info.workouts_done,
        due_info.weeks_elapsed,
        message,
        config.database_path,
    )
