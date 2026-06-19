"""Automatic body-composition sync from the Google Health API.

Eufy Life smart scales sync your weight and body fat to Fitbit / Google Health.
Rather than exporting anything by hand, the agent polls the Google Health Web API
each run and reads your latest readings straight from the cloud.

Google issues short-lived access tokens from a long-lived refresh token. The
refresh token is persisted in the database and reused on the next run. You
authorise the app once (see google_health_auth.py) to obtain the first refresh
token.

This replaces the legacy Fitbit Web API, which is deprecated in September 2026.

Docs: https://developers.google.com/health
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from database import get_meta, set_meta

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL = "https://health.googleapis.com/v4/users/me"
REQUEST_TIMEOUT = 15  # seconds

# Weight and body fat both fall under the health metrics & measurements scope.
SCOPE = (
    "https://www.googleapis.com/auth/googlehealth."
    "health_metrics_and_measurements.readonly"
)

# How far back to look for the most recent reading.
_LOOKBACK_DAYS = 35

_KEY_REFRESH_TOKEN = "google_health_refresh_token"


def _refresh_tokens(
    client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, str] | None:
    """Exchange a refresh token for a new access token.

    Google usually keeps the same refresh token across refreshes, so the
    returned refresh token falls back to the one we sent.
    """
    try:
        response = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("Google Health token refresh failed: %s", exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Google Health returned invalid token JSON: %s", exc)
        return None

    access = payload.get("access_token")
    if not access:
        logger.warning("Google Health token response was missing the access token.")
        return None
    # Google rarely rotates the refresh token; keep the current one if absent.
    new_refresh = payload.get("refresh_token") or refresh_token
    return access, new_refresh


def _latest_value(
    access_token: str, data_type: str, point_key: str, value_field: str
) -> float | None:
    """Return the most recent numeric value from a Google Health data type.

    data_type is the kebab-case endpoint name (e.g. "body-fat"); point_key is
    the camelCase field on the data point (e.g. "bodyFat"); value_field is the
    numeric field within it (e.g. "percentage").
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Filter fields use the snake_case form of the data type name.
    filter_field = data_type.replace("-", "_")
    try:
        response = requests.get(
            f"{BASE_URL}/dataTypes/{data_type}/dataPoints",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params={
                "filter": f'{filter_field}.sample_time.physical_time >= "{cutoff}"'
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        points = response.json().get("dataPoints", [])
    except requests.RequestException as exc:
        logger.warning("Could not fetch Google Health %s: %s", data_type, exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Google Health returned invalid JSON for %s: %s", data_type, exc)
        return None

    best_time: str = ""
    best_value: float | None = None
    for point in points:
        payload = point.get(point_key)
        if not isinstance(payload, dict):
            continue
        value = payload.get(value_field)
        if value is None:
            continue
        sample_time = (payload.get("sampleTime") or {}).get("physicalTime") or ""
        if best_value is None or sample_time > best_time:
            try:
                best_value = float(value)
            except (TypeError, ValueError):
                continue
            best_time = sample_time
    return best_value


def fetch_body_metrics(access_token: str) -> dict[str, Any] | None:
    """Read the latest weight (kg), body fat (%), resting hr, and hrv from Google Health, or None."""
    weight_grams = _latest_value(access_token, "weight", "weight", "weightGrams")
    fat = _latest_value(access_token, "body-fat", "bodyFat", "percentage")
    resting_hr = _latest_value(access_token, "resting-heart-rate", "restingHeartRate", "beatsPerMinute")
    hrv = _latest_value(access_token, "heart-rate-variability", "heartRateVariability", "rmssd")
    metrics: dict[str, Any] = {}
    if weight_grams is not None:
        metrics["weight_kg"] = round(weight_grams / 1000.0, 2)
    if fat is not None:
        metrics["body_fat_pct"] = fat
    if resting_hr is not None:
        metrics["resting_hr"] = resting_hr
    if hrv is not None:
        metrics["hrv"] = hrv
    return metrics or None


def sync_body_metrics(
    client_id: str | None,
    client_secret: str | None,
    env_refresh_token: str | None,
    db_path: str,
) -> dict[str, Any] | None:
    """Pull the latest body composition from Google Health, or None if disabled.

    Persists the refresh token so subsequent runs stay authorised. The refresh
    token may come from the environment or, when the user linked Google Health
    from the web dashboard, from the database — either is sufficient.
    """
    if not (client_id and client_secret):
        return None

    refresh_token = get_meta(_KEY_REFRESH_TOKEN, db_path) or env_refresh_token
    if not refresh_token:
        return None
    tokens = _refresh_tokens(client_id, client_secret, refresh_token)
    if tokens is None and env_refresh_token and refresh_token != env_refresh_token:
        # The stored token may have been invalidated; fall back to the env one.
        tokens = _refresh_tokens(client_id, client_secret, env_refresh_token)
    if tokens is None:
        return None

    access, new_refresh = tokens
    set_meta(_KEY_REFRESH_TOKEN, new_refresh, db_path)
    return fetch_body_metrics(access)
