"""Regression tests for Host validation and reverse-proxy trust boundaries."""

from __future__ import annotations

import asyncio
from ipaddress import ip_network
from pathlib import Path
from typing import Any

import pytest

from webapp.proxy_security import (
    ProxySecurityConfig,
    ProxySecurityConfigurationError,
    ProxySecurityMiddleware,
    load_proxy_security_config,
)


def _config() -> ProxySecurityConfig:
    return ProxySecurityConfig(
        public_scheme="https",
        public_host="workout.example.com",
        allowed_hosts=frozenset(
            {"workout.example.com", "www.workout.example.com"}
        ),
        trusted_proxy_networks=(ip_network("10.0.0.0/8"),),
    )


def _run_request(
    *,
    headers: list[tuple[bytes, bytes]],
    client: tuple[str, int] = ("203.0.113.9", 49152),
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    captured_scope: dict[str, Any] | None = None
    sent: list[dict[str, Any]] = []

    async def downstream(scope, receive, send) -> None:
        nonlocal captured_scope
        captured_scope = scope
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": client,
        "server": ("127.0.0.1", 8000),
    }

    asyncio.run(ProxySecurityMiddleware(downstream, _config())(scope, receive, send))
    return captured_scope, sent


def _headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    return dict(scope["headers"])


def test_production_requires_canonical_https_origin() -> None:
    with pytest.raises(
        ProxySecurityConfigurationError,
        match="WEB_PUBLIC_URL is required",
    ):
        load_proxy_security_config({"APP_ENV": "production"})

    with pytest.raises(
        ProxySecurityConfigurationError,
        match="must use https",
    ):
        load_proxy_security_config(
            {
                "APP_ENV": "production",
                "WEB_PUBLIC_URL": "http://workout.example.com",
            }
        )


def test_public_url_must_be_origin_only() -> None:
    with pytest.raises(
        ProxySecurityConfigurationError,
        match="origin only",
    ):
        load_proxy_security_config(
            {
                "APP_ENV": "production",
                "WEB_PUBLIC_URL": "https://workout.example.com/settings",
            }
        )


def test_untrusted_host_is_rejected_before_application() -> None:
    scope, sent = _run_request(headers=[(b"host", b"evil.example.com")])

    assert scope is None
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400


def test_duplicate_host_is_rejected() -> None:
    scope, sent = _run_request(
        headers=[
            (b"host", b"workout.example.com"),
            (b"host", b"www.workout.example.com"),
        ]
    )

    assert scope is None
    assert sent[0]["status"] == 400


def test_untrusted_peer_cannot_spoof_forwarding_metadata() -> None:
    scope, sent = _run_request(
        client=("203.0.113.9", 49152),
        headers=[
            (b"host", b"www.workout.example.com"),
            (b"x-forwarded-for", b"198.51.100.23"),
            (b"x-real-ip", b"198.51.100.24"),
            (b"x-forwarded-host", b"evil.example.com"),
            (b"x-forwarded-proto", b"http"),
            (b"forwarded", b"for=198.51.100.25;proto=http"),
        ],
    )

    assert sent[0]["status"] == 200
    assert scope is not None
    assert scope["client"] == ("203.0.113.9", 49152)
    assert scope["scheme"] == "https"
    assert scope["server"] == ("workout.example.com", 443)
    assert _headers(scope)[b"host"] == b"workout.example.com"
    assert not any(
        name in _headers(scope)
        for name in (
            b"forwarded",
            b"x-forwarded-for",
            b"x-real-ip",
            b"x-forwarded-host",
            b"x-forwarded-proto",
        )
    )
    assert scope["state"]["validated_origin"] == "https://workout.example.com"
    assert scope["state"]["trusted_proxy"] is False


def test_trusted_proxy_chain_resolves_first_untrusted_client() -> None:
    scope, sent = _run_request(
        client=("10.0.0.5", 49152),
        headers=[
            (b"host", b"workout.example.com"),
            (b"x-forwarded-for", b"198.51.100.23, 10.0.0.4"),
            (b"x-forwarded-host", b"workout.example.com"),
            (b"x-forwarded-proto", b"https"),
        ],
    )

    assert sent[0]["status"] == 200
    assert scope is not None
    assert scope["client"] == ("198.51.100.23", 49152)
    assert scope["scheme"] == "https"
    assert _headers(scope)[b"host"] == b"workout.example.com"
    assert scope["state"]["validated_client_ip"] == "198.51.100.23"
    assert scope["state"]["trusted_proxy"] is True


def test_trusted_proxy_cannot_override_canonical_external_scheme() -> None:
    scope, sent = _run_request(
        client=("10.0.0.5", 49152),
        headers=[
            (b"host", b"workout.example.com"),
            (b"x-forwarded-for", b"198.51.100.23"),
            (b"x-forwarded-proto", b"http"),
        ],
    )

    assert sent[0]["status"] == 200
    assert scope is not None
    assert scope["scheme"] == "https"


def test_trusted_proxy_rejects_unlisted_forwarded_host() -> None:
    scope, sent = _run_request(
        client=("10.0.0.5", 49152),
        headers=[
            (b"host", b"workout.example.com"),
            (b"x-forwarded-host", b"evil.example.com"),
        ],
    )

    assert scope is None
    assert sent[0]["status"] == 400


def test_trusted_proxy_rejects_malformed_forwarded_chain() -> None:
    scope, sent = _run_request(
        client=("10.0.0.5", 49152),
        headers=[
            (b"host", b"workout.example.com"),
            (b"x-forwarded-for", b"198.51.100.23, not-an-ip"),
        ],
    )

    assert scope is None
    assert sent[0]["status"] == 400


def test_docker_disables_uvicorn_proxy_header_preprocessing() -> None:
    dockerfile = Path(__file__).parents[2] / "Dockerfile.web"
    source = dockerfile.read_text(encoding="utf-8")

    assert "--no-proxy-headers" in source
    assert "--forwarded-allow-ips" not in source
