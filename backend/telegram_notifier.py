"""Sends the daily plan to your phone via a Telegram bot."""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15  # seconds
# Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE_LENGTH = 4096

# Characters that must be escaped in Telegram MarkdownV2.
_MARKDOWN_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 reserved character so plain text sends safely."""
    return "".join(
        "\\" + char if char in _MARKDOWN_V2_SPECIAL else char for char in text
    )


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks under ``limit``, preferring line boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single very long line must be hard-split on its own.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str,
    parse_mode: str | None = None,
) -> bool:
    """Send a message, splitting if needed. Returns True if all parts sent.

    When ``parse_mode`` is "MarkdownV2", the message is escaped first so plain
    text never trips Telegram's strict parser.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    text = message
    if parse_mode == "MarkdownV2":
        text = escape_markdown_v2(text)

    for chunk in split_message(text):
        payload: dict[str, str] = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Failed to send Telegram message: %s", exc)
            return False

    return True
