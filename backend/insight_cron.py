from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import account_deletion
from ai_provider import AIProvider
from ai_resolver import resolve_provider
from config import Config, ConfigError
from database import (
    get_all_users,
    get_body_metrics,
    get_daily_logs,
    get_legacy_user_id,
    get_progress_history,
    init_db,
    save_dashboard_insight,
    save_deep_correlation,
    save_notification,
)
from localisation import local_today, prompt_context, spelling_for
from provenance import RETRIEVAL_GUIDANCE, ai_provenance, annotate_for_retrieval
from tenancy import implicit_single_user_allowed
from tenant_isolation import (
    OwnedContext,
    OwnerIdentity,
    OwnershipError,
    authorize_owner,
)

from user_settings import UserSettings, get_user_settings_or_defaults

DASHBOARD_INSIGHT_VERSION = "dashboard-insight/1"
DEEP_CORRELATION_VERSION = "deep-correlation/1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("insight_cron")


def _resolve_insight_user(db_path: str) -> str | None:
    """Return the implicit local tenant, or None when there must be none.

    Outside the explicit non-production local mode there is no shared fallback
    user, so callers must name the account (or iterate real accounts).
    """
    if not implicit_single_user_allowed():
        return None
    try:
        return get_legacy_user_id(db_path)
    except RuntimeError:
        return None


def _settings_for(config: Config, user_id: str | None) -> UserSettings:
    """The acting user's preferences, or explicit defaults with no tenant."""
    if not user_id:
        return UserSettings()
    return get_user_settings_or_defaults(user_id, config.database_path)


def _local_cutoff(settings: UserSettings, days: int) -> str:
    """ISO date ``days`` before the user's *local* today (never server UTC)."""
    today = local_today(settings.timezone, datetime.now(tz=timezone.utc))
    return (today - timedelta(days=days)).isoformat()


def _authorize(config: Config, user_id: str | None) -> OwnerIdentity:
    """Re-authorize the owner a job runs for.

    A missing id means the legacy single-tenant default; it is still verified,
    and the job never proceeds without an owner (no unscoped reads or writes).
    """
    return authorize_owner(
        config.database_path, user_id or _resolve_insight_user(config.database_path)
    )


def _resolve_provider(config: Config, owner: OwnerIdentity) -> AIProvider:
    return resolve_provider(
        user_id=owner.user_id,
        db_path=config.database_path,
        server_gemini_key=config.gemini_api_key,
        server_gemini_model=config.gemini_model,
    )


def generate_daily_header(config: Config, user_id: str | None = None) -> None:
    logger.info("Generating daily insight header...")
    owner = _authorize(config, user_id)
    if account_deletion.is_account_closing(config.database_path, owner.user_id):
        logger.info("Skipping the daily insight: the account has a deletion request.")
        return
    provider = _resolve_provider(config, owner)

    # Fetch last 7 days of data, counted in the user's own calendar days.
    settings = _settings_for(config, owner.user_id)
    cutoff = _local_cutoff(settings, 7)

    metrics = [
        m
        for m in get_body_metrics(
            limit=14,
            db_path=config.database_path,
            user_id=owner.user_id,
            include_provenance=True,
        )
        if m["date"] >= cutoff
    ]
    logs = [
        log
        for log in get_daily_logs(
            limit=14,
            db_path=config.database_path,
            user_id=owner.user_id,
            include_provenance=True,
        )
        if log["date"] >= cutoff
    ]

    context = OwnedContext(
        owner,
        "daily-insight",
        {
            "metrics": annotate_for_retrieval(metrics),
            "logs": annotate_for_retrieval(logs),
        },
    )

    prompt = f"""You are a high-performance strength coach. Analyze the following JSON representing the user's training and recovery data for the last 7 days.

{json.dumps(context.data, indent=2)}

{RETRIEVAL_GUIDANCE}

{prompt_context(settings.timezone, settings.locale, settings.unit_system)}

Provide a 3-bullet point executive summary for a dashboard header:
1. Current fatigue state (Green/Yellow/Red) based on training volume vs. sleep/recovery.
2. The most critical "wins" or "stalls" in the current training block.
3. One actionable adjustment for today's session.

Keep it brutally concise. Write the text values in {spelling_for(settings.locale)}. Output ONLY valid JSON in this exact format, with no markdown code blocks:
{{"fatigue": "string", "wins_stalls": "string", "advice": "string"}}"""

    try:
        text = str(provider.generate(prompt)).strip()
        text = text.removeprefix("```json")
        text = text.removesuffix("```")
        text = text.strip()

        # Validate JSON
        parsed = json.loads(text)
        if "fatigue" in parsed and "wins_stalls" in parsed and "advice" in parsed:
            save_dashboard_insight(
                json.dumps(parsed),
                db_path=config.database_path,
                user_id=context.owner.user_id,
                provenance=ai_provenance(provider, DASHBOARD_INSIGHT_VERSION),
            )
            fatigue = parsed.get("fatigue", "Normal")
            advice = parsed.get("advice", "")
            save_notification(
                context.owner.user_id,
                title="✨ Coach Status Update",
                message=f"Fatigue: {fatigue}. {advice}",
                type="coach",
                link="/dashboard",
                db_path=config.database_path,
            )
            logger.info("Daily insight generated successfully via %s.", provider.name())
        else:
            logger.error("Invalid JSON structure returned: %s", text)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to generate daily insight: %s", e)


def generate_weekly_correlations(config: Config, user_id: str | None = None) -> None:
    logger.info("Generating weekly deep correlations...")
    owner = _authorize(config, user_id)
    if account_deletion.is_account_closing(config.database_path, owner.user_id):
        logger.info("Skipping weekly correlations: the account has a deletion request.")
        return
    provider = _resolve_provider(config, owner)

    # Fetch 60-day trailing window, counted in the user's own calendar days.
    settings = _settings_for(config, owner.user_id)
    cutoff = _local_cutoff(settings, 60)

    metrics = [
        m
        for m in get_body_metrics(
            limit=120,
            db_path=config.database_path,
            user_id=owner.user_id,
            include_provenance=True,
        )
        if m["date"] >= cutoff
    ]
    logs = [
        log
        for log in get_daily_logs(
            limit=120,
            db_path=config.database_path,
            user_id=owner.user_id,
            include_provenance=True,
        )
        if log["date"] >= cutoff
    ]

    # Also fetch training history for the last 60 days
    all_history = get_progress_history(
        limit_per_exercise=60,
        db_path=config.database_path,
        user_id=owner.user_id,
    )
    filtered_history = {}
    for ex, sets in all_history.items():
        recent = [s for s in sets if s["date"] >= cutoff]
        if recent:
            filtered_history[ex] = recent

    context = OwnedContext(
        owner,
        "weekly-correlations",
        {
            "body_metrics": annotate_for_retrieval(metrics),
            "daily_logs": annotate_for_retrieval(logs),
            "exercise_history": filtered_history,
            "exercise_history_provenance": (
                "imported: Hevy workout logs (source data, not recommendations)"
            ),
        },
    )

    prompt = f"""You are an elite data-driven strength coach and analyst. 
You are analyzing a 60-day trailing window of the user's training data, sleep/recovery metrics, and daily lifestyle logs.

Your goal is to hunt for invisible bottlenecks. For example, you might identify that a priority exercise stalls when recovery is poor two nights prior, or that high-volume sessions negatively affect sleep.

Here is the data:
{json.dumps(context.data, indent=2)}

{RETRIEVAL_GUIDANCE}

{prompt_context(settings.timezone, settings.locale, settings.unit_system)}

Analyze this data and produce a "Deep Correlation Engine" report. 
Highlight hidden correlations, potential burnout indicators, and specific tactical recommendations.
Use Markdown format. Output the Markdown report directly.
"""

    try:
        text = str(provider.generate(prompt)).strip()
        if text:
            save_deep_correlation(
                text,
                db_path=config.database_path,
                user_id=context.owner.user_id,
                provenance=ai_provenance(provider, DEEP_CORRELATION_VERSION),
            )
            save_notification(
                context.owner.user_id,
                title="✨ Weekly Intelligence Report",
                message="New 60-day recovery and workout correlations analysis is ready.",
                type="coach",
                link="/stats",
                db_path=config.database_path,
            )
            logger.info(
                "Weekly deep correlation generated successfully via %s.",
                provider.name(),
            )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to generate weekly deep correlation: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Generate daily insight header",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Generate weekly deep correlations",
    )
    parser.add_argument(
        "--user-id",
        help="Generate insights for this server-selected user identifier.",
    )
    args = parser.parse_args()

    try:
        config = Config.load()
        init_db(config.database_path)
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    # An explicit --user-id always wins. With none, the explicit local mode uses
    # its single tenant; otherwise every active account is processed in turn,
    # each strictly scoped to itself. There is no shared fallback user.
    if args.user_id:
        user_ids: list[str | None] = [args.user_id]
    elif implicit_single_user_allowed():
        user_ids = [None]
    else:
        user_ids = [user["id"] for user in get_all_users(config.database_path)]

    try:
        for uid in user_ids:
            if args.daily:
                generate_daily_header(config, user_id=uid)
            if args.weekly:
                generate_weekly_correlations(config, user_id=uid)
    except OwnershipError as exc:
        logger.error("Refusing insight job: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
