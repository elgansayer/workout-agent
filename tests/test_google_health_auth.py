"""Tests for the Google Health one-time authorisation helper (pure functions)."""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

import google_health_auth


def test_build_authorize_url_contains_required_params() -> None:
    url = google_health_auth.build_authorize_url("CID123", "STATE456")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == ["CID123"]
    assert params["response_type"] == ["code"]
    assert params["state"] == ["STATE456"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "googlehealth" in params["scope"][0]


def test_build_authorize_url_custom_redirect_and_scope() -> None:
    url = google_health_auth.build_authorize_url(
        "CID", "ST", redirect_uri="http://example.com/cb", scope="custom"
    )
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["redirect_uri"] == ["http://example.com/cb"]
    assert params["scope"] == ["custom"]


def test_exchange_code_posts_and_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"refresh_token": "RT", "access_token": "AT"}

    def _fake_post(url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int | None = None) -> _Resp:
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(google_health_auth.requests, "post", _fake_post)
    tokens = google_health_auth.exchange_code("CID", "SECRET", "CODE")
    assert tokens is not None
    assert tokens["refresh_token"] == "RT"
    assert captured["url"] == google_health_auth.TOKEN_URL
    assert isinstance(captured["data"], dict)
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "CODE"
    assert captured["data"]["client_id"] == "CID"
    assert captured["data"]["client_secret"] == "SECRET"


def test_exchange_code_request_exception_no_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def _fake_post(url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int | None = None) -> None:
        raise requests.RequestException("network down")

    monkeypatch.setattr(google_health_auth.requests, "post", _fake_post)
    tokens = google_health_auth.exchange_code("CID", "SECRET", "CODE")
    assert tokens is None


def test_exchange_code_request_exception_with_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    class _Resp:
        text = "bad request error"

    exc = requests.RequestException("bad request")
    exc.response = _Resp()  # type: ignore[assignment]

    def _fake_post(url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int | None = None) -> None:
        raise exc

    monkeypatch.setattr(google_health_auth.requests, "post", _fake_post)
    tokens = google_health_auth.exchange_code("CID", "SECRET", "CODE")
    assert tokens is None


def test_exchange_code_value_error_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> None:
            raise ValueError("not JSON")

    def _fake_post(url: str, headers: dict[str, str] | None = None, data: dict[str, str] | None = None, timeout: int | None = None) -> _Resp:
        return _Resp()

    monkeypatch.setattr(google_health_auth.requests, "post", _fake_post)
    tokens = google_health_auth.exchange_code("CID", "SECRET", "CODE")
    assert tokens is None
