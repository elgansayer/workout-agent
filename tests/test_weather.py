"""Tests for the Open-Meteo weather connector with stubbed HTTP."""

from __future__ import annotations

import weather


class _FakeResponse:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise weather.requests.RequestException("boom")

    def json(self):
        return self._payload


def _current(temp, humidity):
    return {"current": {"temperature_2m": temp, "relative_humidity_2m": humidity}}


def test_get_current_weather_returns_conditions(monkeypatch):
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


def test_extreme_heat_above_30c(monkeypatch):
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(_current(31.0, 40.0)),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.is_extreme_heat


def test_extreme_heat_hot_and_humid(monkeypatch):
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse(_current(29.0, 65.0)),
    )
    result = weather.get_current_weather()
    assert result is not None
    assert result.is_extreme_heat


def test_graceful_http_failure(monkeypatch):
    """Connector must return None on an HTTP error, not raise."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({}, ok=False),
    )
    assert weather.get_current_weather() is None


def test_graceful_malformed_response(monkeypatch):
    """Connector must return None when the response shape is wrong."""

    class _Bad:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(weather.requests, "get", lambda url, **kwargs: _Bad())
    assert weather.get_current_weather() is None


def test_graceful_missing_fields(monkeypatch):
    """Connector must return None when required fields are absent."""
    monkeypatch.setattr(
        weather.requests,
        "get",
        lambda url, **kwargs: _FakeResponse({"current": {}}),
    )
    assert weather.get_current_weather() is None


def test_as_text_formatting():
    normal = weather.WeatherConditions(22.0, 50.0, False)
    assert "22.0°C" in normal.as_text()
    assert "Normal" in normal.as_text()

    extreme = weather.WeatherConditions(32.0, 70.0, True)
    assert "Extreme Heat Warning" in extreme.as_text()