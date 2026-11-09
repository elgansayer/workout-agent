"""End-to-end OAuth state, PKCE and redirect allowlist tests for the web app."""

from __future__ import annotations

import base64
import importlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import database
from connectors.oauth import code_challenge_for, create_oauth_state
from fastapi.responses import RedirectResponse
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "oauth-flow-test-secret-that-is-long-enough"
_REDIRECT = "http://testserver/google-health/callback"


def _app(
    tmp_path: Any,
    monkeypatch: Any,
    *,
    redirect_uri: str | None = _REDIRECT,
    allowed_redirects: str | None = None,
    public_url: str | None = None,
    web_login: bool = False,
) -> tuple[Any, str]:
    db_path = str(tmp_path / "oauth.db")
    database.init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "gh-client-id")
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "gh-client-secret")

    if redirect_uri is not None:
        monkeypatch.setenv("GOOGLE_HEALTH_REDIRECT_URI", redirect_uri)
    else:
        monkeypatch.delenv("GOOGLE_HEALTH_REDIRECT_URI", raising=False)
    if allowed_redirects is not None:
        monkeypatch.setenv("GOOGLE_HEALTH_ALLOWED_REDIRECT_URIS", allowed_redirects)
    else:
        monkeypatch.delenv("GOOGLE_HEALTH_ALLOWED_REDIRECT_URIS", raising=False)
    if public_url is not None:
        monkeypatch.setenv("WEB_PUBLIC_URL", public_url)
    else:
        monkeypatch.delenv("WEB_PUBLIC_URL", raising=False)

    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "login-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "login-client-secret")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    if web_login:
        monkeypatch.delenv("WEB_OAUTH_ALLOWED_REDIRECT_URIS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    return app_module, db_path


def _session_cookie(user_id: str, extra: dict[str, Any] | None = None) -> str:
    session: dict[str, Any] = {
        "user": "athlete@example.test",
        "user_id": user_id,
    }
    session.update(extra or {})
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(_SECRET).sign(payload).decode()


def _client(app_module: Any, db_path: str) -> TestClient:
    client = TestClient(app_module.app)
    client.cookies.set(
        "session",
        _session_cookie(database.get_legacy_user_id(db_path)),
    )
    return client


def _connect(client: TestClient) -> dict[str, list[str]]:
    response = client.get("/google-health/connect", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/auth")
    return parse_qs(urlparse(location).query)


def _session_data(client: TestClient) -> dict[str, Any]:
    cookies = [cookie for cookie in client.cookies.jar if cookie.name == "session"]
    assert cookies
    raw = TimestampSigner(_SECRET).unsign(cookies[-1].value).decode()
    return json.loads(base64.b64decode(raw))


def _callback(client: TestClient, state: str, code: str = "auth-code") -> Any:
    return client.get(
        "/google-health/callback",
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def test_pending_state_consumption_is_single_use_and_tenant_scoped(
    tmp_path: Any,
) -> None:
    db_path = str(tmp_path / "consume.db")
    database.init_db(db_path)
    first_user = database.get_legacy_user_id(db_path)
    second_user = database.get_or_create_user("second@example.test", db_path=db_path)["id"]
    key = "oauth_pending:google_health:nonce-1"
    database.set_meta(key, "first", db_path, user_id=first_user)
    database.set_meta(key, "second", db_path, user_id=second_user)

    assert database.consume_meta(key, db_path, user_id=first_user) == "first"
    assert database.consume_meta(key, db_path, user_id=first_user) is None
    assert database.consume_meta(key, db_path, user_id=second_user) == "second"
    assert database.consume_meta(key, db_path, user_id=second_user) is None


def test_connect_uses_signed_state_nonce_and_pkce(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    with _client(app_module, db_path) as client:
        params = _connect(client)

    assert params["state"][0].count(".") == 1
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]
    assert params["redirect_uri"] == [_REDIRECT]


def test_callback_exchanges_pkce_verifier_and_stores_refresh_token(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_exchange(client_id: str, client_secret: str, code: str, **kwargs: Any) -> Any:
        calls.append({"client_id": client_id, "code": code, **kwargs})
        return {"refresh_token": "synthetic-refresh-token"}

    monkeypatch.setattr(app_module, "exchange_code", fake_exchange)
    with _client(app_module, db_path) as client:
        params = _connect(client)
        response = _callback(client, params["state"][0])

    assert response.headers["location"] == "/settings?gh=connected"
    assert calls[0]["code_verifier"]
    assert code_challenge_for(calls[0]["code_verifier"]) == params["code_challenge"][0]
    assert calls[0]["redirect_uri"] == _REDIRECT
    user_id = database.get_legacy_user_id(db_path)
    assert (
        database.get_meta("google_health_refresh_token", db_path, user_id=user_id)
        == "synthetic-refresh-token"
    )


def test_callback_replay_is_rejected(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_exchange(*args: Any, **kwargs: Any) -> Any:
        calls.append("exchanged")
        return {"refresh_token": "synthetic-refresh-token"}

    monkeypatch.setattr(app_module, "exchange_code", fake_exchange)
    with _client(app_module, db_path) as client:
        params = _connect(client)
        first = _callback(client, params["state"][0])
        second = _callback(client, params["state"][0])

    assert first.headers["location"] == "/settings?gh=connected"
    assert second.headers["location"] == "/settings?gh=error"
    assert calls == ["exchanged"]


def test_callback_rejects_login_csrf_from_another_session(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "exchange_code",
        lambda *a, **k: {"refresh_token": "synthetic-refresh-token"},
    )
    with _client(app_module, db_path) as victim:
        params = _connect(victim)
    with _client(app_module, db_path) as attacker:
        response = _callback(attacker, params["state"][0])

    assert response.headers["location"] == "/settings?gh=error"


def test_callback_rejects_expired_state(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "exchange_code",
        lambda *a, **k: {"refresh_token": "synthetic-refresh-token"},
    )
    with _client(app_module, db_path) as client:
        _connect(client)
        session_id = _session_data(client)["oauth_session_id"]
        _, expired_state = create_oauth_state(
            user_id=database.get_legacy_user_id(db_path),
            provider="google_health",
            session_id=session_id,
            redirect_uri=_REDIRECT,
            secret=app_module._OAUTH_STATE_SECRET,
            now=time.time() - 1000,
        )
        response = _callback(client, expired_state)

    assert response.headers["location"] == "/settings?gh=error"


def test_concurrent_connection_attempts_both_complete(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch)
    counter = {"value": 0}

    def fake_exchange(*args: Any, **kwargs: Any) -> Any:
        counter["value"] += 1
        return {"refresh_token": f"synthetic-token-{counter['value']}"}

    monkeypatch.setattr(app_module, "exchange_code", fake_exchange)
    with _client(app_module, db_path) as client:
        first = _connect(client)
        second = _connect(client)
        first_response = _callback(client, first["state"][0])
        second_response = _callback(client, second["state"][0])

    assert first["state"][0] != second["state"][0]
    assert first_response.headers["location"] == "/settings?gh=connected"
    assert second_response.headers["location"] == "/settings?gh=connected"
    assert counter["value"] == 2


def test_connect_fails_closed_for_non_allowlisted_redirect_uri(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(
        tmp_path,
        monkeypatch,
        redirect_uri="https://evil.example/google-health/callback",
        allowed_redirects=_REDIRECT,
    )
    with _client(app_module, db_path) as client:
        response = client.get("/google-health/connect", follow_redirects=False)

    assert response.headers["location"] == "/settings?gh=error"


def test_login_redirect_uri_is_exact_match_allowlisted(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _ = _app(
        tmp_path,
        monkeypatch,
        public_url="https://app.example.test",
        web_login=True,
    )
    monkeypatch.setenv(
        "WEB_OAUTH_ALLOWED_REDIRECT_URIS",
        "https://other.example.test/auth",
    )
    importlib.reload(app_module)
    with TestClient(app_module.app) as client:
        response = client.get("/login/google", follow_redirects=False)

    assert response.headers["location"] == "/login?error=redirect"


def test_login_uses_pkce_and_nonce(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _ = _app(
        tmp_path,
        monkeypatch,
        public_url="https://app.example.test",
        web_login=True,
    )
    monkeypatch.setenv(
        "WEB_OAUTH_ALLOWED_REDIRECT_URIS",
        "https://app.example.test/auth",
    )
    importlib.reload(app_module)
    captured: dict[str, Any] = {}

    async def fake_authorize(request: Any, redirect_uri: str | None = None, **kwargs: Any) -> Any:
        captured["redirect_uri"] = redirect_uri
        captured.update(kwargs)
        return RedirectResponse("https://accounts.google.com/synthetic", status_code=302)

    monkeypatch.setattr(app_module.oauth.google, "authorize_redirect", fake_authorize)
    with TestClient(app_module.app) as client:
        response = client.get("/login/google", follow_redirects=False)

    assert response.status_code == 302
    assert captured["redirect_uri"] == "https://app.example.test/auth"
    assert captured["nonce"]
    assert app_module.oauth.google.client_kwargs["code_challenge_method"] == "S256"


def test_login_callback_accepts_session_bound_fresh_state_once(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, db_path = _app(tmp_path, monkeypatch, web_login=True)
    calls: list[str] = []

    async def fake_access_token(request: Any, **kwargs: Any) -> Any:
        calls.append(request.query_params.get("state", ""))
        request.session.pop("_state_google_fresh", None)
        return {
            "userinfo": {
                "email": "athlete@example.test",
                "name": "Athlete",
            }
        }

    monkeypatch.setattr(
        app_module.oauth.google,
        "authorize_access_token",
        fake_access_token,
    )
    cookie = _session_cookie(
        database.get_legacy_user_id(db_path),
        {"_state_google_fresh": {"exp": time.time() + 60, "data": {}}},
    )
    with TestClient(app_module.app) as client:
        client.cookies.set("session", cookie)
        first = client.get("/auth?state=fresh", follow_redirects=False)
        second = client.get("/auth?state=fresh", follow_redirects=False)

    assert first.headers["location"] == "/"
    assert second.headers["location"] == "/login"
    assert calls == ["fresh"]


def test_login_callback_rejects_missing_or_expired_state(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    app_module, _ = _app(tmp_path, monkeypatch, web_login=True)
    expired = base64.b64encode(
        json.dumps(
            {
                "user": "athlete@example.test",
                "_state_google_expired": {"exp": time.time() - 60, "data": {}},
            }
        ).encode()
    )
    cookie = TimestampSigner(_SECRET).sign(expired).decode()
    with TestClient(app_module.app) as client:
        client.cookies.set("session", cookie)
        missing = client.get("/auth?state=unknown", follow_redirects=False)
        stale = client.get("/auth?state=expired", follow_redirects=False)

    assert missing.headers["location"] == "/login"
    assert stale.headers["location"] == "/login"
