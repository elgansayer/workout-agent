"""Tests for the Google Health body-composition sync, with stubbed HTTP."""

from __future__ import annotations

from typing import Any

import pytest

import google_health_client
from database import get_meta, init_db, set_meta


class _FakeResponse:
    def __init__(self, payload: Any, ok: bool = True) -> None:
        self._payload = payload
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise google_health_client.requests.RequestException("boom")

    def json(self) -> Any:
        return self._payload


def _db(tmp_path: Any) -> str:
    db = str(tmp_path / "test.db")
    init_db(db)
    return db


def _points(point_key: str, value_field: str, samples: list[tuple[str, float]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "dataPoints": [
            {point_key: {value_field: value, "sampleTime": {"physicalTime": when}}}
            for when, value in samples
        ],
    }


def test_fetch_body_metrics_reads_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "body-fat" in url:
            return _FakeResponse(
                _points(
                    "bodyFat",
                    "percentage",
                    [
                        ("2026-06-15T08:00:00Z", 15.2),
                        ("2026-06-17T08:00:00Z", 14.6),
                    ],
                ),
            )
        return _FakeResponse(
            _points(
                "weight",
                "weightGrams",
                [
                    ("2026-06-15T08:00:00Z", 83000),
                    ("2026-06-17T08:00:00Z", 81500),
                ],
            ),
        )

    monkeypatch.setattr(google_health_client.requests, "get", fake_get)
    metrics = google_health_client.fetch_body_metrics("access")
    assert metrics == {"weight_kg": 81.5, "body_fat_pct": 14.6}


def test_fetch_body_metrics_picks_latest_regardless_of_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "body-fat" in url:
            return _FakeResponse({"dataPoints": []})
        return _FakeResponse(
            _points(
                "weight",
                "weightGrams",
                [
                    ("2026-06-17T08:00:00Z", 81500),
                    ("2026-06-15T08:00:00Z", 83000),
                ],
            ),
        )

    monkeypatch.setattr(google_health_client.requests, "get", fake_get)
    metrics = google_health_client.fetch_body_metrics("access")
    assert metrics == {"weight_kg": 81.5}


def test_fetch_body_metrics_none_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"dataPoints": []}),
    )
    assert google_health_client.fetch_body_metrics("access") is None


def test_sync_disabled_without_credentials(tmp_path: Any) -> None:
    db = _db(tmp_path)
    assert google_health_client.sync_body_metrics(None, None, None, db) is None


def test_sync_persists_refresh_token(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        # The request must carry the stored refresh token.
        assert kwargs["data"]["refresh_token"] == "env-token"
        return _FakeResponse(
            {"access_token": "access-1", "refresh_token": "rotated-token"},
        )

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "body-fat" in url:
            return _FakeResponse(
                _points("bodyFat", "percentage", [("2026-06-17T08:00:00Z", 14.0)]),
            )
        return _FakeResponse(
            _points("weight", "weightGrams", [("2026-06-17T08:00:00Z", 80000)]),
        )

    monkeypatch.setattr(google_health_client.requests, "post", fake_post)
    monkeypatch.setattr(google_health_client.requests, "get", fake_get)

    metrics = google_health_client.sync_body_metrics("id", "secret", "env-token", db)
    assert metrics == {"weight_kg": 80.0, "body_fat_pct": 14.0}
    assert get_meta("google_health_refresh_token", db) == "rotated-token"


def test_sync_keeps_refresh_token_when_not_rotated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)

    # Google usually omits the refresh token on refresh; we should keep the old one.
    monkeypatch.setattr(
        google_health_client.requests,
        "post",
        lambda url, **kwargs: _FakeResponse({"access_token": "a"}),
    )
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"dataPoints": []}),
    )

    google_health_client.sync_body_metrics("id", "secret", "env-token", db)
    assert get_meta("google_health_refresh_token", db) == "env-token"


def test_sync_uses_stored_token_when_present(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    set_meta("google_health_refresh_token", "stored-token", db)

    seen: dict[str, str] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        seen["refresh_token"] = kwargs["data"]["refresh_token"]
        return _FakeResponse({"access_token": "a", "refresh_token": "next-token"})

    monkeypatch.setattr(google_health_client.requests, "post", fake_post)
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"dataPoints": []}),
    )

    google_health_client.sync_body_metrics("id", "secret", "env-token", db)
    assert seen["refresh_token"] == "stored-token"
    assert get_meta("google_health_refresh_token", db) == "next-token"


def test_sync_uses_stored_token_without_env_token(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # When the user linked Google Health from the web dashboard, only the stored
    # token exists (no GOOGLE_HEALTH_REFRESH_TOKEN env var) and sync must work.
    db = _db(tmp_path)
    set_meta("google_health_refresh_token", "web-linked", db)

    seen: dict[str, str] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        seen["refresh_token"] = kwargs["data"]["refresh_token"]
        return _FakeResponse({"access_token": "a", "refresh_token": "rotated"})

    monkeypatch.setattr(google_health_client.requests, "post", fake_post)
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"dataPoints": []}),
    )

    google_health_client.sync_body_metrics("id", "secret", None, db)
    assert seen["refresh_token"] == "web-linked"
    assert get_meta("google_health_refresh_token", db) == "rotated"


def test_latest_value_handles_non_dict_sample_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: sampleTime as a string (not dict) must not crash _latest_value."""
    points: dict[str, list[dict[str, Any]]] = {
        "dataPoints": [
            {
                "bodyFat": {
                    "percentage": 14.0,
                    # sampleTime is a string instead of a nested dict
                    "sampleTime": "2026-07-01T08:00:00Z",
                },
            },
        ],
    }
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(points),
    )
    result = google_health_client._latest_value(
        "access",
        "body-fat",
        "bodyFat",
        "percentage",
    )
    assert result == 14.0  # value still readable without a parseable time


def test_latest_value_handles_missing_sample_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: missing sampleTime must not crash _latest_value."""
    points: dict[str, list[dict[str, Any]]] = {
        "dataPoints": [
            {
                "bodyFat": {
                    "percentage": 14.0,
                },
            },
        ],
    }
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(points),
    )
    result = google_health_client._latest_value(
        "access",
        "body-fat",
        "bodyFat",
        "percentage",
    )
    assert result == 14.0  # value still readable without a sample time


def test_fetch_body_metrics_graceful_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector must return None on a 5xx/connection error, not raise."""
    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({}, ok=False),
    )
    metrics = google_health_client.fetch_body_metrics("access")
    assert metrics is None


def test_fetch_body_metrics_graceful_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector must return None on malformed JSON, not raise."""

    class _Bad:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> None:
            raise ValueError("not json")

    monkeypatch.setattr(
        google_health_client.requests,
        "get",
        lambda url, **kwargs: _Bad(),
    )
    metrics = google_health_client.fetch_body_metrics("access")
    assert metrics is None
