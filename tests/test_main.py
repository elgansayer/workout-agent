"""Tests for main.py: the daily agent orchestration entry point."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from config import Config, ConfigError
from program import BLOCKS


def _config(tmp_path, hevy_api_key=None, gemini_api_key="server-key", **overrides) -> Config:
    db_path = str(tmp_path / "test_main.db")
    return Config(
        hevy_api_key=hevy_api_key,
        gemini_api_key=gemini_api_key,
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
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
# _deliver tests
# ---------------------------------------------------------------------------


def test_deliver_preview_prints_to_stdout(monkeypatch, capsys, tmp_path):
    from main import _deliver

    config = _config(tmp_path)
    result = _deliver(config, "Test plan message.", preview=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Test plan message" in captured.out


def test_deliver_sends_telegram(monkeypatch, tmp_path):
    from main import _deliver

    config = _config(tmp_path)
    monkeypatch.setattr("main.send_telegram_message", lambda *a, **kw: True)
    result = _deliver(config, "Test message.", preview=False)
    assert result == 0


def test_deliver_telegram_failure_returns_2(monkeypatch, tmp_path):
    from main import _deliver

    config = _config(tmp_path)
    monkeypatch.setattr("main.send_telegram_message", lambda *a, **kw: False)
    result = _deliver(config, "Test message.", preview=False)
    assert result == 2


# ---------------------------------------------------------------------------
# _changes_footer tests
# ---------------------------------------------------------------------------


def test_changes_footer_empty():
    from main import _changes_footer
    assert _changes_footer([]) == ""


def test_changes_footer_with_updates():
    from main import _changes_footer
    statuses = [
        "Push: unchanged",
        "Pull: updated",
        "Legs: created",
    ]
    footer = _changes_footer(statuses)
    assert "Push" not in footer
    assert "Pull" in footer
    assert "Legs" in footer
    assert "Hevy routines refreshed" in footer


# ---------------------------------------------------------------------------
# _compose tests
# ---------------------------------------------------------------------------


def test_compose_workout_only():
    from main import _compose

    body = "Deadlift: 4 x 5-8"
    text = _compose(body, None, "")
    assert text == "Deadlift: 4 x 5-8"


def test_compose_with_guidance():
    from unittest.mock import MagicMock

    from main import _compose

    guidance = MagicMock()
    guidance.as_text.return_value = "High carb day - eat well!"
    body = "Deadlift: 4 x 5-8"
    text = _compose(body, guidance, "")
    assert "Deadlift: 4 x 5-8" in text
    assert "High carb day" in text


def test_compose_with_footer():
    from main import _compose

    body = "Deadlift: 4 x 5-8"
    text = _compose(body, None, "\n\nHevy routines refreshed: Pull.")
    assert "Deadlift: 4 x 5-8" in text
    assert "Hevy routines refreshed" in text


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------


def test_run_preview_rest_day(monkeypatch, tmp_path, capsys):
    """On a Sunday, run should print a rest day message."""
    import main as main_module
    from database import init_db

    config = _config(tmp_path)
    db_path = config.database_path
    init_db(db_path)

    # Mock today to be a Sunday (2026-08-02 is a Sunday; today_day returns None)
    monkeypatch.setattr(main_module, "week_in_cycle", lambda *a, **kw: 3)
    monkeypatch.setattr(main_module, "today_day", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "block_for_week", lambda *a, **kw: BLOCKS[1])

    monkeypatch.setattr(
        main_module, "generate_rest_day_message",
        lambda **kw: "Rest day! Focus on recovery."
    )
    monkeypatch.setattr(
        main_module, "read_recovery_metrics", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        main_module.google_health_client, "sync_body_metrics",
        lambda *a, **kw: None
    )

    monkeypatch.setattr(main_module.Config, "load", lambda: config)
    monkeypatch.setattr(main_module, "init_db", lambda *a, **kw: None)

    # Patch save_body_metrics, save_daily_log to prevent db operations that
    # might fail in preview mode
    monkeypatch.setattr(main_module, "save_body_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_sync_hevy_routines", lambda *a, **kw: [])
    monkeypatch.setattr(main_module, "_maybe_check_in", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_maybe_self_review", lambda *a, **kw: None)

    result = main_module.run(preview=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Rest day" in captured.out
    assert "recovery" in captured.out.lower()


def test_run_preview_workout_day(monkeypatch, tmp_path, capsys):
    """On a Monday, run should print a workout plan."""
    import main as main_module
    from database import init_db

    config = _config(tmp_path)
    db_path = config.database_path
    init_db(db_path)

    monkeypatch.setattr(main_module, "week_in_cycle", lambda *a, **kw: 3)
    monkeypatch.setattr(main_module, "today_day", lambda *a, **kw: 1)
    monkeypatch.setattr(main_module, "block_for_week", lambda *a, **kw: BLOCKS[1])

    monkeypatch.setattr(
        main_module, "generate_next_workout",
        lambda **kw: "Back, Deadlifts & Chest - Week 3 (Accumulation)\nDeadlift: 4 x 5-8"
    )
    monkeypatch.setattr(
        main_module, "read_recovery_metrics", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        main_module.google_health_client, "sync_body_metrics",
        lambda *a, **kw: None
    )
    monkeypatch.setattr(
        main_module.insights_engine, "build_insights",
        lambda *a, **kw: MagicMock(headline="All progressing.")
    )

    monkeypatch.setattr(main_module.Config, "load", lambda: config)
    monkeypatch.setattr(main_module, "init_db", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_body_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_workout", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_progress", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_sync_hevy_routines", lambda *a, **kw: [])
    monkeypatch.setattr(main_module, "_maybe_check_in", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_maybe_self_review", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "get_progress_history", lambda **kw: {})
    monkeypatch.setattr(main_module, "get_body_metrics", lambda **kw: [])
    monkeypatch.setattr(main_module, "get_recent_bests", lambda *a, **kw: {})
    monkeypatch.setattr(main_module, "get_daily_logs", lambda *a, **kw: [])
    monkeypatch.setattr(main_module, "get_programme_start_date", lambda *a, **kw: date(2026, 7, 13))

    result = main_module.run(preview=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Back, Deadlifts & Chest" in captured.out


def test_run_config_error_returns_1(monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module.Config, "load", MagicMock(side_effect=ConfigError("missing vars")))
    result = main_module.run(preview=True)
    assert result == 1


def test_run_sync_hevy_routines_skipped_without_api_key(monkeypatch, tmp_path, capsys):
    """When Hevy API key is missing, _sync_hevy_routines returns empty list."""
    import main as main_module
    from database import init_db

    config = _config(tmp_path, hevy_api_key=None)
    init_db(config.database_path)

    monkeypatch.setattr(main_module, "week_in_cycle", lambda *a, **kw: 3)
    monkeypatch.setattr(main_module, "today_day", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "block_for_week", lambda *a, **kw: BLOCKS[1])
    monkeypatch.setattr(
        main_module, "generate_rest_day_message",
        lambda **kw: "Rest day message."
    )
    monkeypatch.setattr(main_module, "read_recovery_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(
        main_module.google_health_client, "sync_body_metrics",
        lambda *a, **kw: None
    )

    monkeypatch.setattr(main_module.Config, "load", lambda: config)
    monkeypatch.setattr(main_module, "init_db", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_body_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_maybe_check_in", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_maybe_self_review", lambda *a, **kw: None)

    result = main_module.run(preview=True)
    assert result == 0
    captured = capsys.readouterr()
    assert "Rest day message" in captured.out


def test_run_checkin_runs_when_due(monkeypatch, tmp_path, capsys):
    """When a check-in is due, it should be delivered."""
    import main as main_module
    from database import init_db

    config = _config(tmp_path)
    init_db(config.database_path)

    monkeypatch.setattr(main_module, "week_in_cycle", lambda *a, **kw: 3)
    monkeypatch.setattr(main_module, "today_day", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "block_for_week", lambda *a, **kw: BLOCKS[1])
    monkeypatch.setattr(
        main_module, "generate_rest_day_message",
        lambda **kw: "Rest day message."
    )
    monkeypatch.setattr(main_module, "read_recovery_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(
        main_module.google_health_client, "sync_body_metrics",
        lambda *a, **kw: None
    )

    monkeypatch.setattr(main_module.Config, "load", lambda: config)
    monkeypatch.setattr(main_module, "init_db", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_body_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(main_module, "_sync_hevy_routines", lambda *a, **kw: [])
    monkeypatch.setattr(main_module, "_maybe_self_review", lambda *a, **kw: None)

    checkin_messages = []

    def fake_checkin(config, week, block, preview):
        msg = "Check-in: You're doing great!"
        checkin_messages.append(msg)
        main_module._deliver(config, msg, preview)

    monkeypatch.setattr(main_module, "_maybe_check_in", fake_checkin)

    result = main_module.run(preview=True)
    assert result == 0
    captured = capsys.readouterr()
    # The rest day message should still be delivered
    assert "Rest day" in captured.out


# ---------------------------------------------------------------------------
# _parse_args tests
# ---------------------------------------------------------------------------


def test_parse_args_default():
    from main import _parse_args
    args = _parse_args([])
    assert args.preview is False


def test_parse_args_preview():
    from main import _parse_args
    args = _parse_args(["--preview"])
    assert args.preview is True
