"""Secrets injected into the web and agent processes never reach captured logs.

Each test plants a synthetic credential where a real failure would carry it (an
OAuth callback, a provider exception, a web server access line, a push service
reply, an AI response) and proves it is absent from everything that was logged.
Every secret is synthetic and assembled at run time from short pieces.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

import database
import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from log_redaction import log_redaction_installed

_BODY = "Zq9" * 8
AUTH_CODE = "4/" + "0A" + _BODY + "12345678"
OAUTH_STATE = "st" + "ate-" + "Xk39fQ2mP7sL1vB8nRt"
ACCESS_TOKEN = "ya29." + "a0" + _BODY
REFRESH_TOKEN = "1//" + "0g" + _BODY
BEARER = "opaque-" + "bearer-" + _BODY
CLIENT_SECRET = "GOCSPX-" + _BODY
SESSION_SECRET = "web-" + "session-" + "secret-" + "0123456789abcdef0123456789"
SYNTHETIC_COACH_TEXT = "Squat 5x5 at 82.5kg because the left knee clicks"


@pytest.fixture()
def production_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db_path = str(tmp_path / "log-redaction-web.db")
    database.init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("WEB_AUTH_SECRET", SESSION_SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "redaction-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("INVITE_ONLY", raising=False)
    import webapp.app as module

    return importlib.reload(module)


def test_importing_the_web_package_installs_log_redaction() -> None:
    import webapp  # noqa: F401

    assert log_redaction_installed()


def test_a_failed_oauth_exchange_never_logs_the_code_state_or_tokens(
    production_app: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def failing_exchange(request: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(
            "token endpoint rejected the request: "
            f"POST https://oauth2.provider.test/token?code={AUTH_CODE}&state={OAUTH_STATE} "
            f"headers={{'Authorization': 'Bearer {BEARER}'}} "
            f'body={{"refresh_token": "{REFRESH_TOKEN}", "client_secret": "{CLIENT_SECRET}"}} '
            f"access_token={ACCESS_TOKEN}"
        )

    monkeypatch.setattr(
        production_app.oauth.google, "authorize_access_token", failing_exchange
    )

    with TestClient(production_app.app, follow_redirects=False) as client:
        response = client.get(f"/auth?code={AUTH_CODE}&state={OAUTH_STATE}")

    assert response.status_code in {302, 303, 307}
    assert response.headers["location"] == "/login"
    assert "OAuth token exchange failed" in caplog.text  # the failure is still visible
    for secret in (
        AUTH_CODE,
        OAUTH_STATE,
        BEARER,
        REFRESH_TOKEN,
        CLIENT_SECRET,
        ACCESS_TOKEN,
        SESSION_SECRET,
    ):
        assert secret not in caplog.text, secret
    assert AUTH_CODE not in response.text and AUTH_CODE not in str(response.headers)


@pytest.fixture()
def live_server() -> Iterator[Any]:
    """A real uvicorn server (default access log on) around a callback-shaped app."""
    import uvicorn

    app = FastAPI()

    @app.get("/auth")
    def auth() -> RedirectResponse:
        return RedirectResponse("/login")

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, access_log=True, lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_the_web_server_access_log_never_contains_the_oauth_code(
    live_server: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    response = httpx.get(
        f"{live_server}/auth?code={AUTH_CODE}&state={OAUTH_STATE}&token={ACCESS_TOKEN}",
        follow_redirects=False,
    )

    assert response.status_code == 307
    access: list[str] = []
    deadline = time.monotonic() + 5
    while not access and time.monotonic() < deadline:
        access = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
        time.sleep(0.02)
    assert access, "no access log line was captured"
    assert "GET /auth?code=[REDACTED:QUERY]&state=[REDACTED:QUERY]&token=[REDACTED:QUERY]" in access[0]
    for secret in (AUTH_CODE, OAUTH_STATE, ACCESS_TOKEN):
        assert secret not in caplog.text, secret


def test_a_failed_google_health_code_exchange_prints_no_provider_secrets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import google_health_auth
    import requests

    class _Reply:
        text = json.dumps(
            {"error": "invalid_grant", "access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN}
        )

    failure = requests.HTTPError(
        f"400 Client Error for url: https://oauth2.provider.test/token?code={AUTH_CODE}"
    )
    failure.response = _Reply()  # type: ignore[assignment]
    monkeypatch.setattr(google_health_auth.requests, "post", mock.Mock(side_effect=failure))

    assert google_health_auth.exchange_code("client", CLIENT_SECRET, AUTH_CODE) is None

    printed = capsys.readouterr().err
    assert "Token exchange failed" in printed and "invalid_grant" in printed
    for secret in (AUTH_CODE, ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in printed, secret


def test_a_failed_push_delivery_logs_the_status_but_not_the_endpoint_or_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from push_notifier import WebPushException, send_push_notification

    endpoint = "https://push.provider.test/send/" + _BODY
    failure = WebPushException(f"Push failed: 410 Gone for {endpoint}")
    failure.response = mock.Mock(status_code=410, text=f"subscription {endpoint} expired")
    caplog.set_level(logging.DEBUG)

    with mock.patch("push_notifier.webpush", side_effect=failure):
        delivered = send_push_notification({"endpoint": endpoint}, {"title": "x"}, "private-key")

    assert delivered is False
    assert "HTTP status 410" in caplog.text
    assert endpoint not in caplog.text and _BODY not in caplog.text


def test_a_provider_key_echoed_by_a_transport_error_is_masked_in_the_logged_chain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import hevy_client
    import requests
    from secret_vault import SecretValue

    # A provider key with no recognisable shape (Hevy keys are UUIDs).
    key = "7c5a3e1b-" + "2d4f-4a6b-" + "8c9d-0e1f2a3b4c5d"
    SecretValue(key)  # what the vault does when it decrypts the owner's key
    caplog.set_level(logging.DEBUG)
    echoing = requests.ConnectionError(f"upstream proxy echoed the request, key {key}, and hung up")

    with mock.patch("hevy_client.requests.get", side_effect=echoing):
        try:
            hevy_client.get_workout_count_strict(key)
        except hevy_client.HevyError:
            logging.getLogger("redaction.hevy").exception("history sync failed")

    assert "ConnectionError" in caplog.text  # the cause chain is still there to read
    assert key not in caplog.text


def test_a_telegram_bot_token_in_a_request_error_is_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import requests
    import telegram_notifier

    token = "123456789" + ":" + "AA" + _BODY + "xQQQQQQ"
    failure = requests.ConnectionError(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
        f"with url: /bot{token}/sendMessage (Caused by NameResolutionError)"
    )
    caplog.set_level(logging.DEBUG)

    with mock.patch("telegram_notifier.requests.post", side_effect=failure):
        assert telegram_notifier.send_telegram_message(token, "1", "hello") is False

    assert "Failed to send Telegram message" in caplog.text
    assert token not in caplog.text
    assert _BODY not in caplog.text


def test_an_unusable_ai_response_is_logged_by_size_never_by_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from insight_cron import generate_daily_header

    class _Config:
        database_path = str(tmp_path / "insight.db")
        gemini_api_key = "server-key"
        gemini_model = "gemini-3.8-flash"

    database.init_db(_Config.database_path)
    user_id = database.get_or_create_user(
        "synthetic-insight@example.com", "Synthetic", _Config.database_path
    )["id"]
    provider = mock.MagicMock()
    provider.generate.return_value = json.dumps({"note": SYNTHETIC_COACH_TEXT})
    provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr("insight_cron._resolve_insight_user", lambda path: user_id)
    monkeypatch.setattr("insight_cron.resolve_provider", lambda **kwargs: provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kwargs: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kwargs: [])
    caplog.set_level(logging.DEBUG)

    generate_daily_header(_Config())  # type: ignore[arg-type]

    assert "required fields" in caplog.text
    assert SYNTHETIC_COACH_TEXT not in caplog.text
    assert "knee" not in caplog.text
