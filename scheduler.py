"""Unified scheduler: replaces the bash sleep-loop and Python insight scheduler.

Wakes every 60 seconds, checks each user's local time against the configured
RUN_AT times, and dispatches due coaching runs (`main.py`) and insight jobs
(`insight_cron.py --daily`/`--weekly`). Each per-user dispatch is isolated so
one user's failures do not block another's.

Also runs hourly maintenance sweeps once per hour:
* `dead_code_sweep.py` — catches orphaned modules early.
* `commit_hygiene.py` — checks git history hygiene (.gitignore coverage,
  sensitive files, large binaries, commit-message quality).

Designed to be the single long-running process inside the agent container.
MODE=once / MODE=preview are handled by docker-entrypoint.sh before this
scheduler is started.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from database import get_all_users, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger: logging.Logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_run_at() -> list[str]:
    """Parse the RUN_AT env var into a sorted list of HH:MM strings."""
    raw = os.environ.get("RUN_AT", "07:00").strip()
    times = raw.replace(",", " ").split()
    times = sorted(t for t in times if t)
    if not times:
        times = ["07:00"]
    return times


def _now_in_tz(tz_name: str) -> datetime:
    """Return the current datetime in the given timezone name."""
    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("Invalid timezone '%s', falling back to UTC", tz_name)
        return datetime.now(ZoneInfo("UTC"))


def _is_due(user_tz: str, run_times: list[str]) -> bool:
    """Check whether the current minute matches any configured run time."""
    now = _now_in_tz(user_tz)
    for hhmm in run_times:
        try:
            hour, minute = map(int, hhmm.split(":"))
        except ValueError:
            continue
        if now.hour == hour and now.minute == minute:
            return True
    return False


# ---------------------------------------------------------------------------
# Job dispatch
# ---------------------------------------------------------------------------


def _run_coaching(user_id: str) -> bool:
    """Run a single coaching cycle (main.py) for one user.  Returns True on success."""
    logger.info("Running coaching cycle for user %s ...", user_id)
    try:
        subprocess.run(
            [sys.executable, "main.py"],
            env={**os.environ, "SCHEDULER_USER_ID": user_id},
            check=True,
            timeout=600,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error("Coaching run failed for user %s (exit %s)", user_id, e.returncode)
        return False
    except subprocess.TimeoutExpired:
        logger.error("Coaching run timed out for user %s", user_id)
        return False


def _run_insight_job(flag: str) -> bool:
    """Run insight_cron.py with --daily or --weekly.  Returns True on success."""
    label = flag.lstrip("-")
    logger.info("Running %s insight job ...", label)
    try:
        subprocess.run(
            [sys.executable, "insight_cron.py", flag],
            check=True,
            timeout=300,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error("%s insight job failed (exit %s)", label, e.returncode)
        return False
    except subprocess.TimeoutExpired:
        logger.error("%s insight job timed out", label)
        return False


def _run_commit_hygiene() -> bool:
    """Run the hourly commit-hygiene audit.

    Uses ``--fix`` to apply non-destructive fixes (missing .gitignore entries,
    remove sensitive files from index) and ``--create-issues`` to file GitHub
    issues for any hygiene findings requiring human attention.  Returns True
    on success (exit 0 from the audit).
    """
    logger.info("Running commit-hygiene audit ...")
    try:
        subprocess.run(
            [sys.executable, "commit_hygiene.py", "--fix", "--create-issues"],
            check=False,  # exit 1 means issues found, which is informational
            timeout=120,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.error("Commit-hygiene audit timed out")
        return False


def _run_connector_health() -> bool:
    """Run the daily connector health check.

    Uses ``--create-issues`` to file GitHub issues for any findings.  Returns
    True on success (exit 0 means clean).
    """
    logger.info("Running daily connector health check ...")
    try:
        subprocess.run(
            [sys.executable, "connector_health.py", "--create-issues"],
            check=False,  # exit 1 means findings found, which is informational
            timeout=60,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.error("Connector health check timed out")
        return False


def _run_dead_code_sweep() -> bool:
    """Run the hourly dead-code & orphaned-module sweep.

    Uses ``--create-issues`` to file GitHub issues for any newly discovered
    orphaned modules.  Returns True on success (exit 0 from the sweep).
    """
    logger.info("Running dead-code & orphaned-module sweep ...")
    try:
        subprocess.run(
            [sys.executable, "dead_code_sweep.py", "--create-issues"],
            check=False,  # exit 1 means orphans found, which is informational
            timeout=120,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.error("Dead-code sweep timed out")
        return False


def _run_commit_hygiene() -> bool:
    """Run the hourly commit hygiene sweep.

    Uses ``--create-issues`` to file GitHub issues for any security findings
    that need human review.  Returns True on success.
    """
    logger.info("Running commit hygiene sweep ...")
    try:
        subprocess.run(
            [sys.executable, "commit_hygiene.py", "--create-issues"],
            check=False,  # exit 1 means issues found, which is informational
            timeout=120,
        )
        return True
    except subprocess.TimeoutExpired:
        logger.error("Commit hygiene sweep timed out")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_scheduler() -> None:
    """Entry point: bootstrap, then loop forever dispatching due jobs."""
    db_path = os.environ.get("DATABASE_PATH", "workout_agent.db")
    init_db(db_path)

    run_times = _parse_run_at()
    default_tz = os.environ.get("TZ", "UTC")
    logger.info(
        "Scheduler starting: RUN_AT=%s, default TZ=%s",
        " ".join(run_times),
        default_tz,
    )

    # Bootstrap: run once on startup so data is available immediately.
    logger.info("Running bootstrap cycle...")
    users = get_all_users(db_path)
    for user in users:
        try:
            _run_coaching(user["id"])
        except Exception:
            logger.exception("Bootstrap coaching failed for user %s", user["id"])
    _run_insight_job("--daily")
    _run_insight_job("--weekly")

    # Track which date we last ran the daily/weekly insight jobs on,
    # so they fire at most once per UTC day / per Sunday.
    last_daily_date: str = ""
    last_weekly_date: str = ""
    # Track the last hour we ran the hourly sweeps (fires once per hour).
    last_sweep_hour: str = ""
    # Track the last date we ran the connector health check (once per day).
    last_health_date: str = ""

    while True:
        now_utc = datetime.now(ZoneInfo("UTC"))

        # --- Hourly maintenance sweeps (fire once per hour) ---
        current_hour = now_utc.strftime("%Y-%m-%dT%H")
        if current_hour != last_sweep_hour:
            try:
                _run_dead_code_sweep()
            except Exception:
                logger.exception("Unhandled error in dead-code sweep")
            try:
                _run_commit_hygiene()
            except Exception:
                logger.exception("Unhandled error in commit hygiene sweep")
            last_sweep_hour = current_hour

        # --- Daily connector health check (fires once per UTC day) ---
        today_utc_date = now_utc.strftime("%Y-%m-%d")
        if today_utc_date != last_health_date:
            try:
                _run_connector_health()
            except Exception:
                logger.exception("Unhandled error in connector health check")
            last_health_date = today_utc_date

        # --- Per-user coaching runs ---
        # Re-read users each loop so newly created accounts are picked up.
        users = get_all_users(db_path)
        for user in users:
            tz = user.get("timezone") or default_tz
            try:
                if _is_due(tz, run_times):
                    logger.info("User %s is due for coaching (tz=%s)", user["id"], tz)
                    _run_coaching(user["id"])
            except Exception:
                logger.exception(
                    "Unhandled error dispatching coaching for user %s",
                    user["id"],
                )

        # --- Daily insight job (fires once per UTC day, at the earliest RUN_AT) ---
        today_utc = now_utc.strftime("%Y-%m-%d")
        if today_utc != last_daily_date:
            earliest = run_times[0]
            try:
                h, m = map(int, earliest.split(":"))
            except ValueError:
                h, m = 7, 0
            # Fire when the default-TZ RUN_AT has passed.
            tz_now = _now_in_tz(default_tz)
            if tz_now.hour >= h and tz_now.minute >= m:
                _run_insight_job("--daily")
                last_daily_date = today_utc

                # Weekly correlations on Sundays.
                if now_utc.strftime("%A") == "Sunday":
                    week_key = now_utc.strftime("%Y-%W")
                    if week_key != last_weekly_date:
                        _run_insight_job("--weekly")
                        last_weekly_date = week_key

        # Sleep until the next minute boundary so we wake at :00 seconds.
        sleep_secs = 60 - now_utc.second
        time.sleep(max(1, sleep_secs))


if __name__ == "__main__":
    run_scheduler()
