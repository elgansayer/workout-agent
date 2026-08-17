"""Tests for config.py — configuration loading and validation."""

from __future__ import annotations

import pytest

from config import Config, ConfigError, _parse_weekday


class TestParseWeekday:
    def test_full_name_lowercase(self) -> None:
        assert _parse_weekday("monday") == 0
        assert _parse_weekday("tuesday") == 1
        assert _parse_weekday("wednesday") == 2
        assert _parse_weekday("thursday") == 3
        assert _parse_weekday("friday") == 4
        assert _parse_weekday("saturday") == 5
        assert _parse_weekday("sunday") == 6

    def test_abbreviated_name(self) -> None:
        assert _parse_weekday("mon") == 0
        assert _parse_weekday("tue") == 1
        assert _parse_weekday("wed") == 2
        assert _parse_weekday("thu") == 3
        assert _parse_weekday("fri") == 4
        assert _parse_weekday("sat") == 5
        assert _parse_weekday("sun") == 6

    def test_case_insensitive(self) -> None:
        assert _parse_weekday("MONDAY") == 0
        assert _parse_weekday("Mon") == 0
        assert _parse_weekday("SUN") == 6

    def test_numeric_valid(self) -> None:
        for i in range(7):
            assert _parse_weekday(str(i)) == i

    def test_numeric_out_of_range_returns_default(self) -> None:
        assert _parse_weekday("7") == 6
        assert _parse_weekday("-1") == 6

    def test_empty_string_returns_default(self) -> None:
        assert _parse_weekday("") == 6
        assert _parse_weekday("   ") == 6

    def test_unrecognised_name_returns_default(self) -> None:
        assert _parse_weekday("funday") == 6

    def test_custom_default(self) -> None:
        assert _parse_weekday("", default=0) == 0
        assert _parse_weekday("nope", default=3) == 3


class TestConfigLoad:
    def test_missing_required_raises_config_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(ConfigError, match="Missing required"):
            Config.load()

    def test_missing_shows_all_missing_at_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(ConfigError) as exc_info:
            Config.load()
        msg = str(exc_info.value)
        assert "TELEGRAM_BOT_TOKEN" in msg
        assert "TELEGRAM_CHAT_ID" in msg
        assert "GEMINI_API_KEY" not in msg

    def test_minimal_valid_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
        cfg = Config.load()
        assert cfg.gemini_api_key == "gemini-key"
        assert cfg.telegram_bot_token == "bot-token"
        assert cfg.telegram_chat_id == "chat-id"
        assert cfg.hevy_api_key is None
        assert cfg.gemini_model == "gemini-2.5-flash"
        assert cfg.database_path == "workout_agent.db"

    def test_hevy_api_key_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("HEVY_API_KEY", raising=False)
        cfg = Config.load()
        assert cfg.hevy_api_key is None

    def test_hevy_api_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.setenv("HEVY_API_KEY", "hevy-key")
        cfg = Config.load()
        assert cfg.hevy_api_key == "hevy-key"

    def test_google_health_optional_all_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        for var in (
            "GOOGLE_HEALTH_CLIENT_ID",
            "GOOGLE_HEALTH_CLIENT_SECRET",
            "GOOGLE_HEALTH_REFRESH_TOKEN",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = Config.load()
        assert cfg.google_health_client_id is None
        assert cfg.google_health_client_secret is None
        assert cfg.google_health_refresh_token is None

    def test_google_health_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "gh-id")
        monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "gh-secret")
        monkeypatch.setenv("GOOGLE_HEALTH_REFRESH_TOKEN", "gh-refresh")
        cfg = Config.load()
        assert cfg.google_health_client_id == "gh-id"
        assert cfg.google_health_client_secret == "gh-secret"
        assert cfg.google_health_refresh_token == "gh-refresh"

    def test_boolean_env_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.setenv("HEVY_SYNC_ROUTINES", "1")
        monkeypatch.setenv("HEVY_PREFILL_WEIGHTS", "true")
        monkeypatch.setenv("CHECKIN_ENABLED", "yes")
        monkeypatch.setenv("LIFESTYLE_ENABLED", "on")
        monkeypatch.setenv("SELF_REVIEW_ENABLED", "1")
        cfg = Config.load()
        assert cfg.hevy_sync_routines is True
        assert cfg.hevy_prefill_weights is True
        assert cfg.checkin_enabled is True
        assert cfg.lifestyle_enabled is True
        assert cfg.self_review_enabled is True

    def test_boolean_env_falsey_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.setenv("HEVY_SYNC_ROUTINES", "0")
        monkeypatch.setenv("HEVY_PREFILL_WEIGHTS", "false")
        monkeypatch.setenv("CHECKIN_ENABLED", "no")
        monkeypatch.setenv("LIFESTYLE_ENABLED", "FALSE")
        monkeypatch.setenv("SELF_REVIEW_ENABLED", "0")
        cfg = Config.load()
        assert cfg.hevy_sync_routines is False
        assert cfg.hevy_prefill_weights is False
        assert cfg.checkin_enabled is False
        assert cfg.lifestyle_enabled is False
        assert cfg.self_review_enabled is False

    def test_gemini_model_default_and_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        cfg = Config.load()
        assert cfg.gemini_model == "gemini-2.5-flash"

        monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
        cfg = Config.load()
        assert cfg.gemini_model == "gemini-2.5-pro"

    def test_database_path_default_and_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        cfg = Config.load()
        assert cfg.database_path == "workout_agent.db"

        monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
        cfg = Config.load()
        assert cfg.database_path == "/tmp/test.db"

    def test_telegram_parse_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("TELEGRAM_PARSE_MODE", raising=False)
        cfg = Config.load()
        assert cfg.telegram_parse_mode is None

        monkeypatch.setenv("TELEGRAM_PARSE_MODE", "MarkdownV2")
        cfg = Config.load()
        assert cfg.telegram_parse_mode == "MarkdownV2"

    def test_self_review_weekday_default_and_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("SELF_REVIEW_WEEKDAY", raising=False)
        cfg = Config.load()
        assert cfg.self_review_weekday == 6  # Sunday

        monkeypatch.setenv("SELF_REVIEW_WEEKDAY", "mon")
        cfg = Config.load()
        assert cfg.self_review_weekday == 0

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "  gk  ")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "  tt  ")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "  ci  ")
        monkeypatch.setenv("HEVY_API_KEY", "  hevy  ")
        cfg = Config.load()
        assert cfg.gemini_api_key == "gk"
        assert cfg.telegram_bot_token == "tt"
        assert cfg.telegram_chat_id == "ci"
        assert cfg.hevy_api_key == "hevy"

    def test_whitespace_only_treated_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "   ")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "   ")
        with pytest.raises(ConfigError):
            Config.load()

    def test_health_connect_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        monkeypatch.delenv("HEALTH_CONNECT_FILE", raising=False)
        cfg = Config.load()
        assert cfg.health_connect_file is None

        monkeypatch.setenv("HEALTH_CONNECT_FILE", "/data/health.json")
        cfg = Config.load()
        assert cfg.health_connect_file == "/data/health.json"

    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "gk")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tt")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "ci")
        cfg = Config.load()
        with pytest.raises(AttributeError):
            cfg.gemini_api_key = "new"  # type: ignore[misc]
