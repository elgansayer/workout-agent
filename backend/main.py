"""Entry point: orchestrates the early-morning agent run.

Each morning it works out what you should train today from the real calendar day
(Monday to Saturday follow the 6-day split; Sunday is a rest day), applies
progressive overload from your latest Hevy session, and messages you the plan.

Run manually with `python main.py`, or schedule via cron for 07:00 daily.
Use `python main.py --preview` for a dry run that prints today's plan to stdout
without sending push notification.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import checkin
import google_health_client
import insights as insights_engine
import lifestyle
from accounts import AccountError, require_active_user
from ai_provider import AIProvider
from ai_resolver import resolve_provider
from config import Config, ConfigError
from database import (
    get_body_metrics,
    get_daily_logs,
    get_legacy_user_id,
    get_programme_start_date,
    get_progress_history,
    get_push_subscriptions,
    get_recent_bests,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_notification,
    save_progress,
    save_workout,
)
from gemini_engine import generate_next_workout, generate_rest_day_message
from health_connect import body_metrics_from_recovery, read_recovery_metrics
from hevy_client import fetch_latest_workout
from hevy_parser import parse_workout
from hevy_sync import sync_routines
from legacy_ownership import is_deployment_owner
from program import (
    REST_DAY_FOCUS,
    Block,
    block_for_week,
    day_focus,
    rep_targets,
    today_day,
    week_in_cycle,
)
from push_notifier import send_push_notification
from secret_vault import (
    CredentialStatus,
    CredentialUnavailableError,
    ResolvedCredential,
    SecretVault,
    migrate_legacy_credentials,
)
from tenancy import TenantRequiredError, implicit_single_user_allowed
from tenant_isolation import OwnershipError, authorize_owner

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


def _sync_hevy_routines(
    config: Config,
    *,
    user_id: str,
) -> list[str]:
    """Keep the acting user's Hevy routines ready to start."""
    if not config.hevy_api_key or not config.hevy_sync_routines:
        return []
    try:
        statuses = sync_routines(config, user_id=user_id)
        for status in statuses:
            logger.info("Hevy routine %s", status)
        return statuses
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hevy routine sync failed: %s", exc)
        return []


def _changes_footer(statuses: list[str]) -> str:
    """Build a short footer naming any routines that changed in this sync."""
    changed = [
        status.split(":", 1)[0]
        for status in statuses
        if status.endswith((": updated", ": created"))
    ]
    if not changed:
        return ""
    return "\n\nHevy routines refreshed: " + ", ".join(changed) + "."


def _maybe_check_in(
    config: Config,
    week: int,
    block: Block,
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
    message = checkin.run_checkin(config, due_info, week, block, user_id=user_id)
    _deliver(config, message, preview, user_id)
    if not preview:
        checkin.record(config, due_info, message, user_id=user_id)


def _maybe_self_review(
    config: Config,
    recovery: dict[str, Any] | None,
    week: int,
    block: Block,
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
    _deliver(config, review.as_message(week, block.name), preview, user_id)


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
    """Return the user_id the run should operate under.

    ``$SCHEDULER_USER_ID`` (an active account chosen by the operator or the
    scheduler) is always honoured. Without it the only fallback is the explicit
    non-production local tenant; production has no shared fallback user, so the
    run is refused instead.

    Raises:
        TenantRequiredError: no user was selected and no local fallback applies.
        AccountError: the selected account does not exist or is not active.
    """
    env_id = os.environ.get("SCHEDULER_USER_ID", "").strip()
    if env_id:
        require_active_user(env_id, db_path)
        return env_id
    if implicit_single_user_allowed():
        return get_legacy_user_id(db_path)
    raise TenantRequiredError(
        "No user selected: pass --user-id or set SCHEDULER_USER_ID."
    )


def _vault_credential(
    db_path: str,
    user_id: str,
    provider: str,
    *,
    purpose: str,
) -> ResolvedCredential | None:
    """Resolve one owner's credential, treating unavailability as absent."""
    try:
        return SecretVault(db_path).resolve(user_id, provider, purpose=purpose)
    except CredentialUnavailableError:
        return None


def _vault_record_exists(db_path: str, user_id: str, provider: str) -> bool:
    """Whether the owner has any credential row, including a revoked one."""
    return (
        SecretVault(db_path).describe(user_id, provider).status
        is not CredentialStatus.NOT_CONFIGURED
    )


def _scope_config_to_user(config: Config, user_id: str) -> Config:
    """Resolve user-owned connector secrets and fail closed on shared fallbacks.

    Deployment-wide connector settings (the single-user ``HEVY_API_KEY`` and the
    Google Health environment) are applied only for the deployment owner: the
    verified person the pre-account data was assigned to, or the local tenant in
    the explicit non-production mode. Every other user relies solely on their own
    vaulted credentials.
    """
    hevy_credential = _vault_credential(
        config.database_path,
        user_id,
        "hevy",
        purpose="hevy_sync",
    )
    hevy_record_exists = _vault_record_exists(
        config.database_path, user_id, "hevy"
    )
    user_hevy_key = (
        hevy_credential.api_key.reveal()
        if hevy_credential is not None and hevy_credential.api_key is not None
        else None
    )
    google_credential = _vault_credential(
        config.database_path,
        user_id,
        "google_health",
        purpose="health_sync",
    )
    google_record_exists = _vault_record_exists(
        config.database_path, user_id, "google_health"
    )
    user_google_token = (
        google_credential.refresh_token.reveal()
        if google_credential is not None and google_credential.refresh_token is not None
        else None
    )

    owns_server_config = is_deployment_owner(user_id, config.database_path)
    allow_legacy_hevy = owns_server_config and not hevy_record_exists
    allow_legacy_google = owns_server_config and not google_record_exists
    hevy_key = user_hevy_key or (config.hevy_api_key if allow_legacy_hevy else None)
    google_token = user_google_token or (
        config.google_health_refresh_token if allow_legacy_google else None
    )
    return replace(
        config,
        hevy_api_key=hevy_key,
        health_connect_file=(
            config.health_connect_file if owns_server_config else None
        ),
        # The OAuth *client* belongs to the deployment and is safe to share;
        # only the per-user refresh token is tenant scoped.
        google_health_client_id=config.google_health_client_id,
        google_health_client_secret=config.google_health_client_secret,
        google_health_refresh_token=google_token,
    )


def run(preview: bool = False, user_id: str | None = None) -> int:
    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    init_db(config.database_path)
    migrate_legacy_credentials(config.database_path)

    try:
        if user_id is None:
            user_id = _resolve_run_user(config.database_path)
        owner = authorize_owner(config.database_path, user_id)
        require_active_user(owner.user_id, config.database_path)
        user_id = owner.user_id
    except (TenantRequiredError, AccountError, OwnershipError) as exc:
        logger.error("Cannot start the daily cycle: %s", exc)
        return 1
    config = _scope_config_to_user(config, user_id)
    logger.info("Running daily cycle for user %s", user_id)

    statuses = _sync_hevy_routines(config, user_id=user_id)
    footer = _changes_footer(statuses)

    today = datetime.now(tz=timezone.utc).date()
    when = today.isoformat()
    week = week_in_cycle(
        get_programme_start_date(config.database_path, user_id=user_id), today
    )
    block = block_for_week(week)
    logger.info("Week %s of 12, Block %s: %s.", week, block.number, block.name)

    _maybe_check_in(config, week, block, preview, user_id=user_id)

    recovery = read_recovery_metrics(config.health_connect_file)
    synced = google_health_client.sync_body_metrics(
        config.google_health_client_id,
        config.google_health_client_secret,
        config.google_health_refresh_token,
        config.database_path,
        user_id=user_id,
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
    day = today_day(today)

    if day is None:
        logger.info("Today is %s: a scheduled rest day.", today.strftime("%A"))
        message = generate_rest_day_message(provider, recovery=recovery)
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
                user_id=user_id,
            )
        return _deliver(config, _compose(message, guidance, footer), preview, user_id)

    logger.info("Today is %s: day %s, %s.", today.strftime("%A"), day, day_focus(day))

    recent_workout = (
        fetch_latest_workout(config.hevy_api_key) if config.hevy_api_key else None
    )
    summary = parse_workout(recent_workout, rep_targets(block))
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
            user_id=user_id,
        )
    return _deliver(config, _compose(plan, guidance, footer), preview, user_id)


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


def _sync_history(user_id: str | None = None) -> int:
    """Rebuild one owner's Hevy history (``--sync-history``).

    ``--user-id`` only claims an owner. It is re-authorized before any connector
    credential is read, so a mistyped or forged id cannot select another
    tenant's Hevy key or overwrite their history.
    """
    from scripts.sync_history import sync_all

    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1
    init_db(config.database_path)
    migrate_legacy_credentials(config.database_path)
    try:
        owner = authorize_owner(
            config.database_path, user_id or _resolve_run_user(config.database_path)
        )
        require_active_user(owner.user_id, config.database_path)
    except (OwnershipError, TenantRequiredError, AccountError) as exc:
        logger.error("Refusing history rebuild: %s", exc)
        return 1
    config = _scope_config_to_user(config, owner.user_id)
    if not config.hevy_api_key:
        logger.error("No Hevy API key is configured for this user.")
        return 1
    result = sync_all(config.hevy_api_key, config.database_path, user_id=owner.user_id)
    if "error" in result:
        logger.error("%s", result["error"])
        return 1
    logger.info("%s", result)
    return 0


if __name__ == "__main__":
    args = _parse_args()
    if args.sync_history:
        sys.exit(_sync_history(user_id=args.user_id))
    sys.exit(run(preview=args.preview, user_id=args.user_id))
