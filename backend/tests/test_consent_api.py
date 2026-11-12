"""HTTP contract for purpose-specific consent choices."""

from __future__ import annotations

import importlib
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from database import get_or_create_user, init_db
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "consent-api-test-secret-that-is-long-enough"


def _cookie(webapp_app: Any, user_id: str) -> tuple[str, str]:
    return (
        webapp_app.SESSION_SECURITY.cookie.name,
        signed_session_cookie(
            _SECRET,
            user="person@example.test",
            user_id=user_id,
            session_id=webapp_app.SESSION_STORE.create(user_id),
        ),
    )


@pytest.fixture()
def app_module(tmp_path: Any, monkeypatch: Any) -> Any:
    db = str(tmp_path / "consent-api.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "consent-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "consent-client-secret")
    import webapp.app as module

    return importlib.reload(module)


def _client(app_module: Any, user_id: str) -> TestClient:
    client = TestClient(app_module.app)
    name, value = _cookie(app_module, user_id)
    client.cookies.set(name, value)
    return client


def test_consent_choices_are_tenant_scoped_and_revocable(app_module: Any) -> None:
    alice = get_or_create_user("alice@example.test", "Alice", app_module.DB_PATH)["id"]
    bob = get_or_create_user("bob@example.test", "Bob", app_module.DB_PATH)["id"]
    alice_client = _client(app_module, alice)
    bob_client = _client(app_module, bob)

    initial = alice_client.get("/api/consent")
    assert initial.status_code == 200
    purposes = {item["purpose"]: item for item in initial.json()["purposes"]}
    assert purposes["health_data"]["granted"] is False

    granted = alice_client.post(
        "/api/consent/grant", json={"purpose": "health_data"}
    )
    assert granted.status_code == 200
    assert granted.json()["decision"]["policy_version"]
    health = next(
        item for item in granted.json()["purposes"] if item["purpose"] == "health_data"
    )
    assert health["granted"] is True
    assert health["current"]["actor_id"] == alice

    bob_health = next(
        item
        for item in bob_client.get("/api/consent").json()["purposes"]
        if item["purpose"] == "health_data"
    )
    assert bob_health["granted"] is False

    withdrawn = alice_client.post(
        "/api/consent/withdraw",
        json={"purpose": "health_data", "retention": "retain"},
    )
    assert withdrawn.status_code == 200
    health = next(
        item for item in withdrawn.json()["purposes"] if item["purpose"] == "health_data"
    )
    assert health["granted"] is False
    assert withdrawn.json()["withdrawal"]["consent"]["decision"] == "withdrawn"


def test_consent_rejects_unknown_purposes_and_anonymous_writes(app_module: Any) -> None:
    user_id = get_or_create_user(
        "person@example.test", "Person", app_module.DB_PATH
    )["id"]
    client = _client(app_module, user_id)

    assert client.post("/api/consent/grant", json={"purpose": "everything"}).status_code == 422
    assert client.post(
        "/api/consent/grant", json={"purpose": "health_data", "user_id": user_id}
    ).status_code == 422
    assert TestClient(app_module.app).post(
        "/api/consent/grant", json={"purpose": "health_data"}
    ).status_code == 401
