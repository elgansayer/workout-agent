"""End-to-end coverage of live readiness and Coach enforcement through the routes.

Each test drives the real FastAPI routes against a real SQLite database with
synthetic tenants.  Only the network edges (Hevy and the AI provider) are
replaced, so the readiness a surface reports and the Coach decision the chat
route enforces come from the same persisted, tenant-scoped state.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import database
import pytest
from connector_operations import (
    OPERATION_LEASE,
    OperationPhase,
    SyncOutcome,
    begin_operation,
    read_connector_status,
    record_sync_outcome,
)
from hevy_connection import HevyCredentialState, HevyCredentialTest
from hevy_parser import ExerciseSummary, WorkoutSummary
from secret_vault import SecretVault, VerificationResult
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

_SECRET = "readiness-test-secret-that-is-long-enough"
_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TODAY = datetime.now(tz=timezone.utc).date()

ALL_SURFACES = ("dashboard", "plan", "progress", "stats", "history", "checkins", "coach")


def _programme_definition() -> dict[str, Any]:
    return {
        "name": "Synthetic hypertrophy",
        "source": "hevy",
        "cycle_weeks": 12,
        "programme_spec": {
            "start_date": (_TODAY - timedelta(days=1)).isoformat(),
            "duration_weeks": 12,
            "selected_routine_ids": ["routine-a"],
            "goal": "hypertrophy",
        },
        "blocks": [
            {
                "number": 1,
                "name": "Build",
                "start_week": 1,
                "end_week": 12,
                "weeks": "1–12",
                "focus": "Build volume",
                "rep_emphasis": "8–12 reps",
                "target_rir": 2,
                "volume_multiplier": 1.0,
            }
        ],
        "days": [
            {
                "number": 1,
                "routine_id": "routine-a",
                "focus": "Upper A",
                "muscles": ["chest"],
                "estimated_duration_minutes": 60,
                "exercises": [
                    {
                        "name": "Bench Press",
                        "template_id": "bench",
                        "role": "main",
                        "sets": 3,
                        "rep_range": "8–10",
                        "scheme": "3 × 8–10",
                        "prescriptions": [
                            {"block_number": 1, "sets": 3, "rep_range": "8–10"}
                        ],
                    }
                ],
            }
        ],
        "rules": ["Progressions are deterministic."],
        "analysis": {"confidence": "High"},
        "warnings": [],
    }


def _connected_athlete(db_path: str, email: str, *, verified_ai: bool = True) -> str:
    """A tenant with Hevy, a programme, fresh data and an AI key."""

    user = database.get_or_create_user(email, "Synthetic athlete", db_path)
    user_id = user["id"]
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db_path)
    database.save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at=(datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat(),
        db_path=db_path,
    )
    database.save_user_api_key(user_id, "gemini", "synthetic-ai-key", db_path=db_path)
    if verified_ai:
        SecretVault(db_path).verify_stored(
            user_id,
            "gemini",
            verifier=lambda _credential: VerificationResult(True, "ok", "ok"),
        )
    database.set_active_programme(
        user_id, "hevy", "synthetic-hevy", _programme_definition(), db_path=db_path
    )
    database.save_progress(
        WorkoutSummary(
            title="Upper A",
            date=_TODAY.isoformat(),
            duration_seconds=3600,
            total_volume_kg=2800.0,
            exercises=[ExerciseSummary("Bench Press", 80.0, 8, 4)],
        ),
        db_path,
        user_id=user_id,
    )
    database.save_daily_log(
        _TODAY.isoformat(),
        1,
        "Upper A",
        "high",
        "Synthetic plan",
        "Synthetic lifestyle",
        db_path,
        user_id=user_id,
    )
    return user_id


@pytest.fixture()
def db_path(tmp_path: Any, monkeypatch: Any) -> str:
    monkeypatch.setenv("ENCRYPTION_KEY", _ENCRYPTION_KEY)
    path = str(tmp_path / "readiness.db")
    database.init_db(path)
    return path


@pytest.fixture()
def app_module(db_path: str, monkeypatch: Any) -> Any:
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "readiness-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "readiness-client-secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    import webapp.app as module

    importlib.reload(module)
    module._RATE_LIMITS.clear()
    return module


@pytest.fixture()
def client(app_module: Any) -> Any:
    with TestClient(app_module.app) as test_client:
        yield test_client


def _sign_in(client: TestClient, app_module: Any, user_id: str) -> None:
    client.cookies.set(
        "session",
        signed_session_cookie(
            _SECRET,
            user="athlete@example.test",
            user_id=user_id,
            session_id=app_module.SESSION_STORE.create(user_id),
        ),
    )


def _state(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/setup/state")
    assert response.status_code == 200
    return response.json()


class _RecordingProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, stream: bool = False) -> Any:
        self.prompts.append(prompt)
        return iter(["Synthetic coach reply."]) if stream else "Synthetic coach reply."


def _use_provider(monkeypatch: Any, provider: _RecordingProvider) -> None:
    monkeypatch.setattr(
        "webapp.app.resolve_provider", lambda user_id=None, **_kw: provider
    )


def _ask(client: TestClient, question: str) -> Any:
    return client.get("/api/rag_search", params={"q": question})


def _sse_events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        event = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if event and data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


# -- live readiness through the sync route ---------------------------------


def test_a_sync_is_observable_while_it_runs_and_recorded_when_it_ends(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "sync-ok@example.test")
    _sign_in(client, app_module, user_id)
    assert _state(client)["account"]["state"] == "ready"
    observed: list[dict[str, Any]] = []

    def _fake_sync(_key: str, _db: str, *, user_id: str) -> dict[str, int]:
        # A second request, made while the sync is still running.
        observed.append(app_module._account_setup(user_id).as_dict())
        return {"workouts_found": 5, "processed": 5}

    monkeypatch.setattr("scripts.sync_history.sync_all", _fake_sync)

    response = client.post("/api/settings/sync-history")

    assert response.status_code == 200
    assert observed[0]["state"] == "syncing"
    assert observed[0]["connector"]["operation"] == "syncing"
    after = _state(client)["account"]
    assert after["state"] == "ready"
    assert after["connector"]["operation"] is None
    assert after["connector"]["last_sync"]["outcome"] == "succeeded"
    assert after["connector"]["last_sync"]["records"] == 5


def test_every_surface_says_syncing_while_a_sync_runs(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    user_id = _connected_athlete(db_path, "syncing-surfaces@example.test")
    begin_operation(user_id, OperationPhase.SYNCING, db_path=db_path)
    _sign_in(client, app_module, user_id)

    surfaces = _state(client)["surfaces"]

    for surface in ALL_SURFACES:
        assert surfaces[surface]["state"] == "syncing", surface
        assert surfaces[surface]["action"] is None, surface
        assert "still running" in surfaces[surface]["reason"], surface
    for path in ("/api/dashboard", "/api/progress", "/api/stats", "/api/history"):
        payload = client.get(path).json()
        assert payload["setup"]["state"] == "syncing", path
    assert client.get("/api/checkins").json()["setup"]["state"] == "syncing"


def test_a_failed_sync_degrades_every_surface_and_offers_a_retry(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "sync-fails@example.test")
    _sign_in(client, app_module, user_id)
    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda *_a, **_k: {
            "error": "Could not fetch your workouts from Hevy.",
            "error_code": "hevy_unavailable",
        },
    )

    response = client.post("/api/settings/sync-history")

    assert response.status_code == 400
    state = _state(client)
    assert state["account"]["state"] == "degraded"
    assert state["account"]["reason_code"] == "sync_failed"
    assert state["account"]["connector"]["last_sync"]["code"] == "hevy_unavailable"
    assert state["account"]["connector"]["operation"] is None
    for surface in ("dashboard", "progress", "stats", "history", "coach"):
        assert state["surfaces"][surface]["state"] == "degraded", surface
        assert state["surfaces"][surface]["action"]["id"] == "retry_sync", surface
    assert client.get("/api/stats").json()["setup"]["state"] == "degraded"

    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda *_a, **_k: {"workouts_found": 3, "processed": 3},
    )
    assert client.post("/api/settings/sync-history").status_code == 200
    assert _state(client)["account"]["state"] == "ready"


def test_a_sync_that_raises_is_recorded_as_failed_not_left_running(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "sync-raises@example.test")
    _sign_in(client, app_module, user_id)

    def _explode(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("boom with synthetic-hevy-key inside")

    monkeypatch.setattr("scripts.sync_history.sync_all", _explode)

    with pytest.raises(RuntimeError):
        client.post("/api/settings/sync-history")

    status = read_connector_status(user_id, db_path=db_path)
    assert status.operation is None
    assert status.last_sync_failed
    assert status.last_sync_code == "unexpected_error"
    assert _state(client)["account"]["state"] == "degraded"
    assert "synthetic-hevy-key" not in str(_state(client))


def test_a_second_sync_is_refused_while_one_is_running(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "sync-busy@example.test")
    begin_operation(user_id, OperationPhase.SYNCING, db_path=db_path)
    _sign_in(client, app_module, user_id)
    called: list[bool] = []
    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda *_a, **_k: called.append(True) or {"workouts_found": 0, "processed": 0},
    )

    response = client.post("/api/settings/sync-history")

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]
    assert called == []
    # The refusal must not release the running sync's lease.
    assert _state(client)["account"]["state"] == "syncing"


def test_a_crashed_sync_is_reported_degraded_and_can_be_retried(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "sync-crashed@example.test")
    stale_start = datetime.now(tz=timezone.utc) - OPERATION_LEASE - timedelta(minutes=5)
    begin_operation(
        user_id, OperationPhase.SYNCING, db_path=db_path, now=stale_start
    )
    _sign_in(client, app_module, user_id)

    state = _state(client)
    assert state["account"]["state"] == "degraded"
    assert state["account"]["reason_code"] == "operation_stalled"

    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda *_a, **_k: {"workouts_found": 1, "processed": 1},
    )
    assert client.post("/api/settings/sync-history").status_code == 200
    assert _state(client)["account"]["state"] == "ready"


def test_a_sync_without_a_stored_key_is_refused_before_any_lease(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    user = database.get_or_create_user("no-key@example.test", "No key", db_path)
    _sign_in(client, app_module, user["id"])

    response = client.post("/api/settings/sync-history")

    assert response.status_code == 400
    assert read_connector_status(user["id"], db_path=db_path).operation is None
    assert read_connector_status(user["id"], db_path=db_path).last_sync_outcome is None


# -- live readiness through the connection routes --------------------------


def _verification(state: HevyCredentialState) -> HevyCredentialTest:
    return HevyCredentialTest(
        state=state,
        tested_at=datetime.now(tz=timezone.utc),
        capabilities={"workouts": state is HevyCredentialState.CONNECTED},
        account_username="synthetic-lifter",
        account_id="acct-1",
        detail="synthetic detail",
    )


def test_connecting_is_observable_while_a_key_is_checked(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user = database.get_or_create_user("connecting@example.test", "New", db_path)
    _sign_in(client, app_module, user["id"])
    observed: list[str] = []

    def _fake_verify(_key: str) -> HevyCredentialTest:
        observed.append(app_module._account_setup(user["id"]).state.value)
        return _verification(HevyCredentialState.CONNECTED)

    monkeypatch.setattr(app_module, "verify_hevy_credentials", _fake_verify)

    response = client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": "synthetic-hevy-key"},
    )

    assert response.status_code == 200
    assert observed == ["connecting"]
    after = _state(client)["account"]
    assert after["connector"]["operation"] is None
    assert after["checks"]["hevy_connected"] is True
    assert after["connection_state"] == "connected"


def test_a_rejected_key_is_never_stored_and_leaves_no_lease(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user = database.get_or_create_user("rejected@example.test", "New", db_path)
    _sign_in(client, app_module, user["id"])
    monkeypatch.setattr(
        app_module,
        "verify_hevy_credentials",
        lambda _key: _verification(HevyCredentialState.INVALID),
    )

    response = client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": "synthetic-bad-key"},
    )

    assert response.status_code == 400
    account = _state(client)["account"]
    assert account["state"] == "setup_required"
    assert account["checks"]["hevy_connected"] is False
    assert account["connector"]["operation"] is None


def test_a_stored_key_hevy_rejects_later_is_reported_degraded(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "key-revoked-upstream@example.test")
    _sign_in(client, app_module, user_id)
    assert _state(client)["account"]["state"] == "ready"
    monkeypatch.setattr(
        app_module,
        "verify_hevy_credentials",
        lambda _key: _verification(HevyCredentialState.INVALID),
    )

    response = client.post("/api/settings/verify-hevy", json={})

    assert response.status_code == 400
    state = _state(client)
    assert state["account"]["state"] == "degraded"
    assert state["account"]["reason_code"] == "credential_rejected"
    assert state["surfaces"]["progress"]["action"]["id"] == "reconnect_hevy"

    monkeypatch.setattr(
        app_module,
        "verify_hevy_credentials",
        lambda _key: _verification(HevyCredentialState.CONNECTED),
    )
    assert client.post("/api/settings/verify-hevy", json={}).status_code == 200
    assert _state(client)["account"]["state"] == "ready"


def test_saving_a_new_key_forgets_the_previous_keys_failed_sync(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "new-key@example.test")
    record_sync_outcome(
        user_id, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db_path
    )
    _sign_in(client, app_module, user_id)
    assert _state(client)["account"]["state"] == "degraded"
    monkeypatch.setattr(
        app_module,
        "verify_hevy_credentials",
        lambda _key: _verification(HevyCredentialState.CONNECTED),
    )

    response = client.post(
        "/api/settings/key",
        json={"provider": "hevy", "api_key": "synthetic-new-hevy-key"},
    )

    assert response.status_code == 200
    assert _state(client)["account"]["state"] == "ready"


def test_a_connection_check_is_refused_while_a_sync_runs(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "check-busy@example.test")
    begin_operation(user_id, OperationPhase.SYNCING, db_path=db_path)
    _sign_in(client, app_module, user_id)
    called: list[bool] = []
    monkeypatch.setattr(
        app_module,
        "verify_hevy_credentials",
        lambda _key: called.append(True) or _verification(HevyCredentialState.CONNECTED),
    )

    response = client.post("/api/settings/verify-hevy", json={})

    assert response.status_code == 409
    assert called == []


# -- tenant isolation --------------------------------------------------------


def test_one_tenants_live_sync_never_shows_up_for_or_blocks_another(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    busy = _connected_athlete(db_path, "busy-tenant@example.test")
    other = _connected_athlete(db_path, "other-tenant@example.test")
    begin_operation(busy, OperationPhase.SYNCING, db_path=db_path)
    record_sync_outcome(busy, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db_path)

    _sign_in(client, app_module, other)
    state = _state(client)
    assert state["account"]["state"] == "ready"
    assert state["account"]["connector"]["operation"] is None
    assert state["account"]["connector"]["last_sync"] is None

    monkeypatch.setattr(
        "scripts.sync_history.sync_all",
        lambda *_a, **_k: {"workouts_found": 2, "processed": 2},
    )
    assert client.post("/api/settings/sync-history").status_code == 200

    _sign_in(client, app_module, busy)
    assert _state(client)["account"]["state"] == "syncing"


# -- Coach: verified facts, enforced by the chat route -----------------------


def test_coach_is_ready_only_when_provider_and_data_are_both_verified(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    verified = _connected_athlete(db_path, "verified@example.test")
    untested = _connected_athlete(db_path, "untested@example.test", verified_ai=False)

    _sign_in(client, app_module, verified)
    ready = client.get("/api/coach/capability").json()
    assert ready["status"] == "ready"
    assert ready["provider_state"] == "verified"
    assert ready["provider_verified"] is True
    assert ready["verified_data"] is True
    assert ready["provider_source"] == "user"
    assert _state(client)["surfaces"]["coach"]["state"] == "ready"

    _sign_in(client, app_module, untested)
    pending = client.get("/api/coach/capability").json()
    assert pending["status"] == "provider_unverified"
    assert pending["provider_verified"] is False
    assert pending["can_chat"] is True
    coach = _state(client)["surfaces"]["coach"]
    assert coach["state"] == "setup_required"
    assert coach["action"]["id"] == "verify_ai"


def test_an_account_on_the_shared_server_key_is_not_claimed_verified_nor_nagged(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-server-key")
    user_id = _connected_athlete(db_path, "shared-key@example.test", verified_ai=False)
    database.delete_user_api_key(user_id, "gemini", db_path=db_path)
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    capability = client.get("/api/coach/capability").json()
    assert capability["provider_source"] == "server"
    assert capability["provider_state"] == "unverified"
    assert capability["provider_verified"] is False
    assert capability["status"] == "provider_unverified"
    assert capability["can_chat"] is True

    # There is no key of its own to test, so no setup step points at one.
    state = _state(client)
    assert state["account"]["ai_provider_source"] == "server"
    assert state["account"]["checks"]["ai_verified"] is False
    assert state["surfaces"]["coach"]["state"] == "ready"
    assert state["surfaces"]["coach"]["action"] is None
    assert _ask(client, "What is progressive overload?").status_code == 200


def test_a_failed_provider_test_pauses_coach_in_the_api_as_in_the_ui(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "provider-failed@example.test")
    SecretVault(db_path).verify_stored(
        user_id,
        "gemini",
        verifier=lambda _credential: VerificationResult(False, "rejected", "no"),
    )
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    capability = client.get("/api/coach/capability").json()
    assert capability["can_chat"] is False
    assert capability["status"] == "provider_failed"
    assert capability["provider_state"] == "failed"
    assert _state(client)["surfaces"]["coach"]["action"]["id"] == "fix_ai"

    response = _ask(client, "How is my deadlift progressing?")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "provider_failed"
    assert "connection test failed" in detail["message"]
    assert provider.prompts == []
    # A refused question is not stored as if Coach had answered it.
    assert client.get("/api/chat/history").json()["messages"] == []
    assert client.get("/api/coach/capabilities").json()["provider_ready"] is False


def test_an_untested_provider_may_answer_but_is_never_claimed_to_work(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "untested-chat@example.test", verified_ai=False)
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    response = _ask(client, "What is progressive overload?")

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event for event, _ in events] == ["evidence", "delta", "status", "done"]
    evidence = events[0][1]
    assert evidence["user_id"] == user_id
    assert evidence["items"]
    assert all(
        item["record_id"].startswith(f"{item['source']}:") for item in evidence["items"]
    )
    assert (
        "".join(data["text"] for event, data in events if event == "delta")
        == "Synthetic coach reply."
    )
    status = next(data for event, data in events if event == "status")
    # No citation was offered, so the reply must not be presented as observed data.
    assert status["grounding"] == "inferred"
    assert status["cited"] == []
    assert len(provider.prompts) == 1
    assert client.get("/api/coach/capability").json()["provider_verified"] is False


def test_a_verified_provider_answers_with_only_the_verified_data_it_has(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "verified-chat@example.test")
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    response = _ask(client, "How is my deadlift progressing?")

    assert response.status_code == 200
    prompt = provider.prompts[0]
    assert "Server-verified data access for this user" in prompt
    assert "data readiness: ready" in prompt


def test_data_grounded_questions_are_refused_while_the_history_is_rebuilt(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "rebuilding@example.test")
    begin_operation(user_id, OperationPhase.SYNCING, db_path=db_path)
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    capability = client.get("/api/coach/capability").json()
    assert capability["status"] == "syncing"
    assert capability["verified_data"] is False
    assert capability["has_training_data"] is True

    grounded = _ask(client, "How is my deadlift progressing?")
    assert grounded.status_code == 200
    assert "Data-Grounded Coaching Unavailable" in grounded.text
    assert "sync is running" in grounded.text
    assert provider.prompts == []

    educational = _ask(client, "What is progressive overload?")
    assert educational.status_code == 200
    assert provider.prompts and "may be incomplete" in provider.prompts[0]

    caps = client.get("/api/coach/capabilities").json()
    assert caps["capabilities"]["workout_analysis"]["status"] == "syncing"
    assert caps["capabilities"]["workout_analysis"]["enabled"] is False
    assert caps["capabilities"]["programme_guidance"]["status"] == "syncing"


def test_a_degraded_connection_is_disclosed_to_the_model(
    client: TestClient, app_module: Any, db_path: str, monkeypatch: Any
) -> None:
    user_id = _connected_athlete(db_path, "degraded-chat@example.test")
    record_sync_outcome(user_id, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db_path)
    _sign_in(client, app_module, user_id)
    provider = _RecordingProvider()
    _use_provider(monkeypatch, provider)

    capability = client.get("/api/coach/capability").json()
    assert capability["status"] == "degraded"
    assert capability["readiness"] == "degraded"

    response = _ask(client, "What is progressive overload?")

    assert response.status_code == 200
    assert "last successful sync" in provider.prompts[0]


def test_coach_without_any_provider_is_refused_with_no_state_written(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    user = database.get_or_create_user("no-provider@example.test", "New", db_path)
    _sign_in(client, app_module, user["id"])

    capability = client.get("/api/coach/capability").json()
    assert capability["can_chat"] is False
    assert capability["status"] == "unconfigured"

    response = _ask(client, "What is progressive overload?")

    assert response.status_code == 400
    assert client.get("/api/chat/history").json()["messages"] == []


def test_coach_capability_is_never_borrowed_from_another_tenant(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    _connected_athlete(db_path, "rich@example.test")
    new = database.get_or_create_user("empty@example.test", "Empty", db_path)
    _sign_in(client, app_module, new["id"])

    capability = client.get("/api/coach/capability").json()

    assert capability["has_training_data"] is False
    assert capability["provider_verified"] is False
    assert capability["verified_data"] is False
    assert capability["status"] == "unconfigured"


# -- no fabricated Coach statements -----------------------------------------


def test_the_test_notification_makes_no_coaching_claim(
    client: TestClient, app_module: Any, db_path: str
) -> None:
    user = database.get_or_create_user("notify@example.test", "New", db_path)
    _sign_in(client, app_module, user["id"])

    assert client.post("/api/notifications/test-coach").status_code == 200

    [notification] = client.get("/api/notifications").json()["notifications"]
    text = f"{notification['title']} {notification['message']}".lower()
    assert "coach analysis active" not in text
    assert "test" in text
