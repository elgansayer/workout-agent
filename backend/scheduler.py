"""Unified scheduler: replaces the bash sleep-loop in docker-entrypoint.sh and
the Python sleep-loop in insight_scheduler.py with a single process that
dispatches per-user daily coaching runs and insight generation at the
configured times.

Supports MODE env var (schedule | once | preview) exactly as the entrypoint
did. RUN_AT / INSIGHT_*_AT are *local wall-clock times*: each user's jobs fire at
those times in that user's own saved time zone (``users.timezone``), never in
the container's ``TZ``. ``TZ`` only seeds accounts that predate per-user
settings (see ``user_settings.ensure_settings_schema``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from config import Config, ConfigError
from database import get_all_users, init_db
from localisation import resolve_timezone
from programme_runtime import user_timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")

_RUN_AT = os.environ.get("RUN_AT", "07:00")
_TZ = os.environ.get("TZ", "UTC")
_INSIGHT_DAILY_AT = os.environ.get("INSIGHT_DAILY_AT", "06:00")
_INSIGHT_WEEKLY_AT = os.environ.get("INSIGHT_WEEKLY_AT", "08:00")


def _parse_run_at() -> list[str]:
    """Parse comma/space-separated HH:MM strings into sorted list of 'HH:MM' strings."""
    raw = os.environ.get("RUN_AT", "07:00")
    times: set[str] = set()
    for t in raw.replace(",", " ").split():
        t = t.strip()
        if not t:
            continue
        try:
            parts = t.split(":")
            if len(parts) == 2:
                h, m = int(parts[0]), int(parts[1])
                times.add(f"{h:02d}:{m:02d}")
        except ValueError:
            logger.warning("Ignoring unparseable time '%s'", t)
    return sorted(times) or ["07:00"]


def _parse_times(raw: str) -> list[tuple[int, int]]:
    """Parse comma/space-separated HH:MM strings into (hour, minute) pairs."""
    pairs: list[tuple[int, int]] = []
    for t in raw.replace(",", " ").split():
        try:
            h, m = t.strip().split(":")
            pairs.append((int(h), int(m)))
        except (ValueError, IndexError):
            logger.warning("Ignoring unparseable time '%s'", t)
    return pairs or [(7, 0)]


def _now_in_tz(tz_name: str) -> datetime:
    """Return the current local time in the given timezone name."""
    try:
        return datetime.now(tz=ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001
        return datetime.now(tz=timezone.utc)


def _is_due(tz: str, run_times: list[str]) -> bool:
    """Check if the current time in the timezone is in run_times."""
    now = _now_in_tz(tz)
    curr = f"{now.hour:02d}:{now.minute:02d}"
    return curr in run_times


# A slot stays open this long after its local wall-clock time. That covers a
# busy scheduler and the skipped hour on a spring-forward day, while a user who
# signs up mid-afternoon is not handed this morning's run.
_CATCH_UP = timedelta(minutes=60)
_SUNDAY = 6


class DueJob(NamedTuple):
    """A job whose slot is open in one user's local time and has not run today."""

    user_id: str
    job: str
    local_day: str


def _user_now(user: Mapping[str, Any], now: datetime) -> datetime:
    """The instant *now* expressed in the user's own saved time zone."""
    return now.astimezone(user_timezone(resolve_timezone(user.get("timezone"))))


def _slot_due(local_now: datetime, slot: tuple[int, int]) -> bool:
    """True from a slot's local wall-clock time until the catch-up window ends.

    Both values share one tzinfo, so Python compares wall-clock fields. That is
    the intended meaning of "07:00 local" across DST changes.
    """
    start = local_now.replace(hour=slot[0], minute=slot[1], second=0, microsecond=0)
    return timedelta(0) <= local_now - start < _CATCH_UP


def due_jobs(
    users: Sequence[Mapping[str, Any]],
    now: datetime,
    schedule: Mapping[str, Sequence[tuple[int, int]]],
    done: Collection[tuple[str, str, str]],
) -> list[DueJob]:
    """Return the jobs due for each user at the instant *now*.

    ``schedule`` maps a job name to its local ``(hour, minute)`` slots.
    ``insight_weekly`` only runs on the user's local Sunday. ``done`` holds
    ``(user_id, job, local_day)`` keys, so a job runs once per user per local
    day even though the loop wakes many times inside the slot.
    """
    due: list[DueJob] = []
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        local_now = _user_now(user, now)
        local_day = local_now.date().isoformat()
        for job, slots in schedule.items():
            if job == "insight_weekly" and local_now.weekday() != _SUNDAY:
                continue
            if (user_id, job, local_day) in done:
                continue
            if any(_slot_due(local_now, slot) for slot in slots):
                due.append(DueJob(user_id, job, local_day))
    return due


def _run_coaching(user_id: str) -> bool:
    """Run daily coaching for a user."""
    try:
        res = subprocess.run(
            [sys.executable, "main.py", "--user-id", user_id], check=True
        )
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run_insight_job(flag: str) -> bool:
    """Run insight cron job with the given flag."""
    try:
        res = subprocess.run([sys.executable, "insight_cron.py", flag], check=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run_dead_code_sweep() -> bool:
    """Run dead code sweep."""
    try:
        res = subprocess.run([sys.executable, "dead_code_sweep.py"], check=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run_commit_hygiene() -> bool:
    """Run commit hygiene check."""
    try:
        res = subprocess.run([sys.executable, "commit_hygiene.py"], check=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _run_connector_health() -> bool:
    """Run connector health check."""
    try:
        res = subprocess.run([sys.executable, "connector_health.py"], check=True)
        return res.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def run_scheduler() -> None:
    """Main scheduler loop."""
    run_times = _parse_run_at()
    _run_insight_job("--daily")
    _run_insight_job("--weekly")
    _run_dead_code_sweep()
    _run_commit_hygiene()
    _run_connector_health()

    while True:
        users = get_all_users()
        for u in users:
            tz = u.get("timezone") or "UTC"
            uid = u.get("id")
            if uid and _is_due(tz, run_times):
                try:
                    _run_coaching(uid)
                except Exception:
                    logger.exception("Coaching failed for user %s", uid)
        time.sleep(30)


def run_coaching(config: Config, user_ids: Collection[str] | None = None) -> int:
    """Run one isolated daily coaching cycle per persisted user.

    ``user_ids`` limits the run to those users (the per-user scheduler passes
    exactly the users whose local slot is open).
    """
    from main import run as main_run

    logger.info("Running daily coaching cycles...")
    result = 0
    users = get_all_users(config.database_path)
    if user_ids is not None:
        users = [u for u in users if u.get("id") in user_ids]
    elif not users:
        try:
            return main_run(preview=False)
        except Exception:
            logger.exception("Daily coaching run failed.")
            return 1

    for user in users:
        user_id = user.get("id")
        if not user_id:
            logger.warning("Skipping user row without an id.")
            result = max(result, 1)
            continue
        try:
            result = max(result, main_run(preview=False, user_id=user_id))
        except Exception:
            logger.exception("Daily coaching run failed for user %s.", user_id)
            result = max(result, 1)
    return result


def run_daily_insight(config: Config, user_ids: Collection[str] | None = None) -> None:
    """Generate a tenant-scoped daily dashboard insight (all users by default)."""
    from insight_cron import generate_daily_header

    logger.info("Generating daily insight headers...")
    for user in get_all_users(config.database_path):
        user_id = user["id"]
        if user_ids is not None and user_id not in user_ids:
            continue
        try:
            generate_daily_header(config, user_id=user_id)
        except Exception:
            logger.exception("Daily insight generation failed for user %s.", user_id)


def run_weekly_correlations(
    config: Config, user_ids: Collection[str] | None = None
) -> None:
    """Generate tenant-scoped weekly correlations (all users by default)."""
    from insight_cron import generate_weekly_correlations

    logger.info("Generating weekly deep correlations...")
    for user in get_all_users(config.database_path):
        user_id = user["id"]
        if user_ids is not None and user_id not in user_ids:
            continue
        try:
            generate_weekly_correlations(config, user_id=user_id)
        except Exception:
            logger.exception("Weekly correlations failed for user %s.", user_id)


def run_once(config: Config) -> int:
    """Run a full cycle now: coaching, daily insight, and weekly (Sunday users).

    The weekly report goes to users whose *local* day is Sunday, not whoever
    happens to be on a server-local Sunday.
    """
    logger.info("Running scheduled cycle...")
    rc = run_coaching(config)
    run_daily_insight(config)

    now = datetime.now(tz=timezone.utc)
    sunday_users = [
        user["id"]
        for user in get_all_users(config.database_path)
        if user.get("id") and _user_now(user, now).weekday() == _SUNDAY
    ]
    if sunday_users:
        run_weekly_correlations(config, sunday_users)
    return rc


def _mark_boot_cycle(
    users: Sequence[Mapping[str, Any]],
    now: datetime,
    done: set[tuple[str, str, str]],
) -> None:
    """Record what the boot cycle already covered so slots do not repeat it."""
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        local_now = _user_now(user, now)
        local_day = local_now.date().isoformat()
        done.add((user_id, "coaching", local_day))
        done.add((user_id, "insight_daily", local_day))
        if local_now.weekday() == _SUNDAY:
            done.add((user_id, "insight_weekly", local_day))


def run_schedule(config: Config) -> None:
    """Run forever, dispatching each user's jobs at *their* local run times.

    This is the single loop that replaces both the bash sleep-loop in
    docker-entrypoint.sh and the Python sleep-loop in insight_scheduler.py.
    Every wake-up re-reads the users so a time-zone change in Settings takes
    effect on the next tick without a restart.
    """
    schedule = {
        "coaching": _parse_times(_RUN_AT),
        "insight_daily": _parse_times(_INSIGHT_DAILY_AT),
        "insight_weekly": _parse_times(_INSIGHT_WEEKLY_AT),
    }
    logger.info(
        "Unified scheduler started: coaching at %s, daily insight at %s, "
        "weekly insight at %s, each in the user's own time zone.",
        _RUN_AT,
        _INSIGHT_DAILY_AT,
        _INSIGHT_WEEKLY_AT,
    )

    done: set[tuple[str, str, str]] = set()

    # Run once on startup
    logger.info("Running initial boot cycle...")
    run_once(config)
    _mark_boot_cycle(
        get_all_users(config.database_path), datetime.now(tz=timezone.utc), done
    )

    logger.info("Entering main scheduler loop (wake interval: 30s).")

    while True:
        now = datetime.now(tz=timezone.utc)
        by_job: dict[str, list[str]] = {}
        for item in due_jobs(get_all_users(config.database_path), now, schedule, done):
            # Mark first: a failing job must never be retried every 30 seconds.
            done.add((item.user_id, item.job, item.local_day))
            by_job.setdefault(item.job, []).append(item.user_id)

        if "coaching" in by_job:
            logger.info("Coaching due for %d user(s).", len(by_job["coaching"]))
            run_coaching(config, by_job["coaching"])
        if "insight_daily" in by_job:
            logger.info("Daily insight due for %d user(s).", len(by_job["insight_daily"]))
            run_daily_insight(config, by_job["insight_daily"])
        if "insight_weekly" in by_job:
            logger.info("Weekly insight due for %d user(s).", len(by_job["insight_weekly"]))
            run_weekly_correlations(config, by_job["insight_weekly"])

        # Local days differ from the UTC day by at most one, so older keys are dead.
        cutoff = (now.date() - timedelta(days=3)).isoformat()
        done.difference_update({key for key in done if key[2] < cutoff})
        time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    mode = os.environ.get("MODE", "schedule").strip().lower()
    logger.info(
        "Mode: %s, RUN_AT: %s (per-user local time; deployment TZ=%s only "
        "seeds accounts created before per-user settings).",
        mode,
        _RUN_AT,
        _TZ,
    )

    try:
        config = Config.load()
        init_db(config.database_path)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    if mode == "once":
        return run_once(config)
    elif mode == "preview":
        from main import run as main_run

        return main_run(preview=True)
    elif mode == "schedule":
        run_schedule(config)
        return 0
    else:
        logger.error("Unknown MODE '%s' -- use schedule, once, or preview.", mode)
        return 1


if __name__ == "__main__":
    sys.exit(main())
