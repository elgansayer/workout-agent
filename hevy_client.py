"""Client for the Hevy workout-logging API.

Docs: https://api.hevyapp.com/docs/
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hevyapp.com/v1"
REQUEST_TIMEOUT = 15  # seconds


def fetch_latest_workout(api_key: str) -> dict[str, Any] | None:
    """Return the most recent workout as parsed JSON, or None on failure."""
    url = f"{BASE_URL}/workouts"
    headers = {"api-key": api_key, "Accept": "application/json"}
    params = {"page": 1, "pageSize": 1}

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("Could not fetch latest workout from Hevy: %s", exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Hevy returned invalid JSON: %s", exc)
        return None
