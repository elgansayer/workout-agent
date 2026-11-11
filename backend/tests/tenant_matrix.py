"""Support for the generated tenant-isolation matrix (``test_tenant_matrix.py``).

Two valid accounts, Alice and Bob, live in one real SQLite database that the real
FastAPI application serves. Both own rows in every user-owned store, and every
row carries a canary that belongs to its owner. The matrix is *generated* from
what the code contains - the route table, the ownership registry, the live
schema, the scheduler's job kinds, the outbound senders - and asks, for each
surface and each of ``read``, ``change``, ``export``, ``queue`` and ``receive``,
whether Bob can reach Alice's data. A new route, table, job or sender without a
cell fails the build, so isolation cannot silently stop being proven.

Only the network edges (the AI SDK and Hevy) are replaced; every repository, the
vault, the scheduler and the application are real.

All data is synthetic.
"""

from __future__ import annotations

import ast
import importlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ai_provider
from ai_provider import AIProvider
from cryptography.fernet import Fernet
from database import (
    USER_OWNED_TABLES,
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_checkin,
    save_daily_log,
    save_dashboard_insight,
    save_deep_correlation,
    save_hevy_connection,
    save_notification,
    save_progress,
    save_push_subscription,
    save_reasoning_log,
    save_training_rules,
    save_user_preferences,
    save_workout,
    set_meta,
)
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from hevy_client import HevyError
from hevy_parser import ExerciseSummary, WorkoutSummary
from secret_vault import SecretVault
from session_helpers import signed_session_cookie
from test_database import _exercise_summary

OPERATIONS = ("read", "change", "export", "queue", "receive")

_SIGNING_VALUE = "tenant-matrix-signing-value-long-enough"
REASONING_CONTEXT = "matrix-2026-08-01"


@dataclass(frozen=True)
class Canaries:
    """Everything that identifies one account's data, so a leak is detectable."""

    marker: str  # appears in every free-text field the account owns
    weight: str  # a distinctive number for rows that hold no text
    ai_key: str  # the account's stored AI credential
    hevy_key: str  # the account's stored connector credential
    email: str

    def all(self) -> tuple[str, ...]:
        return (self.marker, self.weight, self.ai_key, self.hevy_key, self.email)

    def secrets(self) -> tuple[str, ...]:
        return (self.ai_key, self.hevy_key)


ALICE = Canaries(
    "ALICE-CANARY-7c41",
    "81.234",
    "sk-alice-ai-0123456789abcdef",
    "hevy-alice-0123456789abcdef",
    "alice-matrix@example.test",
)
BOB = Canaries(
    "BOB-CANARY-92ab",
    "92.345",
    "sk-bob-ai-fedcba9876543210",
    "hevy-bob-fedcba9876543210",
    "bob-matrix@example.test",
)


# ---------------------------------------------------------------------------
# Outbound edges: everything that leaves the system on an account's behalf
# ---------------------------------------------------------------------------


@dataclass
class Outbound:
    """Every call to an AI model or Hevy, with the credential it used."""

    calls: list[tuple[str, str, str]] = field(default_factory=list)  # kind, key, text

    def credentials(self) -> set[str]:
        return {key for _, key, _ in self.calls}

    def text(self) -> str:
        return "\n".join(text for _, _, text in self.calls)

    def clear(self) -> None:
        self.calls.clear()


def install_outbound(monkeypatch: Any) -> Outbound:
    """Replace the network edges with recorders; nothing else is faked."""
    outbound = Outbound()

    class Recording(AIProvider):
        def __init__(self, api_key: str, model: str = "") -> None:
            self._api_key = api_key

        def generate(self, prompt: str, *, stream: bool = False) -> str:
            outbound.calls.append(("ai", self._api_key, prompt))
            seen = " ".join(
                sorted({c for who in (ALICE, BOB) for c in who.all() if c in prompt})
            )
            if "Output ONLY valid JSON" in prompt:
                return json.dumps(
                    {"fatigue": "ok", "wins_stalls": seen, "advice": "ok"}
                )
            return f"RECORDED saw {seen}"

        def name(self) -> str:
            return "Recording"

    for provider_id in list(ai_provider.PROVIDERS):
        monkeypatch.setitem(
            ai_provider.PROVIDERS,
            provider_id,
            {"class": Recording, "default_model": "recording"},
        )

    def fetch_snapshot(api_key: str, **_: Any) -> Any:
        outbound.calls.append(("hevy", api_key, ""))
        raise HevyError("offline")

    def sync_all(api_key: str, *_: Any, **__: Any) -> dict[str, Any]:
        outbound.calls.append(("hevy", api_key, ""))
        return {"workouts_found": 0, "processed": 0}

    def verify(api_key: str, *_: Any, **__: Any) -> Any:
        from hevy_connection import HevyCredentialState, HevyCredentialTest

        outbound.calls.append(("hevy", api_key, ""))
        return HevyCredentialTest(
            HevyCredentialState.CONNECTED,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            capabilities={"workouts": True},
            workout_count=1,
        )

    def workout_count(api_key: str, *_: Any, **__: Any) -> int:
        outbound.calls.append(("hevy", api_key, ""))
        return 1

    monkeypatch.setattr("hevy_reader.fetch_user_training_snapshot", fetch_snapshot)
    monkeypatch.setattr("scripts.sync_history.sync_all", sync_all)
    monkeypatch.setattr("webapp.app.verify_hevy_credentials", verify)
    monkeypatch.setattr("hevy_client.get_workout_count", workout_count)
    return outbound


# ---------------------------------------------------------------------------
# The world: two accounts, the real application, a restorable database
# ---------------------------------------------------------------------------


@dataclass
class World:
    module: Any
    db: str
    golden: str
    alice: str
    bob: str
    ids: dict[str, Any] = field(default_factory=dict)
    outbound: Outbound = field(default_factory=Outbound)

    def canaries(self, user_id: str) -> Canaries:
        return ALICE if user_id == self.alice else BOB

    def other(self, user_id: str) -> str:
        return self.bob if user_id == self.alice else self.alice

    def client(self, user_id: str) -> TestClient:
        client = TestClient(self.module.app, raise_server_exceptions=False)
        client.cookies.set(
            self.module.SESSION_SECURITY.cookie.name,
            signed_session_cookie(
                _SIGNING_VALUE,
                user=self.canaries(user_id).email,
                user_id=user_id,
                session_id=self.module.SESSION_STORE.create(user_id),
            ),
        )
        return client

    def rows(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.db) as conn:
            return conn.execute(sql, params).fetchall()

    def snapshot(self, user_id: str) -> dict[str, Any]:
        """Every row the account owns, in every table that carries its user id."""
        with sqlite3.connect(self.db) as conn:
            tables = [
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
                if "user_id"
                in [col[1] for col in conn.execute(f"PRAGMA table_info({name})")]
            ]
            state: dict[str, Any] = {
                table: conn.execute(
                    f"SELECT * FROM {table} WHERE user_id = ? ORDER BY rowid",
                    (user_id,),
                ).fetchall()
                for table in tables
            }
            state["users"] = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchall()
        return state

    def restore(self) -> None:
        """Return the database to the freshly seeded state and clear throttles."""
        with sqlite3.connect(self.golden) as source, sqlite3.connect(self.db) as target:
            source.backup(target)
        self.module._RATE_LIMITS.clear()

    def leaked(self, victim: str, text: str) -> list[str]:
        """Canaries of *victim* that appear in *text* (should be none for others)."""
        return [c for c in self.canaries(victim).all() if c in text]


# ---------------------------------------------------------------------------
# Seeding: a row in every user-owned store, carrying its owner's canary
# ---------------------------------------------------------------------------


def seed_account(db: str, user_id: str, canary: Canaries) -> None:
    """Give *user_id* data in every user-owned store; free text carries the canary."""
    mark = canary.marker
    save_notification(user_id, f"{mark} title", f"{mark} body", db_path=db)
    save_chat_message("user", f"{mark} chat", db, user_id=user_id)
    save_checkin(1, "2026-08-01", 4, 1, f"{mark} check-in", db_path=db, user_id=user_id)
    save_daily_log(
        "2026-08-01",
        1,
        f"{mark} focus",
        "high",
        "plan",
        "life",
        db_path=db,
        user_id=user_id,
    )
    save_body_metrics(
        {"weight_kg": float(canary.weight)}, "2026-08-01", db, user_id=user_id
    )
    save_workout({"title": f"{mark} workout", "id": f"{mark}-w1"}, db, user_id=user_id)
    save_progress(_exercise_summary(f"{mark} Squat", 100.0, 5), db, user_id=user_id)
    # Recent rows, so the scheduled jobs' trailing windows really contain the data.
    today = datetime.now(tz=timezone.utc).date().isoformat()
    save_body_metrics({"weight_kg": float(canary.weight)}, today, db, user_id=user_id)
    save_daily_log(
        today,
        1,
        f"{mark} focus",
        "high",
        f"{mark} plan",
        f"{mark} lifestyle",
        db_path=db,
        user_id=user_id,
    )
    save_progress(
        WorkoutSummary(
            f"S-{mark}",
            today,
            duration_seconds=3600,
            total_volume_kg=3000.0,
            exercises=[ExerciseSummary(f"{mark} Squat", 100.0, 5, 3)],
        ),
        db,
        user_id=user_id,
    )
    save_reasoning_log(
        REASONING_CONTEXT, "Squat", f"{mark} reasoning", db, user_id=user_id
    )
    save_dashboard_insight(f'{{"summary": "{mark} insight"}}', db, user_id=user_id)
    save_deep_correlation(f"{mark} correlation", db, user_id=user_id)
    save_push_subscription(
        user_id, f"https://push.example.test/{mark}", "p256dh-key", "auth-key", db
    )
    save_user_preferences(user_id, goals=[f"{mark} goal"], db_path=db)
    save_training_rules(user_id, {"contextual_notes": [{"note": f"{mark} rule"}]}, db)
    set_meta("matrix_note", f"{mark} meta", db, user_id=user_id)
    save_hevy_connection(
        user_id,
        state="connected",
        api_version="v1",
        capabilities={"workouts": True},
        tested_at="2026-08-01T00:00:00+00:00",
        account_username=f"{mark}-lifter",
        db_path=db,
    )
    vault = SecretVault(db)
    vault.store(user_id, "gemini", api_key=canary.ai_key)
    vault.store(user_id, "hevy", api_key=canary.hevy_key)
    _seed_domain_tables(db, user_id, mark, canary.weight)


def _seed_domain_tables(db: str, user_id: str, mark: str, weight: str) -> None:
    """Rows for the stores no public helper writes (programme domain, health)."""
    stamp = "2026-08-01T00:00:00+00:00"
    with sqlite3.connect(db) as conn:
        run = conn.execute
        run(
            "INSERT INTO programme_state (user_id, current_day, split_name) "
            "VALUES (?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET split_name = excluded.split_name",
            (user_id, f"{mark} split"),
        )
        run(
            "INSERT INTO hevy_routines (user_id, routine_key, routine_id, content_hash) "
            "VALUES (?, ?, ?, 'h')",
            (user_id, f"{mark}-key", f"{mark}-routine"),
        )
        programme_id = run(
            "INSERT INTO programmes (user_id, source, template_key, definition, active, "
            "created_at, updated_at) VALUES (?, 'template', ?, ?, 0, ?, ?)",
            (
                user_id,
                f"{mark}-template",
                f'{{"name": "{mark} programme"}}',
                stamp,
                stamp,
            ),
        ).lastrowid
        revision_id = run(
            "INSERT INTO programme_revisions (user_id, programme_id, revision_number, "
            "reason, engine_version, created_at) VALUES (?, ?, 1, ?, 'test', ?)",
            (user_id, programme_id, f"{mark} revision", stamp),
        ).lastrowid
        run(
            "INSERT INTO programme_routines (user_id, programme_id, revision_id, position, "
            "source_routine_id, title, snapshot_hash, created_at) "
            "VALUES (?, ?, ?, 1, ?, ?, 'hash', ?)",
            (
                user_id,
                programme_id,
                revision_id,
                f"{mark}-routine",
                f"{mark} routine",
                stamp,
            ),
        )
        run(
            "INSERT INTO programme_blocks (user_id, programme_id, revision_id, position, "
            "key, name, start_week, end_week, created_at) VALUES (?, ?, ?, 1, 'block', ?, 1, 1, ?)",
            (user_id, programme_id, revision_id, f"{mark} block", stamp),
        )
        run(
            "INSERT INTO exercise_prescriptions (user_id, programme_id, revision_id, "
            "exercise_template_id, exercise_title, role, progression_method, rule_version, "
            "created_at) VALUES (?, ?, ?, 'template', ?, 'primary', 'linear', 'test', ?)",
            (user_id, programme_id, revision_id, f"{mark} Squat", stamp),
        )
        run(
            "INSERT INTO programme_week_states (user_id, programme_id, revision_id, "
            "week_number, planned_json, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (
                user_id,
                programme_id,
                revision_id,
                f'{{"note": "{mark} week"}}',
                stamp,
                stamp,
            ),
        )
        run(
            "INSERT INTO adaptation_decisions (user_id, programme_id, to_revision_id, "
            "scope_json, inputs_json, output_json, confidence, rule_version, explanation, "
            "confirmed, created_at) VALUES (?, ?, ?, '{}', '{}', '{}', 1.0, 'test', ?, 0, ?)",
            (user_id, programme_id, revision_id, f"{mark} adaptation", stamp),
        )
        run(
            "INSERT INTO programme_decisions (user_id, programme_id, source, decision, "
            "reason, created_at) VALUES (?, ?, 'synthetic', 'keep', ?, ?)",
            (user_id, programme_id, f"{mark} decision", stamp),
        )
        run(
            "INSERT INTO hevy_routine_snapshots (user_id, snapshot_id, routine_id, title, "
            "content_hash, schema_version, snapshot_json) VALUES (?, ?, ?, ?, 'hash', 1, '{}')",
            (user_id, f"{mark}-snapshot", f"{mark}-routine", f"{mark} snapshot"),
        )
        run(
            "INSERT INTO routine_workout_matches (user_id, programme_id, routine_position, "
            "source_routine_id, workout_id, features_json, method, matcher_version, "
            "matched_at, created_at) VALUES (?, ?, 1, ?, ?, '{}', 'synthetic', 'test', ?, ?)",
            (user_id, programme_id, f"{mark}-routine", f"{mark}-workout", stamp, stamp),
        )
        run(
            "INSERT INTO routine_match_overrides (user_id, pattern_type, pattern_value, "
            "routine_id, routine_snapshot_id, created_at) VALUES (?, 'routine', ?, ?, ?, ?)",
            (user_id, f"{mark} pattern", f"{mark}-routine", f"{mark}-snapshot", stamp),
        )
        run(
            "INSERT INTO exercise_history_baselines (user_id, template_id, rule_version, "
            "confidence, baseline_json, created_at) VALUES (?, ?, 'test', 'high', '{}', ?)",
            (user_id, f"{mark}-template", stamp),
        )
        run(
            "INSERT INTO health_connections (id, user_id, provider, state) "
            "VALUES (?, ?, 'withings', 'connected')",
            (f"conn-{mark}", user_id),
        )
        run(
            "INSERT INTO health_metrics (user_id, metric_type, value, unit, observed_at, "
            "provider, connection_id, fingerprint) "
            "VALUES (?, 'weight_kg', ?, 'kg', ?, 'withings', ?, ?)",
            (user_id, float(weight), stamp, f"conn-{mark}", f"fp-{mark}"),
        )
        run(
            "INSERT INTO health_sync_runs (user_id, provider, connection_id, state, "
            "started_at, cursor_after) VALUES (?, 'withings', ?, 'ok', ?, ?)",
            (user_id, f"conn-{mark}", stamp, f"{mark}-cursor"),
        )


def build_world(directory: Path, monkeypatch: Any) -> World:
    """Create the database, both accounts and the real application on top of it."""
    db = str(directory / "matrix.db")
    for name, value in {
        "DATABASE_PATH": db,
        "APP_ENV": "test",
        "WEB_AUTH_SECRET": _SIGNING_VALUE,
        "WEB_GOOGLE_CLIENT_ID": "matrix-client-id",
        "WEB_GOOGLE_CLIENT_SECRET": "matrix-client-value",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "ALLOWED_EMAILS",
        "ALLOW_ANONYMOUS_WEB",
        "GEMINI_API_KEY",
        "HEVY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    init_db(db)  # after the environment: no local single-user tenant exists

    alice = str(get_or_create_user(ALICE.email, "Alice", db_path=db)["id"])
    bob = str(get_or_create_user(BOB.email, "Bob", db_path=db)["id"])
    seed_account(db, alice, ALICE)
    seed_account(db, bob, BOB)

    import webapp.app as app_module

    importlib.reload(app_module)
    golden = str(directory / "matrix-golden.db")
    with sqlite3.connect(db) as source, sqlite3.connect(golden) as target:
        source.backup(target)
    world = World(app_module, db, golden, alice, bob)
    with sqlite3.connect(db) as conn:
        world.ids["notification"] = conn.execute(
            "SELECT id FROM notifications WHERE user_id = ?", (alice,)
        ).fetchone()[0]
    return world


# ---------------------------------------------------------------------------
# Discovery: the surfaces, read from the live code
# ---------------------------------------------------------------------------

#: Routes behind the login boundary that serve the Angular bundle, not data.
STATIC_SHELL_ROUTES = {
    ("GET", "/"): "serves the single-page application shell",
    ("GET", "/{full_path:path}"): "serves the single-page application shell",
}


def discover_routes(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) of the live application behind the login boundary."""
    from webapp.auth_boundary import is_public_path

    found = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods - {"HEAD", "OPTIONS"}
        if not is_public_path(route.path)
    }
    return sorted(found - set(STATIC_SHELL_ROUTES))


def scheduler_job_kinds(scheduler_source: str) -> list[str]:
    """The owner-scoped job kinds ``scheduler`` dispatches, read from its source."""
    kinds: set[str] = set()
    for node in ast.walk(ast.parse(scheduler_source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_per_owner"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            kinds.add(node.args[1].value)
    return sorted(kinds)


def outbound_senders(backend: Path) -> list[str]:
    """Top-level ``send_*`` functions in production modules (``module.function``)."""
    found: list[str] = []
    for path in sorted(backend.rglob("*.py")):
        if {"tests", "__pycache__"} & set(path.relative_to(backend).parts):
            continue
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("send_"):
                found.append(f"{path.stem}.{node.name}")
    return found


def owned_tables(db: str) -> list[str]:
    """Registered user-owned tables, confirmed against the live schema."""
    with sqlite3.connect(db) as conn:
        present = {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    return [table for table in USER_OWNED_TABLES if table in present]


# ---------------------------------------------------------------------------
# Attacks: what Bob sends, for every route that is not a plain read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attack:
    """One route Bob calls. Placeholders name Alice's objects, spelled like the
    route's own parameters: ``{notification_id}``, ``{connection_id}``,
    ``{session_id}``; ``{alice}`` is her user id."""

    method: str
    path: str
    body: Any = None
    #: Statuses a legitimate call by the owner may return.
    legit: tuple[int, ...] = (200,)
    #: Statuses Bob's call may return. Naming Alice's object must look "not found".
    foreign: tuple[int, ...] = (200,)
    #: The owner's own call changes the owner's rows (proves the probe is not vacuous).
    changes_owner: bool = True
    #: Alice's object is named in the request.
    names_object: bool = False
    #: Operations beyond ``change`` the call exercises.
    also: tuple[str, ...] = ()
    #: The credential the owner's own call must reach the provider with.
    owner_key_used: Callable[[Canaries], str] | None = None
    #: Distinguishes two attacks on the same route.
    name: str = ""

    @property
    def route(self) -> tuple[str, str]:
        return self.method, self.path.split("?", 1)[0]

    @property
    def id(self) -> str:
        return f"{self.method} {self.path}" + (f" [{self.name}]" if self.name else "")


def _hevy(canary: Canaries) -> str:
    return canary.hevy_key


def _ai(canary: Canaries) -> str:
    return canary.ai_key


PREVIEW = {"selected_routine_ids": ["routine-1"], "duration_weeks": 8}
PROVIDER_STATUSES = (200, 401, 429, 502)

ATTACKS: tuple[Attack, ...] = (
    Attack("POST", "/api/auth/logout"),
    Attack("POST", "/api/auth/logout-all"),
    Attack("POST", "/api/sessions/revoke-others"),
    Attack("DELETE", "/api/sessions/{session_id}", foreign=(404,), names_object=True),
    Attack(
        "POST",
        "/api/body-metrics/corrections",
        {"date": "2026-08-01", "weight_kg": 70.5},
    ),
    Attack("POST", "/api/chat/clear"),
    Attack("DELETE", "/api/notifications"),
    Attack("POST", "/api/notifications/clear"),
    Attack("POST", "/api/notifications/read-all"),
    Attack("POST", "/api/notifications/test-coach", also=("receive",)),
    Attack(
        "DELETE",
        "/api/notifications/{notification_id}",
        foreign=(404,),
        names_object=True,
    ),
    Attack(
        "POST",
        "/api/notifications/{notification_id}/read",
        foreign=(404,),
        names_object=True,
    ),
    Attack(
        "DELETE",
        "/api/health/connections/{connection_id}?mode=purge&confirmed=true",
        foreign=(404,),
        names_object=True,
    ),
    Attack(
        "POST",
        "/google-health/disconnect",
        legit=(303,),
        foreign=(303,),
        changes_owner=False,
    ),
    Attack("POST", "/api/settings/display", {"timezone": "Asia/Tokyo"}),
    Attack(
        "POST",
        "/api/settings/display",
        {"timezone": "Asia/Tokyo", "user_id": "{alice}"},
        legit=(200, 422),
        foreign=(404,),
        changes_owner=False,
        name="claims Alice's id in the body",
    ),
    Attack(
        "POST",
        "/api/settings/display?owner_id={alice}",
        {"timezone": "Asia/Tokyo"},
        legit=(200, 422),
        foreign=(404,),
        changes_owner=False,
        name="claims Alice's id in the query",
    ),
    Attack("POST", "/api/settings/preferences", {"goals": ["new goal"]}),
    Attack(
        "POST",
        "/api/settings/preferences",
        {"goals": ["new goal"], "profile": {"account_id": "{alice}"}},
        legit=(200, 422),
        foreign=(404,),
        changes_owner=False,
        name="claims Alice's id in nested json",
    ),
    Attack(
        "POST", "/api/settings/rules", {"contextual_notes": [{"note": "a new rule"}]}
    ),
    Attack(
        "POST",
        "/api/settings/push-subscribe",
        {
            "endpoint": "https://push.example.test/bobs-other-phone",
            "keys": {"p256dh": "bob-key", "auth": "bob-auth"},
        },
        also=("receive",),
    ),
    Attack(
        "POST",
        "/api/settings/key",
        {"provider": "openai", "api_key": "sk-new-0123456789abcdef"},
        legit=(200, 201),
        foreign=(200, 201),
    ),
    Attack(
        "POST",
        "/api/settings/key/rotate",
        {"provider": "gemini", "api_key": "sk-rotated-0123456789abcdef"},
    ),
    Attack("POST", "/api/settings/key/revoke", {"provider": "gemini"}),
    Attack("POST", "/api/settings/key/delete", {"provider": "gemini"}),
    Attack(
        "POST",
        "/api/settings/key/test",
        {"provider": "gemini"},
        also=("export",),
        owner_key_used=_ai,
    ),
    Attack(
        "POST",
        "/api/settings/verify-hevy",
        {},
        also=("export",),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/settings/sync-history",
        {},
        also=("queue", "export"),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/programmes/refresh",
        legit=PROVIDER_STATUSES,
        foreign=PROVIDER_STATUSES,
        changes_owner=False,
        also=("export",),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/programmes/preview",
        PREVIEW,
        legit=PROVIDER_STATUSES,
        foreign=PROVIDER_STATUSES,
        changes_owner=False,
        also=("export",),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/programmes/activate",
        {**PREVIEW, "preview_token": "0123456789abcdef0123"},
        legit=(*PROVIDER_STATUSES, 409, 422),
        foreign=(*PROVIDER_STATUSES, 409, 422),
        changes_owner=False,
        also=("export",),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/programmes/history/override",
        {"pattern_type": "workout", "pattern_value": "w-1", "routine_id": "routine-1"},
        legit=(*PROVIDER_STATUSES, 404, 422),
        foreign=(*PROVIDER_STATUSES, 404, 422),
        changes_owner=False,
        also=("export",),
        owner_key_used=_hevy,
    ),
    Attack(
        "POST",
        "/api/account/deletion",
        {"scope": "health_data", "confirmation": "DELETE HEALTH DATA"},
        legit=(200, 202),
        foreign=(200, 202),
        also=("queue",),
    ),
    Attack(
        "POST",
        "/api/account/deletion/cancel",
        legit=(404, 409),
        foreign=(404, 409),
        changes_owner=False,
    ),
)


def render(world: World, text: str, *, missing: bool = False) -> str:
    """Substitute Alice's object ids into an attack, or ids that do not exist."""
    notification = "999999" if missing else str(world.ids["notification"])
    session = "999999" if missing else str(world.ids.get("session"))
    connection = "conn-missing" if missing else f"conn-{ALICE.marker}"
    return (
        text.replace("{notification_id}", notification)
        .replace("{session_id}", session)
        .replace("{connection_id}", connection)
        .replace("{alice}", world.alice)
    )
