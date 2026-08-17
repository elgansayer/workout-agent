"""Environmental Thermal Scaling.

Polls the free Open-Meteo API to check if the local environment is too hot/humid,
so training volume can be scaled down to protect the central nervous system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# London coordinates by default
LATITUDE = 51.5085
LONGITUDE = -0.1257
TIMEOUT = 5


@dataclass
class WeatherConditions:
    temperature_c: float
    humidity_pct: float
    is_extreme_heat: bool

    def as_text(self) -> str:
        status = "Extreme Heat Warning" if self.is_extreme_heat else "Normal"
        return (
            f"{self.temperature_c:.1f}°C, {self.humidity_pct:.0f}% humidity ({status})"
        )


def get_current_weather(
    lat: float = LATITUDE,
    lon: float = LONGITUDE,
) -> WeatherConditions | None:
    """Fetch current temperature and relative humidity from Open-Meteo."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m"
    try:
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        response_data = response.json()
        if not isinstance(response_data, dict):
            logger.warning("Weather API returned non-object JSON")
            return None
        data = response_data.get("current", {})
        if not isinstance(data, dict):
            logger.warning("Weather API returned non-object current data")
            return None

        temp = data.get("temperature_2m")
        hum = data.get("relative_humidity_2m")

        if temp is None or hum is None:
            return None

        # Defensively coerce to float; the API spec says number but a
        # malformed response could deliver a string that makes the comparison
        # below raise TypeError — that must not crash the caller.
        try:
            temp_f = float(temp)
            hum_f = float(hum)
        except (TypeError, ValueError):
            logger.warning("Weather API returned non-numeric current values")
            return None

        # Define extreme heat (e.g. >28C and >60% humidity, or just >30C)
        is_extreme = temp_f > 30.0 or (temp_f > 28.0 and hum_f > 60.0)

        return WeatherConditions(
            temperature_c=temp_f,
            humidity_pct=hum_f,
            is_extreme_heat=is_extreme,
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.warning("Could not fetch weather data: %s", exc)
        return None
