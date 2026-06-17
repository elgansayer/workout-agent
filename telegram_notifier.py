"""Sends the daily plan to your phone via a Telegram bot."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds
# Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message. Returns True on success, False otherwise."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = message[:MAX_MESSAGE_LENGTH]
    payload = {"chat_id": chat_id, "text": text}

    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        return False
