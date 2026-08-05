"""Tests for hevy_client — the thin REST wrapper for the Hevy API.

All network calls are mocked; no test may hit a real external API.
"""

from __future__ import annotations

from unittest.mock import patch

import hevy_client

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
