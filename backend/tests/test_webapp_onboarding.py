"""End-to-end coverage for the resumable onboarding API and dashboard action."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from typing import Any

import database
import pytest
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient
from test_webapp_setup_states import _programme_definition

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

_SECRET = "onboarding-test-secret-that-is-long-enough"


@pytest.fixture()
def db_path(tmp_path: Any, monkeypatch: Any) -> str:
    path = str(tmp_path / "onboarding-api.db")
    monkeypatch.setenv(
        "ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )
    database.init_db(path)
    return path


@pytest.fixture()
def users(db_path: str) -> dict[str, str]:
    return {
        "first": database.get_or_create_user(
            "first@example.test", "First athlete", db_path
        )["id"],
        "second": database.get_or_create_user(
            "second@example.test", "Second athlete", db_path
        )["id"],
    }


@pytest.fixture()
def client(db_path: str, users: dict[str, str], monkeypatch: Any) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "onboarding-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "onboarding-client-secret")
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


def _onboarding(client: TestClient, user_id: str) -> dict[str, Any]:
    _sign_in(client, user_id)
    response = client.get("/api/onboarding")
    assert response.status_code == 200
    return response.json()


def _step(payload: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(step for step in payload["steps"] if step["id"] == step_id)


def test_onboarding_requires_an_authenticated_session(client: TestClient) -> None:
    assert client.get("/api/onboarding").status_code == 401
    assert client.post("/api/onboarding/skip", json={"step": "health_data"}).status_code == 401


def test_a_new_account_resumes_at_the_highest_value_required_step(
    client: TestClient, users: dict[str, str]
) -> None:
    payload = _onboarding(client, users["first"])

    assert payload["account_ready"] is False
    assert payload["next_step"]["id"] == "hevy"
    assert [step["id"] for step in payload["steps"]] == [
        "preferences",
        "hevy",
        "health_data",
        "ai_provider",
        "programme",
        "notifications",
    ]
    assert all(step["status"] == "pending" for step in payload["steps"])


def test_skipping_and_resuming_an_optional_step_changes_progress(
    client: TestClient, users: dict[str, str]
) -> None:
    _sign_in(client, users["first"])

    skipped = client.post("/api/onboarding/skip", json={"step": "health_data"})
    assert skipped.status_code == 200
    payload = skipped.json()
    assert _step(payload, "health_data")["status"] == "skipped"
    assert payload["skipped_count"] == 1

    reloaded = _onboarding(client, users["first"])
    assert _step(reloaded, "health_data")["status"] == "skipped"

    resumed = client.post("/api/onboarding/resume", json={"step": "health_data"})
    assert resumed.status_code == 200
    assert _step(resumed.json(), "health_data")["status"] == "pending"
    assert resumed.json()["skipped_count"] == 0


def test_required_steps_cannot_be_skipped(
    client: TestClient, users: dict[str, str]
) -> None:
    _sign_in(client, users["first"])

    response = client.post("/api/onboarding/skip", json={"step": "hevy"})

    assert response.status_code == 400
    assert "required" in response.json()["detail"]
    payload = _onboarding(client, users["first"])
    assert _step(payload, "hevy")["status"] == "pending"


def test_unknown_steps_are_rejected(
    client: TestClient, users: dict[str, str]
) -> None:
    _sign_in(client, users["first"])

    response = client.post("/api/onboarding/skip", json={"step": "not_a_step"})

    assert response.status_code == 400


def test_one_tenants_skip_does_not_change_another(
    client: TestClient, users: dict[str, str]
) -> None:
    _sign_in(client, users["first"])
    assert client.post("/api/onboarding/skip", json={"step": "notifications"}).status_code == 200

    first = _onboarding(client, users["first"])
    second = _onboarding(client, users["second"])

    assert _step(first, "notifications")["status"] == "skipped"
    assert _step(second, "notifications")["status"] == "pending"


def test_dashboard_offers_the_next_onboarding_action_until_ready(
    client: TestClient, users: dict[str, str], db_path: str
) -> None:
    _sign_in(client, users["first"])
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["onboarding"]["account_ready"] is False
    assert dashboard["onboarding"]["next_step"]["id"] == "hevy"
    assert dashboard["onboarding"]["next_step"]["href"] == "/settings?section=connections"

    database.save_user_api_key(
        users["first"], "hevy", "synthetic-hevy-key", db_path=db_path
    )
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["onboarding"]["next_step"]["id"] == "programme"

    today = datetime.now(tz=timezone.utc).date().isoformat()
    database.set_active_programme(
        users["first"],
        "hevy",
        "synthetic-hevy",
        _programme_definition(today),
        db_path=db_path,
    )
    dashboard = client.get("/api/dashboard").json()
    assert dashboard["onboarding"]["account_ready"] is True
