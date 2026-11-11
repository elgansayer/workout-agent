"""API coverage for account session visibility and revocation."""

from __future__ import annotations

import base64
import importlib
import json
from typing import Any

import pytest
from database import get_or_create_user, init_db
from itsdangerous import TimestampSigner
from session_registry import create_session, validate_session
from starlette.testclient import TestClient

_SECRET = "sessions-api-test-secret-that-is-long-enough"


def _cookie(user_id: str, session_id: str) -> str:
    payload = {
        "user": f"{user_id}@example.test",
        "user_id": user_id,
        "sid": session_id,
    }
    encoded = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner(_SECRET).sign(encoded).decode()


def _client_for(app: Any, user_id: str, session_id: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", _cookie(user_id, session_id))
    return client


@pytest.fixture()
def app_with_users(tmp_path: Any, monkeypatch: Any) -> tuple[Any, str, str, str]:
    db_path = str(tmp_path / "sessions-api.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "sessions-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "sessions-client-secret")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    first = str(
        get_or_create_user("first@example.test", db_path=db_path)["id"]
    )
    second = str(
        get_or_create_user("second@example.test", db_path=db_path)["id"]
    )

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module, db_path, first, second


def test_account_lists_only_own_active_sessions(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, second = app_with_users
    current = create_session(
        first,
        user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36",
        client_ip="203.0.113.9",
        country_code="GB",
        db_path=db_path,
    )
    sibling = create_session(
        first,
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1",
        client_ip="198.51.100.7",
        db_path=db_path,
    )
    foreign = create_session(second, db_path=db_path)

    client = _client_for(app_module.app, first, current["sid"])
    response = client.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()["sessions"]
    ids = {item["id"] for item in sessions}
    assert ids == {current["id"], sibling["id"]}
    assert foreign["id"] not in ids

    current_row = next(item for item in sessions if item["id"] == current["id"])
    assert current_row["current"] is True
    assert current_row["device"] == "Chrome on Linux"
    assert "GB" in current_row["location"]
    assert "203.0.113.9" not in response.text
    assert "session_hash" not in response.text
    assert "sid" not in response.text
    for field in ("created_at", "last_used_at", "device", "location"):
        assert current_row[field]


def test_revoking_another_session_rotates_current_credential(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, second = app_with_users
    current = create_session(first, db_path=db_path)
    target = create_session(first, db_path=db_path)
    foreign = create_session(second, db_path=db_path)

    old_cookie = _cookie(first, current["sid"])
    client = _client_for(app_module.app, first, current["sid"])

    forbidden = client.delete(f"/api/sessions/{foreign['id']}")
    assert forbidden.status_code == 404
    assert validate_session(second, foreign["sid"], db_path) is True

    response = client.delete(f"/api/sessions/{target['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["logged_out"] is False
    assert body["current_session_id"] != current["id"]
    assert validate_session(first, target["sid"], db_path) is False
    assert validate_session(first, current["sid"], db_path) is False

    stale_client = TestClient(app_module.app)
    stale_client.cookies.set("session", old_cookie)
    assert stale_client.get("/api/me").status_code == 401

    refreshed = client.get("/api/sessions")
    assert refreshed.status_code == 200
    assert all(item["id"] != target["id"] for item in refreshed.json()["sessions"])
    assert any(item["current"] is True for item in refreshed.json()["sessions"])


def test_revoke_other_sessions_keeps_current_and_revokes_rest(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, _second = app_with_users
    current = create_session(first, db_path=db_path)
    second_session = create_session(first, db_path=db_path)
    third_session = create_session(first, db_path=db_path)

    client = _client_for(app_module.app, first, current["sid"])
    response = client.post("/api/sessions/revoke-others")

    assert response.status_code == 200
    assert response.json()["revoked"] == 2
    assert validate_session(first, second_session["sid"], db_path) is False
    assert validate_session(first, third_session["sid"], db_path) is False
    assert validate_session(first, current["sid"], db_path) is False

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["current"] is True
    assert sessions[0]["id"] == response.json()["current_session_id"]


def test_revoking_own_session_logs_out_immediately(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, _second = app_with_users
    current = create_session(first, db_path=db_path)
    client = _client_for(app_module.app, first, current["sid"])

    response = client.delete(f"/api/sessions/{current['id']}")

    assert response.status_code == 200
    assert response.json()["logged_out"] is True
    assert validate_session(first, current["sid"], db_path) is False
    assert client.get("/api/me").status_code == 401


def test_revocation_from_another_session_takes_effect_promptly(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, _second = app_with_users
    current = create_session(first, db_path=db_path)
    other = create_session(first, db_path=db_path)

    other_client = _client_for(app_module.app, first, other["sid"])
    assert other_client.get("/api/me").status_code == 200

    revoker = _client_for(app_module.app, first, current["sid"])
    assert revoker.delete(f"/api/sessions/{other['id']}").status_code == 200

    assert other_client.get("/api/me").status_code == 401


def test_legacy_cookie_without_registered_session_is_rejected(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, _db_path, first, _second = app_with_users
    payload = {"user": "first@example.test", "user_id": first}
    encoded = base64.b64encode(json.dumps(payload).encode())
    legacy_cookie = TimestampSigner(_SECRET).sign(encoded).decode()

    client = TestClient(app_module.app)
    client.cookies.set("session", legacy_cookie)

    # The hardened registry (issue #752) refuses every sid-less cookie rather
    # than adopting one that cannot be individually revoked.
    assert client.get("/api/sessions").status_code == 401


def test_legacy_cookie_is_rejected_for_every_account(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, _db_path, first, second = app_with_users
    for user_id, email in ((first, "first@example.test"), (second, "second@example.test")):
        payload = {"user": email, "user_id": user_id}
        encoded = base64.b64encode(json.dumps(payload).encode())
        legacy_cookie = TimestampSigner(_SECRET).sign(encoded).decode()

        client = TestClient(app_module.app)
        client.cookies.set("session", legacy_cookie)
        assert client.get("/api/sessions").status_code == 401


def test_legacy_cookie_is_rejected_after_revoking_other_sessions(
    app_with_users: tuple[Any, str, str, str],
) -> None:
    app_module, db_path, first, _second = app_with_users
    current = create_session(first, db_path=db_path)
    revoker = _client_for(app_module.app, first, current["sid"])
    assert revoker.post("/api/sessions/revoke-others").status_code == 200

    payload = {"user": "first@example.test", "user_id": first}
    encoded = base64.b64encode(json.dumps(payload).encode())
    legacy = TestClient(app_module.app)
    legacy.cookies.set("session", TimestampSigner(_SECRET).sign(encoded).decode())
    assert legacy.get("/api/sessions").status_code == 401
