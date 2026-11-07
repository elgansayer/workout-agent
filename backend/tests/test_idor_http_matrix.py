"""HTTP-level IDOR matrix driven by real signed sessions.

``test_idor_matrix.py`` proves the repository boundary.  This module proves the
same boundary through the FastAPI application: two real accounts, each holding a
validly signed session cookie, and *no* authentication shortcut patched in.  The
attacker is always ``attacker@example.test`` and the victim is
``victim@example.test``; every victim record carries a unique marker so a leak
is a plain substring match and a vacuous fixture is caught by also asserting the
victim can see the marker.

Operations covered: read, write, delete (including the destructive history
rebuild), indirect object references (global notification ids and reasoning
context ids), client-supplied owner claims, shared-cache safety, and stale or
forged sessions.  Export and scheduled background-job isolation are asserted at
the service layer in ``test_idor_matrix.py`` and ``test_scheduler.py`` because
neither is reachable over HTTP.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from cryptography.fernet import Fernet
from database import (
    get_chat_messages,
    get_notifications,
    get_progress_history,
    get_reasoning_log,
    get_recent_hevy_logs,
    get_user_api_key,
    get_user_preferences,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_checkin,
    save_daily_log,
    save_notification,
    save_progress,
    save_reasoning_log,
    save_user_api_key,
    save_user_preferences,
    save_workout,
    set_active_programme,
)
from fastapi.testclient import TestClient
from hevy_parser import ExerciseSummary, WorkoutSummary
from ownership import AuthenticatedOwner
from tenant_fixtures import create_owner, signed_session_cookie

_SECRET = "idor-http-matrix-secret-that-is-long-enough"
_MARKER = "VICTIM-ONLY-MARKER"
_VICTIM_EMAIL = "victim@example.test"
_ATTACKER_EMAIL = "attacker@example.test"
_CONTEXT_ID = "2026-03-02_VictimLift"

#: Every GET route that returns tenant records.  ``/api/me`` is excluded: it only
#: echoes the caller's own signed identity.
_TENANT_READ_ROUTES = (
    "/api/notifications",
    "/api/chat-history",
    "/api/chat/history",
    "/api/checkins",
    "/api/history",
    "/api/progress",
    "/api/stats",
    "/api/dashboard",
    "/api/plan",
    "/api/programmes",
    "/api/settings",
)

#: Mutating routes that must refuse a request with no usable session.
_TENANT_WRITE_ROUTES = (
    ("post", "/api/notifications/test-coach"),
    ("post", "/api/notifications/clear"),
    ("post", "/api/chat/clear"),
    ("post", "/api/settings/preferences"),
)


@dataclass(frozen=True)
class World:
    """Two tenants, their clients and the victim's seeded records."""

    db: str
    module: Any
    victim: AuthenticatedOwner
    attacker: AuthenticatedOwner
    victim_client: TestClient
    attacker_client: TestClient
    notification_id: int


def _client(module: Any, user_id: str, email: str) -> TestClient:
    client = TestClient(module.app, raise_server_exceptions=False)
    client.cookies.set("session", signed_session_cookie(_SECRET, user_id, email))
    return client


def _seed_victim(db: str, victim: AuthenticatedOwner) -> int:
    notification_id = save_notification(
        victim, f"{_MARKER} title", f"{_MARKER} message", db_path=db
    )
    save_chat_message("user", f"{_MARKER} chat", db, owner=victim)
    save_checkin(1, "2026-03-01", 24, 4, f"{_MARKER} check-in", db, owner=victim)
    save_daily_log(
        "2026-03-02",
        1,
        f"{_MARKER} focus",
        "high",
        f"{_MARKER} plan",
        f"{_MARKER} lifestyle",
        db,
        owner=victim,
    )
    save_body_metrics({"weight_kg": 81.5}, "2026-03-02", db, owner=victim)
    save_reasoning_log(
        _CONTEXT_ID, "VictimLift", f"{_MARKER} reasoning", db, owner=victim
    )
    save_user_preferences(victim, goals=[f"{_MARKER} goal"], db_path=db)
    set_active_programme(
        victim,
        source="hevy",
        template_key="hevy-victim",
        definition={"name": f"{_MARKER} programme"},
        db_path=db,
    )
    save_progress(
        WorkoutSummary(
            title="Day 1",
            date="2026-03-02",
            duration_seconds=3600,
            total_volume_kg=2800.0,
            exercises=[ExerciseSummary(f"{_MARKER} Lift", 140.0, 5, 4)],
        ),
        db,
        owner=victim,
    )
    save_workout({"workouts": [{"title": _MARKER}]}, db, owner=victim)
    save_user_api_key(victim, "openai", f"sk-{_MARKER}", db_path=db)
    return notification_id


@pytest.fixture()
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[World, None, None]:
    db = str(tmp_path / "idor-http.db")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    init_db(db)
    victim = create_owner(db, _VICTIM_EMAIL)
    attacker = create_owner(db, _ATTACKER_EMAIL)
    notification_id = _seed_victim(db, victim)

    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "idor-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "idor-client-secret")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)

    import webapp.app as app_module

    importlib.reload(app_module)
    yield World(
        db=db,
        module=app_module,
        victim=victim,
        attacker=attacker,
        victim_client=_client(app_module, victim.user_id, _VICTIM_EMAIL),
        attacker_client=_client(app_module, attacker.user_id, _ATTACKER_EMAIL),
        notification_id=notification_id,
    )


def _victim_data_is_intact(world: World) -> None:
    """Assert every victim record the matrix seeded is still present and unchanged."""

    notifications = get_notifications(world.victim, db_path=world.db)
    assert [row["id"] for row in notifications] == [world.notification_id]
    assert notifications[0]["is_read"] is False
    assert [
        m["content"] for m in get_chat_messages(db_path=world.db, owner=world.victim)
    ] == [f"{_MARKER} chat"]
    assert get_reasoning_log(_CONTEXT_ID, world.db, owner=world.victim) == (
        f"{_MARKER} reasoning"
    )
    assert get_user_preferences(world.victim, world.db)["goals"] == [f"{_MARKER} goal"]
    key = get_user_api_key(world.victim, "openai", db_path=world.db)
    assert key is not None and key["api_key"] == f"sk-{_MARKER}"
    assert len(get_recent_hevy_logs(db_path=world.db, owner=world.victim)) == 1
    assert f"{_MARKER} Lift" in get_progress_history(
        db_path=world.db, owner=world.victim
    )


# ---------------------------------------------------------------------------
# READ: the attacker sees none of the victim's records on any tenant route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", _TENANT_READ_ROUTES)
def test_http_read_never_returns_another_tenants_records(
    world: World, route: str
) -> None:
    victim_view = world.victim_client.get(route)
    attacker_view = world.attacker_client.get(route)

    assert victim_view.status_code == 200
    assert _MARKER in victim_view.text, "the fixture must prove the data is reachable"
    assert attacker_view.status_code == 200
    assert _MARKER not in attacker_view.text


@pytest.mark.parametrize("route", _TENANT_READ_ROUTES)
def test_http_read_ignores_client_supplied_owner_claims(
    world: World, route: str
) -> None:
    # A client-chosen identifier must never select the record owner, whether it
    # arrives in the query string or in a header.
    response = world.attacker_client.get(
        route,
        params={"user_id": world.victim.user_id, "owner_id": world.victim.user_id},
        headers={"x-user-id": world.victim.user_id},
    )

    assert response.status_code == 200
    assert _MARKER not in response.text
    assert world.victim.user_id not in response.text


# ---------------------------------------------------------------------------
# CACHES: tenant responses can never be stored by a shared cache or proxy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", _TENANT_READ_ROUTES)
def test_http_responses_are_never_shareable_between_tenants(
    world: World, route: str
) -> None:
    for client in (world.victim_client, world.attacker_client):
        response = client.get(route)
        cache_control = response.headers["cache-control"].lower()
        assert "no-store" in cache_control
        assert "private" in cache_control
        # The same URL answers differently per session, so any cache that did
        # store it must key on the credentials.
        assert "cookie" in response.headers["vary"].lower()


# ---------------------------------------------------------------------------
# INDIRECT OBJECT REFERENCES: guessed global ids resolve only for their owner
# ---------------------------------------------------------------------------


def test_http_notification_ids_are_not_global_handles(world: World) -> None:
    attacker = world.attacker_client
    path = f"/api/notifications/{world.notification_id}"

    assert attacker.post(f"{path}/read").status_code == 404
    assert attacker.delete(path).status_code == 404

    _victim_data_is_intact(world)
    # The owner can still use their own handle.
    assert world.victim_client.post(f"{path}/read").status_code == 200
    assert world.victim_client.delete(path).status_code == 200


def test_http_reasoning_context_ids_are_owner_scoped(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-server-key")
    resolved_for: list[str] = []

    class _Provider:
        def generate(self, prompt: Any, *, stream: bool = False) -> str:
            return "ATTACKER-GENERATED reasoning"

    def _resolve(owner: AuthenticatedOwner | None = None, **_: Any) -> _Provider:
        assert owner is not None
        resolved_for.append(owner.user_id)
        return _Provider()

    monkeypatch.setattr("webapp.app.resolve_provider", _resolve)

    response = world.attacker_client.get(f"/api/xai_reasoning/{_CONTEXT_ID}")

    assert response.status_code == 200
    assert _MARKER not in response.text
    assert response.json() == {"reasoning": "ATTACKER-GENERATED reasoning"}
    # The attacker's provider was resolved for the attacker, and the generated
    # text was stored under the attacker without touching the victim's record.
    assert resolved_for == [world.attacker.user_id]
    assert get_reasoning_log(_CONTEXT_ID, world.db, owner=world.attacker) == (
        "ATTACKER-GENERATED reasoning"
    )
    _victim_data_is_intact(world)


# ---------------------------------------------------------------------------
# WRITE: every mutation lands on the session owner and nobody else
# ---------------------------------------------------------------------------


def test_http_writes_only_touch_the_session_owner(world: World) -> None:
    attacker = world.attacker_client

    preferences = attacker.post(
        "/api/settings/preferences", json={"goals": ["attacker goal"]}
    )
    key = attacker.post(
        "/api/settings/key", json={"provider": "openai", "api_key": "sk-attacker"}
    )

    assert preferences.status_code == 200
    assert key.status_code == 200
    assert get_user_preferences(world.attacker, world.db)["goals"] == ["attacker goal"]
    stored = get_user_api_key(world.attacker, "openai", db_path=world.db)
    assert stored is not None and stored["api_key"] == "sk-attacker"
    _victim_data_is_intact(world)


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"goals": ["hijack"], "user_id": "{victim}"}},
        {"json": {"goals": ["hijack"], "profile": {"owner_id": "{victim}"}}},
        {"params": {"user_id": "{victim}"}, "json": {"goals": ["hijack"]}},
        {"headers": {"x-owner-id": "{victim}"}, "json": {"goals": ["hijack"]}},
        {"headers": {"x-account-id": "{victim}"}, "json": {"goals": ["hijack"]}},
    ],
    ids=["body", "nested-body", "query", "owner-header", "account-header"],
)
def test_http_client_supplied_owner_claims_cannot_redirect_a_write(
    world: World, request_kwargs: dict[str, Any]
) -> None:
    def _bind(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{victim}", world.victim.user_id)
        if isinstance(value, dict):
            return {key: _bind(item) for key, item in value.items()}
        return value

    response = world.attacker_client.post(
        "/api/settings/preferences", **_bind(request_kwargs)
    )

    assert response.status_code == 404
    assert get_user_preferences(world.attacker, world.db)["goals"] == []
    _victim_data_is_intact(world)


# ---------------------------------------------------------------------------
# DELETE: bulk and keyed deletes only ever reach the session owner's rows
# ---------------------------------------------------------------------------


def test_http_bulk_deletes_only_clear_the_session_owner(world: World) -> None:
    attacker = world.attacker_client
    assert attacker.post("/api/chat/clear").status_code == 200
    assert attacker.post("/api/notifications/clear").status_code == 200
    assert attacker.delete("/api/notifications").status_code == 200

    _victim_data_is_intact(world)


def test_http_api_key_delete_cannot_remove_another_tenants_key(world: World) -> None:
    attacker = world.attacker_client
    assert (
        attacker.post(
            "/api/settings/key", json={"provider": "openai", "api_key": "sk-attacker"}
        ).status_code
        == 200
    )

    response = attacker.post("/api/settings/key/delete", json={"provider": "openai"})

    assert response.status_code == 200
    assert get_user_api_key(world.attacker, "openai", db_path=world.db) is None
    _victim_data_is_intact(world)


def test_http_history_rebuild_never_touches_another_tenants_history(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rebuild endpoint deletes the caller's workout history before importing
    # it again, so it is the one write path that can destroy a lot of data.
    save_user_api_key(world.attacker, "hevy", "hevy-attacker-key", db_path=world.db)
    fetched_with: list[str] = []

    def _fetch(api_key: str) -> list[dict[str, Any]]:
        fetched_with.append(api_key)
        return [{"start_time": "2026-03-05T10:00:00Z", "title": "Attacker Day"}]

    monkeypatch.setattr("scripts.sync_history.get_all_workouts", _fetch)

    response = world.attacker_client.post("/api/settings/sync-history")

    assert response.status_code == 200
    assert fetched_with == ["hevy-attacker-key"]
    assert len(get_recent_hevy_logs(db_path=world.db, owner=world.attacker)) == 1
    _victim_data_is_intact(world)


# ---------------------------------------------------------------------------
# FORGED OR STALE SESSIONS: fail closed and never create orphaned records
# ---------------------------------------------------------------------------


def _orphan_rows(db: str) -> list[tuple[str, int]]:
    """Return every user-owned table holding rows whose owner is not a stored user."""

    from ownership import USER_SCOPED_TABLES

    orphans: list[tuple[str, int]] = []
    with sqlite3.connect(db) as conn:
        for table in USER_SCOPED_TABLES:
            if table.name.startswith("health_"):
                continue
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table.name} "
                f"WHERE {table.owner_column} NOT IN (SELECT id FROM users)"
            ).fetchone()[0]
            if count:
                orphans.append((table.name, count))
    return orphans


@pytest.mark.parametrize("route", _TENANT_READ_ROUTES)
def test_http_requests_without_a_session_are_rejected(world: World, route: str) -> None:
    anonymous = TestClient(world.module.app, raise_server_exceptions=False)

    response = anonymous.get(route)

    assert response.status_code == 401
    assert _MARKER not in response.text


@pytest.mark.parametrize(("method", "route"), _TENANT_WRITE_ROUTES)
def test_http_forged_and_stale_sessions_cannot_mutate(
    world: World, method: str, route: str
) -> None:
    forged = TestClient(world.module.app, raise_server_exceptions=False)
    forged.cookies.set(
        "session",
        signed_session_cookie("a-different-secret-entirely", world.victim.user_id),
    )
    stale = TestClient(world.module.app, raise_server_exceptions=False)
    stale.cookies.set(
        "session",
        signed_session_cookie(_SECRET, "deleted-user-id", "deleted@example.test"),
    )

    for client in (forged, stale):
        response = getattr(client, method)(route, json={"goals": ["x"]})
        assert response.status_code == 401, (route, response.text)

    assert _orphan_rows(world.db) == []
    _victim_data_is_intact(world)


@pytest.mark.parametrize("route", _TENANT_READ_ROUTES)
def test_http_stale_session_is_unauthorised_not_an_empty_tenant(
    world: World, route: str
) -> None:
    stale = TestClient(world.module.app, raise_server_exceptions=False)
    stale.cookies.set(
        "session",
        signed_session_cookie(_SECRET, "deleted-user-id", "deleted@example.test"),
    )

    response = stale.get(route)

    # A restored or reset database leaves older cookies valid; the account no
    # longer exists, so the caller must sign in again rather than see "empty".
    assert response.status_code == 401
    assert _MARKER not in response.text
