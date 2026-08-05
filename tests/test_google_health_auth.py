"""Tests for the Google Health one-time authorisation helper (pure functions)."""

from __future__ import annotations

import urllib.parse

import google_health_auth


def test_build_authorize_url_contains_required_params():
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


def test_exchange_code_posts_and_returns_json(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"refresh_token": "RT", "access_token": "AT"}

    def _fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(google_health_auth.requests, "post", _fake_post)
    tokens = google_health_auth.exchange_code("CID", "SECRET", "CODE")
    assert tokens["refresh_token"] == "RT"
    assert captured["url"] == google_health_auth.TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "CODE"
    assert captured["data"]["client_id"] == "CID"
    assert captured["data"]["client_secret"] == "SECRET"
