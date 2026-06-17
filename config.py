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


@dataclass(frozen=True)
class Config:
    hevy_api_key: str
    gemini_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    gemini_model: str
    health_connect_file: str | None
    database_path: str

    @classmethod
    def load(cls) -> "Config":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ConfigError(
                    f"Missing required environment variable: {name}. "
                    "Copy .env.example to .env and fill it in."
                )
            return value

        health_file = os.environ.get("HEALTH_CONNECT_FILE", "").strip() or None

        return cls(
            hevy_api_key=required("HEVY_API_KEY"),
            gemini_api_key=required("GEMINI_API_KEY"),
            telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=required("TELEGRAM_CHAT_ID"),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash").strip(),
            health_connect_file=health_file,
            database_path=os.environ.get("DATABASE_PATH", "workout_agent.db").strip(),
        )
