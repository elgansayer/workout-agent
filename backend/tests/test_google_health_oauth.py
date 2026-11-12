"""Replay, mix-up, expiry and concurrency tests for the Google Health OAuth flow."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

import connectors.oauth as oauth_primitives
from database import get_or_create_user, init_db
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "google-health-oauth-test-secret-long-enough"
_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"1" * 32).decode()
_EMAIL = "person@example.test"
_REDIRECT_URI = "https://testserver/google-health/callback"


def _cookie(user_id: str, session_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": _EMAIL, "user_id": user_id, "sid": session_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


@dataclass
class _Env:
    module: Any
    db: str
    user_id: str
    client_for: Callable[[], TestClient]


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    db = str(tmp_path / "google-health-oauth.db")
    init_db(db)
    for name, value in {
        "DATABASE_PATH": db,
        "APP_ENV": "test",
        "WEB_GOOGLE_CLIENT_ID": "login-client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "login-client-secret",
        "WEB_AUTH_SECRET": _SECRET,
        "ENCRYPTION_KEY": _ENCRYPTION_KEY,
        "GOOGLE_HEALTH_CLIENT_ID": "health-client-id",
        "GOOGLE_HEALTH_CLIENT_SECRET": "health-client-secret",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GOOGLE_HEALTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("WEB_GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("WEB_PUBLIC_URL", raising=False)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    user_id = get_or_create_user(_EMAIL, "Synthetic Person", db_path=db)["id"]

    def client_for() -> TestClient:
        client = TestClient(app_module.app)
        session_id = app_module.SESSION_STORE.create(user_id)
        client.cookies.set("session", _cookie(user_id, session_id))
        return client

    return _Env(app_module, db, user_id, client_for)


def _connect(client: TestClient) -> Any:
    response = client.get("/google-health/connect", follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def _start(client: TestClient) -> dict[str, list[str]]:
    location = _connect(client).headers["location"]
    return parse_qs(urlsplit(location).query)


def _state(client: TestClient) -> str:
    return _start(client)["state"][0]


def _callback(client: TestClient, *, code: str, state: str) -> int:
    return client.get(
        f"/google-health/callback?code={code}&state={state}",
        follow_redirects=False,
    ).status_code


def _recording_exchange(calls: list[dict[str, Any]]) -> Callable[..., dict[str, str]]:
    def exchange(*_args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(kwargs)
        return {"refresh_token": "synthetic"}

    return exchange


def test_connect_binds_state_nonce_pkce_and_redirect_uri(env: _Env) -> None:
    query = _start(env.client_for())

    assert query["state"][0]
    assert query["nonce"][0]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) >= 43
    assert query["redirect_uri"] == [_REDIRECT_URI]


def test_replayed_callback_is_rejected_without_a_second_exchange(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def exchange(*_args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(kwargs)
        return {"refresh_token": "synthetic-refresh-token"}

    monkeypatch.setattr(env.module, "exchange_code", exchange)
    client = env.client_for()
    state = _state(client)

    assert _callback(client, code="synthetic-code", state=state) == 303
    assert _callback(client, code="synthetic-code", state=state) == 303

    assert len(calls) == 1
    assert calls[0]["redirect_uri"] == _REDIRECT_URI
    assert calls[0]["code_verifier"]


def test_callback_from_another_session_is_rejected(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        env.module,
        "exchange_code",
        _recording_exchange(calls),
    )
    attacker = env.client_for()
    victim = env.client_for()
    state = _state(attacker)

    assert _callback(victim, code="synthetic-code", state=state) == 303
    assert calls == []


def test_concurrent_connections_each_complete(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        env.module,
        "exchange_code",
        _recording_exchange(calls),
    )
    client = env.client_for()
    first = _state(client)
    second = _state(client)
    assert first != second

    assert _callback(client, code="synthetic-code-1", state=first) == 303
    assert _callback(client, code="synthetic-code-2", state=second) == 303
    assert len(calls) == 2


def test_expired_state_is_rejected(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        env.module,
        "exchange_code",
        _recording_exchange(calls),
    )
    original_begin = env.module.begin_authorization
    original_consume = env.module.consume_authorization

    def begin(*args: Any, **kwargs: Any) -> Any:
        kwargs["now"] = 1_000
        return original_begin(*args, **kwargs)

    def consume(*args: Any, **kwargs: Any) -> Any:
        kwargs["now"] = 1_000 + oauth_primitives.STATE_TTL_SECONDS + 1
        return original_consume(*args, **kwargs)

    monkeypatch.setattr(env.module, "begin_authorization", begin)
    monkeypatch.setattr(env.module, "consume_authorization", consume)
    client = env.client_for()
    state = _state(client)

    assert _callback(client, code="synthetic-code", state=state) == 303
    assert calls == []


def test_callback_uses_redirect_uri_stored_at_initiation(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        env.module,
        "exchange_code",
        _recording_exchange(calls),
    )
    # A fixed public origin keeps the allowlist independent of either request's
    # Host header, exactly as in production.
    monkeypatch.setenv("WEB_PUBLIC_URL", "https://app.example.test")
    client = env.client_for()
    response = _connect(client)
    started = parse_qs(urlsplit(response.headers["location"]).query)
    other = TestClient(env.module.app, base_url="https://other.example.test")
    session_cookie = client.cookies.get("session")
    assert session_cookie is not None
    other.cookies.set("session", session_cookie)

    assert _callback(other, code="synthetic-code", state=started["state"][0]) == 303
    assert calls[0]["redirect_uri"] == started["redirect_uri"][0]
    assert started["redirect_uri"] == [
        "https://app.example.test/google-health/callback"
    ]


def test_google_health_connect_ignores_a_forged_host_header(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEB_PUBLIC_URL", "https://app.example.test")
    client = TestClient(env.module.app, base_url="https://evil.example.test")
    session_id = env.module.SESSION_STORE.create(env.user_id)
    client.cookies.set("session", _cookie(env.user_id, session_id))

    query = _start(client)

    assert query["redirect_uri"] == [
        "https://app.example.test/google-health/callback"
    ]


def test_login_redirect_ignores_host_and_query_open_redirect(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_authorization_url(
        _redirect_uri: str | None = None, **_kwargs: Any
    ) -> dict[str, str]:
        captured["redirect_uri"] = _redirect_uri
        return {
            "url": "https://accounts.example.test/o/oauth2/auth",
            "state": "synthetic-login-state",
            "nonce": "synthetic-login-nonce",
        }

    monkeypatch.setattr(
        env.module.oauth.google,
        "create_authorization_url",
        fake_create_authorization_url,
    )
    monkeypatch.setenv("WEB_PUBLIC_URL", "https://app.example.test")
    client = TestClient(env.module.app, base_url="https://evil.example.test")

    response = client.get(
        "/login/google?redirect_uri=https://evil.example.test/callback",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert captured["redirect_uri"] == "https://app.example.test/auth"


def test_configured_login_callback_override_is_used(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_authorization_url(
        _redirect_uri: str | None = None, **_kwargs: Any
    ) -> dict[str, str]:
        captured["redirect_uri"] = _redirect_uri
        return {
            "url": "https://accounts.example.test/o/oauth2/auth",
            "state": "synthetic-login-state",
            "nonce": "synthetic-login-nonce",
        }

    monkeypatch.setattr(
        env.module.oauth.google,
        "create_authorization_url",
        fake_create_authorization_url,
    )
    monkeypatch.setattr(
        env.module,
        "WEB_GOOGLE_REDIRECT_URI",
        "https://login.example.test/auth",
    )

    response = env.client_for().get("/login/google", follow_redirects=False)

    assert response.status_code == 302
    assert captured["redirect_uri"] == "https://login.example.test/auth"


def test_login_state_cannot_be_replayed_in_another_browser(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create_authorization_url(
        _redirect_uri: str | None = None, **_kwargs: Any
    ) -> dict[str, str]:
        return {
            "url": "https://accounts.example.test/o/oauth2/auth",
            "state": "synthetic-login-state",
            "nonce": "synthetic-login-nonce",
        }

    monkeypatch.setattr(
        env.module.oauth.google,
        "create_authorization_url",
        fake_create_authorization_url,
    )
    initiator = TestClient(env.module.app)
    assert initiator.get("/login/google", follow_redirects=False).status_code == 302

    attacker = TestClient(env.module.app)
    response = attacker.get(
        "/auth?code=synthetic-code&state=synthetic-login-state",
        follow_redirects=False,
    )

    assert response.headers["location"] == "/login"
    assert attacker.get("/api/me").status_code == 401


def test_login_uses_pkce_and_short_lived_session_state(env: _Env) -> None:
    google = env.module.oauth.google
    assert google.client_kwargs["code_challenge_method"] == "S256"
    assert google.framework.expires_in == oauth_primitives.STATE_TTL_SECONDS

    expired = {
        "_state_google_synthetic": {
            "data": {"nonce": "synthetic-nonce"},
            "exp": time.time() - 1,
        }
    }
    assert (
        asyncio.run(google.framework.get_state_data(expired, "synthetic")) is None
    )
    assert "_state_google_synthetic" not in expired

    live = {
        "_state_google_synthetic": {
            "data": {"nonce": "synthetic-nonce"},
            "exp": time.time() + 60,
        }
    }
    assert asyncio.run(google.framework.get_state_data(live, "synthetic")) == {
        "nonce": "synthetic-nonce"
    }
    # The same state (for example from an older copy of the cookie) is spent.
    assert (
        asyncio.run(google.framework.get_state_data(live, "synthetic")) is None
    )
