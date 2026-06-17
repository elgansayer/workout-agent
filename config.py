"""Configuration loading.

Secrets are read from environment variables (optionally populated from a local
.env file) so that no API keys are ever committed to source control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


# A one-line hint for each required key, shown when it is missing.
_REQUIRED_HINTS = {
    "GEMINI_API_KEY": "Create one at https://aistudio.google.com/app/apikey",
    "TELEGRAM_BOT_TOKEN": "Talk to @BotFather on Telegram to create a bot.",
    "TELEGRAM_CHAT_ID": "Message your bot, then read chat.id from getUpdates.",
}


@dataclass(frozen=True)
class Config:
    hevy_api_key: str | None
    gemini_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_model: str
    health_connect_file: str | None
    database_path: str
    telegram_parse_mode: str | None
    hevy_sync_routines: bool
    hevy_prefill_weights: bool
    checkin_enabled: bool
    lifestyle_enabled: bool
    google_health_client_id: str | None
    google_health_client_secret: str | None
    google_health_refresh_token: str | None

    @classmethod
    def load(cls) -> "Config":
        missing: list[str] = []

        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                missing.append(name)
            return value

        gemini_api_key = required("GEMINI_API_KEY")
        telegram_bot_token = required("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = required("TELEGRAM_CHAT_ID")

        if missing:
            lines = ["Missing required environment variables:"]
            for name in missing:
                lines.append(f"  - {name}: {_REQUIRED_HINTS.get(name, '')}")
            lines.append("Copy .env.example to .env and fill it in.")
            raise ConfigError("\n".join(lines))

        # Hevy is optional: without it the agent still builds and sends a plan,
        # it just cannot reference your last logged session.
        hevy_api_key = os.environ.get("HEVY_API_KEY", "").strip() or None
        health_file = os.environ.get("HEALTH_CONNECT_FILE", "").strip() or None
        parse_mode = os.environ.get("TELEGRAM_PARSE_MODE", "").strip() or None
        sync_routines = os.environ.get("HEVY_SYNC_ROUTINES", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        prefill_weights = os.environ.get(
            "HEVY_PREFILL_WEIGHTS", "1"
        ).strip().lower() not in ("0", "false", "no")

        checkin_enabled = os.environ.get(
            "CHECKIN_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no")

        lifestyle_enabled = os.environ.get(
            "LIFESTYLE_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no")

        # Google Health is optional: it auto-syncs body weight and fat from a
        # smart scale (e.g. Eufy Life) so nothing has to be exported by hand.
        google_health_client_id = (
            os.environ.get("GOOGLE_HEALTH_CLIENT_ID", "").strip() or None
        )
        google_health_client_secret = (
            os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET", "").strip() or None
        )
        google_health_refresh_token = (
            os.environ.get("GOOGLE_HEALTH_REFRESH_TOKEN", "").strip() or None
        )

        return cls(
            hevy_api_key=hevy_api_key,
            gemini_api_key=gemini_api_key,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            health_connect_file=health_file,
            database_path=os.environ.get("DATABASE_PATH", "workout_agent.db").strip(),
            telegram_parse_mode=parse_mode,
            hevy_sync_routines=sync_routines,
            hevy_prefill_weights=prefill_weights,
            checkin_enabled=checkin_enabled,
            lifestyle_enabled=lifestyle_enabled,
            google_health_client_id=google_health_client_id,
            google_health_client_secret=google_health_client_secret,
            google_health_refresh_token=google_health_refresh_token,
        )
