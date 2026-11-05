"""Tests for hevy_client — the thin REST wrapper for the Hevy API.

All network calls are mocked; no test may hit a real external API.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import hevy_client
import pytest

# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------


def test_headers_contains_api_key() -> None:
    headers = hevy_client._headers("sk-test-123")
    assert headers["api-key"] == "sk-test-123"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# fetch_latest_workout
# ---------------------------------------------------------------------------


def test_fetch_latest_workout_success() -> None:
    workout = {"workouts": [{"id": "W1", "title": "Push Day"}]}
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = workout
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.fetch_latest_workout("fake-key")
        assert result == workout
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert call_args[1]["params"] == {"page": 1, "pageSize": 1}


def test_fetch_latest_workout_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.fetch_latest_workout("fake-key")
        assert result is None


def test_fetch_latest_workout_invalid_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.side_effect = ValueError("bad json")
        result = hevy_client.fetch_latest_workout("fake-key")
        assert result is None


# ---------------------------------------------------------------------------
# get_workout_count
# ---------------------------------------------------------------------------


def test_get_workout_count_success() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"workout_count": 42}
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_workout_count("fake-key")
        assert result == 42


def test_get_workout_count_none_value() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {}
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_workout_count("fake-key")
        assert result is None


def test_get_workout_count_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.get_workout_count("fake-key")
        assert result is None


# ---------------------------------------------------------------------------
# get_user_info
# ---------------------------------------------------------------------------


def test_get_user_info_success() -> None:
    profile = {"username": "lifter42", "id": "user-1"}
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = profile
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_user_info("fake-key")
        assert result == profile


def test_get_user_info_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.get_user_info("fake-key")
        assert result is None


def test_get_user_info_invalid_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.side_effect = ValueError("bad json")
        result = hevy_client.get_user_info("fake-key")
        assert result is None


# ---------------------------------------------------------------------------
# _get_all_pages / get_all_workouts / get_routines / get_exercise_templates
# ---------------------------------------------------------------------------


def test_get_all_workouts_single_page() -> None:
    workouts = [{"id": "W1"}, {"id": "W2"}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "workouts": workouts,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_all_workouts("fake-key")
        assert result == workouts


def test_get_all_workouts_multi_page() -> None:
    page1 = [{"id": "W1"}]
    page2 = [{"id": "W2"}]

    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.side_effect = [
            {"workouts": page1, "page_count": 2},
            {"workouts": page2, "page_count": 2},
        ]

        result = hevy_client.get_all_workouts("fake-key")
        assert result == [{"id": "W1"}, {"id": "W2"}]
        assert mock_get.call_count == 2


def test_get_routines_success() -> None:
    routines = [{"id": "R1", "title": "Push Day"}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "routines": routines,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_routines("fake-key")
        assert result == routines


def test_get_routine_folders_success() -> None:
    folders = [{"id": 1, "title": "My Folder"}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "routine_folders": folders,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_routine_folders("fake-key")
        assert result == folders


def test_get_all_workouts_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.get_all_workouts("fake-key")
        assert result is None


def test_get_all_workouts_invalid_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.side_effect = ValueError("bad json")
        result = hevy_client.get_all_workouts("fake-key")
        assert result is None


def test_get_exercise_templates_success() -> None:
    templates = [{"id": "001", "title": "Bench Press", "primary_muscle_group": "chest"}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "exercise_templates": templates,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_exercise_templates("fake-key")
        assert result == templates
        # verify page_size=100 is used
        call_args = mock_get.call_args
        assert call_args[1]["params"]["pageSize"] == 100


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int,
        payload: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_probe_returns_payload_and_status() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(200, {"username": "lifter42"})
        result = hevy_client.probe("fake-key", "user/info")
        assert result.ok is True
        assert result.status_code == 200
        assert result.payload == {"username": "lifter42"}
        assert result.api_version == "v1"


def test_probe_sends_the_key_as_a_header_only() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(200, {})
        hevy_client.probe("fake-key", "workouts", params={"page": 1, "pageSize": 1})
        call_args = mock_get.call_args
        assert call_args[1]["headers"]["api-key"] == "fake-key"
        assert "fake-key" not in call_args[0][0]
        assert call_args[1]["params"] == {"page": 1, "pageSize": 1}
        assert call_args[1]["timeout"] == hevy_client.CREDENTIAL_TEST_TIMEOUT


def test_probe_reports_a_refused_credential() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(401, {"error": "unauthorized"})
        result = hevy_client.probe("fake-key", "user/info")
        assert result.ok is False
        assert result.rejected is True
        assert result.status_code == 401
        assert result.payload is None


def test_probe_never_follows_a_redirect() -> None:
    # The transport keeps custom headers such as ``api-key`` when it follows
    # a redirect to another host, so a redirect answer must not be followed.
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(
            302,
            {"username": "lifter42"},
            headers={"location": "https://example.invalid/collect"},
        )
        result = hevy_client.probe("fake-key", "user/info")

        assert mock_get.call_args[1]["allow_redirects"] is False

    assert result.ok is False
    assert result.rejected is False
    assert result.status_code == 302
    assert result.payload is None


def test_probe_reports_a_transport_failure_without_a_status() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.probe("fake-key", "user/info")
        assert result.status_code is None
        assert result.ok is False
        assert result.rejected is False
        assert result.payload is None


def test_probe_rejects_a_non_object_body() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(200, [1, 2, 3])
        result = hevy_client.probe("fake-key", "user/info")
        assert result.status_code == 200
        assert result.payload is None


def test_probe_reports_the_api_version_header() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value = _FakeResponse(
            200,
            {},
            headers={"x-api-version": "2024-11"},
        )
        result = hevy_client.probe("fake-key", "user/info")
        assert result.api_version == "2024-11"


def test_probe_never_logs_the_credential_from_a_transport_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-secret-hevy-key"
    # The transport quotes the offending header value back in its message.
    error = hevy_client.requests.exceptions.InvalidHeader(
        f"Invalid leading whitespace, reserved character(s), or return "
        f"character(s) in header value: {secret!r}"
    )
    with (
        patch("hevy_client.requests.get", side_effect=error),
        caplog.at_level(logging.DEBUG),
    ):
        result = hevy_client.probe(secret, "user/info")

    assert result.status_code is None
    assert result.ok is False
    assert secret not in caplog.text
    assert secret not in repr(result)


def test_probe_survives_a_non_request_exception_from_the_transport(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Header encoding failures are plain ValueError subclasses, not
    # RequestException instances.
    secret = "synthetic-secret-hevy-key"
    with (
        patch(
            "hevy_client.requests.get",
            side_effect=UnicodeEncodeError("latin-1", secret, 0, 1, "not latin-1"),
        ),
        caplog.at_level(logging.DEBUG),
    ):
        result = hevy_client.probe(secret, "user/info")

    assert result.status_code is None
    assert result.ok is False
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "with-newline\nkey",
        "with-carriage-return\rkey",
        "with-tab\tkey",
        "with-null\x00key",
        "non-ascii-\u20ac",
        "",
        "x" * (hevy_client.MAX_CREDENTIAL_LENGTH + 1),
    ],
)
def test_is_transmittable_credential_rejects_unusable_values(value: str) -> None:
    assert hevy_client.is_transmittable_credential(value) is False


@pytest.mark.parametrize(
    "value",
    ["a", "synthetic-hevy-key", "with space", "0123456789abcdef" * 8],
)
def test_is_transmittable_credential_accepts_usable_values(value: str) -> None:
    assert hevy_client.is_transmittable_credential(value) is True


# ---------------------------------------------------------------------------
# get_recent_workouts
# ---------------------------------------------------------------------------


def test_get_recent_workouts_single_page() -> None:
    workouts = [{"id": "W1"}, {"id": "W2"}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "workouts": workouts,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_recent_workouts("fake-key", limit=5)
        assert result == workouts


def test_get_recent_workouts_caps_limit() -> None:
    workouts = [{"id": f"W{i}"} for i in range(10)]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {
            "workouts": workouts,
            "page_count": 1,
        }
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_recent_workouts("fake-key", limit=3)
        assert result == workouts[:3]


def test_get_recent_workouts_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.get_recent_workouts("fake-key")
        assert result is None


# ---------------------------------------------------------------------------
# create_routine / update_routine / create_routine_folder
# ---------------------------------------------------------------------------


def test_create_routine_success() -> None:
    created = {"id": "R-new", "title": "My Routine"}
    payload = {"routine": {"title": "My Routine", "exercises": []}}
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.return_value.json.return_value = created
        mock_post.return_value.raise_for_status.return_value = None
        result = hevy_client.create_routine("fake-key", payload)
        assert result == created
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["json"] == payload


def test_create_routine_network_error() -> None:
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.create_routine("fake-key", {})
        assert result is None


def test_create_routine_invalid_json() -> None:
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.side_effect = ValueError("bad json")
        result = hevy_client.create_routine("fake-key", {})
        assert result is None


def test_update_routine_success() -> None:
    updated = {"id": "R1", "title": "Updated Routine"}
    payload = {"routine": {"title": "Updated Routine"}}
    with patch("hevy_client.requests.put") as mock_put:
        mock_put.return_value.json.return_value = updated
        mock_put.return_value.raise_for_status.return_value = None
        result = hevy_client.update_routine("fake-key", "R1", payload)
        assert result == updated
        mock_put.assert_called_once()


def test_create_routine_folder_success() -> None:
    created = {"id": 5, "title": "New Folder"}
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.return_value.json.return_value = created
        mock_post.return_value.raise_for_status.return_value = None
        result = hevy_client.create_routine_folder("fake-key", "New Folder")
        assert result == created
        call_json = mock_post.call_args[1]["json"]
        assert call_json == {"routine_folder": {"title": "New Folder"}}


def test_create_routine_folder_network_error() -> None:
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.create_routine_folder("fake-key", "My Folder")
        assert result is None


# ---------------------------------------------------------------------------
# get_exercise_history
# ---------------------------------------------------------------------------


def test_get_exercise_history_success() -> None:
    history = [{"weight_kg": 80, "reps": 10}]
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"exercise_history": history}
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_exercise_history("fake-key", "001")
        assert result == history


def test_get_exercise_history_with_date_filter() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.json.return_value = {"exercise_history": []}
        mock_get.return_value.raise_for_status.return_value = None
        result = hevy_client.get_exercise_history(
            "fake-key",
            "001",
            start_date="2025-01-01",
        )
        assert result == []
        assert mock_get.call_args[1]["params"] == {"start_date": "2025-01-01"}


def test_get_exercise_history_network_error() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.side_effect = hevy_client.requests.RequestException("timeout")
        result = hevy_client.get_exercise_history("fake-key", "001")
        assert result is None


def test_get_all_workouts_non_dict_json() -> None:
    """Regression: non-object JSON (e.g. a list) must return None, not crash."""
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.get_all_workouts("fake-key")
        assert result is None


def test_fetch_latest_workout_non_dict_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.fetch_latest_workout("fake-key")
        assert result is None


def test_get_workout_count_non_dict_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.get_workout_count("fake-key")
        assert result is None


def test_get_user_info_non_dict_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = "just a string"
        result = hevy_client.get_user_info("fake-key")
        assert result is None


def test_get_recent_workouts_non_dict_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.get_recent_workouts("fake-key")
        assert result is None


def test_create_routine_non_dict_json() -> None:
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.create_routine("fake-key", {})
        assert result is None


def test_create_routine_folder_non_dict_json() -> None:
    with patch("hevy_client.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.return_value = None
        mock_post.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.create_routine_folder("fake-key", "Test")
        assert result is None


def test_update_routine_non_dict_json() -> None:
    with patch("hevy_client.requests.put") as mock_put:
        mock_put.return_value.raise_for_status.return_value = None
        mock_put.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.update_routine("fake-key", "R1", {})
        assert result is None


def test_get_exercise_history_non_dict_json() -> None:
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = [1, 2, 3]
        result = hevy_client.get_exercise_history("fake-key", "001")
        assert result is None


# ---------------------------------------------------------------------------
# non-list collection value regression tests
# ---------------------------------------------------------------------------


def test_get_all_workouts_non_list_workouts() -> None:
    """Regression: non-list 'workouts' value must return None, not crash."""
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "workouts": 42,
            "page_count": 1,
        }
        result = hevy_client.get_all_workouts("fake-key")
        assert result is None


def test_get_recent_workouts_non_list_workouts() -> None:
    """Regression: non-list 'workouts' value must return None, not crash."""
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "workouts": "not-a-list",
            "page_count": 1,
        }
        result = hevy_client.get_recent_workouts("fake-key")
        assert result is None


def test_get_routines_non_list_collection() -> None:
    """Regression: non-list 'routines' value must return None, not crash."""
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "routines": None,
            "page_count": 1,
        }
        result = hevy_client.get_routines("fake-key")
        assert result is None


def test_get_exercise_templates_non_list_collection() -> None:
    """Regression: non-list 'exercise_templates' value must return None, not crash."""
    with patch("hevy_client.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.json.return_value = {
            "exercise_templates": {"id": "001"},
            "page_count": 1,
        }
        result = hevy_client.get_exercise_templates("fake-key")
        assert result is None
