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
from database import require_tenant
from secret_vault import (
    CredentialExistsError,
    CredentialUnavailableError,
    ResolvedCredential,
    SecretVault,
    UnavailableReason,
    VaultError,
)
from tenant_isolation import OwnerIdentity

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

# Version of the Google Health data point -> body-metric transformation (unit
# conversion and latest-sample selection), recorded as provenance.
GOOGLE_HEALTH_TRANSFORM_VERSION = "google-health-body/1"


class BodyMetricsReading(dict[str, Any]):
    """Latest body metrics plus the provider sample time of each metric.

    Behaves exactly like the plain metrics dict it replaces (equality, merging,
    serialisation); ``sample_times`` carries source-timestamp provenance.
    """

    def __init__(
        self,
        values: dict[str, Any],
        sample_times: dict[str, str] | None = None,
    ) -> None:
        super().__init__(values)
        self.sample_times: dict[str, str] = dict(sample_times or {})



def _refresh_tokens(
    client_id: str,
    client_secret: str,
    refresh_token: str,
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
        if not isinstance(payload, dict):
            logger.warning("Google Health returned non-object token JSON.")
            return None
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
    access_token: str,
    data_type: str,
    point_key: str,
    value_field: str,
) -> float | None:
    """Return the most recent numeric value from a Google Health data type."""
    sample = _latest_sample(access_token, data_type, point_key, value_field)
    return sample[0] if sample else None


def _latest_sample(
    access_token: str,
    data_type: str,
    point_key: str,
    value_field: str,
) -> tuple[float, str | None] | None:
    """Return the newest (value, sample time) from a Google Health data type.

    data_type is the kebab-case endpoint name (e.g. "body-fat"); point_key is
    the camelCase field on the data point (e.g. "bodyFat"); value_field is the
    numeric field within it (e.g. "percentage").
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
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
                "filter": f'{filter_field}.sample_time.physical_time >= "{cutoff}"',
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Google Health returned non-object JSON for %s", data_type)
            return None
        points = data.get("dataPoints") or []
    except requests.RequestException as exc:
        logger.warning("Could not fetch Google Health %s: %s", data_type, exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Google Health returned invalid JSON for %s: %s", data_type, exc)
        return None

    best_time: str = ""
    best_value: float | None = None
    found = False
    for point in points:
        if not isinstance(point, dict):
            continue
        payload = point.get(point_key)
        if not isinstance(payload, dict):
            continue
        value = payload.get(value_field)
        if value is None:
            continue
        sample_time_obj = payload.get("sampleTime")
        sample_time_raw: str | None = (
            sample_time_obj.get("physicalTime")
            if isinstance(sample_time_obj, dict)
            else None
        )
        sample_time = sample_time_raw or ""
        if best_value is None or sample_time > best_time:
            try:
                best_value = float(value)
            except (TypeError, ValueError):
                continue
            best_time = sample_time
            found = True
    if not found or best_value is None:
        return None
    return best_value, best_time or None


def fetch_body_metrics(access_token: str) -> dict[str, Any] | None:
    """Read the latest weight (kg), body fat (%), resting hr, and hrv from Google Health, or None.

    The result is a ``BodyMetricsReading``: a plain metrics dict that also
    exposes the provider sample time of each metric as ``sample_times``.
    """
    weight = _latest_sample(access_token, "weight", "weight", "weightGrams")
    fat = _latest_sample(access_token, "body-fat", "bodyFat", "percentage")
    resting_hr = _latest_sample(
        access_token, "resting-heart-rate", "restingHeartRate", "beatsPerMinute"
    )
    hrv = _latest_sample(
        access_token, "heart-rate-variability", "heartRateVariability", "rmssd"
    )
    metrics: dict[str, Any] = {}
    sample_times: dict[str, str] = {}
    for key, sample, convert in (
        ("weight_kg", weight, lambda grams: round(grams / 1000.0, 2)),
        ("body_fat_pct", fat, lambda value: value),
        ("resting_hr", resting_hr, lambda value: value),
        ("hrv", hrv, lambda value: value),
    ):
        if sample is None:
            continue
        metrics[key] = convert(sample[0])
        if sample[1]:
            sample_times[key] = sample[1]
    return BodyMetricsReading(metrics, sample_times) if metrics else None


def _resolve_refresh_token(
    vault: SecretVault,
    user_id: str,
    env_refresh_token: str | None,
) -> tuple[str | None, ResolvedCredential | None]:
    """Resolve the owner's vaulted token, falling back only when unconfigured."""
    try:
        credential = vault.resolve(user_id, "google_health", purpose="health_sync")
    except CredentialUnavailableError as exc:
        if exc.reason is not UnavailableReason.NOT_CONFIGURED:
            logger.warning(
                "Google Health credentials are unavailable (%s).",
                exc.reason.value,
            )
            return None, None
        return env_refresh_token, None

    token = (
        credential.refresh_token.reveal()
        if credential.refresh_token is not None
        else None
    )
    return token or env_refresh_token, credential


def _persist_refresh_token(
    vault: SecretVault,
    user_id: str,
    credential: ResolvedCredential | None,
    new_refresh_token: str,
) -> None:
    """Re-seal a rotated OAuth token for the same owner (never another user)."""
    if (
        credential is not None
        and credential.refresh_token is not None
        and credential.refresh_token.reveal() == new_refresh_token
    ):
        return
    try:
        if credential is not None:
            vault.rotate(
                user_id,
                "google_health",
                refresh_token=new_refresh_token,
                actor="system",
                source="oauth_refresh",
            )
        else:
            try:
                vault.store(
                    user_id,
                    "google_health",
                    refresh_token=new_refresh_token,
                    actor="system",
                    source="oauth_refresh",
                )
            except CredentialExistsError:
                vault.rotate(
                    user_id,
                    "google_health",
                    refresh_token=new_refresh_token,
                    actor="system",
                    source="oauth_refresh",
                )
    except (VaultError, ValueError):
        # Never log the token; a failed rotation leaves the previous credential
        # in place and the next run will simply retry.
        logger.warning("Could not persist a rotated Google Health refresh token.")


def _resolve_refresh_token(
    vault: SecretVault,
    user_id: str,
    env_refresh_token: str | None,
) -> tuple[str | None, ResolvedCredential | None]:
    """Resolve the owner's vaulted token, falling back only when unconfigured."""
    try:
        credential = vault.resolve(user_id, "google_health", purpose="health_sync")
    except CredentialUnavailableError as exc:
        if exc.reason is not UnavailableReason.NOT_CONFIGURED:
            logger.warning(
                "Google Health credentials are unavailable (%s).",
                exc.reason.value,
            )
            return None, None
        return env_refresh_token, None

    token = (
        credential.refresh_token.reveal()
        if credential.refresh_token is not None
        else None
    )
    return token or env_refresh_token, credential


def _persist_refresh_token(
    vault: SecretVault,
    user_id: str,
    credential: ResolvedCredential | None,
    new_refresh_token: str,
) -> None:
    """Re-seal a rotated OAuth token for the same owner (never another user)."""
    if (
        credential is not None
        and credential.refresh_token is not None
        and credential.refresh_token.reveal() == new_refresh_token
    ):
        return
    try:
        if credential is not None:
            vault.rotate(
                user_id,
                "google_health",
                refresh_token=new_refresh_token,
                actor="system",
                source="oauth_refresh",
            )
        else:
            try:
                vault.store(
                    user_id,
                    "google_health",
                    refresh_token=new_refresh_token,
                    actor="system",
                    source="oauth_refresh",
                )
            except CredentialExistsError:
                vault.rotate(
                    user_id,
                    "google_health",
                    refresh_token=new_refresh_token,
                    actor="system",
                    source="oauth_refresh",
                )
    except (VaultError, ValueError):
        # Never log the token; a failed rotation leaves the previous credential
        # in place and the next run will simply retry.
        logger.warning("Could not persist a rotated Google Health refresh token.")


def sync_body_metrics(
    client_id: str | None,
    client_secret: str | None,
    env_refresh_token: str | None,
    db_path: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Pull the latest body composition from Google Health, or None if disabled.

    Resolves the refresh token for the exact owning user through the secret
    vault and persists any rotation back under that same user. The environment
    token is only a legacy fallback when no vault credential is configured.

    The owner is resolved with :func:`database.require_tenant`, so outside the
    explicit non-production local mode a call without a user is refused rather
    than silently acting as a shared tenant.
    """
    if not (client_id and client_secret):
        return None

    if user_id is None:
        user_id = require_tenant(user_id, db_path)
    owner = OwnerIdentity(user_id)
    vault = SecretVault(db_path)
    refresh_token, credential = _resolve_refresh_token(
        vault, owner.user_id, env_refresh_token
    )
    if not refresh_token:
        return None
    tokens = _refresh_tokens(client_id, client_secret, refresh_token)
    if tokens is None and env_refresh_token and refresh_token != env_refresh_token:
        # A legacy stored token may have been invalidated; try the env one.
        tokens = _refresh_tokens(client_id, client_secret, env_refresh_token)
    if tokens is None:
        return None

    access, new_refresh = tokens
    _persist_refresh_token(vault, owner.user_id, credential, new_refresh)
    return fetch_body_metrics(access)
