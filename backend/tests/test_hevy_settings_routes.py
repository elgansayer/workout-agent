"""Route tests for the Hevy credential gate in the settings API.

A Hevy credential is verified before it is stored or activated, and the
verified connection metadata is recorded per tenant. No test reaches the real
Hevy API.
"""

from __future__ import annotations

import base64
import importlib
import logging
import sqlite3
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

import database
import hevy_client
from secret_vault import CredentialUnavailableError, SecretVault

_SECRET = "hevy-connection-test-secret-that-is-long-enough"
_CANDIDATE_KEY = "synthetic-hevy-key"
# A deterministic synthetic Fernet key so credential storage is exercised
# encrypted instead of falling back to the plaintext warning mode.
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"0" * 32).decode()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _successful_responses() -> dict[str, Any]:
    return {
        "user/info": _FakeResponse(200, {"id": "acct-1", "username": "lifter42"}),
        "workouts": _FakeResponse(200, {"workouts": [{"id": "w-1"}], "page_count": 2}),
        "routines": _FakeResponse(200, {"routines": [], "page_count": 1}),
        "exercise_templates": _FakeResponse(200, {"exercise_templates": []}),
        "workouts/count": _FakeResponse(200, {"workout_count": 42}),
    }


def _install_hevy(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
    def _get(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        allow_redirects: bool = True,
    ) -> _FakeResponse:
        path = url.split("/v1/", 1)[-1]
        return responses.get(path, _FakeResponse(404, {}))

    monkeypatch.setattr(hevy_client.requests, "get", _get)


@pytest.fixture()
def webapp_client(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    db_file = str(tmp_path / "hevy-settings.db")
    database.init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "hevy-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "hevy-client-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", _ENCRYPTION_KEY)

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
    # The rate limiter is process-wide; keep each test independent.
    app_module._RATE_LIMITS.clear()

    # Authenticate the way production login does: register a server-side
    # session in the app's store and carry its identifier in the signed cookie.
    user_id = database.get_legacy_user_id(db_file)
    session_id = app_module.SESSION_STORE.create(user_id)
    cookie = signed_session_cookie(
        _SECRET,
        user="athlete@example.test",
        user_id=user_id,
        session_id=session_id,
    )

    client = TestClient(app_module.app)
    client.cookies.set("session", cookie)
    client.hevy_db_file = db_file  # type: ignore[attr-defined]
    return client


def _user_id(client: Any) -> str:
    return database.get_legacy_user_id(client.hevy_db_file)


def _store_key(
    user_id: str, provider: str, api_key: str, db_file: str
) -> None:
    """Store a credential directly through the vault (bypassing the routes)."""
    SecretVault(db_file, environ={"ENCRYPTION_KEY": _ENCRYPTION_KEY}).store(
        user_id, provider, api_key=api_key
    )


def _stored_key(user_id: str, provider: str, db_file: str) -> str | None:
    """Return a stored plaintext credential, or ``None`` when unset."""
    try:
        credential = SecretVault(
            db_file, environ={"ENCRYPTION_KEY": _ENCRYPTION_KEY}
        ).resolve(user_id, provider, purpose="credential_test")
    except CredentialUnavailableError:
        return None
    return credential.api_key.reveal() if credential.api_key is not None else None


# ---------------------------------------------------------------------------
# Storing a credential
# ---------------------------------------------------------------------------


def test_unverified_hevy_key_is_rejected_and_never_stored(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(401, {"error": "unauthorized"})
    _install_hevy(monkeypatch, responses)

    response = webapp_client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": "synthetic-bad-key"},
    )

    assert response.status_code == 400
    assert "rejected" in response.json()["detail"].lower()
    assert _CANDIDATE_KEY not in response.text

    assert _stored_key(_user_id(webapp_client), "hevy", webapp_client.hevy_db_file) is None
    assert (
        database.get_hevy_connection(_user_id(webapp_client), webapp_client.hevy_db_file)
        is None
    )


def test_hevy_key_without_workout_access_is_not_activated(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["workouts"] = _FakeResponse(403, {"error": "forbidden"})
    _install_hevy(monkeypatch, responses)

    response = webapp_client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": _CANDIDATE_KEY},
    )

    assert response.status_code == 400
    assert _stored_key(_user_id(webapp_client), "hevy", webapp_client.hevy_db_file) is None


def test_redirect_answer_does_not_activate_a_hevy_key(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redirect answer is not an API response: following it would resend the
    # credential to another host and the account would never be verified.
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(
        302,
        {"username": "lifter42"},
        headers={"location": "https://example.invalid/collect"},
    )
    _install_hevy(monkeypatch, responses)

    response = webapp_client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": _CANDIDATE_KEY},
    )

    assert response.status_code == 502
    assert _CANDIDATE_KEY not in response.text
    assert _stored_key(_user_id(webapp_client), "hevy", webapp_client.hevy_db_file) is None


def test_unusable_hevy_key_is_rejected_without_a_server_error(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A key that cannot be placed in an HTTP header must not crash the
    # request and must never reach the log.
    _install_hevy(monkeypatch, _successful_responses())

    with caplog.at_level(logging.DEBUG):
        response = webapp_client.post(
            "/api/settings/key",
            json={"provider": "hevy", "api_key": "synthetic-secret-key\nwith-newline"},
        )

    assert response.status_code == 400
    assert "synthetic-secret-key" not in response.text
    assert "synthetic-secret-key" not in caplog.text
    assert _stored_key(_user_id(webapp_client), "hevy", webapp_client.hevy_db_file) is None


def test_verified_hevy_key_is_stored_with_its_connection_metadata(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())

    response = webapp_client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": _CANDIDATE_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    connection = body["connection"]
    assert connection["state"] == "connected"
    assert connection["account_username"] == "lifter42"
    assert connection["api_version"] == "v1"
    assert connection["capabilities"]["workouts"] is True
    assert connection["tested_at"]
    # The plaintext credential is never echoed to the browser.
    assert _CANDIDATE_KEY not in response.text

    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    assert _stored_key(user_id, "hevy", db_file) == _CANDIDATE_KEY

    with sqlite3.connect(db_file, timeout=10) as conn:
        raw = conn.execute(
            "SELECT api_key FROM user_api_keys WHERE user_id = ? AND provider = 'hevy'",
            (user_id,),
        ).fetchone()
    assert raw is not None
    assert raw[0] != _CANDIDATE_KEY

    record = database.get_hevy_connection(user_id, db_file)
    assert record is not None
    assert record["state"] == "connected"
    assert record["account_username"] == "lifter42"
    assert record["api_version"] == "v1"
    assert record["capabilities"]["workouts"] is True
    assert record["tested_at"]


def test_ai_provider_keys_are_not_affected_by_the_hevy_gate(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())

    response = webapp_client.post(
        "/api/settings/key",
        json={"provider": "gemini", "api_key": "synthetic-gemini-key"},
    )

    assert response.status_code == 200
    assert (
        _stored_key(_user_id(webapp_client), "gemini", webapp_client.hevy_db_file)
        == "synthetic-gemini-key"
    )


# ---------------------------------------------------------------------------
# Verifying a credential
# ---------------------------------------------------------------------------


def test_verify_hevy_checks_the_stored_key(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    _store_key(user_id, "hevy", _CANDIDATE_KEY, db_file)

    response = webapp_client.post("/api/settings/verify-hevy", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["account_username"] == "lifter42"
    assert body["api_version"] == "v1"
    assert body["capabilities"]["workouts"] is True
    assert body["workout_count"] == 42
    assert body["tested_at"]
    assert _CANDIDATE_KEY not in response.text

    record = database.get_hevy_connection(user_id, db_file)
    assert record is not None
    assert record["state"] == "connected"
    assert record["account_username"] == "lifter42"


def test_verify_hevy_without_a_stored_key_returns_400(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())

    response = webapp_client.post("/api/settings/verify-hevy", json={})

    assert response.status_code == 400
    assert "no hevy api key" in response.json()["detail"].lower()


def test_verify_hevy_rejects_an_invalid_candidate_without_touching_the_stored_key(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    _store_key(user_id, "hevy", _CANDIDATE_KEY, db_file)

    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(401, {"error": "unauthorized"})
    _install_hevy(monkeypatch, responses)

    response = webapp_client.post(
        "/api/settings/verify-hevy",
        json={"api_key": "synthetic-bad-key"},
    )

    assert response.status_code == 400
    assert "synthetic-bad-key" not in response.text
    assert _stored_key(user_id, "hevy", db_file) == _CANDIDATE_KEY


def test_verify_hevy_marks_a_rejected_stored_key_as_invalid(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    _store_key(user_id, "hevy", _CANDIDATE_KEY, db_file)
    database.save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        db_path=db_file,
    )

    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(401, {"error": "unauthorized"})
    _install_hevy(monkeypatch, responses)

    response = webapp_client.post("/api/settings/verify-hevy", json={})

    assert response.status_code == 400
    record = database.get_hevy_connection(user_id, db_file)
    assert record is not None
    assert record["state"] == "invalid"


# ---------------------------------------------------------------------------
# Reporting and cleanup
# ---------------------------------------------------------------------------


def test_settings_reports_the_verified_connection_without_the_credential(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    _store_key(user_id, "hevy", _CANDIDATE_KEY, db_file)
    database.save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        db_path=db_file,
    )

    response = webapp_client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["hevy_connection"]["state"] == "connected"
    assert body["hevy_connection"]["account_username"] == "lifter42"
    assert body["hevy_connection"]["api_version"] == "v1"
    assert body["hevy_connection"]["tested_at"] == "2026-11-05T10:00:00+00:00"
    assert body["hevy_connection"]["capabilities"]["workouts"] is True
    assert _CANDIDATE_KEY not in response.text


def test_removing_the_hevy_key_clears_the_connection_metadata(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    user_id = _user_id(webapp_client)
    _store_key(user_id, "hevy", _CANDIDATE_KEY, db_file)
    database.save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        db_path=db_file,
    )

    response = webapp_client.post("/api/settings/key/delete", json={"provider": "hevy"})

    assert response.status_code == 200
    assert _stored_key(user_id, "hevy", db_file) is None
    assert database.get_hevy_connection(user_id, db_file) is None


def test_connection_metadata_is_not_visible_to_another_tenant(
    webapp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hevy(monkeypatch, _successful_responses())
    db_file = webapp_client.hevy_db_file
    owner_id = _user_id(webapp_client)
    _store_key(owner_id, "hevy", _CANDIDATE_KEY, db_file)
    database.save_hevy_connection(
        owner_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        db_path=db_file,
    )

    other = database.get_or_create_user(
        "other@example.test",
        display_name="Other Athlete",
        db_path=db_file,
    )
    assert database.get_hevy_connection(other["id"], db_file) is None
    assert _stored_key(other["id"], "hevy", db_file) is None
