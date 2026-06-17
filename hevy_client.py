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


def _headers(api_key: str) -> dict[str, str]:
    return {
        "api-key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


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


def _get_all_pages(
    api_key: str, path: str, collection_key: str, page_size: int
) -> list[dict[str, Any]] | None:
    """Fetch every page of a paginated Hevy collection, or None on failure."""
    url = f"{BASE_URL}/{path}"
    items: list[dict[str, Any]] = []
    page = 1
    try:
        while True:
            response = requests.get(
                url,
                headers=_headers(api_key),
                params={"page": page, "pageSize": page_size},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            items.extend(data.get(collection_key, []))
            if page >= int(data.get("page_count", 1)):
                break
            page += 1
        return items
    except requests.RequestException as exc:
        logger.warning("Could not list %s from Hevy: %s", collection_key, exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Hevy returned invalid JSON for %s: %s", collection_key, exc)
        return None


def get_routines(api_key: str) -> list[dict[str, Any]] | None:
    """Return all routines on the account, or None on failure."""
    return _get_all_pages(api_key, "routines", "routines", page_size=10)


def get_routine_folders(api_key: str) -> list[dict[str, Any]] | None:
    """Return all routine folders on the account, or None on failure."""
    return _get_all_pages(api_key, "routine_folders", "routine_folders", page_size=10)


def create_routine_folder(api_key: str, title: str) -> dict[str, Any] | None:
    """Create a routine folder and return it, or None on failure."""
    url = f"{BASE_URL}/routine_folders"
    body = {"routine_folder": {"title": title}}
    try:
        response = requests.post(
            url, headers=_headers(api_key), json=body, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("Could not create routine folder '%s': %s", title, exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON creating a folder: %s", exc)
        return None


def create_routine(api_key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Create a routine from a PostRoutinesRequestBody payload, or None."""
    url = f"{BASE_URL}/routines"
    try:
        response = requests.post(
            url, headers=_headers(api_key), json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("Could not create routine: %s", exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON creating a routine: %s", exc)
        return None


def update_routine(
    api_key: str, routine_id: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Update a routine from a PutRoutinesRequestBody payload, or None."""
    url = f"{BASE_URL}/routines/{routine_id}"
    try:
        response = requests.put(
            url, headers=_headers(api_key), json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("Could not update routine %s: %s", routine_id, exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON updating a routine: %s", exc)
        return None


def get_exercise_history(
    api_key: str, template_id: str, start_date: str | None = None
) -> list[dict[str, Any]] | None:
    """Return logged history entries for an exercise template, or None.

    Each entry includes weight_kg, reps and the workout start time. Optionally
    bounded by an ISO 8601 start_date.
    """
    url = f"{BASE_URL}/exercise_history/{template_id}"
    params: dict[str, Any] = {}
    if start_date:
        params["start_date"] = start_date
    try:
        response = requests.get(
            url, headers=_headers(api_key), params=params, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json().get("exercise_history", [])
    except requests.RequestException as exc:
        logger.warning(
            "Could not fetch exercise history for %s: %s", template_id, exc
        )
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON for exercise history: %s", exc)
        return None


def get_workout_count(api_key: str) -> int | None:
    """Return the total number of logged workouts on the account, or None."""
    url = f"{BASE_URL}/workouts/count"
    try:
        response = requests.get(
            url, headers=_headers(api_key), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        count = response.json().get("workout_count")
        return int(count) if count is not None else None
    except requests.RequestException as exc:
        logger.warning("Could not fetch workout count from Hevy: %s", exc)
        return None
    except (ValueError, TypeError) as exc:
        logger.warning("Hevy returned an invalid workout count: %s", exc)
        return None


