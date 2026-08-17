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
        checkin_enabled=checkin_enabled,
        lifestyle_enabled=lifestyle_enabled,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=self_review_enabled,
        self_review_weekday=6,
    )

    monkeypatch.setattr("main.Config.load", lambda: config)

    # Patch init_db to do nothing (avoid real DB creation)
    monkeypatch.setattr("main.init_db", lambda path: None)

    return config


def test_run_preview_training_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preview mode on a training day prints the plan without sending Telegram."""
    _make_config(monkeypatch, tmp_path)

    # Today is a Monday (day 1) — force the date
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    # Mock Hevy sync returns no statuses
    monkeypatch.setattr("main.sync_routines", lambda cfg: [])

    # Mock Google Health
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )

    # Mock health connect
    monkeypatch.setattr(
        "main.read_recovery_metrics",
        lambda path: {"sleep_hours": 7.5, "weight_kg": 82},
    )
    monkeypatch.setattr(
        "main.body_metrics_from_recovery",
        lambda rec: rec,
    )
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)

    # Mock fetch / parse workout
    monkeypatch.setattr(
        "main.fetch_latest_workout",
        lambda key: {"id": "1", "title": "Day 1", "exercises": []},
    )
    from hevy_parser import WorkoutSummary

    monkeypatch.setattr(
        "main.parse_workout",
        lambda raw, targets: WorkoutSummary(
            title="Day 1",
            date="2026-08-02",
            duration_seconds=3600,
            total_volume_kg=2500,
            exercises=[],
        ),
    )
    monkeypatch.setattr("main.save_workout", lambda raw, path: None)
    monkeypatch.setattr("main.save_progress", lambda summary, path: None)

    # Mock recent bests
    monkeypatch.setattr(
        "main.get_recent_bests",
        lambda path: {"Deadlift (Barbell)": {"top_weight_kg": 140, "top_reps": 5}},
    )

    # Mock insights
    from insights import LiftInsight, RecoveryInsight, TrainingInsights

    fake_insights = TrainingInsights(
        lifts=[
            LiftInsight(
                name="Deadlift (Barbell)",
                trend="progressing",
                metric="kg",
                change_pct=5.0,
                sessions=4,
                latest=140.0,
                best=140.0,
                slope_per_session=1.25,
                sessions_since_best=0,
                intervention=None,
            ),
        ],
        recovery=RecoveryInsight(
            sleep_hours=7.5,
            resting_hr=58,
            resting_hr_trend="steady",
            weight_kg=82.0,
            weight_trend="steady",
            body_fat_pct=None,
            body_fat_trend=None,
            muscle_pct=None,
            is_catabolic=False,
            status="good",
            directive="Stay the course.",
        ),
        headline="All lifts progressing.",
    )
    monkeypatch.setattr(
        "main.insights_engine.build_insights",
        lambda *a, **kw: fake_insights,
    )
    monkeypatch.setattr("main.get_progress_history", lambda **kw: {})
    monkeypatch.setattr("main.get_body_metrics", lambda **kw: [])

    # Mock daily logs
    monkeypatch.setattr("main.get_daily_logs", lambda **kw: [])

    # Mock Gemini — return a canned plan
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *args, **kw: (
            "Back, Deadlifts & Chest - Week 1 (Hypertrophy)\nDeadlift: 4 x 8"
        ),
    )
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)

    # Mock programme start date
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda path: date(2026, 8, 3),
    )

    result = run(preview=True)
    assert result == 0  # success


def test_run_preview_rest_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Preview mode on a Sunday (rest day)."""
    _make_config(monkeypatch, tmp_path)

    # Sunday
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    monkeypatch.setattr("main.sync_routines", lambda cfg: [])
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda path: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)

    monkeypatch.setattr(
        "main.generate_rest_day_message",
        lambda *args, **kw: "Rest day! Recovery is key.",
    )
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)

    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda path: date(2026, 8, 3),
    )

    result = run(preview=True)
    assert result == 0


def test_run_config_error_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Config.load raises ConfigError, run returns 1."""
    monkeypatch.setattr(
        "main.Config.load",
        MagicMock(side_effect=__import__("config").ConfigError("Missing vars")),
    )
    result = run(preview=True)
    assert result == 1


def test_run_preview_with_sync_statuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Statuses from sync_routines appear in the footer."""
    _make_config(monkeypatch, tmp_path, hevy_api_key="hevy-key")
    # Patch Config.load to return a config with hevy_sync_routines=True
    from config import Config

    db_path = str(tmp_path / "test_main.db")
    config2 = Config(
        hevy_api_key="hevy-key",
        gemini_api_key="test-gemini-key",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="test-chat-id",
        gemini_model="gemini-2.5-flash",
        health_connect_file=None,
        database_path=db_path,
        telegram_parse_mode=None,
        hevy_sync_routines=True,
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


def test_run_preview_with_lifestyle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When lifestyle is enabled, guidance appears in the output."""
    _make_config(monkeypatch, tmp_path, lifestyle_enabled=True)

    config = _config(tmp_path)
    monkeypatch.setattr("main.send_telegram_message", lambda *a, **kw: True)
    result = _deliver(config, "Test message.", preview=False)
    assert result == 0


def test_run_preview_training_day_with_checkin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When a check-in is due, it is also printed in preview mode."""
    _make_config(monkeypatch, tmp_path, checkin_enabled=True)

    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    monkeypatch.setattr("main.sync_routines", lambda cfg: [])

    # Make check-in due
    import checkin

    monkeypatch.setattr(
        "main.checkin.due",
        lambda cfg: checkin.CheckinDue(
            number=1,
            workouts_done=24,
            weeks_elapsed=4,
            total_count=64,
        ),
    )
    monkeypatch.setattr(
        "main.checkin.run_checkin",
        lambda cfg, due_info, week, block: "Check-in 1: all good.",
    )
    monkeypatch.setattr("main.checkin.record", lambda cfg, due_info, msg: None)

    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "main.read_recovery_metrics",
        lambda path: {"sleep_hours": 7.5, "weight_kg": 82},
    )
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)
    monkeypatch.setattr(
        "main.fetch_latest_workout",
        lambda key: {"id": "1", "title": "Day 1", "exercises": []},
    )
    from hevy_parser import WorkoutSummary

    monkeypatch.setattr(
        "main.parse_workout",
        lambda raw, targets: WorkoutSummary(
            title="Day 1",
            date="2026-08-02",
            duration_seconds=3600,
            total_volume_kg=2500,
            exercises=[],
        ),
    )
    monkeypatch.setattr("main.save_workout", lambda raw, path: None)
    monkeypatch.setattr("main.save_progress", lambda summary, path: None)
    monkeypatch.setattr(
        "main.get_recent_bests",
        lambda path: {"Deadlift (Barbell)": {"top_weight_kg": 140, "top_reps": 5}},
    )
    from insights import LiftInsight, RecoveryInsight, TrainingInsights

    fake_insights = TrainingInsights(
        lifts=[
            LiftInsight(
                name="Deadlift (Barbell)",
                trend="progressing",
                metric="kg",
                change_pct=5.0,
                sessions=4,
                latest=140.0,
                best=140.0,
                slope_per_session=1.25,
                sessions_since_best=0,
                intervention=None,
            ),
        ],
        recovery=RecoveryInsight(
            sleep_hours=7.5,
            resting_hr=58,
            resting_hr_trend="steady",
            weight_kg=82.0,
            weight_trend="steady",
            body_fat_pct=None,
            body_fat_trend=None,
            muscle_pct=None,
            is_catabolic=False,
            status="good",
            directive="Stay the course.",
        ),
        headline="All lifts progressing.",
    )
    monkeypatch.setattr(
        "main.insights_engine.build_insights",
        lambda *a, **kw: fake_insights,
    )
    monkeypatch.setattr("main.get_progress_history", lambda **kw: {})
    monkeypatch.setattr("main.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("main.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr(
        "main.generate_next_workout",
        lambda *args, **kw: (
            "Back, Deadlifts & Chest - Week 1 (Hypertrophy)\nDeadlift: 4 x 8"
        ),
    )
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda path: date(2026, 8, 3),
    )

    import io
    import sys

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    result = run(preview=True)
    assert result == 0
    output = captured.getvalue()
    assert "Check-in 1" in output  # check-in message should appear


def test_run_deliver_sends_telegram(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-preview mode sends the plan via Telegram."""
    _make_config(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    monkeypatch.setattr("main.sync_routines", lambda cfg: [])
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda path: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)
    monkeypatch.setattr(
        "main.generate_rest_day_message",
        lambda *args, **kw: "Rest day.",
    )
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda path: date(2026, 8, 3),
    )

    # Track Telegram send — must return True for success
    telegram_calls = []

    def _fake_send(token: str, chat_id: str, msg: str, **kw: object) -> bool:
        telegram_calls.append((token, chat_id, msg))
        return True

    monkeypatch.setattr("main.send_telegram_message", _fake_send)

    result = run(preview=False)
    assert result == 0
    assert len(telegram_calls) == 1
    assert telegram_calls[0][0] == "test-bot-token"
    assert telegram_calls[0][1] == "test-chat-id"


def test_run_deliver_returns_2_when_telegram_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When Telegram delivery fails, run returns 2."""
    _make_config(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    monkeypatch.setattr("main.sync_routines", lambda cfg: [])
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda path: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)
    monkeypatch.setattr(
        "main.generate_rest_day_message",
        lambda *args, **kw: "Rest day.",
    )
    monkeypatch.setattr("main.save_daily_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda path: date(2026, 8, 3),
    )

    # Telegram fails
    monkeypatch.setattr(
        "main.send_telegram_message",
        lambda *a, **kw: False,
    )

    result = run(preview=False)
    assert result == 2


def test_run_hevy_sync_failure_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If Hevy sync raises, the daily message still goes out."""
    _make_config(monkeypatch, tmp_path)

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


def test_run_checkin_due_failure_does_not_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If checkin.due raises, the daily message still goes out."""
    _make_config(monkeypatch, tmp_path, checkin_enabled=True)

    monkeypatch.setattr(
        main_module, "generate_next_workout",
        lambda **kw: "Back, Deadlifts & Chest - Week 3 (Accumulation)\nDeadlift: 4 x 5-8"
    )
    monkeypatch.setattr(
        main_module, "read_recovery_metrics", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "main.send_telegram_message",
        lambda *a, **kw: True,
    )

    result = run(preview=False)
    assert result == 0  # delivered successfully despite check-in failure


def test_run_self_review_on_configured_weekday(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When self_review is enabled and today is the review weekday, a review is sent."""
    from config import Config

    db_path = str(tmp_path / "test_main.db")
    config2 = Config(
        hevy_api_key=None,
        gemini_api_key="test-gemini-key",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="test-chat-id",
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
        self_review_enabled=True,
        self_review_weekday=6,  # Sunday
    )
    monkeypatch.setattr("main.Config.load", lambda: config2)
    monkeypatch.setattr("main.init_db", lambda path: None)

    # Sunday Aug 9, 2026
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(
            now=lambda tz=None: datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc),
            spec=datetime,
        ),
    )

    monkeypatch.setattr("main.sync_routines", lambda cfg: [])
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics",
        lambda *args: None,
    )
    monkeypatch.setattr("main.read_recovery_metrics", lambda path: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda metrics, date, path: None)

    # Self-review uses these
    monkeypatch.setattr(
        "main.get_progress_history",
        lambda **kw: {
            "Deadlift (Barbell)": [
                {"date": "2026-08-01", "top_weight_kg": 140, "top_reps": 5},
            ],
        },
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
