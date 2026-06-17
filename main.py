"""Entry point: orchestrates the nightly/early-morning agent run.

Run manually with `python main.py`, or schedule via cron for 07:00 daily.
"""

from __future__ import annotations

import logging
import sys

from config import Config, ConfigError
from database import (
    advance_day,
    get_current_day,
    init_db,
    save_workout,
)
from gemini_engine import generate_next_workout
from health_connect import read_recovery_metrics
from hevy_client import fetch_latest_workout
from telegram_notifier import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("workout_agent")


def run() -> int:
    try:
        config = Config.load()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    init_db(config.database_path)

    current_day = get_current_day(config.database_path)
    logger.info("Current day in cycle: %s", current_day)

    recent_workout = fetch_latest_workout(config.hevy_api_key)
    save_workout(recent_workout, config.database_path)

    recovery = read_recovery_metrics(config.health_connect_file)

    plan = generate_next_workout(
        api_key=config.gemini_api_key,
        model_name=config.gemini_model,
        day=current_day,
        recent_workout=recent_workout,
        recovery=recovery,
    )

    sent = send_telegram_message(
        config.telegram_bot_token, config.telegram_chat_id, plan
    )
    if not sent:
        logger.error("Plan was generated but could not be delivered.")
        return 2

    advance_day(config.database_path)
    logger.info("Plan delivered and cycle advanced.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
