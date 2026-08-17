"""Tests for the production web security boundary."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from webapp.security import (
    PRIVATE_CACHE_CONTROL,
    PrivateResponseHeadersMiddleware,
    SecurityConfigurationError,
    validate_web_security,
)


def _production_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "APP_ENV": "production",
        "WEB_AUTH_MODE": "google",
        "WEB_AUTH_SECRET": "0123456789abcdef0123456789ABCDEF",
        "WEB_GOOGLE_CLIENT_ID": "client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "client-secret",
        "ALLOWED_EMAILS": "owner@example.com",
    }
    environment.update(overrides)
    return environment


def test_local_anonymous_mode_must_be_explicit() -> None:
    with pytest.raises(SecurityConfigurationError, match="explicitly"):
        validate_web_security({"APP_ENV": "development"})


def test_explicit_local_anonymous_mode_is_allowed() -> None:
    validate_web_security(
        {"APP_ENV": "development", "WEB_ALLOW_ANONYMOUS": "1"},
    )


def test_production_rejects_missing_authentication_configuration() -> None:
    with pytest.raises(SecurityConfigurationError) as error:
        validate_web_security({"APP_ENV": "production"})
    message = str(error.value)
    assert "WEB_AUTH_SECRET is required" in message
    assert "WEB_GOOGLE_CLIENT_ID is required" in message
    assert "WEB_GOOGLE_CLIENT_SECRET is required" in message
    assert "ALLOWED_EMAILS" in message


def test_production_rejects_weak_session_secret() -> None:
    with pytest.raises(SecurityConfigurationError, match="at least 32 characters"):
        validate_web_security(_production_environment(WEB_AUTH_SECRET="short"))


def test_production_rejects_low_entropy_session_secret() -> None:
    with pytest.raises(SecurityConfigurationError, match="entropy"):
        validate_web_security(_production_environment(WEB_AUTH_SECRET="s" * 40))


def test_production_rejects_anonymous_override() -> None:
    with pytest.raises(SecurityConfigurationError, match="cannot be enabled"):
        validate_web_security(
            _production_environment(
                WEB_ALLOW_ANONYMOUS="true",
                WEB_AUTH_SECRET="",
                WEB_GOOGLE_CLIENT_ID="",
                WEB_GOOGLE_CLIENT_SECRET="",
                ALLOWED_EMAILS="",
            ),
        )


def test_anonymous_mode_cannot_hide_partial_auth_configuration() -> None:
    with pytest.raises(SecurityConfigurationError, match="cannot be combined"):
        validate_web_security(
            {
                "APP_ENV": "development",
                "WEB_ALLOW_ANONYMOUS": "1",
                "WEB_AUTH_SECRET": "partially-configured",
            },
        )


def test_production_rejects_invalid_allowlist_address() -> None:
    with pytest.raises(SecurityConfigurationError, match="invalid address"):
        validate_web_security(
            _production_environment(ALLOWED_EMAILS="not-an-email"),
        )


def test_production_accepts_complete_google_authentication() -> None:
    validate_web_security(_production_environment())


def _run_middleware(path: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html"),
                    (b"vary", b"Accept-Encoding"),
                ],
            },
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    middleware = PrivateResponseHeadersMiddleware(inner_app)
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": path, "headers": []},
            receive,
            send,
        ),
    )
    return messages


def _headers(messages: list[dict[str, Any]]) -> dict[str, str]:
    start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }


def test_personalised_response_is_private_and_not_stored() -> None:
    headers = _headers(_run_middleware("/settings"))
    assert headers["cache-control"] == PRIVATE_CACHE_CONTROL
    assert headers["pragma"] == "no-cache"
    assert headers["expires"] == "0"
    assert {token.strip().lower() for token in headers["vary"].split(",")} == {
        "accept-encoding",
        "cookie",
    }


def test_static_assets_keep_their_existing_cache_policy() -> None:
    headers = _headers(_run_middleware("/static/style.css"))
    assert "cache-control" not in headers
    assert headers["vary"] == "Accept-Encoding"
