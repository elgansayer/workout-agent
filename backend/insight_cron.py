from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from ai_provider import AIProvider
from ai_resolver import resolve_provider
from config import Config, ConfigError
from database import (
    get_body_metrics,
    get_daily_logs,
    get_progress_history,
    init_db,
    save_dashboard_insight,
    save_deep_correlation,
    save_notification,
)
from ownership import AuthenticatedOwner, owner_for_legacy_tenant, owner_for_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("insight_cron")


def _resolve_insight_owner(db_path: str) -> AuthenticatedOwner | None:
    try:
        return owner_for_legacy_tenant(db_path)
    except Exception:  # noqa: BLE001
        return None


def _resolve_provider(
    config: Config,
    owner: AuthenticatedOwner | None = None,
) -> AIProvider:
    return resolve_provider(
        owner=owner,
        db_path=config.database_path,
        server_gemini_key=config.gemini_api_key,
        server_gemini_model=config.gemini_model,
    )


def generate_daily_header(
    config: Config,
    owner: AuthenticatedOwner | None = None,
) -> None:
    logger.info("Generating daily insight header...")
    resolved = owner or _resolve_insight_owner(config.database_path)
    if resolved is None:
        logger.error("No insight owner could be resolved for this run.")
        return
    owner = resolved
    provider = _resolve_provider(config, owner=owner)

    # Fetch last 7 days of data
    cutoff = (datetime.now(tz=timezone.utc).date() - timedelta(days=7)).isoformat()

    metrics = [
        m
        for m in get_body_metrics(limit=14, db_path=config.database_path, owner=owner)
        if m["date"] >= cutoff
    ]
    logs = [
        log
        for log in get_daily_logs(limit=14, db_path=config.database_path, owner=owner)
        if log["date"] >= cutoff
    ]

    data = {"metrics": metrics, "logs": logs}

    prompt = f"""You are a high-performance strength coach. Analyze the following JSON representing the user's training and recovery data for the last 7 days.

{json.dumps(data, indent=2)}

Provide a 3-bullet point executive summary for a dashboard header:
1. Current fatigue state (Green/Yellow/Red) based on training volume vs. sleep/recovery.
2. The most critical "wins" or "stalls" in the current training block.
3. One actionable adjustment for today's session.

Keep it brutally concise. Output ONLY valid JSON in this exact format, with no markdown code blocks:
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
                json.dumps(parsed), db_path=config.database_path, owner=owner
            )
            fatigue = parsed.get("fatigue", "Normal")
            advice = parsed.get("advice", "")
            save_notification(
                owner,
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


def generate_weekly_correlations(
    config: Config,
    owner: AuthenticatedOwner | None = None,
) -> None:
    logger.info("Generating weekly deep correlations...")
    resolved = owner or _resolve_insight_owner(config.database_path)
    if resolved is None:
        logger.error("No insight owner could be resolved for this run.")
        return
    owner = resolved
    provider = _resolve_provider(config, owner=owner)

    # Fetch 60-day trailing window
    cutoff = (datetime.now(tz=timezone.utc).date() - timedelta(days=60)).isoformat()

    metrics = [
        m
        for m in get_body_metrics(limit=120, db_path=config.database_path, owner=owner)
        if m["date"] >= cutoff
    ]
    logs = [
        log
        for log in get_daily_logs(limit=120, db_path=config.database_path, owner=owner)
        if log["date"] >= cutoff
    ]

    # Also fetch training history for the last 60 days
    all_history = get_progress_history(
        limit_per_exercise=60,
        db_path=config.database_path,
        owner=owner,
    )
    filtered_history = {}
    for ex, sets in all_history.items():
        recent = [s for s in sets if s["date"] >= cutoff]
        if recent:
            filtered_history[ex] = recent

    data = {
        "body_metrics": metrics,
        "daily_logs": logs,
        "exercise_history": filtered_history,
    }

    prompt = f"""You are an elite data-driven strength coach and analyst. 
You are analyzing a 60-day trailing window of the user's training data, sleep/recovery metrics, and daily lifestyle logs.

Your goal is to hunt for invisible bottlenecks. For example, you might identify that weighted pull-up progression consistently stalls when the user has had poor recovery two nights prior, or that high-volume leg days negatively impact sleep.

Here is the data:
{json.dumps(data, indent=2)}

Analyze this data and produce a "Deep Correlation Engine" report. 
Highlight hidden correlations, potential burnout indicators, and specific tactical recommendations.
Use Markdown format. Output the Markdown report directly.
"""

    try:
        text = str(provider.generate(prompt)).strip()
        if text:
            save_deep_correlation(text, db_path=config.database_path, owner=owner)
            save_notification(
                owner,
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

    owner = None
    if args.user_id:
        owner = owner_for_user(config.database_path, args.user_id)

    if args.daily:
        generate_daily_header(config, owner=owner)

    if args.weekly:
        generate_weekly_correlations(config, owner=owner)


if __name__ == "__main__":
    main()
