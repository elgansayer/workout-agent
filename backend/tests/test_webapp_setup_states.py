"""End-to-end coverage for the five account setup states.

Every fixture below creates one tenant per state inside a single database, so
each surface is checked against tenant-scoped records only.  The states are the
ones a real account passes through: new, partially configured, ready, stale and
disconnected.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone
from typing import Any

import database
import pytest
from hevy_parser import ExerciseSummary, WorkoutSummary
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

_SECRET = "setup-state-test-secret-that-is-long-enough"

ACCOUNT_STATES = ("new", "partially_configured", "ready", "stale", "disconnected")


def _programme_definition(start_date: str) -> dict[str, Any]:
    """A minimal but valid Hevy-derived programme definition."""

    return {
        "name": "Synthetic hypertrophy",
        "source": "hevy",
        "cycle_weeks": 12,
        "programme_spec": {
            "start_date": start_date,
            "duration_weeks": 12,
            "selected_routine_ids": ["routine-a"],
            "goal": "hypertrophy",
        },
        "blocks": [
            {
                "number": 1,
                "name": "Build",
                "start_week": 1,
                "end_week": 12,
                "weeks": "1–12",
                "focus": "Build volume",
                "rep_emphasis": "8–12 reps",
                "target_rir": 2,
                "volume_multiplier": 1.0,
            }
        ],
        "days": [
            {
                "number": 1,
                "routine_id": "routine-a",
                "focus": "Upper A",
                "muscles": ["chest"],
                "estimated_duration_minutes": 60,
                "exercises": [
                    {
                        "name": "Bench Press",
                        "template_id": "bench",
                        "role": "main",
                        "sets": 3,
                        "rep_range": "8–10",
                        "scheme": "3 × 8–10",
                        "prescriptions": [
                            {"block_number": 1, "sets": 3, "rep_range": "8–10"}
                        ],
                    }
                ],
            }
        ],
        "rules": ["Progressions are deterministic."],
        "analysis": {"confidence": "High"},
        "warnings": [],
    }


def _summary(when: date) -> WorkoutSummary:
    return WorkoutSummary(
        title="Upper A",
        date=when.isoformat(),
        duration_seconds=3600,
        total_volume_kg=2800.0,
        exercises=[ExerciseSummary("Bench Press", 80.0, 8, 4)],
    )


@pytest.fixture()
def db_path(tmp_path: Any) -> str:
    return str(tmp_path / "setup-states.db")


@pytest.fixture()
def tenants(db_path: str, monkeypatch: Any) -> dict[str, str]:
    """Create one tenant per account state and return their user ids."""

    monkeypatch.setenv(
        "ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )
    database.init_db(db_path)
    today = datetime.now(tz=timezone.utc).date()

    ids: dict[str, str] = {}

    new_user = database.get_or_create_user("new@example.test", "New athlete", db_path)
    ids["new"] = new_user["id"]

    partial_user = database.get_or_create_user(
        "partial@example.test", "Partial athlete", db_path
    )
    ids["partially_configured"] = partial_user["id"]
    database.save_user_api_key(
        partial_user["id"], "hevy", "synthetic-hevy-key", db_path=db_path
    )

    for state, offset_days in (("ready", 1), ("stale", 30), ("disconnected", 2)):
        user = database.get_or_create_user(
            f"{state}@example.test", f"{state} athlete", db_path
        )
        ids[state] = user["id"]
        database.save_user_api_key(
            user["id"], "hevy", "synthetic-hevy-key", db_path=db_path
        )
        database.save_user_api_key(
            user["id"], "gemini", "synthetic-ai-key", db_path=db_path
        )
        database.set_active_programme(
            user["id"],
            "hevy",
            "synthetic-hevy",
            _programme_definition((today - timedelta(days=offset_days)).isoformat()),
            db_path=db_path,
        )
        logged_on = today - timedelta(days=offset_days)
        database.save_progress(_summary(logged_on), db_path, user_id=user["id"])
        database.save_daily_log(
            logged_on.isoformat(),
            1,
            "Upper A",
            "high",
            "Synthetic plan",
            "Synthetic lifestyle",
            db_path,
            user_id=user["id"],
        )
        if state == "disconnected":
            database.delete_user_api_key(user["id"], "hevy", db_path=db_path)

    return ids


@pytest.fixture()
def client(
    db_path: str,
    monkeypatch: Any,
    tenants: dict[str, str],
) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "setup-state-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "setup-state-client-secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as test_client:
        yield test_client


def _sign_in(client: TestClient, user_id: str) -> None:
    import webapp.app as app_module

    client.cookies.set(
        "session",
        signed_session_cookie(
            _SECRET,
            user="athlete@example.test",
            user_id=user_id,
            session_id=app_module.SESSION_STORE.create(user_id),
        ),
    )


def _setup(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, path
    return response.json()


def test_new_account_is_asked_to_connect_hevy_first(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["new"])

    dashboard = _setup(client, "/api/dashboard")
    assert dashboard["setup"]["state"] == "new"
    assert dashboard["setup"]["action"]["id"] == "connect_hevy"
    assert dashboard["setup"]["action"]["href"] == "/settings"
    assert dashboard["setup"]["reason"]

    for path in ("/api/progress", "/api/stats", "/api/history", "/api/plan"):
        payload = _setup(client, path)
        assert payload["setup"]["state"] == "new", path
        assert payload["setup"]["action"]["id"] == "connect_hevy", path
        assert payload["setup"]["missing"], path


def test_partially_configured_account_is_asked_to_build_a_programme(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["partially_configured"])

    dashboard = _setup(client, "/api/dashboard")
    assert dashboard["setup"]["state"] == "partially_configured"
    assert dashboard["setup"]["action"]["id"] == "build_programme"
    assert dashboard["setup"]["action"]["href"] == "/programmes"
    assert dashboard["setup_required"] is True

    progress = _setup(client, "/api/progress")
    assert progress["setup"]["state"] == "partially_configured"
    assert progress["setup"]["action"]["id"] == "sync_hevy"


def test_ready_account_reports_no_setup_action(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["ready"])

    for path in ("/api/dashboard", "/api/progress", "/api/stats", "/api/history", "/api/plan"):
        payload = _setup(client, path)
        assert payload["setup"]["state"] == "ready", path
        assert payload["setup"]["action"] is None, path
        assert payload["setup"]["reason"] == "", path

    dashboard = _setup(client, "/api/dashboard")
    assert dashboard["setup_required"] is False
    assert dashboard["rows"]


def test_stale_account_is_told_to_sync_again(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["stale"])

    stats = _setup(client, "/api/stats")
    assert stats["setup"]["state"] == "stale"
    assert stats["setup"]["stale"] is True
    assert stats["setup"]["action"]["id"] == "sync_hevy"
    assert stats["setup"]["age_days"] >= 30


def test_disconnected_account_is_told_to_reconnect_hevy(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["disconnected"])

    for path in ("/api/dashboard", "/api/progress", "/api/stats", "/api/history"):
        payload = _setup(client, path)
        assert payload["setup"]["state"] == "disconnected", path
        assert payload["setup"]["action"]["id"] == "connect_hevy", path
        assert "removed" in payload["setup"]["reason"], path


def test_setup_state_endpoint_covers_every_surface(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["partially_configured"])

    payload = _setup(client, "/api/setup/state")

    assert payload["account"]["state"] == "partially_configured"
    assert payload["account"]["checks"]["hevy_connected"] is True
    assert payload["account"]["checks"]["programme_active"] is False
    for surface in ("dashboard", "plan", "progress", "stats", "history", "coach", "checkins"):
        assert surface in payload["surfaces"]
        assert payload["surfaces"][surface]["action"]["href"] in {
            "/settings",
            "/programmes",
        }


def test_setup_state_never_leaks_another_tenants_records(
    client: TestClient,
    tenants: dict[str, str],
) -> None:
    _sign_in(client, tenants["new"])

    new_state = _setup(client, "/api/setup/state")
    assert new_state["account"]["state"] == "new"

    _sign_in(client, tenants["ready"])
    ready_state = _setup(client, "/api/setup/state")
    assert ready_state["account"]["state"] == "ready"
    assert ready_state["surfaces"]["progress"]["action"] is None


def test_coach_surface_reports_a_missing_provider(
    client: TestClient,
    tenants: dict[str, str],
    db_path: str,
) -> None:
    _sign_in(client, tenants["ready"])

    payload = _setup(client, "/api/setup/state")
    assert payload["surfaces"]["coach"]["state"] == "ready"
    assert payload["surfaces"]["coach"]["action"] is None

    database.delete_user_api_key(tenants["ready"], "gemini", db_path=db_path)

    payload = _setup(client, "/api/setup/state")
    assert payload["surfaces"]["coach"]["state"] == "partially_configured"
    assert payload["surfaces"]["coach"]["action"]["id"] == "configure_ai"
