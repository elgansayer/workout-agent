"""The scheduled agent feeds the same connector ledger the web app reads."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import database
import integration_status as svc
import main
import pytest
from hevy_parser import WorkoutSummary
from integration_state import IntegrationState
from test_main import _make_config


def _ledger(tmp_path: Path) -> tuple[str, str]:
    db = str(tmp_path / "agent.db")
    database.init_db(db)
    user = database.get_or_create_user("agent@example.test", "Agent", db_path=db)["id"]
    return db, user


def _config(db: str) -> Any:
    return MagicMock(database_path=db, hevy_api_key="synthetic-key")


def test_successful_read_is_recorded_as_a_sync(tmp_path: Path) -> None:
    db, user = _ledger(tmp_path)
    main._record_hevy_sync(_config(db), user, {"workouts": [{"id": "w1"}]})
    (run,) = database.get_connector_runs(user, "hevy", db)
    assert (run["kind"], run["status"], run["fetched"], run["written"]) == ("sync", "succeeded", 1, 1)


def test_empty_but_successful_read_is_still_a_success(tmp_path: Path) -> None:
    db, user = _ledger(tmp_path)
    main._record_hevy_sync(_config(db), user, {"workouts": []})
    (run,) = database.get_connector_runs(user, "hevy", db)
    assert (run["status"], run["fetched"]) == ("succeeded", 0)


def test_failed_read_is_recorded_as_a_retryable_provider_error(tmp_path: Path) -> None:
    db, user = _ledger(tmp_path)
    main._record_hevy_sync(_config(db), user, None)
    (run,) = database.get_connector_runs(user, "hevy", db)
    assert (run["status"], run["error_code"], run["retryable"]) == ("failed", "provider_error", True)


def test_recording_can_never_break_the_daily_run(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = MagicMock(database_path=str(tmp_path / "missing" / "no.db"), hevy_api_key="k")
    config.database_path = str(tmp_path)  # a directory is not a database
    main._record_hevy_sync(config, "user", {"workouts": []})
    assert "Could not record the Hevy sync outcome" in caplog.text
    assert "synthetic" not in caplog.text


def test_agent_feeds_the_state_model_across_days(tmp_path: Path) -> None:
    db, user = _ledger(tmp_path)
    database.save_user_api_key(user, "hevy", "synthetic-key", db_path=db)
    with __import__("sqlite3").connect(db) as conn:
        conn.execute("UPDATE user_api_keys SET updated_at = '2020-01-01T00:00:00+00:00'")
    ctx = svc.StatusContext(db_path=db)
    config = _config(db)
    main._record_hevy_sync(config, user, {"workouts": [{"id": "w1"}]})
    assert svc.component_status(user, svc.HEVY, ctx).state is IntegrationState.READY
    # Two failed mornings in a row: degraded, and an alert exists exactly once.
    main._record_hevy_sync(config, user, None)
    assert svc.component_status(user, svc.HEVY, ctx).state is IntegrationState.READY
    main._record_hevy_sync(config, user, None)
    assert svc.component_status(user, svc.HEVY, ctx).state is IntegrationState.DEGRADED
    main._record_hevy_sync(config, user, None)
    assert len(database.get_notifications(user, db_path=db)) == 1


def _patch_training_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fetched: Any) -> list[tuple[Any, ...]]:
    _make_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(now=lambda tz=None: datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc), spec=datetime),
    )
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr("main._record_hevy_sync", lambda *args: recorded.append(args))
    monkeypatch.setattr("main.sync_routines", lambda cfg, **kw: [])
    monkeypatch.setattr("main.google_health_client.sync_body_metrics", lambda *args: None)
    monkeypatch.setattr("main.read_recovery_metrics", lambda *a, **k: None)
    monkeypatch.setattr("main.body_metrics_from_recovery", lambda rec: rec)
    monkeypatch.setattr("main.save_body_metrics", lambda *a, **k: None)
    monkeypatch.setattr("main.fetch_latest_workout", lambda key: fetched)
    monkeypatch.setattr(
        "main.parse_workout",
        lambda raw, targets: WorkoutSummary("Day 1", "2026-08-02", 3600, 2500, []),
    )
    monkeypatch.setattr("main.save_workout", lambda *a, **k: None)
    monkeypatch.setattr("main.save_progress", lambda *a, **k: None)
    monkeypatch.setattr("main.get_recent_bests", lambda *a, **k: {})
    monkeypatch.setattr("main.get_progress_history", lambda **k: {})
    monkeypatch.setattr("main.get_body_metrics", lambda **k: [])
    monkeypatch.setattr("main.get_daily_logs", lambda **k: [])
    monkeypatch.setattr("main.save_daily_log", lambda *a, **k: None)
    monkeypatch.setattr("main.generate_next_workout", lambda *a, **k: "Plan")
    monkeypatch.setattr("main.get_programme_start_date", lambda *a, **k: date(2026, 8, 3))
    monkeypatch.setattr("main.get_push_subscriptions", lambda *a: [])
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "test-vapid")
    from insights import RecoveryInsight, TrainingInsights

    monkeypatch.setattr(
        "main.insights_engine.build_insights",
        lambda *a, **k: TrainingInsights(
            lifts=[],
            recovery=RecoveryInsight(None, None, "steady", None, "steady", None, None, None, False, "good", "ok"),
            headline="h",
        ),
    )
    return recorded


def test_training_day_run_records_the_hevy_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {"workouts": [{"id": "w1"}]}
    recorded = _patch_training_day(monkeypatch, tmp_path, payload)
    main.run(preview=False)
    assert len(recorded) == 1 and recorded[0][1:] == ("test-user-id", payload)


def test_training_day_run_records_a_failed_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = _patch_training_day(monkeypatch, tmp_path, None)
    main.run(preview=False)
    assert len(recorded) == 1 and recorded[0][2] is None


def test_preview_runs_do_not_write_to_the_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    recorded = _patch_training_day(monkeypatch, tmp_path, {"workouts": []})
    assert main.run(preview=True) == 0
    assert recorded == []
