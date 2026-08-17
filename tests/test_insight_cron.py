"""Tests for insight_cron.py: daily header and weekly deep correlation generation."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from config import Config
from insight_cron import (
    _resolve_provider,
    generate_daily_header,
    generate_weekly_correlations,
    main,
)


def _config(tmp_path, gemini_api_key="server-key", **overrides) -> Config:
    db_path = str(tmp_path / "test_cron.db")
    return Config(
        hevy_api_key=None,
        gemini_api_key=gemini_api_key,
        telegram_bot_token="t",
        telegram_chat_id="c",
        gemini_model="gemini-2.5-flash",
        health_connect_file=None,
        database_path=db_path,
        telegram_parse_mode=None,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
        **overrides,
    )


# ---------------------------------------------------------------------------
# _resolve_provider tests
# ---------------------------------------------------------------------------


def test_resolve_provider_returns_gemini_provider(tmp_path):
    from database import init_db

    config = _config(tmp_path)
    init_db(config.database_path)

    provider = _resolve_provider(config)
    assert "gemini" in provider.name().lower()


def test_resolve_provider_raises_without_key(tmp_path):
    config = _config(tmp_path, gemini_api_key="")
    with pytest.raises(ValueError, match="No AI provider key"):
        _resolve_provider(config)


# ---------------------------------------------------------------------------
# generate_daily_header tests
# ---------------------------------------------------------------------------


def test_generate_daily_header_invalid_json_logs_error(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    fake_provider = MagicMock()
    fake_provider.generate.return_value = "not valid json"
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr(
        "insight_cron.get_body_metrics", lambda **kw: []
    )
    monkeypatch.setattr(
        "insight_cron.get_daily_logs", lambda **kw: []
    )

    # Should not raise; should log an error
    generate_daily_header(config)


def test_generate_daily_header_valid_json_saves_insight(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    valid_json = json.dumps({
        "fatigue": "Green",
        "wins_stalls": "Deadlift progressing well",
        "advice": "Push it today",
    })

    saved_insights: list = []

    fake_provider = MagicMock()
    fake_provider.generate.return_value = valid_json
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr(
        "insight_cron.get_body_metrics", lambda **kw: [
            {"date": "2026-08-05", "weight_kg": 82.0, "body_fat_pct": 15.0},
        ]
    )
    monkeypatch.setattr(
        "insight_cron.get_daily_logs", lambda **kw: [
            {"date": "2026-08-05", "day": 1, "focus": "Back", "plan": "..."},
        ]
    )
    monkeypatch.setattr(
        "insight_cron.save_dashboard_insight",
        lambda insight_json, **kw: saved_insights.append(insight_json),
    )

    generate_daily_header(config)

    assert len(saved_insights) == 1
    parsed = json.loads(saved_insights[0])
    assert parsed["fatigue"] == "Green"


def test_generate_daily_header_missing_fields_logs_error(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    fake_provider = MagicMock()
    fake_provider.generate.return_value = json.dumps({"fatigue": "Green"})
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])

    # Should not raise; should log an error for missing fields
    generate_daily_header(config)


def test_generate_daily_header_strips_markdown_wrapping(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    valid_json = json.dumps({
        "fatigue": "Yellow",
        "wins_stalls": "Sleep improving",
        "advice": "Hold steady",
    })
    wrapped = "```json\n" + valid_json + "\n```"

    saved_insights: list = []

    fake_provider = MagicMock()
    fake_provider.generate.return_value = wrapped
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr(
        "insight_cron.save_dashboard_insight",
        lambda insight_json, **kw: saved_insights.append(insight_json),
    )

    generate_daily_header(config)
    assert len(saved_insights) == 1
    parsed = json.loads(saved_insights[0])
    assert parsed["fatigue"] == "Yellow"


def test_generate_daily_header_provider_error_logs_gracefully(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    fake_provider = MagicMock()
    fake_provider.generate.side_effect = RuntimeError("API error")
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)

    # Should not raise
    generate_daily_header(config)


# ---------------------------------------------------------------------------
# generate_weekly_correlations tests
# ---------------------------------------------------------------------------


def test_generate_weekly_correlations_saves_correlation(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    saved_correlations: list = []

    fake_provider = MagicMock()
    fake_provider.generate.return_value = "# Deep Correlation Report\n\nInteresting findings..."
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr(
        "insight_cron.get_body_metrics", lambda **kw: [
            {"date": "2026-08-01", "weight_kg": 82.0},
        ]
    )
    monkeypatch.setattr(
        "insight_cron.get_daily_logs", lambda **kw: [
            {"date": "2026-08-01", "day": 1},
        ]
    )
    monkeypatch.setattr(
        "insight_cron.get_progress_history", lambda **kw: {
            "Deadlift": [{"date": "2026-08-01", "top_weight_kg": 120.0, "top_reps": 5}],
        }
    )
    monkeypatch.setattr(
        "insight_cron.save_deep_correlation",
        lambda insight_markdown, **kw: saved_correlations.append(insight_markdown),
    )

    generate_weekly_correlations(config)

    assert len(saved_correlations) == 1
    assert "Deep Correlation" in saved_correlations[0]


def test_generate_weekly_correlations_empty_response_no_save(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    saved_correlations: list = []

    fake_provider = MagicMock()
    fake_provider.generate.return_value = ""
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_progress_history", lambda **kw: {})
    monkeypatch.setattr(
        "insight_cron.save_deep_correlation",
        lambda insight_markdown, **kw: saved_correlations.append(insight_markdown),
    )

    generate_weekly_correlations(config)

    assert saved_correlations == []


def test_generate_weekly_correlations_error_logs_gracefully(tmp_path, monkeypatch):
    config = _config(tmp_path)
    from database import init_db

    init_db(config.database_path)

    fake_provider = MagicMock()
    fake_provider.generate.side_effect = RuntimeError("API down")
    monkeypatch.setattr("insight_cron._resolve_provider", lambda cfg: fake_provider)

    # Should not raise
    generate_weekly_correlations(config)


# ---------------------------------------------------------------------------
# main() CLI tests
# ---------------------------------------------------------------------------


def test_main_daily_flag(monkeypatch, tmp_path):
    from database import init_db

    db_path = str(tmp_path / "test_main.db")

    # Setup env for Config.load
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    init_db(db_path)

    called = []

    def fake_daily(config):
        called.append(("daily", config))

    monkeypatch.setattr("insight_cron.generate_daily_header", fake_daily)

    import sys
    monkeypatch.setattr(sys, "argv", ["insight_cron.py", "--daily"])

    main()
    assert len(called) == 1
    assert called[0][0] == "daily"


def test_main_weekly_flag(monkeypatch, tmp_path):
    from database import init_db

    db_path = str(tmp_path / "test_main_w.db")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    init_db(db_path)

    called = []

    def fake_weekly(config):
        called.append(("weekly", config))

    monkeypatch.setattr("insight_cron.generate_weekly_correlations", fake_weekly)

    import sys
    monkeypatch.setattr(sys, "argv", ["insight_cron.py", "--weekly"])

    main()
    assert len(called) == 1
    assert called[0][0] == "weekly"


def test_main_both_flags(monkeypatch, tmp_path):
    from database import init_db

    db_path = str(tmp_path / "test_main_b.db")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    init_db(db_path)

    called = []

    def fake_daily(config):
        called.append("daily")

    def fake_weekly(config):
        called.append("weekly")

    monkeypatch.setattr("insight_cron.generate_daily_header", fake_daily)
    monkeypatch.setattr("insight_cron.generate_weekly_correlations", fake_weekly)

    import sys
    monkeypatch.setattr(sys, "argv", ["insight_cron.py", "--daily", "--weekly"])

    main()
    assert called == ["daily", "weekly"]


def test_main_no_flags_runs_silently(monkeypatch, tmp_path):
    from database import init_db

    db_path = str(tmp_path / "test_main_n.db")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    init_db(db_path)

    called = []

    monkeypatch.setattr("insight_cron.generate_daily_header", lambda cfg: called.append("daily"))
    monkeypatch.setattr("insight_cron.generate_weekly_correlations", lambda cfg: called.append("weekly"))

    import sys
    monkeypatch.setattr(sys, "argv", ["insight_cron.py"])

    main()
    assert called == []


def test_main_config_error_exits(monkeypatch, tmp_path):
    import sys

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setattr(sys, "argv", ["insight_cron.py", "--daily"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
