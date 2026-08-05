"""Tests for config.py — environment-variable-based configuration loading."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from config import Config, ConfigError, _parse_weekday


@pytest.fixture(autouse=True)
def _clear_env() -> None:
    """Ensure tests don't leak env vars between each other."""
    for key in (
        "GEMINI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "HEVY_API_KEY",
        "HEALTH_CONNECT_FILE",
        "TELEGRAM_PARSE_MODE",
        "HEVY_SYNC_ROUTINES",
        "HEVY_PREFILL_WEIGHTS",
        "CHECKIN_ENABLED",
        "LIFESTYLE_ENABLED",
        "GOOGLE_HEALTH_CLIENT_ID",
        "GOOGLE_HEALTH_CLIENT_SECRET",
        "GOOGLE_HEALTH_REFRESH_TOKEN",
        "SELF_REVIEW_ENABLED",
        "SELF_REVIEW_WEEKDAY",
        "GEMINI_MODEL",
        "DATABASE_PATH",
    ):
        os.environ.pop(key, None)


def _set_required_vars() -> None:
    os.environ["GEMINI_API_KEY"] = "test-gemini-key"
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:bot-token"
    os.environ["TELEGRAM_CHAT_ID"] = "456789"


# ── _parse_weekday ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mon", 0),
        ("tue", 1),
        ("wed", 2),
        ("thu", 3),
        ("fri", 4),
        ("sat", 5),
        ("sun", 6),
        ("Mon", 0),
        ("MON", 0),
        ("monday", 0),
        ("Tuesday", 1),
        ("WEDNESDAY", 2),
        ("0", 0),
        ("6", 6),
        ("", 6),
        ("invalid", 6),
        ("-1", 6),
        ("7", 6),
        ("99", 6),
    ],
)
def test_parse_weekday(value: str, expected: int) -> None:
    assert _parse_weekday(value) == expected


def test_parse_weekday_default_override() -> None:
    """The default parameter works when value is empty."""
    assert _parse_weekday("", default=0) == 0
    assert _parse_weekday("   ", default=0) == 0


# ── required vars ───────────────────────────────────────────────────────


def test_load_raises_when_required_vars_missing() -> None:
    """Config.load() raises ConfigError with helpful message."""
    with pytest.raises(ConfigError, match="Missing required environment variables"):
        Config.load()


def test_load_succeeds_with_minimal_required_vars() -> None:
    """Config.load() works with just the three required vars set."""
    _set_required_vars()
    cfg = Config.load()
    assert cfg.gemini_api_key == "test-gemini-key"
    assert cfg.telegram_bot_token == "123:bot-token"
    assert cfg.telegram_chat_id == "456789"


# ── defaults ────────────────────────────────────────────────────────────


def test_defaults() -> None:
    """Optional fields have sensible defaults."""
    _set_required_vars()
    cfg = Config.load()
    assert cfg.hevy_api_key is None
    assert cfg.health_connect_file is None
    assert cfg.google_health_client_id is None
    assert cfg.google_health_client_secret is None
    assert cfg.google_health_refresh_token is None
    assert cfg.telegram_parse_mode is None
    assert cfg.gemini_model == "gemini-2.5-flash"
    assert cfg.database_path == "workout_agent.db"
    assert cfg.hevy_sync_routines is True
    assert cfg.hevy_prefill_weights is True
    assert cfg.checkin_enabled is True
    assert cfg.lifestyle_enabled is True
    assert cfg.self_review_enabled is True
    assert cfg.self_review_weekday == 6  # Sunday


# ── optional vars ───────────────────────────────────────────────────────


def test_hevy_api_key_set() -> None:
    """HEVY_API_KEY is captured when set."""
    _set_required_vars()
    os.environ["HEVY_API_KEY"] = "hevy-token-abc"
    cfg = Config.load()
    assert cfg.hevy_api_key == "hevy-token-abc"


def test_hevy_api_key_whitespace_is_none() -> None:
    """Whitespace-only HEVY_API_KEY is treated as None."""
    _set_required_vars()
    os.environ["HEVY_API_KEY"] = "   "
    cfg = Config.load()
    assert cfg.hevy_api_key is None


def test_google_health_credentials() -> None:
    """Google Health OAuth credentials are captured."""
    _set_required_vars()
    os.environ["GOOGLE_HEALTH_CLIENT_ID"] = "gh-client-id"
    os.environ["GOOGLE_HEALTH_CLIENT_SECRET"] = "gh-secret"
    os.environ["GOOGLE_HEALTH_REFRESH_TOKEN"] = "gh-refresh"
    cfg = Config.load()
    assert cfg.google_health_client_id == "gh-client-id"
    assert cfg.google_health_client_secret == "gh-secret"
    assert cfg.google_health_refresh_token == "gh-refresh"


def test_gemini_model_override() -> None:
    """GEMINI_MODEL env var overrides the default."""
    _set_required_vars()
    os.environ["GEMINI_MODEL"] = "gemini-2.5-pro"
    cfg = Config.load()
    assert cfg.gemini_model == "gemini-2.5-pro"


def test_database_path_override() -> None:
    """DATABASE_PATH env var overrides the default."""
    _set_required_vars()
    os.environ["DATABASE_PATH"] = "/data/test.db"
    cfg = Config.load()
    assert cfg.database_path == "/data/test.db"


# ── boolean flags ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["0", "false", "False", "FALSE", "no", "NO", "No"])
def test_boolean_false_values(value: str) -> None:
    _set_required_vars()
    os.environ["CHECKIN_ENABLED"] = value
    os.environ["LIFESTYLE_ENABLED"] = value
    os.environ["HEVY_SYNC_ROUTINES"] = value
    os.environ["HEVY_PREFILL_WEIGHTS"] = value
    os.environ["SELF_REVIEW_ENABLED"] = value
    cfg = Config.load()
    assert cfg.checkin_enabled is False
    assert cfg.lifestyle_enabled is False
    assert cfg.hevy_sync_routines is False
    assert cfg.hevy_prefill_weights is False
    assert cfg.self_review_enabled is False


def test_self_review_weekday_custom() -> None:
    """SELF_REVIEW_WEEKDAY can be set to a day name or number."""
    _set_required_vars()
    os.environ["SELF_REVIEW_WEEKDAY"] = "mon"
    cfg = Config.load()
    assert cfg.self_review_weekday == 0


# ── immutability ────────────────────────────────────────────────────────


def test_config_is_frozen() -> None:
    """Config dataclass is frozen (immutable)."""
    _set_required_vars()
    cfg = Config.load()
    with pytest.raises(FrozenInstanceError):
        cfg.gemini_api_key = "new-key"  # type: ignore[misc]


# ── ConfigError subclass ────────────────────────────────────────────────


def test_config_error_is_runtime_error() -> None:
    """ConfigError is a subclass of RuntimeError."""
    assert issubclass(ConfigError, RuntimeError)