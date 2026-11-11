"""API surfaces share one integration state model (settings, dashboard, plan,
programme builder, Coach, notifications, status endpoint)."""

from __future__ import annotations

import base64
import importlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import database
import hevy_client
import integration_status as svc
import pytest
from health_sync import SyncState
from hevy_parser import ExerciseSummary, WorkoutSummary
from integration_state import RunKind
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

pytest.importorskip("fastapi")

_SECRET = "integration-status-test-secret-long-enough"
NOW = datetime.now(timezone.utc)


def _cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": f"{user_id}@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


@dataclass
class Env:
    app: Any
    db: str
    alice: str
    bob: str
    client: TestClient
    other: TestClient

    @property
    def ctx(self) -> svc.StatusContext:
        return self.app._status_context()


def build_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, google: bool = False) -> Env:
    db = str(tmp_path / "web.db")
    database.init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if google:
        monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", "gh-id")
        monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", "gh-secret")
    else:
        monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    alice = database.get_or_create_user("alice@example.test", "Alice", db_path=db)["id"]
    bob = database.get_or_create_user("bob@example.test", "Bob", db_path=db)["id"]
    client, other = TestClient(app_module.app), TestClient(app_module.app)
    client.cookies.set("session", _cookie(alice))
    other.cookies.set("session", _cookie(bob))
    return Env(app_module, db, alice, bob, client, other)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return build_env(tmp_path, monkeypatch)


def save_hevy_key(env: Env, user_id: str, *, age_days: int = 30) -> None:
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=env.db)
    with sqlite3.connect(env.db) as conn:
        conn.execute(
            "UPDATE user_api_keys SET updated_at = ? WHERE user_id = ? AND provider = 'hevy'",
            ((NOW - timedelta(days=age_days)).isoformat(), user_id),
        )


def probe(monkeypatch: pytest.MonkeyPatch, result: hevy_client.HevyProbe) -> None:
    monkeypatch.setattr(hevy_client, "probe_connection", lambda api_key: result)


OK = hevy_client.HevyProbe(True, workout_count=3)
EXPIRED = hevy_client.HevyProbe(False, error_code="unauthorized")
DOWN = hevy_client.HevyProbe(False, error_code="provider_unavailable", retryable=True)


def hevy_status(env: Env, user: str) -> dict[str, Any]:
    client = env.client if user == env.alice else env.other
    return client.get("/api/integrations/status").json()["components"]["hevy"]


def record_hevy(env: Env, user: str, status: SyncState, minutes_ago: float, **kw: Any) -> None:
    svc.record_run(
        user, svc.HEVY, RunKind.SYNC, status, env.ctx,
        now=NOW - timedelta(minutes=minutes_ago), notify=False, **kw,
    )


# --- status endpoint --------------------------------------------------------


def test_status_endpoint_requires_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_env(tmp_path, monkeypatch)  # reloads the app with this test's database
    import webapp.secure_app as secure

    anonymous = TestClient(secure.app, follow_redirects=False)
    response = anonymous.get("/api/integrations/status")
    assert response.status_code == 401
    assert "components" not in response.text


def test_new_account_reports_setup_required_for_every_required_component(env: Env) -> None:
    body = env.client.get("/api/integrations/status").json()
    assert body["state"] == "setup_required"
    assert body["blocking"] == ["hevy", "programme", "ai_provider"]
    assert body["required"] == ["hevy", "programme", "ai_provider"]
    assert body["components"]["google_health"]["required"] is False
    assert body["components"]["google_health"]["reason"] == "not_available"
    assert body["next_action"] == "connect" and body["action_href"].startswith("/settings")


def test_status_is_tenant_scoped(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 5, fetched=4, written=4)
    save_hevy_key(env, env.bob)
    record_hevy(env, env.bob, SyncState.FAILED, 5, error_code="unauthorized")
    assert hevy_status(env, env.alice)["state"] == "ready"
    assert hevy_status(env, env.bob)["reason"] == "credential_rejected"
    # A client-supplied owner id is ignored; identity comes from the session.
    spoof = env.client.get("/api/integrations/status", params={"user_id": env.bob})
    assert spoof.json()["components"]["hevy"]["state"] == "ready"


def test_status_payload_never_contains_secrets(env: Env) -> None:
    save_hevy_key(env, env.alice)
    database.save_user_api_key(env.alice, "claude", "synthetic-claude-key", db_path=env.db)
    text = env.client.get("/api/integrations/status").text
    assert "synthetic-hevy-key" not in text and "synthetic-claude-key" not in text


# --- settings ---------------------------------------------------------------


def test_settings_exposes_the_shared_model_next_to_legacy_flags(env: Env) -> None:
    save_hevy_key(env, env.alice)
    body = env.client.get("/api/settings").json()
    assert body["user_keys"]["hevy"]["has_key"] is True  # legacy field kept
    hevy = body["integrations"]["components"]["hevy"]
    assert (hevy["state"], hevy["reason"], hevy["next_action"]) == (
        "connecting",
        "awaiting_verification",
        "verify",
    )
    assert body["gh_connected"] is False and body["gh_configured"] is False
    assert body["integrations"] == env.client.get("/api/integrations/status").json() | {
        "generated_at": body["integrations"]["generated_at"]
    }


# --- verify -----------------------------------------------------------------


def test_verifying_the_stored_key_records_evidence(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    response = env.client.post("/api/settings/verify-hevy", json={})
    body = response.json()
    assert response.status_code == 200 and body["status"] == "ok"
    assert body["workout_count"] == 3 and body["recorded"] is True
    assert body["integration"]["reason"] == "awaiting_first_sync"
    assert hevy_status(env, env.alice)["reason"] == "awaiting_first_sync"


@pytest.mark.parametrize("body", [{}, {"api_key": ""}, {"api_key": "__stored__"}, {"api_key": "synthetic-hevy-key"}])
def test_stored_key_is_used_when_no_replacement_is_entered(
    env: Env, monkeypatch: pytest.MonkeyPatch, body: dict[str, str]
) -> None:
    save_hevy_key(env, env.alice)
    seen: list[str] = []

    def fake(api_key: str) -> hevy_client.HevyProbe:
        seen.append(api_key)
        return OK

    monkeypatch.setattr(hevy_client, "probe_connection", fake)
    assert env.client.post("/api/settings/verify-hevy", json=body).json()["recorded"] is True
    assert seen == ["synthetic-hevy-key"]  # never the sentinel placeholder


def test_a_candidate_key_is_probed_but_never_vouches_for_the_stored_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    body = env.client.post("/api/settings/verify-hevy", json={"api_key": "typed-but-unsaved"}).json()
    assert body["status"] == "ok" and body["recorded"] is False and body["integration"] is None
    assert hevy_status(env, env.alice)["reason"] == "awaiting_verification"


def test_verify_without_any_key_is_a_clear_error(env: Env) -> None:
    response = env.client.post("/api/settings/verify-hevy", json={})
    assert response.status_code == 400
    assert "No Hevy API key" in response.json()["detail"]


def test_expired_key_is_reported_as_such_alerted_once_and_cleared_by_a_new_key(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 120)
    probe(monkeypatch, EXPIRED)
    body = env.client.post("/api/settings/verify-hevy", json={}).json()
    assert body["status"] == "error"
    assert "rejected your credentials" in body["detail"] and "unauthorized" not in body["detail"]
    assert body["integration"]["state"] == "degraded"
    assert body["integration"]["reason"] == "credential_rejected"

    env.client.post("/api/settings/verify-hevy", json={})
    alerts = env.client.get("/api/notifications").json()["notifications"]
    assert [item["title"] for item in alerts] == ["⚠️ Hevy needs attention"]  # once, not per retry
    assert env.other.get("/api/notifications").json()["notifications"] == []

    probe(monkeypatch, OK)
    env.client.post("/api/settings/key", json={"provider": "hevy", "api_key": "fresh-synthetic-key"})
    assert hevy_status(env, env.alice)["reason"] == "awaiting_verification"


def test_removing_the_key_returns_to_setup_and_forgets_history(env: Env) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 5)
    assert env.client.post("/api/settings/key/delete", json={"provider": "hevy"}).status_code == 200
    assert hevy_status(env, env.alice)["reason"] == "credential_missing"
    assert database.get_connector_runs(env.alice, "hevy", env.db) == []


# --- sync history -----------------------------------------------------------


def patch_sync(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | Exception, seen: list[Any] | None = None) -> None:
    def fake(api_key: str, db_path: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        if seen is not None:
            seen.append((api_key, user_id))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("scripts.sync_history.sync_all", fake)


def test_successful_sync_makes_hevy_ready_and_summarises(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    seen: list[Any] = []
    patch_sync(monkeypatch, {"workouts_found": 5, "processed": 4}, seen)
    body = env.client.post("/api/settings/sync-history").json()
    assert body["status"] == "ok" and body["processed"] == 4
    assert body["integration"]["state"] == "ready"
    assert body["integration"]["last_run"] == {"fetched": 5, "written": 4, "skipped": 1}
    assert seen == [("synthetic-hevy-key", env.alice)]
    titles = [n["title"] for n in env.client.get("/api/notifications").json()["notifications"]]
    assert titles == ["✓ Hevy History Synced"]


def test_syncing_is_visible_to_other_requests_while_the_import_runs(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    observed: list[Any] = []

    def during(api_key: str, db_path: str | None = None, *, user_id: str | None = None) -> dict[str, Any]:
        observed.append(svc.component_status(env.alice, svc.HEVY, env.ctx))
        observed.append(svc.begin_run(env.alice, svc.HEVY, RunKind.SYNC, env.ctx))
        return {"workouts_found": 1, "processed": 1}

    monkeypatch.setattr("scripts.sync_history.sync_all", during)
    env.client.post("/api/settings/sync-history")
    state, duplicate = observed
    assert state.state.value == "syncing" and duplicate is None  # retry/double submit refused


def test_second_sync_while_one_is_running_is_refused_without_touching_hevy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    assert svc.begin_run(env.alice, svc.HEVY, RunKind.SYNC, env.ctx) is not None
    probe(monkeypatch, OK)
    calls: list[Any] = []
    patch_sync(monkeypatch, {"workouts_found": 1, "processed": 1}, calls)
    response = env.client.post("/api/settings/sync-history")
    assert response.status_code == 409 and calls == []
    assert env.other.post("/api/settings/sync-history").status_code == 400  # other tenant unaffected


@pytest.mark.parametrize(
    ("result", "status_code", "fragment"),
    [(EXPIRED, 400, "rejected your credentials"), (DOWN, 502, "temporarily unavailable")],
)
def test_failed_sync_is_classified_and_never_clears_history(
    env: Env, monkeypatch: pytest.MonkeyPatch, result: hevy_client.HevyProbe, status_code: int, fragment: str
) -> None:
    save_hevy_key(env, env.alice)
    database.save_workout({"workouts": [{"id": "keep"}]}, env.db, when="2026-01-01", user_id=env.alice)
    probe(monkeypatch, result)
    called: list[Any] = []
    patch_sync(monkeypatch, {"workouts_found": 1, "processed": 1}, called)
    response = env.client.post("/api/settings/sync-history")
    assert response.status_code == status_code and fragment in response.json()["detail"]
    assert called == []  # the rebuild never started
    assert len(database.get_recent_hevy_logs(db_path=env.db, user_id=env.alice)) == 1
    run = database.get_connector_runs(env.alice, "hevy", env.db)[-1]
    assert (run["status"], run["error_code"]) == ("failed", result.error_code)


def test_fetch_failure_after_a_good_probe_is_a_retryable_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    patch_sync(monkeypatch, {"error": "Hevy could not be reached."})
    response = env.client.post("/api/settings/sync-history")
    assert response.status_code == 502
    run = database.get_connector_runs(env.alice, "hevy", env.db)[-1]
    assert (run["status"], run["error_code"], run["retryable"]) == ("failed", "provider_unavailable", True)


def test_partial_import_degrades_and_a_retry_recovers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    patch_sync(monkeypatch, {"workouts_found": 10, "processed": 6, "failed": 3})
    body = env.client.post("/api/settings/sync-history").json()
    assert body["status"] == "partial" and body["failed"] == 3
    assert body["integration"]["state"] == "degraded"
    assert body["integration"]["reason"] == "partial_import"
    assert "6 of 10" in body["integration"]["message"]
    assert body["integration"]["last_run"] == {"fetched": 10, "written": 6, "skipped": 1}
    titles = [n["title"] for n in env.client.get("/api/notifications").json()["notifications"]]
    assert titles == ["⚠️ Hevy needs attention"]

    patch_sync(monkeypatch, {"workouts_found": 10, "processed": 10})
    assert env.client.post("/api/settings/sync-history").json()["integration"]["state"] == "ready"
    titles = [n["title"] for n in env.client.get("/api/notifications").json()["notifications"]]
    assert titles == [
        "✓ Hevy History Synced",
        "✓ Hevy is working again",
        "⚠️ Hevy needs attention",
    ]


def test_unexpected_sync_crash_is_recorded_not_left_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, OK)
    patch_sync(monkeypatch, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        env.client.post("/api/settings/sync-history")
    run = database.get_connector_runs(env.alice, "hevy", env.db)[-1]
    assert (run["status"], run["error_code"]) == ("failed", "internal_error")
    assert hevy_status(env, env.alice)["state"] != "syncing"


def test_stale_sync_is_reported_without_any_event(env: Env) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 5 * 24 * 60, fetched=3, written=3)
    status = hevy_status(env, env.alice)
    assert (status["state"], status["freshness"], status["next_action"]) == ("stale", "stale", "sync")
    assert "5 days ago" in status["message"]


# --- programme builder / dashboard / plan ----------------------------------


def test_programme_builder_surfaces_an_expired_key_instead_of_empty_data(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    probe(monkeypatch, EXPIRED)
    body = env.client.get("/api/programmes").json()
    connection = body["connection"]
    assert (connection["state"], connection["reason"]) == ("setup_required", "credential_rejected")
    assert connection["load_failed"] is True and "rejected your credentials" in connection["detail"]
    assert body["source"]["routines"] == []

    preview = env.client.post("/api/programmes/preview", json={
        "selected_routine_ids": ["a"], "duration_weeks": 8, "goal": "hypertrophy",
        "start_date": "2026-09-01", "sessions_per_week": 3, "experience": "intermediate",
        "max_session_minutes": 60, "adaptation_aggressiveness": "balanced",
    })
    assert preview.status_code == 400 and "rejected your credentials" in preview.json()["detail"]


def test_builder_read_failures_do_not_degrade_a_fresh_sync_pipeline(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 60)
    probe(monkeypatch, DOWN)
    for _ in range(3):
        connection = env.client.get("/api/programmes").json()["connection"]
        # The page reports its own failed read, but history sync readiness is
        # a property of sync runs, so repeated page loads cannot flap it.
        assert connection["state"] == "ready" and connection["load_failed"] is True
        assert "temporarily unavailable" in connection["detail"]
        assert connection["consecutive_failures"] == 0


def test_dashboard_and_plan_share_programme_readiness(env: Env) -> None:
    definition = {
        "name": "Synthetic",
        "programme_spec": {"start_date": (NOW - timedelta(days=3)).date().isoformat(), "duration_weeks": 8},
        "blocks": [{"number": 1, "name": "Base", "start_week": 1, "end_week": 8}],
        "days": [{"number": 1, "focus": "Upper", "exercises": []}],
    }
    before_dash = env.client.get("/api/dashboard").json()
    before_plan = env.client.get("/api/plan").json()
    for payload in (before_dash, before_plan):
        assert payload["setup_required"] is True
        assert payload["readiness"]["components"]["programme"]["reason"] == "programme_missing"

    database.set_active_programme(env.alice, "hevy", "synthetic", definition, db_path=env.db)
    dash = env.client.get("/api/dashboard").json()
    plan = env.client.get("/api/plan").json()
    for payload in (dash, plan):
        assert payload["setup_required"] is False
        assert payload["readiness"]["components"]["programme"]["state"] == "ready"
    assert dash["readiness"]["components"] == plan["readiness"]["components"]

    other = env.other.get("/api/dashboard").json()  # tenant isolation
    assert other["setup_required"] is True


def test_dashboard_setup_flag_cannot_diverge_from_the_programme_component(env: Env) -> None:
    database.set_active_programme(
        env.alice, "hevy", "bad",
        {"programme_spec": {"start_date": "not-a-date", "duration_weeks": 4}, "days": [], "blocks": []},
        db_path=env.db,
    )
    dash = env.client.get("/api/dashboard").json()
    programme = dash["readiness"]["components"]["programme"]
    assert (programme["state"], programme["reason"]) == ("degraded", "programme_needs_review")
    assert dash["setup_required"] is False and dash["programme_status"] == "needs_review"


# --- Coach ------------------------------------------------------------------


class _Provider:
    def __init__(self, behaviour: str = "ok") -> None:
        self.prompts: list[str] = []
        self.behaviour = behaviour

    def generate(self, prompt: str, *, stream: bool = False) -> Any:
        self.prompts.append(prompt)
        if self.behaviour == "ok":
            return iter(["Fine ", "answer"])
        if self.behaviour == "empty":
            return iter([])
        raise self.behaviour  # type: ignore[misc]


def use_provider(env: Env, monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> None:
    monkeypatch.setattr(env.app, "resolve_provider", lambda *a, **k: provider)
    monkeypatch.setattr(env.app, "get_config", lambda: SimpleNamespace(gemini_api_key="k", gemini_model="m"))


def coach_status(env: Env) -> dict[str, Any]:
    return env.client.get("/api/integrations/status").json()["components"]["ai_provider"]


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda e: None, "No workout history has been imported from Hevy yet"),
        (
            lambda e: (
                save_hevy_key(e, e.alice),
                database.save_progress(
                    WorkoutSummary("Day", "2026-01-02", 3600, 100.0, [ExerciseSummary("Squat", 100.0, 5, 3)]),
                    e.db,
                    user_id=e.alice,
                ),
            ),
            "the age of the workout history below is unknown",
        ),
        (lambda e: (save_hevy_key(e, e.alice), record_hevy(e, e.alice, SyncState.SUCCEEDED, 6 * 24 * 60)),
         "Workout history was last synced on"),
        (lambda e: (save_hevy_key(e, e.alice), record_hevy(e, e.alice, SyncState.SUCCEEDED, 600),
                    record_hevy(e, e.alice, SyncState.PARTIAL, 5, fetched=9, written=3)),
         "Hevy syncing is failing"),
    ],
)
def test_coach_prompt_states_how_current_its_data_is(
    env: Env, monkeypatch: pytest.MonkeyPatch, setup: Callable[[Env], Any], expected: str
) -> None:
    setup(env)
    provider = _Provider()
    use_provider(env, monkeypatch, provider)
    assert env.client.get("/api/rag_search", params={"q": "How am I doing?"}).text == "Fine answer"
    (prompt,) = provider.prompts
    assert "Data readiness (system-generated, authoritative):" in prompt and expected in prompt


def test_coach_prompt_has_no_caveat_when_data_is_current(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    save_hevy_key(env, env.alice)
    record_hevy(env, env.alice, SyncState.SUCCEEDED, 30)
    provider = _Provider()
    use_provider(env, monkeypatch, provider)
    env.client.get("/api/rag_search", params={"q": "hello"})
    assert "Data readiness" not in provider.prompts[0]


def test_coach_requests_are_recorded_and_failures_degrade_without_flapping(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    database.save_user_api_key(env.alice, "gemini", "synthetic-gemini-key", db_path=env.db)
    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE user_api_keys SET updated_at = ? WHERE provider = 'gemini'",
                     ((NOW - timedelta(days=2)).isoformat(),))
    assert coach_status(env)["state"] == "ready"

    class Limited(Exception):
        status_code = 429

    use_provider(env, monkeypatch, _Provider(Limited()))
    first = env.client.get("/api/rag_search", params={"q": "one"})
    assert "currently unavailable" in first.text
    assert coach_status(env)["state"] == "ready"  # a single transient failure is absorbed
    env.client.get("/api/rag_search", params={"q": "two"})
    degraded = coach_status(env)
    assert (degraded["state"], degraded["last_error_code"]) == ("degraded", "rate_limited")
    assert "limiting requests" in degraded["message"]

    use_provider(env, monkeypatch, _Provider())
    env.client.get("/api/rag_search", params={"q": "three"})
    assert coach_status(env)["state"] == "ready"
    titles = [n["title"] for n in env.client.get("/api/notifications").json()["notifications"]]
    assert titles.count("⚠️ AI coach (Google Gemini) needs attention") == 1
    assert env.other.get("/api/integrations/status").json()["components"]["ai_provider"]["state"] == "setup_required"


def test_rejected_coach_key_is_reported_as_setup_required(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    database.save_user_api_key(env.alice, "gemini", "synthetic-gemini-key", db_path=env.db)
    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE user_api_keys SET updated_at = ? WHERE provider = 'gemini'",
                     ((NOW - timedelta(days=2)).isoformat(),))

    class Denied(Exception):
        status_code = 401

    use_provider(env, monkeypatch, _Provider(Denied("secret provider text")))
    env.client.get("/api/rag_search", params={"q": "x"})
    status = coach_status(env)
    assert (status["state"], status["reason"], status["next_action"]) == (
        "setup_required", "credential_rejected", "reconnect",
    )
    assert "secret provider text" not in json.dumps(status)


def test_empty_provider_reply_counts_as_a_failure(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    database.save_user_api_key(env.alice, "gemini", "synthetic-gemini-key", db_path=env.db)
    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE user_api_keys SET updated_at = ? WHERE provider = 'gemini'",
                     ((NOW - timedelta(days=2)).isoformat(),))
    use_provider(env, monkeypatch, _Provider("empty"))
    env.client.get("/api/rag_search", params={"q": "x"})
    runs = database.get_connector_runs(env.alice, "ai:gemini", env.db)
    assert [(r["status"], r["error_code"]) for r in runs] == [("failed", "provider_error")]


def test_coach_without_a_key_stays_a_plain_setup_error(env: Env) -> None:
    response = env.client.get("/api/rag_search", params={"q": "x"})
    assert response.status_code == 400 and "No AI provider key available" in response.json()["detail"]
    assert coach_status(env)["reason"] == "credential_missing"


# --- notifications ----------------------------------------------------------


def test_coach_test_notification_never_claims_an_unready_coach_is_active(env: Env) -> None:
    response = env.client.post("/api/notifications/test-coach")
    assert response.status_code == 200
    (item,) = env.client.get("/api/notifications").json()["notifications"]
    assert item["title"] == "⚠️ Coach is not ready"
    assert "Coach analysis active" not in item["message"]
    assert item["link"] == "/settings?section=ai"

    database.save_user_api_key(env.alice, "gemini", "synthetic-gemini-key", db_path=env.db)
    env.client.post("/api/notifications/test-coach")
    titles = [n["title"] for n in env.client.get("/api/notifications").json()["notifications"]]
    assert "✨ Coach Status Update" in titles


# --- Google Health ----------------------------------------------------------


@pytest.fixture()
def gh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return build_env(tmp_path, monkeypatch, google=True)


def gh_status(env: Env, client: TestClient | None = None) -> dict[str, Any]:
    return (client or env.client).get("/api/integrations/status").json()["components"]["google_health"]


def test_google_health_authorisation_is_modelled_end_to_end(
    gh_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = gh_env
    assert gh_status(env)["reason"] == "credential_missing"
    redirect = env.client.get("/google-health/connect", follow_redirects=False)
    assert redirect.status_code == 303
    assert gh_status(env)["state"] == "connecting" and gh_status(env)["reason"] == "authorizing"
    assert gh_status(env, env.other)["reason"] == "credential_missing"  # other tenant untouched

    state = database.get_meta(env.app._GH_STATE_KEY, env.db, user_id=env.alice)
    monkeypatch.setattr(env.app, "exchange_code", lambda *a, **k: {"refresh_token": "synthetic-refresh"})
    done = env.client.get(f"/google-health/callback?code=abc&state={state}", follow_redirects=False)
    assert done.headers["location"] == "/settings?gh=connected"
    connected = gh_status(env)
    assert (connected["state"], connected["reason"]) == ("connecting", "awaiting_first_sync")
    assert "synthetic-refresh" not in env.client.get("/api/settings").text

    env.client.post("/google-health/disconnect", follow_redirects=False)
    assert gh_status(env)["reason"] == "credential_missing"
    assert database.get_connector_runs(env.alice, "google_health", env.db) == []


@pytest.mark.parametrize(
    ("query", "code"),
    [("error=access_denied", "authorization_denied"), ("code=abc&state=forged", "invalid_state")],
)
def test_google_health_failed_authorisation_is_recorded(
    gh_env: Env, query: str, code: str
) -> None:
    env = gh_env
    env.client.get("/google-health/connect", follow_redirects=False)
    env.client.get(f"/google-health/callback?{query}", follow_redirects=False)
    status = gh_status(env)
    assert (status["state"], status["reason"], status["last_error_code"]) == (
        "setup_required", "authorization_failed", code,
    )
    assert status["next_action"] == "connect"


def test_google_health_token_exchange_failure_is_recorded(
    gh_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = gh_env
    env.client.get("/google-health/connect", follow_redirects=False)
    state = database.get_meta(env.app._GH_STATE_KEY, env.db, user_id=env.alice)
    monkeypatch.setattr(env.app, "exchange_code", lambda *a, **k: None)
    env.client.get(f"/google-health/callback?code=abc&state={state}", follow_redirects=False)
    assert gh_status(env)["last_error_code"] == "token_exchange_failed"


# --- registry connectors ----------------------------------------------------


def test_registry_connectors_use_the_shared_vocabulary(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from connectors.base import ConnectorState, ConnectorStatus

    class FakeConnector:
        provider = "synthetic_provider"
        capabilities = SimpleNamespace(authorize=True)

        def status(self, context: Any) -> ConnectorStatus:
            return ConnectorStatus("synthetic_provider", ConnectorState.ERROR, message="Provider down")

    registry = SimpleNamespace(all=lambda: [FakeConnector()])
    monkeypatch.setattr(env.app, "build_builtin_registry", lambda: registry)
    (info,) = env.client.get("/api/settings").json()["connectors_info"]
    assert (info["state"], info["reason"], info["message"]) == ("degraded", "provider_failing", "Provider down")
