"""Tests for the Hevy credential verification gate.

The gate runs before a Hevy key is stored or activated: it proves the key
identifies an account and carries the minimum access the product needs, using
bounded requests. These tests never reach the real Hevy API.
"""

from __future__ import annotations

import logging
from typing import Any

import hevy_client
import pytest
import requests
from database import (
    delete_hevy_connection,
    get_hevy_connection,
    init_db,
    save_hevy_connection,
)
from hevy_connection import (
    HevyCredentialState,
    verify_hevy_credentials,
)
from owned_rows import ensure_users

# ---------------------------------------------------------------------------
# Test doubles
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


class _Recorder:
    """Replacement for ``requests.get`` that records every call."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int | None = None,
        allow_redirects: bool = True,
    ) -> _FakeResponse:
        path = url.split("/v1/", 1)[-1]
        self.calls.append(
            {
                "path": path,
                "headers": headers or {},
                "params": params,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
            }
        )
        result = self._responses.get(path, _FakeResponse(404, {}))
        if isinstance(result, Exception):
            raise result
        return result


def _successful_responses() -> dict[str, Any]:
    return {
        "user/info": _FakeResponse(200, {"id": "acct-1", "username": "lifter42"}),
        "workouts": _FakeResponse(
            200,
            {"workouts": [{"id": "w-1", "title": "Push Day"}], "page_count": 4},
        ),
        "routines": _FakeResponse(200, {"routines": [], "page_count": 1}),
        "exercise_templates": _FakeResponse(200, {"exercise_templates": [], "page_count": 1}),
        "workouts/count": _FakeResponse(200, {"workout_count": 42}),
    }


def _install(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> _Recorder:
    recorder = _Recorder(responses)
    monkeypatch.setattr(hevy_client.requests, "get", recorder)
    return recorder


# ---------------------------------------------------------------------------
# Verification outcomes
# ---------------------------------------------------------------------------


def test_successful_verification_records_identity_time_version_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _successful_responses())

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.CONNECTED
    assert result.connected is True
    assert result.account_username == "lifter42"
    assert result.account_id == "acct-1"
    assert result.api_version == "v1"
    assert result.workout_count == 42
    assert result.capabilities["workouts"] is True
    assert result.tested_at.tzinfo is not None

    metadata = result.as_public_metadata()
    assert metadata["state"] == "connected"
    assert metadata["account_username"] == "lifter42"
    assert metadata["api_version"] == "v1"
    assert metadata["tested_at"]
    assert metadata["capabilities"]["workouts"] is True


def test_verification_uses_bounded_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _install(monkeypatch, _successful_responses())

    verify_hevy_credentials("synthetic-hevy-key")

    # A bounded check asks for a single item per collection and never pages.
    assert recorder.calls
    assert len(recorder.calls) <= 5
    for call in recorder.calls:
        assert call["timeout"] is not None
        assert call["timeout"] <= hevy_client.CREDENTIAL_TEST_TIMEOUT
        if call["path"] in ("user/info", "workouts/count"):
            assert call["params"] is None
        else:
            assert call["params"] == {"page": 1, "pageSize": 1}


def test_rejected_key_is_reported_as_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(401, {"error": "unauthorized"})
    recorder = _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-bad-key")

    assert result.state is HevyCredentialState.INVALID
    assert result.connected is False
    assert "rejected" in result.detail.lower()
    # Fail fast: no further requests are made once the key is refused.
    assert [call["path"] for call in recorder.calls] == ["user/info"]


def test_missing_workout_access_is_reported_as_insufficient_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["workouts"] = _FakeResponse(403, {"error": "forbidden"})
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-limited-key")

    assert result.state is HevyCredentialState.INSUFFICIENT_SCOPE
    assert result.connected is False
    assert result.account_username == "lifter42"


def test_unreachable_hevy_is_unverified_not_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["user/info"] = requests.RequestException("connection reset")
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.UNVERIFIED
    assert result.connected is False


def test_redirect_answer_is_not_followed_and_never_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redirect answer must not be followed: the transport would resend the
    # credential to the redirect target, and it is not a verified account.
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(
        302,
        {"username": "lifter42"},
        headers={"location": "https://example.invalid/collect"},
    )
    recorder = _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.UNVERIFIED
    assert result.connected is False
    assert result.account_username is None
    assert all(call["allow_redirects"] is False for call in recorder.calls)
    assert [call["path"] for call in recorder.calls] == ["user/info"]


def test_hevy_server_error_is_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = _successful_responses()
    responses["workouts"] = _FakeResponse(503, {"error": "unavailable"})
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.UNVERIFIED
    assert result.connected is False


def test_empty_key_is_invalid_without_calling_hevy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _install(monkeypatch, _successful_responses())

    result = verify_hevy_credentials("   ")

    assert result.state is HevyCredentialState.INVALID
    assert recorder.calls == []


def test_unreadable_identity_body_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(200, ValueError("not json"))
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.UNVERIFIED


def test_api_version_header_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = _successful_responses()
    responses["user/info"] = _FakeResponse(
        200,
        {"id": "acct-1", "username": "lifter42"},
        headers={"x-api-version": "2024-11"},
    )
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.api_version == "2024-11"


def test_optional_capability_failure_does_not_fail_the_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _successful_responses()
    responses["routines"] = _FakeResponse(403, {"error": "forbidden"})
    _install(monkeypatch, responses)

    result = verify_hevy_credentials("synthetic-hevy-key")

    assert result.state is HevyCredentialState.CONNECTED
    assert result.capabilities["routines"] is False
    assert result.capabilities["workouts"] is True


def test_credential_is_never_logged_or_returned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-secret-hevy-key"
    _install(monkeypatch, _successful_responses())

    with caplog.at_level(logging.DEBUG):
        result = verify_hevy_credentials(secret)

    assert secret not in caplog.text
    assert secret not in str(result.as_public_metadata())
    assert secret not in repr(result)


@pytest.mark.parametrize(
    "candidate",
    [
        "synthetic-secret-hevy-key\nwith-newline",
        "synthetic-secret-hevy-key\rwith-carriage-return",
        "synthetic-secret-hevy-key\twith-tab",
        "synthetic-secret-hevy-key\x00with-null",
        "synthetic-secret-hevy-key-with-euro-\u20ac",
    ],
)
def test_unusable_credential_is_rejected_without_being_transmitted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    candidate: str,
) -> None:
    recorder = _install(monkeypatch, _successful_responses())

    with caplog.at_level(logging.DEBUG):
        result = verify_hevy_credentials(candidate)

    assert result.state is HevyCredentialState.INVALID
    assert result.connected is False
    # The credential is never put on the wire.
    assert recorder.calls == []
    # ...and it never reaches the log either.
    assert candidate not in caplog.text
    assert "synthetic-secret-hevy-key" not in caplog.text


def test_transport_error_does_not_leak_the_credential_into_the_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-secret-hevy-key"
    responses = _successful_responses()
    responses["user/info"] = hevy_client.requests.exceptions.InvalidHeader(
        f"Invalid leading whitespace, reserved character(s), or return "
        f"character(s) in header value: {secret!r}"
    )
    _install(monkeypatch, responses)

    with caplog.at_level(logging.DEBUG):
        result = verify_hevy_credentials(secret)

    assert result.state is HevyCredentialState.UNVERIFIED
    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Connection metadata persistence
# ---------------------------------------------------------------------------


def test_connection_metadata_round_trip(tmp_path: Any) -> None:
    db = str(tmp_path / "hevy.db")
    init_db(db)
    ensure_users(db, "user-a")

    save_hevy_connection(
        "user-a",
        state="connected",
        api_version="v1",
        capabilities={"workouts": True, "routines": False},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        account_id="acct-1",
        db_path=db,
    )

    stored = get_hevy_connection("user-a", db)
    assert stored is not None
    assert stored["state"] == "connected"
    assert stored["account_username"] == "lifter42"
    assert stored["account_id"] == "acct-1"
    assert stored["api_version"] == "v1"
    assert stored["capabilities"] == {"workouts": True, "routines": False}
    assert stored["tested_at"] == "2026-11-05T10:00:00+00:00"


def test_connection_metadata_is_tenant_scoped(tmp_path: Any) -> None:
    db = str(tmp_path / "hevy.db")
    init_db(db)
    ensure_users(db, "user-a", "user-b")

    save_hevy_connection(
        "user-a",
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        db_path=db,
    )

    assert get_hevy_connection("user-b", db) is None
    assert get_hevy_connection("user-a", db) is not None


def test_connection_metadata_is_replaced_not_duplicated(tmp_path: Any) -> None:
    db = str(tmp_path / "hevy.db")
    init_db(db)
    ensure_users(db, "user-a")

    save_hevy_connection(
        "user-a",
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        account_username="lifter42",
        db_path=db,
    )
    save_hevy_connection(
        "user-a",
        state="unverified",
        api_version="v1",
        capabilities={},
        tested_at="2026-11-06T10:00:00+00:00",
        db_path=db,
    )

    stored = get_hevy_connection("user-a", db)
    assert stored is not None
    assert stored["state"] == "unverified"
    assert stored["tested_at"] == "2026-11-06T10:00:00+00:00"


def test_delete_connection_metadata(tmp_path: Any) -> None:
    db = str(tmp_path / "hevy.db")
    init_db(db)
    ensure_users(db, "user-a")

    save_hevy_connection(
        "user-a",
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-11-05T10:00:00+00:00",
        db_path=db,
    )
    delete_hevy_connection("user-a", db)

    assert get_hevy_connection("user-a", db) is None
