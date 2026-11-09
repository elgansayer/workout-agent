"""Client for the Hevy workout-logging API.

Docs: https://api.hevyapp.com/docs/

Two call styles are exposed on purpose:

* ``*_strict`` helpers raise a classified :class:`HevyError` so callers can
  distinguish an expired credential from a rate limit or an outage;
* the legacy helpers keep returning ``None`` on failure so existing callers
  are unaffected.

Nothing here logs or returns the API key.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hevyapp.com/v1"
REQUEST_TIMEOUT = 15  # seconds
# Credential checks must fail fast: they run inside a user-facing request.
CREDENTIAL_TEST_TIMEOUT = 10  # seconds
# The version segment of the API base URL, used when the API does not report
# its own version in a response header.
API_VERSION = "v1"
_VERSION_HEADERS = ("x-api-version", "api-version", "x-hevy-api-version")
# An API key is an opaque token. Only printable ASCII can be placed in an
# HTTP header value, and the transport quotes the offending value back in some
# of its error messages, so anything else is refused before a request is made.
MAX_CREDENTIAL_LENGTH = 512
_CREDENTIAL_PATTERN = re.compile(r"\A[\x21-\x7e][\x20-\x7e]*\Z")


def is_transmittable_credential(value: str) -> bool:
    """True when the value can be sent as an HTTP header value.

    Control characters and non-ASCII bytes either break the header framing or
    make the transport raise an error whose message quotes the credential
    back. Such a value is never a usable key, so it is rejected up front
    instead of being transmitted and reported.
    """
    if not value or len(value) > MAX_CREDENTIAL_LENGTH:
        return False
    return bool(_CREDENTIAL_PATTERN.match(value))


def _redact_credential(message: str, api_key: str) -> str:
    """Remove the credential from a transport error message before logging.

    The transport echoes the offending header value in some of its errors, so
    the raw message must never reach the log.
    """
    if not api_key:
        return message
    # The transport usually quotes the value with repr(), which escapes
    # control characters, so both the escaped and the raw form are removed.
    escaped = repr(api_key)[1:-1]
    pattern = re.escape(api_key)
    if escaped and escaped != api_key:
        pattern = f"(?:{re.escape(escaped)}|{re.escape(api_key)})"
    # A single pass keeps the replacement marker from being matched again.
    return re.sub(pattern, "[redacted]", message)


class HevyError(RuntimeError):
    """Base class for classified Hevy API failures."""

    status_code: int | None = None

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HevyAuthError(HevyError):
    """The provider rejected the credential (401/403)."""


class HevyRateLimitError(HevyError):
    """The provider refused the request because of rate limiting (429)."""


class HevyUnavailableError(HevyError):
    """The provider was unreachable or returned a server error."""


class HevyPayloadError(HevyError):
    """The provider returned an unusable payload for the requested resource."""


def _headers(api_key: str) -> dict[str, str]:
    return {
        "api-key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _classify_response_error(status: int | None, resource: str) -> HevyError:
    """Map an HTTP status code onto a classified provider error."""

    if status in (401, 403):
        return HevyAuthError(
            f"Hevy rejected the API key while reading {resource}.",
            status_code=status,
        )
    if status == 429:
        return HevyRateLimitError(
            f"Hevy rate limit reached while reading {resource}.",
            status_code=status,
        )
    if status is not None and status >= 500:
        return HevyUnavailableError(
            f"Hevy returned a server error while reading {resource}.",
            status_code=status,
        )
    return HevyError(
        f"Hevy returned HTTP {status if status is not None else 'an error'} "
        f"while reading {resource}.",
        status_code=status,
    )


def _request_json(
    url: str,
    api_key: str,
    resource: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    """Perform a GET request and return decoded JSON, or raise HevyError."""

    try:
        response = requests.get(
            url,
            headers=_headers(api_key),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise _classify_response_error(status, resource) from exc
    except requests.RequestException as exc:
        raise HevyUnavailableError(
            f"Hevy could not be reached while reading {resource}."
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise HevyPayloadError(
            f"Hevy returned invalid JSON while reading {resource}."
        ) from exc



@dataclass(frozen=True, slots=True)
class HevyProbe:
    """Outcome of a single bounded request against the Hevy API.

    Only the request outcome is reported. The API key is transmitted as a
    request header and is never part of this record.
    """

    path: str
    status_code: int | None
    payload: dict[str, Any] | None
    api_version: str

    @property
    def ok(self) -> bool:
        """True when Hevy answered with a 2xx status."""
        return self.status_code is not None and 200 <= self.status_code < 300

    @property
    def rejected(self) -> bool:
        """True when Hevy refused the credential (401 or 403)."""
        return self.status_code in (401, 403)


def _api_version_from(headers: Any) -> str:
    """Return the API version reported by Hevy, or the configured default."""
    getter = getattr(headers, "get", None)
    if callable(getter):
        for name in _VERSION_HEADERS:
            try:
                value = getter(name)
            except Exception:  # noqa: BLE001 - headers must never break a probe
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return API_VERSION


def probe(
    api_key: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = CREDENTIAL_TEST_TIMEOUT,
) -> HevyProbe:
    """Perform one bounded GET request and report the outcome.

    The request is bounded by the caller (single item pages and a short
    timeout). Transport failures and unusable bodies are reported as
    ``status_code=None`` so callers can distinguish "could not verify" from
    "credential refused".

    Redirects are never followed: the transport keeps custom headers such as
    ``api-key`` when it follows a redirect to another host, so a redirect
    answer would hand the credential to a host the user never chose. A
    redirect is therefore reported as an unsuccessful probe.
    """
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        response = requests.get(
            url,
            headers=_headers(api_key),
            params=params,
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # The transport can quote the offending header value in its error
        # message, so the credential is removed from the text before the
        # message is logged.
        detail = _redact_credential(str(exc), api_key)
        logger.warning("Hevy request to %s failed: %s", path, detail)
        return HevyProbe(
            path=path,
            status_code=None,
            payload=None,
            api_version=API_VERSION,
        )
    except (UnicodeError, ValueError) as exc:
        # Header encoding failures (for example a non-latin-1 key) surface as
        # plain UnicodeError/ValueError subclasses, which are not
        # RequestException instances.
        detail = _redact_credential(str(exc), api_key)
        logger.warning("Hevy request to %s failed: %s", path, detail)
        return HevyProbe(
            path=path,
            status_code=None,
            payload=None,
            api_version=API_VERSION,
        )

    status_code = int(response.status_code)
    api_version = _api_version_from(getattr(response, "headers", None))
    if not 200 <= status_code < 300:
        # Includes redirects: a 3xx answer is not an API response and must
        # never be treated as a verified credential.
        logger.warning("Hevy request to %s returned HTTP %s", path, status_code)
        return HevyProbe(
            path=path,
            status_code=status_code,
            payload=None,
            api_version=api_version,
        )

    try:
        data = response.json()
    except ValueError:
        logger.warning("Hevy returned invalid JSON for %s", path)
        return HevyProbe(
            path=path,
            status_code=status_code,
            payload=None,
            api_version=api_version,
        )
    if not isinstance(data, dict):
        logger.warning("Hevy returned non-object JSON for %s", path)
        return HevyProbe(
            path=path,
            status_code=status_code,
            payload=None,
            api_version=api_version,
        )
    return HevyProbe(
        path=path,
        status_code=status_code,
        payload=data,
        api_version=api_version,
    )

def fetch_latest_workout(api_key: str) -> dict[str, Any] | None:
    """Return the most recent workout as parsed JSON, or None on failure."""
    url = f"{BASE_URL}/workouts"
    headers = {"api-key": api_key, "Accept": "application/json"}
    params = {"page": 1, "pageSize": 1}

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Hevy returned non-object JSON for workouts")
            return None
        return data
    except requests.RequestException as exc:
        logger.warning("Could not fetch latest workout from Hevy: %s", exc)
        return None
    except ValueError as exc:  # invalid JSON
        logger.warning("Hevy returned invalid JSON: %s", exc)
        return None


def get_all_workouts(api_key: str) -> list[dict[str, Any]] | None:
    """Return all logged workouts on the account, or None on failure."""
    return _get_all_pages(api_key, "workouts", "workouts", page_size=10)


def _get_all_pages_strict(
    api_key: str,
    path: str,
    collection_key: str,
    page_size: int,
) -> list[dict[str, Any]]:
    """Fetch every page of a paginated Hevy collection or raise HevyError."""

    url = f"{BASE_URL}/{path}"
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _request_json(
            url,
            api_key,
            collection_key,
            params={"page": page, "pageSize": page_size},
        )
        if not isinstance(data, dict):
            raise HevyPayloadError(
                f"Hevy returned non-object JSON for {collection_key}"
            )
        batch = data.get(collection_key, [])
        if not isinstance(batch, list):
            raise HevyPayloadError(
                f"Hevy returned non-list {collection_key}"
            )
        items.extend(batch)
        if page >= int(data.get("page_count", 1)):
            break
        page += 1
    return items


def _get_all_pages(
    api_key: str,
    path: str,
    collection_key: str,
    page_size: int,
) -> list[dict[str, Any]] | None:
    """Fetch every page of a paginated Hevy collection, or None on failure."""

    try:
        return _get_all_pages_strict(api_key, path, collection_key, page_size)
    except HevyError as exc:
        logger.warning("Could not list %s from Hevy: %s", collection_key, exc)
        return None


def get_routines(api_key: str) -> list[dict[str, Any]] | None:
    """Return all routines on the account, or None on failure."""
    return _get_all_pages(api_key, "routines", "routines", page_size=10)


def get_routines_strict(api_key: str) -> list[dict[str, Any]]:
    """Return all routines on the account, or raise a classified error."""
    return _get_all_pages_strict(api_key, "routines", "routines", page_size=10)


def get_routine_folders(api_key: str) -> list[dict[str, Any]] | None:
    """Return all routine folders on the account, or None on failure."""
    return _get_all_pages(api_key, "routine_folders", "routine_folders", page_size=10)


def get_routine_folders_strict(api_key: str) -> list[dict[str, Any]]:
    """Return all routine folders on the account, or raise a classified error."""
    return _get_all_pages_strict(
        api_key, "routine_folders", "routine_folders", page_size=10
    )


def create_routine_folder(api_key: str, title: str) -> dict[str, Any] | None:
    """Create a routine folder and return it, or None on failure."""
    url = f"{BASE_URL}/routine_folders"
    body = {"routine_folder": {"title": title}}
    try:
        response = requests.post(
            url,
            headers=_headers(api_key),
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Hevy returned non-object JSON creating a folder")
            return None
        return data
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
            url,
            headers=_headers(api_key),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Hevy returned non-object JSON creating a routine")
            return None
        return data
    except requests.RequestException as exc:
        logger.warning("Could not create routine: %s", exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON creating a routine: %s", exc)
        return None


def update_routine(
    api_key: str,
    routine_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Update a routine from a PutRoutinesRequestBody payload, or None."""
    url = f"{BASE_URL}/routines/{routine_id}"
    try:
        response = requests.put(
            url,
            headers=_headers(api_key),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Hevy returned non-object JSON updating a routine")
            return None
        return data
    except requests.RequestException as exc:
        logger.warning("Could not update routine %s: %s", routine_id, exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON updating a routine: %s", exc)
        return None


def get_exercise_history(
    api_key: str,
    template_id: str,
    start_date: str | None = None,
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
            url,
            headers=_headers(api_key),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            logger.warning("Hevy returned non-object JSON for exercise history")
            return None
        history: list[dict[str, Any]] = data.get("exercise_history", [])
        return history
    except requests.RequestException as exc:
        logger.warning("Could not fetch exercise history for %s: %s", template_id, exc)
        return None
    except ValueError as exc:
        logger.warning("Hevy returned invalid JSON for exercise history: %s", exc)
        return None


def get_workout_count(api_key: str) -> int | None:
    """Return the total number of logged workouts on the account, or None."""
    try:
        return get_workout_count_strict(api_key)
    except HevyError as exc:
        logger.warning("Could not fetch workout count from Hevy: %s", exc)
        return None


def get_workout_count_strict(api_key: str) -> int | None:
    """Return the total number of logged workouts, or raise a classified error."""

    url = f"{BASE_URL}/workouts/count"
    data = _request_json(url, api_key, "workout count")
    if not isinstance(data, dict):
        raise HevyPayloadError("Hevy returned non-object JSON for workout count")
    count = data.get("workout_count")
    try:
        return int(count) if count is not None else None
    except (TypeError, ValueError) as exc:
        raise HevyPayloadError(
            "Hevy returned an invalid workout count."
        ) from exc


def get_exercise_templates(api_key: str) -> list[dict[str, Any]] | None:
    """Return all exercise templates (built-in + custom), or None on failure.

    Each template has at least ``id``, ``title``, ``type``,
    ``primary_muscle_group``, and ``secondary_muscle_groups``.
    """
    return _get_all_pages(
        api_key, "exercise_templates", "exercise_templates", page_size=100
    )


def get_exercise_templates_strict(api_key: str) -> list[dict[str, Any]]:
    """Return all exercise templates, or raise a classified error."""

    return _get_all_pages_strict(
        api_key, "exercise_templates", "exercise_templates", page_size=100
    )


def get_user_info(api_key: str) -> dict[str, Any] | None:
    """Return the authenticated user's profile, or None on failure."""
    try:
        return get_user_info_strict(api_key)
    except HevyError as exc:
        logger.warning("Could not fetch user info from Hevy: %s", exc)
        return None


def get_user_info_strict(api_key: str) -> dict[str, Any]:
    """Return the authenticated user's profile, or raise a classified error."""

    url = f"{BASE_URL}/user/info"
    data = _request_json(url, api_key, "user info")
    if not isinstance(data, dict):
        raise HevyPayloadError("Hevy returned non-object JSON for user info")
    return data


def get_recent_workouts(api_key: str, limit: int = 10) -> list[dict[str, Any]] | None:
    """Return the N most recent workouts (most recent first), or None."""
    url = f"{BASE_URL}/workouts"
    items: list[dict[str, Any]] = []
    page = 1
    per_page = min(limit, 10)
    try:
        while len(items) < limit:
            data = _request_json(
                url,
                api_key,
                "workouts",
                params={"page": page, "pageSize": per_page},
            )
            if not isinstance(data, dict):
                raise HevyPayloadError(
                    "Hevy returned non-object JSON for workouts"
                )
            batch = data.get("workouts", [])
            if not isinstance(batch, list):
                raise HevyPayloadError("Hevy returned non-list workouts")
            items.extend(batch)
            if page >= int(data.get("page_count", 1)):
                break
            page += 1
        return items[:limit]
    except HevyError as exc:
        logger.warning("Could not fetch recent workouts from Hevy: %s", exc)
        return None


def get_recent_workouts_strict(api_key: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the N most recent workouts, or raise a classified error."""

    url = f"{BASE_URL}/workouts"
    items: list[dict[str, Any]] = []
    page = 1
    per_page = min(limit, 10)
    while len(items) < limit:
        data = _request_json(
            url,
            api_key,
            "workouts",
            params={"page": page, "pageSize": per_page},
        )
        if not isinstance(data, dict):
            raise HevyPayloadError("Hevy returned non-object JSON for workouts")
        batch = data.get("workouts", [])
        if not isinstance(batch, list):
            raise HevyPayloadError("Hevy returned non-list workouts")
        items.extend(batch)
        if page >= int(data.get("page_count", 1)):
            break
        page += 1
    return items[:limit]
