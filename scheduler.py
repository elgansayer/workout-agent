"""Unified scheduler: replaces the bash sleep-loop in docker-entrypoint.sh and
the Python sleep-loop in insight_scheduler.py with a single process that
dispatches per-user daily coaching runs and insight generation at the
configured times.

Supports MODE env var (schedule | once | preview) exactly as the entrypoint
did, and reads RUN_AT / TZ for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from datetime import timezone as dt_timezone

from config import Config, ConfigError
from database import init_db

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")

_RUN_AT = os.environ.get("RUN_AT", "07:00")
_TZ = os.environ.get("TZ", "UTC")
_INSIGHT_DAILY_AT = os.environ.get("INSIGHT_DAILY_AT", "06:00")
_INSIGHT_WEEKLY_AT = os.environ.get("INSIGHT_WEEKLY_AT", "08:00")


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


def _now_in_zone(tz_name: str) -> datetime:
    """Return the current local time in the given timezone name."""
    if ZoneInfo is not None:
        return datetime.now(tz=ZoneInfo(tz_name))
    return datetime.now(tz=dt_timezone.utc)


def run_coaching(config: Config) -> int:
    """Run the daily coaching message (main.py's run function)."""
    from main import run as main_run

    logger.info("Running daily coaching cycle...")
    try:
        return main_run(preview=False)
    except Exception:
        logger.exception("Daily coaching run failed.")
        return 1


def run_daily_insight(config: Config) -> None:
    """Generate the daily dashboard insight header."""
    from insight_cron import generate_daily_header

    logger.info("Generating daily insight header...")
    try:
        generate_daily_header(config)
    except Exception:
        logger.exception("Daily insight generation failed.")


def run_weekly_correlations(config: Config) -> None:
    """Generate weekly deep correlations."""
    from insight_cron import generate_weekly_correlations

    logger.info("Generating weekly deep correlations...")
    try:
        generate_weekly_correlations(config)
    except Exception:
        logger.exception("Weekly correlations failed.")


def run_once(config: Config) -> int:
    """Run a full scheduled cycle: coaching, daily insight, and weekly (if Sunday)."""
    logger.info("Running scheduled cycle...")
    rc = run_coaching(config)
    run_daily_insight(config)

    now = _now_in_zone(_TZ)
    if now.weekday() == 6:  # Sunday
        run_weekly_correlations(config)
    return rc


def run_schedule(config: Config) -> None:
    """Run forever, waking at the configured times to dispatch jobs.

    This is the single loop that replaces both the bash sleep-loop in
    docker-entrypoint.sh and the Python sleep-loop in insight_scheduler.py.
    """
    coaching_times = _parse_times(_RUN_AT)
    insight_daily_times = _parse_times(_INSIGHT_DAILY_AT)
    insight_weekly_times = _parse_times(_INSIGHT_WEEKLY_AT)

    logger.info(
        "Unified scheduler started: coaching at %s, daily insight at %s, "
        "weekly insight at %s (TZ=%s).",
        _RUN_AT,
        _INSIGHT_DAILY_AT,
        _INSIGHT_WEEKLY_AT,
        _TZ,
    )

    today = _now_in_zone(_TZ).date()
    jobs_run: set[str] = set()

    def _reset_if_new_day() -> None:
        nonlocal today, jobs_run
        current = _now_in_zone(_TZ).date()
        if current != today:
            today = current
            jobs_run.clear()
            logger.info("New day: %s, rescheduling jobs.", today)

    # Run once on startup
    logger.info("Running initial boot cycle...")
    run_once(config)
    jobs_run.add("coaching")
    jobs_run.add("insight_daily")
    if _now_in_zone(_TZ).weekday() == 6:
        jobs_run.add("insight_weekly")

    logger.info(
        "Entering main scheduler loop (wake interval: 30s). "
        "Coaching: %s, Daily insight: %s, Weekly insight: %s.",
        _RUN_AT,
        _INSIGHT_DAILY_AT,
        _INSIGHT_WEEKLY_AT,
    )

    while True:
        _reset_if_new_day()

        now_local = _now_in_zone(_TZ)
        now_h, now_m = now_local.hour, now_local.minute

        if "coaching" not in jobs_run:
            for h, m in coaching_times:
                if now_h == h and now_m == m:
                    logger.info("Coaching time %02d:%02d triggered.", h, m)
                    run_coaching(config)
                    jobs_run.add("coaching")
                    break

        if "insight_daily" not in jobs_run:
            for h, m in insight_daily_times:
                if now_h == h and now_m == m:
                    logger.info("Daily insight time %02d:%02d triggered.", h, m)
                    run_daily_insight(config)
                    jobs_run.add("insight_daily")
                    break

        if "insight_weekly" not in jobs_run and now_local.weekday() == 6:
            for h, m in insight_weekly_times:
                if now_h == h and now_m == m:
                    logger.info("Weekly insight time %02d:%02d triggered.", h, m)
                    run_weekly_correlations(config)
                    jobs_run.add("insight_weekly")
                    break

        time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    mode = os.environ.get("MODE", "schedule").strip().lower()
    logger.info("Mode: %s, RUN_AT: %s, TZ: %s", mode, _RUN_AT, _TZ)

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
        logger.error(
            "Unknown MODE '%s' -- use schedule, once, or preview.", mode
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
