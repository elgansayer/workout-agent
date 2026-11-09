"""Entry point: orchestrates the early-morning agent run.

Each morning it resolves the authenticated user's active Hevy-native programme,
applies progressive overload from the latest Hevy session, and messages the
plan. When no programme is active it records a setup-required skip instead of
fabricating a workout.

Run manually with `python main.py`, or schedule via cron for 07:00 daily.
Use `python main.py --preview` for a dry run that prints today's plan to stdout
without sending push notification.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import checkin
import google_health_client
import insights as insights_engine
import lifestyle
from ai_provider import AIProvider
from ai_resolver import resolve_provider
from config import Config, ConfigError
from database import (
    get_active_programme,
    get_body_metrics,
    get_daily_logs,
    get_legacy_user_id,
    get_or_create_user,
    get_progress_history,
    get_push_subscriptions,
    get_recent_bests,
    get_recent_hevy_logs,
    get_user_api_key,
    get_user_by_id,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_notification,
    save_progress,
    save_workout,
)
from gemini_engine import generate_next_workout
from health_connect import body_metrics_from_recovery, read_recovery_metrics
from hevy_client import fetch_latest_workout
from hevy_parser import normalise_name, parse_workout
from programme_runtime import local_today, resolve_programme_runtime
from push_notifier import send_push_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("workout_agent")


def _deliver(config: Config, message: str, preview: bool, user_id: str) -> int:
    """Print the message in preview mode, otherwise send it via Web Push."""
    if user_id:
        try:
            first_line = (
                message.strip().split("\n")[0]
                if message
                else "Today's workout plan is ready."
            )
            save_notification(
                user_id,
                title="🏋️ Today's Workout Plan Ready",
                message=first_line,
                type="coach",
                link="/plan",
                db_path=config.database_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not save in-app notification: %s", e)

    if preview:
        print(message)
        logger.info("Preview only: nothing sent.")
        return 0

    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not vapid_private:
        logger.error("VAPID_PRIVATE_KEY is not configured.")
        return 2

    subscriptions = get_push_subscriptions(user_id, config.database_path)
    if not subscriptions:
        logger.warning(
            f"No push subscriptions found for user {user_id}. Plan was not delivered."
        )
        return 2

    success = False
    for sub in subscriptions:
        payload = {"title": "Workout Agent", "body": message}
        if send_push_notification(sub, payload, vapid_private):
            success = True

    if not success:
        logger.error("Plan was generated but could not be delivered to any device.")
        return 2

    logger.info("Plan delivered via Push Notifications.")
    return 0


def _maybe_check_in(
    config: Config,
    week: int,
    cycle_weeks: int,
    block: dict[str, Any],
    days: list[dict[str, Any]],
    rules: list[str],
    preview: bool,
    *,
    user_id: str,
) -> None:
    """Run a programme check-in if one is due, delivering it as its own message."""
    if not config.checkin_enabled:
        return
    try:
        due_info = checkin.due(config, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
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
    message = checkin.run_checkin(
        config,
        due_info,
        week,
        cycle_weeks,
        block,
        days,
        rules,
        user_id=user_id,
    )
    _deliver(config, message, preview, user_id)
    if not preview:
        checkin.record(config, due_info, message, user_id=user_id)


def _maybe_self_review(
    config: Config,
    recovery: dict[str, Any] | None,
    week: int,
    block: dict[str, Any],
    preview: bool,
    *,
    user_id: str,
) -> None:
    """On the configured weekday, send a self-review of how training is going."""
    if not config.self_review_enabled:
        return
    if datetime.now(tz=timezone.utc).date().weekday() != config.self_review_weekday:
        return
    review = insights_engine.build_insights(
        get_progress_history(db_path=config.database_path, user_id=user_id),
        get_body_metrics(db_path=config.database_path, user_id=user_id),
        recovery,
    )
    if not review.lifts:
        logger.info("Self-review skipped: not enough logged history yet.")
        return
    logger.info("Self-review due: %s", review.headline)
    block_name = str(block.get("name") or "Programme")
    _deliver(config, review.as_message(week, block_name), preview, user_id)


def _compose(body: str, guidance: lifestyle.DailyGuidance | None, footer: str) -> str:
    """Stitch the workout/rest message, lifestyle pillars, and changes footer."""
    text = body
    if guidance is not None:
        text += "\n\n" + guidance.as_text()
    return text + footer


def _resolve_provider(
    config: Config,
    *,
    user_id: str | None = None,
) -> AIProvider:
    """Resolve the AI provider for a run, scoping to *user_id* when given."""
    return resolve_provider(
        user_id=user_id,
        db_path=config.database_path,
        server_gemini_key=config.gemini_api_key,
        server_gemini_model=config.gemini_model,
    )


def _resolve_run_user(db_path: str) -> str:
    """Return the user_id the scheduled run should operate under.

    Prefers $SCHEDULER_USER_ID (set by scheduler.py when dispatching per-user).
    Falls back to the first legacy user for single-tenant backwards compat.
    """
    env_id = os.environ.get("SCHEDULER_USER_ID", "").strip()
    if env_id:
        return env_id
    return get_or_create_user("legacy@local", "Legacy Data", db_path)["id"]


def _scope_config_to_user(config: Config, user_id: str) -> Config:
    """Resolve user-owned connector secrets and fail closed on shared fallbacks."""
    key_record = get_user_api_key(user_id, "hevy", db_path=config.database_path)
    user_hevy_key = None
    if key_record:
        user_hevy_key = str(key_record.get("api_key") or "").strip() or None

    is_legacy_user = user_id == get_legacy_user_id(config.database_path)
    hevy_key = user_hevy_key or (config.hevy_api_key if is_legacy_user else None)
    return replace(
        config,
        hevy_api_key=hevy_key,
        health_connect_file=(config.health_connect_file if is_legacy_user else None),
        google_health_client_id=(
            config.google_health_client_id if is_legacy_user else None
        ),
        google_health_client_secret=(
            config.google_health_client_secret if is_legacy_user else None
        ),
        google_health_refresh_token=(
            config.google_health_refresh_token if is_legacy_user else None
        ),
    )


def _rep_targets_from_days(days: list[dict[str, Any]]) -> dict[str, int]:
    """Return top-of-range rep targets for the active programme's exercises."""
    targets: dict[str, int] = {}
    for day in days:
        for exercise in day.get("exercises", []):
            name = str(exercise.get("name") or "")
            if not name:
                continue
            numbers = re.findall(r"\d+", str(exercise.get("rep_range") or ""))
            if numbers:
                targets[normalise_name(name)] = int(numbers[-1])
    return targets


def run(preview: bool = False, user_id: str | None = None) -> int:
    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    init_db(config.database_path)

    if user_id is None:
        user_id = _resolve_run_user(config.database_path)
    config = _scope_config_to_user(config, user_id)
    logger.info("Running daily cycle for user %s", user_id)

    user = get_user_by_id(user_id, config.database_path)
    timezone_name = (user.get("timezone") if user else None) or "UTC"
    today = local_today(timezone_name)
    when = today.isoformat()

    active = get_active_programme(user_id, db_path=config.database_path)
    runtime = resolve_programme_runtime(
        active,
        get_recent_hevy_logs(
            limit=56,
            db_path=config.database_path,
            user_id=user_id,
        ),
        today=today,
        timezone_name=timezone_name,
    )
    if runtime.status != "active" or runtime.next_day is None:
        message = runtime.message or "Connect Hevy and select routines to begin."
        logger.info("Skipping daily workout for user %s: %s", user_id, message)
        if not preview:
            save_notification(
                user_id,
                title="Programme setup required",
                message=message,
                type="programme",
                link="/programmes",
                db_path=config.database_path,
            )
        return 0

    definition = (active or {}).get("definition") or {}
    if not isinstance(definition, dict):
        definition = {}
    programme_name = str(definition.get("name") or "Hevy programme")
    rules = [str(rule) for rule in definition.get("rules") or []]
    days = runtime.days
    week = runtime.week
    cycle_weeks = runtime.cycle_weeks
    block = runtime.block
    day = runtime.next_day
    focus = str(day.get("focus") or "Training")
    logger.info(
        "Week %s of %s, Block %s: %s.",
        week,
        cycle_weeks,
        block.get("number") or 0,
        block.get("name") or "Programme",
    )

    _maybe_check_in(
        config,
        week,
        cycle_weeks,
        block,
        days,
        rules,
        preview,
        user_id=user_id,
    )

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
        save_body_metrics(
            body_metrics_from_recovery(recovery),
            when,
            config.database_path,
            user_id=user_id,
        )

    _maybe_self_review(
        config,
        recovery,
        week,
        block,
        preview,
        user_id=user_id,
    )

    try:
        provider = _resolve_provider(config, user_id=user_id)
    except (ImportError, ValueError) as exc:
        logger.error("AI provider configuration failed: %s", exc)
        return 1

    logger.info("Today is %s: %s.", today.strftime("%A"), focus)

    recent_workout = (
        fetch_latest_workout(config.hevy_api_key) if config.hevy_api_key else None
    )
    summary = parse_workout(recent_workout, _rep_targets_from_days(days))
    if not preview:
        save_workout(recent_workout, config.database_path, user_id=user_id)
        save_progress(summary, config.database_path, user_id=user_id)

    history = get_recent_bests(config.database_path, user_id=user_id)

    review = insights_engine.build_insights(
        get_progress_history(db_path=config.database_path, user_id=user_id),
        get_body_metrics(db_path=config.database_path, user_id=user_id),
        recovery,
    )
    logger.info("%s", review.headline)

    logs = get_daily_logs(limit=5, db_path=config.database_path, user_id=user_id)
    last_plan = None
    for log in logs:
        if log.get("day") is not None and log.get("plan"):
            last_plan = log["plan"]
            break

    plan = generate_next_workout(
        provider,
        day=day,
        week=week,
        cycle_weeks=cycle_weeks,
        programme_name=programme_name,
        rules=rules,
        workout_summary=summary,
        recovery=recovery,
        history=history,
        insights=review,
        last_plan=last_plan,
    )
    guidance = (
        lifestyle.daily_guidance(focus, False, recovery)
        if config.lifestyle_enabled
        else None
    )
    if not preview:
        save_daily_log(
            when,
            int(day.get("number") or 1),
            focus,
            guidance.carb_tier if guidance else "",
            plan,
            guidance.as_text() if guidance else "",
            config.database_path,
            user_id=user_id,
        )
    return _deliver(config, _compose(plan, guidance, ""), preview, user_id)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous workout agent: builds and delivers today's plan.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Dry run: print the generated plan without sending a notification.",
    )
    parser.add_argument(
        "--user-id",
        help="Run for this server-selected user identifier.",
    )
    parser.add_argument(
        "--sync-history",
        action="store_true",
        help="Rebuild Hevy history before exiting.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.sync_history:
        from scripts.sync_history import sync_all

        config = Config.load()
        init_db(config.database_path)
        sync_user_id = args.user_id or _resolve_run_user(config.database_path)
        config = _scope_config_to_user(config, sync_user_id)
        api_key = config.hevy_api_key
        if not api_key:
            logger.error("No Hevy API key is configured for this user.")
            sys.exit(1)
        result = sync_all(
            api_key,
            config.database_path,
            user_id=sync_user_id,
        )
        if "error" in result:
            logger.error("%s", result["error"])
            sys.exit(1)
        logger.info("%s", result)
        sys.exit(0)
    sys.exit(run(preview=args.preview, user_id=args.user_id))
