"""Entry point: orchestrates the early-morning agent run.

Each morning it works out what you should train today from the real calendar day
(Monday to Saturday follow the 6-day split; Sunday is a rest day), applies
progressive overload from your latest Hevy session, and messages you the plan.

Run manually with `python main.py`, or schedule via cron for 07:00 daily.
Use `python main.py --preview` for a dry run that prints today's plan to stdout
without sending Telegram.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from config import Config, ConfigError
from database import (
    get_body_metrics,
    get_programme_start_date,
    get_progress_history,
    get_recent_bests,
    get_daily_logs,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_progress,
    save_workout,
)
import checkin
import google_health_client
import insights as insights_engine
import lifestyle
from gemini_engine import generate_next_workout, generate_rest_day_message
from health_connect import body_metrics_from_recovery, read_recovery_metrics
from hevy_client import fetch_latest_workout
from hevy_parser import parse_workout
from hevy_sync import sync_routines
from program import (
    REST_DAY_FOCUS,
    block_for_week,
    day_focus,
    rep_targets,
    today_day,
    week_in_cycle,
)
from telegram_notifier import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("workout_agent")


def _deliver(config: Config, message: str, preview: bool) -> int:
    """Print the message in preview mode, otherwise send it via Telegram."""
    if preview:
        print(message)
        logger.info("Preview only: nothing sent.")
        return 0

    sent = send_telegram_message(
        config.telegram_bot_token,
        config.telegram_chat_id,
        message,
        parse_mode=config.telegram_parse_mode,
    )
    if not sent:
        logger.error("Plan was generated but could not be delivered.")
        return 2

    logger.info("Plan delivered.")
    return 0


def _sync_hevy_routines(config: Config) -> list[str]:
    """Keep the Hevy routines in sync so today's session is ready to start.

    Returns the per-routine status lines so changes can be reported.
    """
    if not config.hevy_api_key or not config.hevy_sync_routines:
        return []
    try:
        statuses = sync_routines(config)
        for status in statuses:
            logger.info("Hevy routine %s", status)
        return statuses
    except Exception as exc:  # never let a sync issue block the daily message
        logger.warning("Hevy routine sync failed: %s", exc)
        return []


def _changes_footer(statuses: list[str]) -> str:
    """Build a short footer naming any routines that changed in this sync."""
    changed = [
        status.split(":", 1)[0]
        for status in statuses
        if status.endswith(": updated") or status.endswith(": created")
    ]
    if not changed:
        return ""
    return "\n\nHevy routines refreshed: " + ", ".join(changed) + "."


def _maybe_check_in(config: Config, week: int, block, preview: bool) -> None:
    """Run a programme check-in if one is due, delivering it as its own message."""
    if not config.checkin_enabled:
        return
    try:
        due_info = checkin.due(config)
    except Exception as exc:  # a check-in must never block the daily message
        logger.warning("Check-in scheduling failed: %s", exc)
        return
    if due_info is None:
        return
    logger.info(
        "Check-in %s due: %s sessions over %s weeks.",
        due_info.number,
        due_info.workouts_done,
        due_info.weeks_elapsed,
    )
    message = checkin.run_checkin(config, due_info, week, block)
    _deliver(config, message, preview)
    if not preview:
        checkin.record(config, due_info, message)


def _maybe_self_review(
    config: Config,
    recovery: dict | None,
    week: int,
    block,
    preview: bool,
) -> None:
    """On the configured weekday, send a self-review of how training is going."""
    if not config.self_review_enabled:
        return
    if date.today().weekday() != config.self_review_weekday:
        return
    review = insights_engine.build_insights(
        get_progress_history(db_path=config.database_path),
        get_body_metrics(db_path=config.database_path),
        recovery,
    )
    if not review.lifts:
        logger.info("Self-review skipped: not enough logged history yet.")
        return
    logger.info("Self-review due: %s", review.headline)
    _deliver(config, review.as_message(week, block.name), preview)


def _compose(body: str, guidance: lifestyle.DailyGuidance | None, footer: str) -> str:
    """Stitch the workout/rest message, lifestyle pillars, and changes footer."""
    text = body
    if guidance is not None:
        text += "\n\n" + guidance.as_text()
    return text + footer


def run(preview: bool = False) -> int:
    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    init_db(config.database_path)

    statuses = _sync_hevy_routines(config)
    footer = _changes_footer(statuses)

    today = date.today()
    when = today.isoformat()
    week = week_in_cycle(get_programme_start_date(config.database_path), today)
    block = block_for_week(week)
    logger.info("Week %s of 12, Block %s: %s.", week, block.number, block.name)

    _maybe_check_in(config, week, block, preview)

    recovery = read_recovery_metrics(config.health_connect_file)
    synced = google_health_client.sync_body_metrics(
        config.google_health_client_id,
        config.google_health_client_secret,
        config.google_health_refresh_token,
        config.database_path,
    )
    if synced:
        # Live scale readings take precedence over a stale file.
        recovery = {**(recovery or {}), **synced}
    if not preview:
        save_body_metrics(body_metrics_from_recovery(recovery), when, config.database_path)

    _maybe_self_review(config, recovery, week, block, preview)

    day = today_day(today)

    if day is None:
        logger.info("Today is %s: a scheduled rest day.", today.strftime("%A"))
        message = generate_rest_day_message(
            api_key=config.gemini_api_key,
            model_name=config.gemini_model,
            recovery=recovery,
        )
        guidance = (
            lifestyle.daily_guidance(None, True, recovery)
            if config.lifestyle_enabled
            else None
        )
        if not preview:
            save_daily_log(
                when,
                None,
                REST_DAY_FOCUS,
                guidance.carb_tier if guidance else "",
                message,
                guidance.as_text() if guidance else "",
                config.database_path,
            )
        return _deliver(config, _compose(message, guidance, footer), preview)

    logger.info(
        "Today is %s: day %s, %s.", today.strftime("%A"), day, day_focus(day)
    )

    recent_workout = (
        fetch_latest_workout(config.hevy_api_key)
        if config.hevy_api_key
        else None
    )
    summary = parse_workout(recent_workout, rep_targets(block))
    if not preview:
        save_workout(recent_workout, config.database_path)
        save_progress(summary, config.database_path)

    history = get_recent_bests(config.database_path)

    review = insights_engine.build_insights(
        get_progress_history(db_path=config.database_path),
        get_body_metrics(db_path=config.database_path),
        recovery,
    )
    logger.info("%s", review.headline)

    logs = get_daily_logs(limit=5, db_path=config.database_path)
    last_plan = None
    for log in logs:
        if log.get("day") is not None and log.get("plan"):
            last_plan = log["plan"]
            break

    plan = generate_next_workout(
        api_key=config.gemini_api_key,
        model_name=config.gemini_model,
        day=day,
        week=week,
        block=block,
        workout_summary=summary,
        recovery=recovery,
        history=history,
        insights=review,
        last_plan=last_plan,
    )
    guidance = (
        lifestyle.daily_guidance(day, False, recovery)
        if config.lifestyle_enabled
        else None
    )
    if not preview:
        save_daily_log(
            when,
            day,
            day_focus(day),
            guidance.carb_tier if guidance else "",
            plan,
            guidance.as_text() if guidance else "",
            config.database_path,
        )
    return _deliver(config, _compose(plan, guidance, footer), preview)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous workout agent: builds and delivers today's plan."
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Dry run: print the generated plan to stdout without sending "
        "Telegram.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run(preview=args.preview))
