"""Tests for the Open-Meteo weather connector with stubbed HTTP."""

from __future__ import annotations

from typing import Any

import pytest

import weather


class _FakeResponse:
    def __init__(self, payload: Any, ok: bool = True) -> None:
        self._payload = payload
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise weather.requests.RequestException("boom")

    def json(self) -> Any:
        return self._payload


def _current(temp: float, humidity: float) -> dict[str, object]:
    return {"current": {"temperature_2m": temp, "relative_humidity_2m": humidity}}


def test_get_current_weather_returns_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(_current(22.0, 50.0)),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.temperature_c == 22.0
    assert result.humidity_pct == 50.0
    assert not result.is_extreme_heat


def test_extreme_heat_above_30c(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(_current(31.0, 40.0)),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.is_extreme_heat


def test_extreme_heat_hot_and_humid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(_current(29.0, 65.0)),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.is_extreme_heat


def test_graceful_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector must return None on an HTTP error, not raise."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({}, ok=False),
    )
    assert weather.get_current_weather() is None


def test_graceful_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector must return None when the response shape is wrong."""

    class _Bad:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            raise ValueError("bad json")

    monkeypatch.setattr(weather.requests, "get", lambda url, **kwargs: _Bad())
    assert weather.get_current_weather() is None


def test_graceful_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector must return None when required fields are absent."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"current": {}}),
    )
    assert weather.get_current_weather() is None


def test_as_text_formatting() -> None:
    normal = weather.WeatherConditions(22.0, 50.0, False)
    assert "22.0°C" in normal.as_text()
    assert "Normal" in normal.as_text()

    extreme = weather.WeatherConditions(32.0, 70.0, True)
    assert "Extreme Heat Warning" in extreme.as_text()


def test_graceful_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: non-object JSON from weather API must return None, not crash."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse([1, 2, 3]),
    )
    assert weather.get_current_weather() is None


def test_graceful_non_object_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: non-object 'current' field must return None, not crash."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"current": [1, 2, 3]}),
    )
    assert weather.get_current_weather() is None


def test_graceful_string_numeric_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: defensive float coercion handles string-number responses."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(
            {"current": {"temperature_2m": "22.5", "relative_humidity_2m": "55"}},
        ),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.temperature_c == 22.5
    assert result.humidity_pct == 55.0


def test_graceful_uncoercable_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: non-coercible strings must return None, not crash."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(
            {"current": {"temperature_2m": "warm", "relative_humidity_2m": [1, 2]}},
        ),
    )
    assert weather.get_current_weather() is None
