"""Tests for scheduler.py: unified scheduling entry point."""

from __future__ import annotations

from unittest import mock

import pytest

from scheduler import _now_in_zone, _parse_times, main


class TestParseTimes:
    def test_single_time(self) -> None:
        assert _parse_times("07:00") == [(7, 0)]

    def test_comma_separated(self) -> None:
        assert _parse_times("00:00,05:00") == [(0, 0), (5, 0)]

    def test_space_separated(self) -> None:
        assert _parse_times("00:00 05:00") == [(0, 0), (5, 0)]

    def test_mixed_separators(self) -> None:
        assert _parse_times("00:00, 05:00") == [(0, 0), (5, 0)]

    def test_empty_returns_default(self) -> None:
        assert _parse_times("") == [(7, 0)]

    def test_unparseable_returns_default(self) -> None:
        assert _parse_times("garbage") == [(7, 0)]

    def test_leading_trailing_whitespace(self) -> None:
        assert _parse_times("  06:30  ") == [(6, 30)]


class TestNowInZone:
    def test_returns_datetime_utc_fallback(self) -> None:
        dt = _now_in_zone("UTC")
        assert dt.tzinfo is not None

    def test_returns_datetime_for_common_zone(self) -> None:
        dt = _now_in_zone("Europe/London")
        assert dt.tzinfo is not None


class TestMain:
    def test_once_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("MODE", "once")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

        with mock.patch("scheduler.run_once", return_value=0) as mock_run:
            rc = main([])
            assert rc == 0
            mock_run.assert_called_once()

    def test_preview_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("MODE", "preview")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

        with mock.patch("main.run", return_value=0) as mock_run:
            rc = main([])
            assert rc == 0
            mock_run.assert_called_once_with(preview=True)

    def test_schedule_mode_enters_loop(self, monkeypatch) -> None:
        monkeypatch.setenv("MODE", "schedule")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

        with mock.patch("scheduler.run_schedule") as mock_sched:
            main([])
            mock_sched.assert_called_once()

    def test_unknown_mode_exits_1(self, monkeypatch) -> None:
        monkeypatch.setenv("MODE", "bogus")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")

        rc = main([])
        assert rc == 1

    def test_missing_required_env_exits_1(self, monkeypatch) -> None:
        monkeypatch.setenv("MODE", "once")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        rc = main([])
        assert rc == 1


class TestRunOnce:
    def test_run_once_calls_coaching_and_insights(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("MODE", "once")

        from config import Config, ConfigError
        from database import init_db
        from scheduler import run_once

        try:
            config = Config.load()
        except ConfigError:
            pytest.skip("Config not available")
        init_db(config.database_path)

        with (
            mock.patch("scheduler.run_coaching", return_value=0) as mock_coach,
            mock.patch("scheduler.run_daily_insight") as mock_daily,
            mock.patch("scheduler.run_weekly_correlations"),
        ):
            rc = run_once(config)
            assert rc == 0
            mock_coach.assert_called_once_with(config)
            mock_daily.assert_called_once_with(config)