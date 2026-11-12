"""IDOR matrix: every way one signed-in user could reach another user's records.

Alice owns a record in every user-scoped store. Bob is a second, fully valid
account with his own data. For each operation - read, write, export, delete,
background job, cache and indirect object reference - Bob tries to reach or
alter Alice's data through the real FastAPI application, the real repositories
and a real SQLite file. Only the network edges are absent.

Each probe asserts two things so a passing test cannot be vacuous: Bob never
sees or changes anything of Alice's, *and* the same call works for the owner
(Alice reads her own record; Bob reads his own). All data is synthetic.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from account_deletion import export_directory
from cryptography.fernet import Fernet
from database import (
    USER_OWNED_TABLES,
    get_cached_workout_payloads,
    get_chat_messages,
    get_dashboard_insight,
    get_notifications,
    get_or_create_user,
    get_push_subscriptions,
    get_recent_hevy_logs,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_checkin,
    save_daily_log,
    save_dashboard_insight,
    save_hevy_connection,
    save_notification,
    save_progress,
    save_push_subscription,
    save_reasoning_log,
    save_user_preferences,
    save_workout,
)
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from health_export import HealthExport
from owned_rows import insert_owned_row
from programme_domain import (
    ProgrammeNotFoundError,
    ProgrammeService,
    ProgrammeWeekState,
    RoutineWorkoutMatch,
)
from provenance import RecordType
from scheduler import _run_per_owner
from scripts.sync_history import sync_all
from secret_vault import SecretVault
from session_helpers import signed_session_cookie
from session_registry import list_user_sessions
from tenant_isolation import (
    JobStatus,
    OwnedJob,
    OwnerIdentity,
    OwnershipError,
    authorize_owner,
    owner_scoped_key,
    run_owned_job,
)
from test_database import _exercise_summary
from test_programme_domain import _draft
from training_export import ExportedWorkout
from user_settings import export_preferences, get_user_settings

_SECRET = "-".join(("idor", "matrix", "signing", "value", "long", "enough", "for", "tests"))
ALICE = "alice-canary-4f9c1e"
BOB = "bob-canary-83b2d7"
ALICE_KEY = "sk-alice-secret-0123456789abcdef"
REASONING_CONTEXT = "2026-08-01_Squat"


@dataclass
class World:
    """Two valid accounts, the real app, and the ids of Alice's records."""

    module: Any
    db: str
    alice: str
    bob: str
    ids: dict[str, Any] = field(default_factory=dict)

    def client(self, user_id: str) -> TestClient:
        email = "alice@example.test" if user_id == self.alice else "bob@example.test"
        client = TestClient(self.module.app)
        client.cookies.set(
            self.module.SESSION_SECURITY.cookie.name,
            signed_session_cookie(
                _SECRET,
                user=email,
                user_id=user_id,
                session_id=self.module.SESSION_STORE.create(user_id),
            ),
        )
        return client

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, params).fetchall()

    def count(self, table: str, user_id: str) -> int:
        return int(
            self.rows(f"SELECT count(*) FROM {table} WHERE user_id = ?", user_id)[0][0]
        )


def _seed(world: World, user_id: str, marker: str) -> None:
    db = world.db
    save_notification(user_id, f"{marker} title", f"{marker} body", db_path=db)
    save_chat_message("user", f"{marker} chat", db, user_id=user_id)
    save_checkin(1, "2026-08-01", 4, 1, f"{marker} check-in", db_path=db, user_id=user_id)
    save_daily_log(
        "2026-08-01", 1, f"{marker} focus", "high", "plan", "life", db_path=db, user_id=user_id
    )
    save_body_metrics(
        {"weight_kg": 81.234 if marker == ALICE else 92.345},
        "2026-08-01",
        db,
        user_id=user_id,
    )
    save_workout({"title": f"{marker} workout", "id": f"{marker}-w1"}, db, user_id=user_id)
    save_progress(_exercise_summary(f"{marker} Squat", 100.0, 5), db, user_id=user_id)
    save_reasoning_log(
        REASONING_CONTEXT, "Squat", f"{marker} reasoning", db, user_id=user_id
    )
    save_dashboard_insight(json.dumps({"summary": f"{marker} insight"}), db, user_id=user_id)
    save_push_subscription(
        user_id,
        endpoint=f"https://push.example.test/{marker}",
        p256dh="p256dh-key",
        auth="auth-key",
        db_path=db,
    )
    save_user_preferences(user_id, goals=[f"{marker} goal"], db_path=db)
    save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-08-01T00:00:00+00:00",
        account_username=f"{marker}-lifter",
        db_path=db,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO health_connections "
            "(id, user_id, provider, state, granted_capabilities) VALUES (?, ?, ?, ?, ?)",
            (f"conn-{marker}", user_id, "withings", "connected", "[]"),
        )
        conn.execute(
            "INSERT INTO health_metrics (user_id, metric_type, value, unit, observed_at, "
            "provider, connection_id, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                "weight_kg",
                81.234 if marker == ALICE else 92.345,
                "kg",
                "2026-08-01T00:00:00+00:00",
                "withings",
                f"conn-{marker}",
                f"fp-{marker}",
            ),
        )


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    db = str(tmp_path / "idor-matrix.db")
    init_db(db)
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "idor-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "-".join(("idor", "client", "value")))
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("ALLOW_ANONYMOUS_WEB", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    alice = str(get_or_create_user("alice@example.test", "Alice", db_path=db)["id"])
    bob = str(get_or_create_user("bob@example.test", "Bob", db_path=db)["id"])

    import webapp.app as app_module

    importlib.reload(app_module)
    built = World(app_module, db, alice, bob)
    _seed(built, alice, ALICE)
    _seed(built, bob, BOB)
    SecretVault(db).store(alice, "gemini", api_key=ALICE_KEY)
    service = ProgrammeService(db)
    for user_id, marker in ((alice, ALICE), (bob, BOB)):
        owner = authorize_owner(db, user_id)
        draft = dataclasses.replace(_draft(user_id=user_id), name=f"{marker} block")
        programme = service.persist_draft(user_id, draft)
        service.activate(user_id, programme.id)
        built.ids[f"programme:{user_id}"] = programme.id

    with sqlite3.connect(db) as conn:
        built.ids["notification"] = conn.execute(
            "SELECT id FROM notifications WHERE user_id = ?", (alice,)
        ).fetchone()[0]
    built.ids["alice_session"] = built.module.SESSION_STORE.create(alice)
    built.ids["alice_session_row"] = list_user_sessions(alice, None, db)[0]["id"]
    return built


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

#: GET routes returning tenant data, with a substring that proves the call
#: returned the *owner's* record (so a pass cannot be an empty response).
READ_ROUTES: tuple[tuple[str, bool], ...] = (
    ("/api/notifications", True),
    ("/api/chat/history", True),
    ("/api/chat-history", True),
    ("/api/checkins", True),
    ("/api/history", True),
    ("/api/progress", True),
    ("/api/stats", True),
    ("/api/dashboard", True),
    ("/api/plan", True),
    ("/api/health/connections", True),
    ("/api/settings", False),
    ("/api/settings/rules", False),
    ("/api/settings/display", False),
    ("/api/settings/credentials/audit", False),
    ("/api/sessions", False),
    ("/api/me", False),
    ("/api/setup/state", False),
    ("/api/programmes", False),
    ("/api/account/deletion", False),
)


@pytest.mark.parametrize(("path", "carries_marker"), READ_ROUTES)
def test_read_never_returns_another_users_record(
    world: World, path: str, carries_marker: bool
) -> None:
    alice = world.client(world.alice).get(path)
    bob = world.client(world.bob).get(path)
    assert alice.status_code == 200, (path, alice.text[:200])
    assert bob.status_code == 200, (path, bob.text[:200])
    if carries_marker:
        assert ALICE in alice.text, f"{path} did not return Alice's own record"
        assert BOB in bob.text, f"{path} did not return Bob's own record"
    assert ALICE not in bob.text, path
    assert "alice@example.test" not in bob.text, path
    assert ALICE_KEY not in alice.text + bob.text, path


# ---------------------------------------------------------------------------
# Write and delete
# ---------------------------------------------------------------------------


def _alice_state(world: World) -> dict[str, Any]:
    """Every row Alice owns, plus her account, sessions and credential."""
    state: dict[str, Any] = {
        table: world.rows(
            f"SELECT * FROM {table} WHERE user_id = ? ORDER BY rowid", world.alice
        )
        for table in USER_OWNED_TABLES
    }
    state["account"] = world.rows(
        "SELECT id, email, timezone, units, status FROM users WHERE id = ?",
        world.alice,
    )
    state["sessions"] = world.rows(
        "SELECT session_token_hash, revoked_at FROM user_sessions "
        "WHERE user_id = ? ORDER BY rowid",
        world.alice,
    )
    return state


@dataclass(frozen=True)
class Attack:
    """One request Bob sends that names, or tries to reach, Alice's data."""

    name: str
    method: str
    path: str  # may use {notification}, {session}, {connection}, {alice}
    body: dict[str, Any] | None = None
    status: tuple[int, ...] = (404,)
    #: A legitimate call on Bob's own data: it must succeed for Bob and still
    #: leave Alice untouched.
    own_effect: Callable[[World], bool] | None = None


def _bob_timezone(world: World) -> bool:
    return world.rows("SELECT timezone FROM users WHERE id = ?", world.bob) == [
        ("Asia/Tokyo",)
    ]


def _bob_weight_corrected(world: World) -> bool:
    return world.rows(
        "SELECT weight_kg FROM body_metrics WHERE user_id = ?", world.bob
    ) == [(70.5,)]


def _bob_has_alices_endpoint(world: World) -> bool:
    endpoint = f"https://push.example.test/{ALICE}"
    return any(
        row["endpoint"] == endpoint
        for row in get_push_subscriptions(world.bob, world.db)
    )


def _bob_chat_cleared(world: World) -> bool:
    return get_chat_messages(db_path=world.db, user_id=world.bob) == []


def _bob_notifications_cleared(world: World) -> bool:
    return get_notifications(world.bob, db_path=world.db) == []


def _bob_health_data_erased(world: World) -> bool:
    return world.count("health_metrics", world.bob) == 0


ATTACKS: tuple[Attack, ...] = (
    # --- write: a call that names Alice's object is refused -----------------
    Attack("mark-alices-notification-read", "POST", "/api/notifications/{notification}/read"),
    Attack("rotate-alices-credential", "POST", "/api/settings/key/rotate",
           {"provider": "gemini", "api_key": "-".join(("bob", "replacement", "value"))}, (400, 404, 409)),
    Attack("revoke-alices-credential", "POST", "/api/settings/key/revoke",
           {"provider": "gemini"}, (400, 404, 409)),
    Attack("test-alices-credential", "POST", "/api/settings/key/test",
           {"provider": "gemini"}, (400, 404, 409, 422)),
    # --- write: a call on Bob's own data never reaches Alice ----------------
    Attack("change-own-timezone", "POST", "/api/settings/display",
           {"timezone": "Asia/Tokyo"}, (200,), _bob_timezone),
    Attack("correct-own-body-metric-for-alices-date", "POST", "/api/body-metrics/corrections",
           {"date": "2026-08-01", "weight_kg": 70.5}, (200,), _bob_weight_corrected),
    Attack("subscribe-with-alices-push-endpoint", "POST", "/api/settings/push-subscribe",
           {"endpoint": f"https://push.example.test/{ALICE}",
            "keys": {"p256dh": "bob-key", "auth": "bob-auth"}}, (200,), _bob_has_alices_endpoint),
    # --- owner claims in the request never choose the owner -----------------
    Attack("claim-alice-in-json", "POST", "/api/settings/display",
           {"timezone": "Asia/Tokyo", "user_id": "{alice}"}),
    Attack("claim-alice-in-query", "POST", "/api/settings/display?owner_id={alice}",
           {"timezone": "Asia/Tokyo"}),
    Attack("claim-alice-in-nested-json", "POST", "/api/settings/preferences",
           {"goals": ["x"], "profile": {"account_id": "{alice}"}}),
    # --- delete: a call that names Alice's object is refused ----------------
    Attack("delete-alices-notification", "DELETE", "/api/notifications/{notification}"),
    Attack("revoke-alices-session", "DELETE", "/api/sessions/{session}"),
    Attack("disconnect-and-purge-alices-connection", "DELETE",
           "/api/health/connections/conn-" + ALICE + "?mode=purge&confirmed=true"),
    Attack("delete-alices-credential", "POST", "/api/settings/key/delete",
           {"provider": "gemini"}, (200, 400, 404, 409)),
    # --- delete: bulk calls only ever touch the caller's rows --------------
    Attack("clear-chat", "POST", "/api/chat/clear", None, (200,), _bob_chat_cleared),
    Attack("clear-notifications", "POST", "/api/notifications/clear", None, (200,),
           _bob_notifications_cleared),
    Attack("clear-notifications-by-delete", "DELETE", "/api/notifications", None, (200,),
           _bob_notifications_cleared),
    Attack("revoke-other-sessions", "POST", "/api/sessions/revoke-others", None, (200,)),
    Attack("erase-own-health-data", "POST", "/api/account/deletion",
           {"scope": "health_data", "confirmation": "DELETE HEALTH DATA"}, (200, 202),
           _bob_health_data_erased),
)


def _render(world: World, text: str) -> str:
    return (
        text.replace("{notification}", str(world.ids["notification"]))
        .replace("{session}", str(world.ids["alice_session_row"]))
        .replace("{alice}", world.alice)
    )


@pytest.mark.parametrize("attack", ATTACKS, ids=lambda attack: attack.name)
def test_write_and_delete_never_change_another_users_records(
    world: World, attack: Attack
) -> None:
    alice_client = world.client(world.alice)
    bob_client = world.client(world.bob)
    assert alice_client.get("/api/notifications").status_code == 200
    before = _alice_state(world)

    body = (
        json.loads(_render(world, json.dumps(attack.body)))
        if attack.body is not None
        else None
    )
    response = bob_client.request(attack.method, _render(world, attack.path), json=body)

    assert response.status_code in attack.status, (
        attack.name,
        response.status_code,
        response.text[:300],
    )
    assert _alice_state(world) == before, attack.name
    if attack.own_effect is not None:
        assert attack.own_effect(world), f"{attack.name}: Bob's own call had no effect"

    # Alice's session and credential are still fully usable afterwards.
    after = alice_client.get("/api/notifications")
    assert after.status_code == 200
    assert ALICE in after.text
    vault = SecretVault(world.db).describe(world.alice, "gemini")
    assert vault.configured
    assert ALICE_KEY not in response.text


def test_the_owner_can_perform_the_same_calls(world: World) -> None:
    """The refusals above are about ownership, not about broken routes."""
    alice = world.client(world.alice)
    nid = world.ids["notification"]
    assert alice.post(f"/api/notifications/{nid}/read").status_code == 200
    assert alice.delete(f"/api/notifications/{nid}").status_code == 200
    assert alice.post("/api/chat/clear").status_code == 200
    assert world.count("notifications", world.alice) == 0
    assert world.count("chat_messages", world.alice) == 0
    assert world.count("notifications", world.bob) == 1  # Bob's untouched


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_exports_are_bound_to_the_authenticated_owner(world: World) -> None:
    now = datetime.now(tz=timezone.utc)
    export = HealthExport(user_id=1, generated_at=now, schema_version=1, data={})
    export.validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        export.validate(authenticated_user_id=2)

    workout = ExportedWorkout(
        user_id=1,
        provider="hevy",
        name="Synthetic session",
        source_programme_id="programme-1",
        explicit_user_approval=True,
    )
    workout.validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        workout.validate(authenticated_user_id=2)


def test_retained_exports_live_in_a_directory_per_owner(world: World) -> None:
    alice_dir = export_directory(world.db, world.alice)
    bob_dir = export_directory(world.db, world.bob)
    assert alice_dir is not None and bob_dir is not None
    assert alice_dir != bob_dir
    assert alice_dir.parent == bob_dir.parent
    # A hostile identifier can never address Alice's directory (or anything else).
    for hostile in (
        f"../{world.alice}",
        f"{world.bob}/../{world.alice}",
        "..",
        "/etc",
        f"{world.alice}\x00",
        "",
    ):
        assert export_directory(world.db, hostile) is None, hostile


def test_exported_preferences_describe_only_the_owner(world: World) -> None:
    world.client(world.bob).post("/api/settings/display", json={"timezone": "Asia/Tokyo"})
    alice = export_preferences(get_user_settings(world.alice, world.db))
    bob = export_preferences(get_user_settings(world.bob, world.db))
    assert bob["timezone"] == "Asia/Tokyo"
    assert alice["timezone"] != "Asia/Tokyo"


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


def test_a_job_queued_for_a_missing_owner_is_denied_without_running(world: World) -> None:
    ran: list[str] = []
    outcome = run_owned_job(
        OwnedJob("probe", OwnerIdentity("no-such-owner")),
        world.db,
        lambda owner: ran.append(owner.user_id),
    )
    assert outcome.status is JobStatus.DENIED
    assert ran == []


def test_a_job_only_ever_sees_the_records_of_the_owner_it_was_verified_for(
    world: World,
) -> None:
    seen: dict[str, list[str]] = {}

    def work(owner: OwnerIdentity) -> int:
        seen[owner.user_id] = [
            message["content"]
            for message in get_chat_messages(db_path=world.db, user_id=owner.user_id)
        ]
        return 0

    for user_id in (world.alice, world.bob):
        outcome = run_owned_job(OwnedJob("probe", OwnerIdentity(user_id)), world.db, work)
        assert outcome.status is JobStatus.COMPLETED
    assert seen[world.alice] and all(ALICE in text for text in seen[world.alice])
    assert seen[world.bob] and not any(ALICE in text for text in seen[world.bob])


def test_a_job_queued_before_an_account_closed_is_denied_and_cannot_write(
    world: World,
) -> None:
    job = OwnedJob("probe", OwnerIdentity(world.bob))
    with sqlite3.connect(world.db) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (world.bob,))

    ran: list[str] = []
    outcome = run_owned_job(job, world.db, lambda owner: ran.append(owner.user_id))
    assert outcome.status is JobStatus.DENIED and ran == []

    # Even a straggler that skips the job runner cannot write for the closed
    # account: the database refuses an owner that no longer exists.
    with pytest.raises(sqlite3.IntegrityError, match="must reference an existing user"):
        save_chat_message("assistant", "late", world.db, user_id=world.bob)


def test_one_owners_failing_job_never_reaches_another_owners_job(world: World) -> None:
    ran: list[str] = []

    def work(owner: OwnerIdentity) -> int:
        ran.append(owner.user_id)
        if owner.user_id == world.alice:
            raise RuntimeError("synthetic failure")
        return 0

    users: list[Any] = [{"id": world.alice}, {"id": "forged-owner"}, {"id": world.bob}]
    config = SimpleNamespace(database_path=world.db)
    assert _run_per_owner(config, "probe", users, work) == 1  # type: ignore[arg-type]
    assert ran == [world.alice, world.bob]  # the forged id was denied, Bob still ran


def test_job_entry_points_reverify_the_owner_before_touching_data(world: World) -> None:
    for claim in (None, "", "no-such-owner"):
        with pytest.raises(OwnershipError):
            authorize_owner(world.db, claim)
    before = world.rows("SELECT * FROM workout_history ORDER BY id")
    # The destructive history rebuild refuses an unverifiable owner before any
    # network call or deletion.
    assert "error" in sync_all("synthetic-key", world.db, user_id="no-such-owner")
    assert world.rows("SELECT * FROM workout_history ORDER BY id") == before
    assert authorize_owner(world.db, world.alice).user_id == world.alice


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "_carries_marker"), READ_ROUTES)
def test_personalised_responses_are_private_and_never_shared(
    world: World, path: str, _carries_marker: bool
) -> None:
    for user_id in (world.alice, world.bob):
        response = world.client(user_id).get(path)
        cache_control = response.headers.get("cache-control", "").lower()
        assert "no-store" in cache_control and "private" in cache_control, path
        assert "public" not in cache_control, path


def test_refusals_are_also_private(world: World) -> None:
    nid = world.ids["notification"]
    refused = world.client(world.bob).post(f"/api/notifications/{nid}/read")
    assert refused.status_code == 404
    assert "no-store" in refused.headers.get("cache-control", "").lower()
    assert ALICE not in refused.text


def test_a_cache_keyed_by_a_client_chosen_id_is_still_owner_scoped(world: World) -> None:
    """Alice and Bob stored a reasoning log under the *same* context id."""
    alice = world.client(world.alice).get(f"/api/xai_reasoning/{REASONING_CONTEXT}")
    bob = world.client(world.bob).get(f"/api/xai_reasoning/{REASONING_CONTEXT}")
    assert alice.status_code == bob.status_code == 200
    assert f"{ALICE} reasoning" in alice.text and BOB not in alice.text
    assert f"{BOB} reasoning" in bob.text and ALICE not in bob.text


def test_derived_data_caches_are_owner_scoped(world: World) -> None:
    payloads = json.dumps(get_cached_workout_payloads(world.bob, world.db))
    assert BOB in payloads and ALICE not in payloads
    insight = json.dumps(get_dashboard_insight(db_path=world.db, user_id=world.bob))
    assert BOB in insight and ALICE not in insight
    assert ALICE not in json.dumps(
        get_recent_hevy_logs(limit=20, db_path=world.db, user_id=world.bob)
    )


def test_one_owners_traffic_never_exhausts_another_owners_throttle(world: World) -> None:
    world.module._RATE_LIMITS.clear()
    bob = world.client(world.bob)
    alice = world.client(world.alice)
    statuses = [
        bob.post("/api/settings/display", json={"timezone": "Asia/Tokyo"}).status_code
        for _ in range(11)
    ]
    assert statuses[-1] == 429  # Bob hit his own limit ...
    assert alice.post("/api/settings/display", json={"timezone": "UTC"}).status_code == 200


def test_a_spoofed_forwarded_address_cannot_join_another_owners_throttle_bucket(
    world: World,
) -> None:
    world.module._RATE_LIMITS.clear()
    keys: set[str] = set()
    for user_id in (world.alice, world.bob):
        request = SimpleNamespace(
            session={"user_id": user_id},
            headers={"x-forwarded-for": "203.0.113.7"},
            client=SimpleNamespace(host="198.51.100.1"),
        )
        keys.add(world.module._rate_limit_key(request))
    assert len(keys) == 2
    assert all("203.0.113.7" not in key for key in keys)


def test_scoped_keys_cannot_collide_or_impersonate_another_owner() -> None:
    alice, bob = OwnerIdentity("alice"), OwnerIdentity("bob")
    assert owner_scoped_key(alice, "cursor", "x") != owner_scoped_key(bob, "cursor", "x")
    # A crafted part cannot smuggle a second owner or namespace into the key.
    crafted = owner_scoped_key(bob, "cursor", "x:alice:cursor")
    assert crafted.split(":")[:2] == ["cursor", "bob"]
    assert crafted != owner_scoped_key(alice, "cursor", "x")
    assert owner_scoped_key(alice, "a", "b:c") != owner_scoped_key(alice, "a", "b", "c")
    with pytest.raises(OwnershipError):
        owner_scoped_key("alice", "cursor")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Indirect object references
# ---------------------------------------------------------------------------

#: Every route that takes an object identifier in its path, and the probe that
#: proves another user cannot use it. A route missing here fails the guard below.
OBJECT_ROUTES: dict[str, str] = {
    "/api/notifications/{notification_id}/read": "mark-alices-notification-read",
    "/api/notifications/{notification_id}": "delete-alices-notification",
    "/api/sessions/{session_id}": "revoke-alices-session",
    "/api/health/connections/{connection_id}": "disconnect-and-purge-alices-connection",
    "/api/xai_reasoning/{context_id}": "test_a_cache_keyed_by_a_client_chosen_id_is_still_owner_scoped",
    "/api/provenance/{record_type}/{record_key}": "test_a_record_only_its_owner_has_is_indistinguishable_from_a_missing_one",
}
#: Routes with a path parameter that carry no tenant data.
EXEMPT_ROUTES = {"/{full_path:path}": "serves the public frontend bundle only"}


def test_every_route_with_an_object_identifier_has_a_probe(world: World) -> None:
    from webapp.auth_boundary import is_public_path

    declared = {
        route.path
        for route in world.module.app.routes
        if isinstance(route, APIRoute) and "{" in route.path
    }
    unprobed = {
        path for path in declared if path not in OBJECT_ROUTES and path not in EXEMPT_ROUTES
    }
    assert unprobed == set(), f"add an IDOR probe for: {sorted(unprobed)}"
    assert not (set(OBJECT_ROUTES) - declared), "a probed route no longer exists"
    names = {attack.name for attack in ATTACKS}
    defined = {name for name in globals() if name.startswith("test_")}
    for route, probe in OBJECT_ROUTES.items():
        assert probe in names or probe in defined, (route, probe)
    assert all(not is_public_path(path) for path in OBJECT_ROUTES)


def test_guessing_sequential_ids_only_ever_reaches_the_callers_own_records(
    world: World,
) -> None:
    bob = world.client(world.bob)
    own = {row[0] for row in world.rows("SELECT id FROM notifications WHERE user_id = ?", world.bob)}
    foreign = {row[0] for row in world.rows("SELECT id FROM notifications WHERE user_id = ?", world.alice)}
    outcomes = {
        nid: bob.post(f"/api/notifications/{nid}/read").status_code
        for nid in range(1, max(own | foreign) + 6)
    }
    assert {nid for nid, code in outcomes.items() if code == 200} == own
    # A foreign id answers exactly like an id that does not exist at all.
    assert {outcomes[nid] for nid in foreign} == {outcomes[max(own | foreign) + 5]} == {404}
    assert world.rows("SELECT is_read FROM notifications WHERE user_id = ?", world.alice) == [(0,)]


def test_a_record_only_its_owner_has_is_indistinguishable_from_a_missing_one(
    world: World,
) -> None:
    save_body_metrics({"weight_kg": 79.0}, "2026-08-02", world.db, user_id=world.alice)
    kind = RecordType.BODY_METRIC.value
    alice = world.client(world.alice).get(f"/api/provenance/{kind}/2026-08-02")
    bob = world.client(world.bob).get(f"/api/provenance/{kind}/2026-08-02")
    missing = world.client(world.bob).get(f"/api/provenance/{kind}/2031-01-01")
    assert alice.status_code == 200
    assert bob.status_code == missing.status_code == 404
    assert bob.json() == missing.json()


def test_a_missing_connection_answers_like_another_users_connection(world: World) -> None:
    bob = world.client(world.bob)
    foreign = bob.delete(f"/api/health/connections/conn-{ALICE}")
    missing = bob.delete("/api/health/connections/conn-does-not-exist")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert world.count("health_connections", world.alice) == 1


def test_programme_identifiers_are_not_global_across_owners(world: World) -> None:
    """Bob holds a real owner context and Alice's programme and revision ids."""
    service = ProgrammeService(world.db)
    bob = authorize_owner(world.db, world.bob)
    programme_id = world.ids[f"programme:{world.alice}"]
    revision_id = world.rows(
        "SELECT id FROM programme_revisions WHERE programme_id = ?", programme_id
    )[0][0]
    before = _alice_state(world)

    assert service.get_programme(bob, programme_id) is None
    assert service.get_active_programme(bob).name == f"{BOB} block"  # his own
    assert service.list_revision_history(bob, programme_id) == []
    assert service.get_revision(bob, programme_id, 1) is None
    assert service.get_programme_week(bob, programme_id, 1) is None
    assert service.list_adaptation_decisions(bob, programme_id) == []
    assert service.list_routine_workout_matches(bob, programme_id) == []
    for change in (service.activate, service.pause, service.complete, service.archive):
        with pytest.raises(ProgrammeNotFoundError):
            change(bob, programme_id)
    with pytest.raises(ProgrammeNotFoundError):
        service.create_revision_from_rebase(
            bob, programme_id, _draft(user_id=world.bob), reason="rebase"
        )
    with pytest.raises(ProgrammeNotFoundError):
        service.save_week_state(bob, programme_id, revision_id, ProgrammeWeekState(week_number=1))
    with pytest.raises(ProgrammeNotFoundError):
        service.save_routine_workout_match(
            bob,
            RoutineWorkoutMatch(
                programme_id=programme_id,
                revision_id=revision_id,
                routine_position=1,
                source_routine_id="routine-b",
                workout_id="workout-1",
                score=0.9,
                confidence=0.8,
                method="provider_id",
            ),
        )

    assert _alice_state(world) == before
    alice = authorize_owner(world.db, world.alice)
    assert service.get_programme(alice, programme_id).status == "active"  # type: ignore[union-attr]


def test_the_database_refuses_a_row_pointed_into_another_users_programme(
    world: World,
) -> None:
    """A repository bug or hand-written SQL still cannot cross the tenant line."""
    alice_programme = world.ids[f"programme:{world.alice}"]
    alice_revision = world.rows(
        "SELECT id FROM programme_revisions WHERE programme_id = ?", alice_programme
    )[0][0]
    bob_programme = world.ids[f"programme:{world.bob}"]
    before = _alice_state(world)

    with sqlite3.connect(world.db) as conn:
        for table, column, key in (
            ("programme_revisions", "programme_id", alice_programme),
            ("programme_routines", "revision_id", alice_revision),
            ("programme_blocks", "programme_id", alice_programme),
            ("exercise_prescriptions", "revision_id", alice_revision),
            ("programme_week_states", "programme_id", alice_programme),
            ("adaptation_decisions", "to_revision_id", alice_revision),
            ("routine_workout_matches", "programme_id", alice_programme),
            ("programme_decisions", "programme_id", alice_programme),
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match="must reference a row owned by the same user"
            ):
                insert_owned_row(conn, table, world.bob, **{column: key})
        # Re-pointing Bob's own programme at Alice's revision is refused as well.
        with pytest.raises(sqlite3.IntegrityError, match="same user"):
            conn.execute(
                "UPDATE programmes SET current_revision_id = ? WHERE id = ?",
                (alice_revision, bob_programme),
            )
        # And a row can never be handed to the other account after the fact.
        with pytest.raises(sqlite3.IntegrityError, match="user_id is immutable"):
            conn.execute(
                "UPDATE programmes SET user_id = ? WHERE id = ?",
                (world.bob, alice_programme),
            )
    assert _alice_state(world) == before
