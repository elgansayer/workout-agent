"""Tests for scheduler.py: unified scheduling, time helpers, and job dispatch."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import scheduler

# ---------------------------------------------------------------------------
# _parse_run_at
# ---------------------------------------------------------------------------


def test_parse_run_at_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_AT", raising=False)
    assert scheduler._parse_run_at() == ["07:00"]


def test_parse_run_at_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_AT", "05:30")
    assert scheduler._parse_run_at() == ["05:30"]


def test_parse_run_at_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_AT", "00:00,05:00")
    assert scheduler._parse_run_at() == ["00:00", "05:00"]


def test_parse_run_at_space_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_AT", "00:00  05:00")
    assert scheduler._parse_run_at() == ["00:00", "05:00"]


def test_parse_run_at_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUN_AT", "23:00,01:00")
    assert scheduler._parse_run_at() == ["01:00", "23:00"]


# ---------------------------------------------------------------------------
# _is_due
# ---------------------------------------------------------------------------


def test_is_due_true(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = datetime(2026, 8, 5, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now_in_tz", lambda tz: fake_now)
    assert scheduler._is_due("UTC", ["07:00"]) is True


def test_is_due_false_wrong_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = datetime(2026, 8, 5, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now_in_tz", lambda tz: fake_now)
    assert scheduler._is_due("UTC", ["07:00"]) is False


def test_is_due_false_wrong_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now_in_tz", lambda tz: fake_now)
    assert scheduler._is_due("UTC", ["07:00"]) is False


def test_is_due_multiple_times(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_now = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now_in_tz", lambda tz: fake_now)
    assert scheduler._is_due("UTC", ["00:00", "05:00"]) is True


# ---------------------------------------------------------------------------
# Job dispatch helpers
# ---------------------------------------------------------------------------


def test_run_coaching_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert scheduler._run_coaching("user-1") is True
    assert mock_run.call_count == 1


def test_run_coaching_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(side_effect=subprocess.CalledProcessError(1, "cmd"))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert scheduler._run_coaching("user-1") is False


def test_run_insight_daily_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert scheduler._run_insight_job("--daily") is True


def test_run_insight_weekly_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(side_effect=subprocess.CalledProcessError(1, "cmd"))
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert scheduler._run_insight_job("--weekly") is False


# ---------------------------------------------------------------------------
# run_scheduler main loop (smoke test via one iteration)
# ---------------------------------------------------------------------------


def test_run_scheduler_bootstrap_calls_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    coaching_calls: list[str] = []
    insight_calls: list[str] = []

    def _mock_coaching(uid: str) -> bool:
        coaching_calls.append(uid)
        return True

    def _mock_insight(flag: str) -> bool:
        insight_calls.append(flag)
        return True

    monkeypatch.setattr(scheduler, "_run_coaching", _mock_coaching)
    monkeypatch.setattr(scheduler, "_run_insight_job", _mock_insight)
    monkeypatch.setattr(scheduler, "_run_dead_code_sweep", lambda: True)
    monkeypatch.setattr(scheduler, "_run_commit_hygiene", lambda: True)
    monkeypatch.setattr(scheduler, "_run_connector_health", lambda: True)

    iteration = [0]

    def _fake_sleep(secs: float) -> None:
        iteration[0] += 1
        if iteration[0] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(scheduler.time, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "get_all_users", lambda db_path=None: [])

    try:
        scheduler.run_scheduler()
    except SystemExit:
        pass

    assert "--daily" in insight_calls
    assert "--weekly" in insight_calls
    assert len(coaching_calls) == 0


def test_run_scheduler_dispatches_due_users(monkeypatch: pytest.MonkeyPatch) -> None:
    coaching_calls: list[str] = []
    insight_calls: list[str] = []

    def _mock_coaching(uid: str) -> bool:
        coaching_calls.append(uid)
        return True

    def _mock_insight(flag: str) -> bool:
        insight_calls.append(flag)
        return True

    monkeypatch.setattr(scheduler, "_run_coaching", _mock_coaching)
    monkeypatch.setattr(scheduler, "_run_insight_job", _mock_insight)
    monkeypatch.setattr(scheduler, "_run_dead_code_sweep", lambda: True)
    monkeypatch.setattr(scheduler, "_run_commit_hygiene", lambda: True)
    monkeypatch.setattr(scheduler, "_run_connector_health", lambda: True)
    monkeypatch.setattr(scheduler, "_is_due", lambda tz, rt: True)

    users = [
        {"id": "user-1", "timezone": "Europe/London"},
        {"id": "user-2", "timezone": "America/New_York"},
    ]

    iteration = [0]

    def _fake_sleep(secs: float) -> None:
        iteration[0] += 1
        if iteration[0] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(scheduler.time, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "get_all_users", lambda db_path=None: users)

    try:
        scheduler.run_scheduler()
    except SystemExit:
        pass

    assert "user-1" in coaching_calls
    assert "user-2" in coaching_calls
    assert coaching_calls.count("user-1") >= 2
    assert coaching_calls.count("user-2") >= 2


def test_run_scheduler_user_failure_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def _failing_coaching(uid: str) -> bool:
        if uid == "user-fail":
            raise RuntimeError("Simulated user failure")
        return True

    monkeypatch.setattr(scheduler, "_run_coaching", _failing_coaching)
    monkeypatch.setattr(scheduler, "_run_insight_job", lambda flag: True)
    monkeypatch.setattr(scheduler, "_run_dead_code_sweep", lambda: True)
    monkeypatch.setattr(scheduler, "_run_commit_hygiene", lambda: True)
    monkeypatch.setattr(scheduler, "_run_connector_health", lambda: True)
    monkeypatch.setattr(scheduler, "_is_due", lambda tz, rt: True)

    users = [
        {"id": "user-fail", "timezone": "UTC"},
        {"id": "user-ok", "timezone": "UTC"},
    ]

    iteration = [0]

    def _fake_sleep(secs: float) -> None:
        iteration[0] += 1
        if iteration[0] >= 2:
            raise SystemExit(0)

    monkeypatch.setattr(scheduler.time, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "get_all_users", lambda db_path=None: users)

    try:
        scheduler.run_scheduler()
    except SystemExit:
        pass


# ---------------------------------------------------------------------------
# _now_in_tz
# ---------------------------------------------------------------------------


def test_now_in_tz_valid() -> None:
    dt = scheduler._now_in_tz("UTC")
    assert dt.tzinfo is not None


def test_now_in_tz_invalid_falls_back_to_utc() -> None:
    dt = scheduler._now_in_tz("Not/A_Valid_Zone")
    assert dt.tzinfo is not None
    assert str(dt.tzinfo) == "UTC"


def _seed_users(tmp_path: Path, *names: str) -> tuple[str, list[str]]:
    """Create a real database with synthetic users the scheduler can re-authorize."""
    from database import get_or_create_user, init_db

    db = str(tmp_path / "scheduler.db")
    init_db(db)
    ids = [
        get_or_create_user(f"{name}@example.com", name.title(), db)["id"]
        for name in names
    ]
    return db, ids


def test_active_scheduler_runs_coaching_for_each_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, (user_a, user_b) = _seed_users(tmp_path, "alpha", "bravo")
    calls: list[str | None] = []
    config = MagicMock(database_path=db)
    monkeypatch.setattr(
        scheduler,
        "get_all_users",
        lambda _db: [{"id": user_a}, {"id": user_b}],
    )
    monkeypatch.setattr(
        "main.run",
        lambda *, preview, user_id=None: calls.append(user_id) or 0,
    )

    assert scheduler.run_coaching(config) == 0
    assert calls == [user_a, user_b]


def test_active_scheduler_scopes_each_insight_job_to_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, (user_a, user_b) = _seed_users(tmp_path, "alpha", "bravo")
    daily_calls: list[str] = []
    weekly_calls: list[str] = []
    config = MagicMock(database_path=db)
    monkeypatch.setattr(
        scheduler,
        "get_all_users",
        lambda _db: [{"id": user_a}, {"id": user_b}],
    )
    monkeypatch.setattr(
        "insight_cron.generate_daily_header",
        lambda _config, *, user_id=None: daily_calls.append(user_id),
    )
    monkeypatch.setattr(
        "insight_cron.generate_weekly_correlations",
        lambda _config, *, user_id=None: weekly_calls.append(user_id),
    )

    scheduler.run_daily_insight(config)
    scheduler.run_weekly_correlations(config)

    assert daily_calls == [user_a, user_b]
    assert weekly_calls == [user_a, user_b]


# ---------------------------------------------------------------------------
# Owner isolation: re-authorization, denial and failure containment
# ---------------------------------------------------------------------------


def test_scheduler_reauthorizes_each_owner_when_the_job_executes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A user listed for a run but removed before it executes is denied."""
    db, (user_a, user_b) = _seed_users(tmp_path, "alpha", "bravo")
    config = MagicMock(database_path=db)
    calls: list[str | None] = []

    def _run(*, preview: bool, user_id: str | None = None) -> int:
        calls.append(user_id)
        if user_id == user_a:
            # The next owner's account is deleted while this job is running.
            import sqlite3

            with sqlite3.connect(db) as conn:
                conn.execute("DELETE FROM users WHERE id = ?", (user_b,))
        return 0

    monkeypatch.setattr(
        scheduler, "get_all_users", lambda _db: [{"id": user_a}, {"id": user_b}]
    )
    monkeypatch.setattr("main.run", _run)

    assert scheduler.run_coaching(config) == 1
    assert calls == [user_a]


def test_scheduler_denies_a_forged_owner_without_running_the_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, (user_a,) = _seed_users(tmp_path, "alpha")
    config = MagicMock(database_path=db)
    calls: list[str | None] = []
    monkeypatch.setattr(
        scheduler,
        "get_all_users",
        lambda _db: [{"id": "forged-owner"}, {"id": user_a}],
    )
    monkeypatch.setattr(
        "main.run",
        lambda *, preview, user_id=None: calls.append(user_id) or 0,
    )

    assert scheduler.run_coaching(config) == 1
    assert calls == [user_a]


def test_scheduler_skips_user_rows_without_a_usable_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, (user_a,) = _seed_users(tmp_path, "alpha")
    config = MagicMock(database_path=db)
    calls: list[str | None] = []
    monkeypatch.setattr(
        scheduler,
        "get_all_users",
        lambda _db: [{"id": None}, {"id": ""}, {}, {"id": user_a}],
    )
    monkeypatch.setattr(
        "main.run",
        lambda *, preview, user_id=None: calls.append(user_id) or 0,
    )

    assert scheduler.run_coaching(config) == 1
    assert calls == [user_a]


def test_scheduler_failure_for_one_owner_does_not_stop_the_next(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db, (user_a, user_b) = _seed_users(tmp_path, "alpha", "bravo")
    config = MagicMock(database_path=db)
    calls: list[str | None] = []

    def _run(*, preview: bool, user_id: str | None = None) -> int:
        calls.append(user_id)
        if user_id == user_a:
            raise RuntimeError("synthetic provider outage")
        return 0

    monkeypatch.setattr(
        scheduler, "get_all_users", lambda _db: [{"id": user_a}, {"id": user_b}]
    )
    monkeypatch.setattr("main.run", _run)

    assert scheduler.run_coaching(config) == 1
    assert calls == [user_a, user_b]
