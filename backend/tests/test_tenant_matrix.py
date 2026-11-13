"""Generated tenant-isolation matrix: Bob cannot read, change, export, queue or
receive Alice's data.

The cells are generated from the live code (see ``tenant_matrix.py``), not from a
hand-kept list, and every probe is paired with a control: the same call works for
the owner, so a pass can never be an empty answer or a broken route. Two valid
accounts, the real application, the real repositories and a real SQLite file; only
the AI SDK and Hevy are replaced by recorders that note which credential they
were given and what they were sent.

``test_the_matrix_is_complete`` fails when a route, table, job or outbound sender
exists without a cell. ``test_the_matrix_renders`` prints the matrix (``-s``) and
writes it to ``$TENANT_MATRIX_REPORT`` when set.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("authlib")

import account_deletion
import tenant_matrix as tm
import webapp.app as _application
from config import Config
from database import (
    USER_OWNED_TABLES,
    get_notifications,
    get_or_create_user,
    get_push_subscriptions,
)
from encryption import is_sealed
from session_registry import list_user_sessions

BACKEND = Path(__file__).resolve().parents[1]

# The routes are read from the application module itself, so the parametrisation
# below is generated from the route table rather than listed by hand.
_ATTACKED = {attack.route for attack in tm.ATTACKS}
GET_ROUTES = [
    path
    for method, path in tm.discover_routes(_application.app)
    if method == "GET" and (method, path) not in _ATTACKED
]
QUEUE_KINDS = tm.scheduler_job_kinds((BACKEND / "scheduler.py").read_text())


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tm.World]:
    monkeypatch = pytest.MonkeyPatch()
    try:
        built = tm.build_world(tmp_path_factory.mktemp("tenant-matrix"), monkeypatch)
        built.outbound = tm.install_outbound(monkeypatch)
        yield built
    finally:
        monkeypatch.undo()
        # Leave the application module as other test modules expect to find it.
        importlib.reload(_application)


@pytest.fixture(autouse=True)
def pristine(world: tm.World) -> None:
    """Every probe starts from the same freshly seeded database."""
    world.restore()
    world.outbound.clear()


def _config(world: tm.World) -> Config:
    return Config(
        hevy_api_key="matrix-server-hevy-key",
        gemini_api_key="matrix-server-gemini-key",
        gemini_model="gemini-matrix",
        health_connect_file=None,
        database_path=world.db,
        hevy_sync_routines=True,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )


def _assert_outbound_is_only(
    world: tm.World, caller: str, *, provided: set[str] | None = None
) -> None:
    """Everything sent to a model or to Hevy used the caller's key and data only.

    ``provided`` holds credentials the caller supplied in this request (for
    example a candidate key being verified before it is stored). They belong to
    the caller by construction, never to the other account.
    """
    own, other = world.canaries(caller), world.canaries(world.other(caller))
    supplied = provided or set()
    assert not supplied & set(other.secrets()), "a request carried another account's key"
    allowed = set(own.secrets()) | supplied
    assert world.outbound.credentials() <= allowed, "a call used another key"
    leaked = [c for c in other.all() if c in world.outbound.text()]
    assert not leaked, f"a prompt carried another account's data: {leaked}"


def _submitted_keys(attack: tm.Attack) -> set[str]:
    """A credential the caller included in the request body, if any."""
    if not isinstance(attack.body, dict):
        return set()
    value = attack.body.get("api_key")
    return {value} if isinstance(value, str) and value else set()


# ---------------------------------------------------------------------------
# Read: every route behind the login boundary that is not a mutation
# ---------------------------------------------------------------------------

#: Concrete request for routes that need a path parameter or a query.
READ_REQUESTS = {
    "/api/provenance/{record_type}/{record_key}": "/api/provenance/body_metrics/2026-08-01",
    "/api/xai_reasoning/{context_id}": f"/api/xai_reasoning/{tm.REASONING_CONTEXT}",
    "/api/rag_search": "/api/rag_search?q=how+is+my+squat",
}

#: Routes whose answer must show the owner's own canary, so a pass is not an empty reply.
CARRIES_OWNER_DATA = frozenset(
    {
        "/api/chat-history",
        "/api/chat/history",
        "/api/checkins",
        "/api/dashboard",
        "/api/health/connections",
        "/api/history",
        "/api/notifications",
        "/api/progress",
        "/api/settings",
        "/api/stats",
        "/api/xai_reasoning/{context_id}",
    }
)


@pytest.mark.parametrize("read_route", GET_ROUTES)
def test_read_never_returns_another_accounts_data(
    world: tm.World, read_route: str
) -> None:
    request_path = READ_REQUESTS.get(read_route, read_route)
    clients = {
        world.bob: world.client(world.bob),
        world.alice: world.client(world.alice),
    }
    seen: dict[str, str] = {}
    for caller, client in clients.items():
        world.outbound.clear()
        bystander = world.other(caller)
        before = world.snapshot(bystander)

        response = client.get(request_path, follow_redirects=False)

        # A 502 is the fake provider being offline, and only counts if the route
        # really reached out (with the caller's key, checked below).
        reached_hevy = any(kind == "hevy" for kind, _, _ in world.outbound.calls)
        assert response.status_code < 500 or (
            response.status_code == 502 and reached_hevy
        ), (
            read_route,
            response.status_code,
        )
        text = response.text + json.dumps(dict(response.headers))
        assert not world.leaked(bystander, text), f"{read_route} leaked to {caller}"
        assert not any(secret in text for secret in world.canaries(caller).secrets()), (
            f"{read_route} returned a stored secret to its own owner"
        )
        _assert_outbound_is_only(world, caller)
        assert world.snapshot(bystander) == before, "a read changed another account"
        seen[caller] = text

    if read_route in CARRIES_OWNER_DATA:
        assert tm.ALICE.marker in seen[world.alice], f"{read_route}: Alice sees no data"
        assert tm.BOB.marker in seen[world.bob], f"{read_route}: Bob sees no data"


# ---------------------------------------------------------------------------
# Change (and, for routes that also send, queue or deliver: export, queue, receive)
# ---------------------------------------------------------------------------


def _send(
    client: Any, world: tm.World, attack: tm.Attack, *, missing: bool = False
) -> Any:
    path = tm.render(world, attack.path, missing=missing)
    body = (
        json.loads(tm.render(world, json.dumps(attack.body), missing=missing))
        if attack.body is not None
        else None
    )
    return client.request(attack.method, path, json=body, follow_redirects=False)


def _open(world: tm.World) -> tuple[Any, Any]:
    """A client per account, and the id of Alice's session row."""
    alice, bob = world.client(world.alice), world.client(world.bob)
    world.ids["session"] = list_user_sessions(world.alice, None, world.db)[0]["id"]
    return alice, bob


@pytest.mark.parametrize("attack", tm.ATTACKS, ids=lambda attack: attack.id)
def test_change_never_reaches_another_accounts_data(
    world: tm.World, attack: tm.Attack
) -> None:
    _, bob = _open(world)
    before = world.snapshot(world.alice)

    response = _send(bob, world, attack)

    assert response.status_code in attack.foreign, (
        response.status_code,
        response.text[:200],
    )
    named = tm.render(world, attack.path).rsplit("/", 1)[-1].split("?")[0]
    echoed = (response.text + json.dumps(dict(response.headers))).replace(named, "")
    assert not world.leaked(world.alice, echoed), f"{attack.id} leaked to Bob"
    assert not any(secret in echoed for secret in tm.BOB.secrets())
    assert world.snapshot(world.alice) == before, f"{attack.id} changed Alice's data"
    _assert_outbound_is_only(world, world.bob, provided=_submitted_keys(attack))
    if attack.names_object:
        # Alice's object answers exactly like one that never existed.
        missing = _send(bob, world, attack, missing=True)
        assert (missing.status_code, missing.text.replace("conn-missing", named)) == (
            response.status_code,
            response.text,
        )

    # Control: the owner makes the same call and it works.
    world.restore()
    world.outbound.clear()
    alice, _ = _open(world)
    before = world.snapshot(world.alice)
    owner = _send(alice, world, attack)
    assert owner.status_code in attack.legit, (owner.status_code, owner.text[:200])
    assert not any(secret in owner.text for secret in tm.ALICE.secrets()), (
        "a secret was echoed"
    )
    if attack.changes_owner:
        assert world.snapshot(world.alice) != before, (
            f"{attack.id}: the owner's call did nothing"
        )
    if attack.owner_key_used is not None:
        assert attack.owner_key_used(tm.ALICE) in world.outbound.credentials()
    _assert_outbound_is_only(world, world.alice, provided=_submitted_keys(attack))


# ---------------------------------------------------------------------------
# Queue: work is dispatched, and executed, for exactly one verified owner
# ---------------------------------------------------------------------------


def _run_job(kind: str, world: tm.World, monkeypatch: pytest.MonkeyPatch) -> None:
    import scheduler

    config = _config(world)
    if kind == "coaching":
        started: list[str | None] = []

        def record(preview: bool = False, user_id: str | None = None) -> int:
            started.append(user_id)
            return 0

        monkeypatch.setattr("main.run", record)
        scheduler.run_coaching(config)
        assert sorted(filter(None, started)) == sorted([world.alice, world.bob])
        assert None not in started
    elif kind == "insight-daily":
        scheduler.run_daily_insight(config)
    elif kind == "insight-weekly":
        scheduler.run_weekly_correlations(config)
    else:  # pragma: no cover - a new kind needs a probe, see the completeness gate
        raise AssertionError(f"no probe for the {kind} job")


@pytest.mark.parametrize("kind", QUEUE_KINDS)
def test_queued_jobs_run_only_for_their_own_owner(
    world: tm.World, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    before = {user: world.snapshot(user) for user in (world.alice, world.bob)}

    _run_job(kind, world, monkeypatch)

    for _, key, text in world.outbound.calls:
        owner = world.alice if key in tm.ALICE.secrets() else world.bob
        assert key in world.canaries(owner).secrets()
        assert not world.leaked(world.other(owner), text), (
            "a job prompt crossed accounts"
        )
    for user in (world.alice, world.bob):
        # Whatever a job wrote for an account carries only that account's canaries.
        for note in get_notifications(user, db_path=world.db):
            assert not world.leaked(world.other(user), json.dumps(note))
        if kind != "coaching":
            assert world.snapshot(user) != before[user], (
                f"{kind} did nothing for {user}"
            )
    assert kind == "coaching" or world.outbound.calls, f"{kind} reached no model"


def test_a_deletion_queued_by_one_account_runs_only_on_that_account(
    world: tm.World,
) -> None:
    alice, _ = _open(world)
    before = world.snapshot(world.alice)
    status = account_deletion.request_deletion(
        world.db, world.bob, "account", tm.BOB.email
    )
    assert status["state"] == "pending"
    assert world.snapshot(world.bob)["chat_messages"], "Bob's data waits out the window"

    later = datetime.now(tz=timezone.utc) + timedelta(days=30)
    assert account_deletion.process_due_deletions(world.db, now=later) >= 1

    assert world.snapshot(world.bob)["chat_messages"] == []  # the queued work ran
    assert world.snapshot(world.alice) == before  # ...and only there
    assert tm.ALICE.marker in alice.get("/api/chat/history").text


class _Reply:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_connector_sync_uses_and_rotates_only_the_owners_token(
    world: tm.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import google_health_client
    import requests
    from secret_vault import SecretVault

    vault = SecretVault(world.db)
    accounts = ((world.alice, tm.ALICE, 81_000), (world.bob, tm.BOB, 92_000))
    for user, canary, _ in accounts:
        vault.store(user, "google_health", refresh_token=f"gh-refresh-{canary.marker}")
    presented: list[str] = []

    def post(url: str, **kwargs: Any) -> _Reply:
        token = kwargs["data"]["refresh_token"]
        presented.append(token)
        return _Reply(
            {"access_token": f"access:{token}", "refresh_token": f"rotated:{token}"}
        )

    def get(url: str, **kwargs: Any) -> _Reply:
        if "/dataTypes/weight/" not in url:
            return _Reply({"dataPoints": []})
        grams = next(
            g for _, c, g in accounts if c.marker in kwargs["headers"]["Authorization"]
        )
        sample = {
            "weightGrams": grams,
            "sampleTime": {"physicalTime": "2026-08-01T08:00:00Z"},
        }
        return _Reply({"dataPoints": [{"weight": sample}]})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)

    def token_of(user: str) -> str:
        credential = vault.resolve(user, "google_health", purpose="test_readback")
        assert credential.refresh_token is not None
        return credential.refresh_token.reveal()

    for user, canary, grams in accounts:
        other_token = token_of(world.other(user))
        result = google_health_client.sync_body_metrics(
            "id", "secret", None, world.db, user_id=user
        )
        assert result == {"weight_kg": grams / 1000}
        assert token_of(user) == f"rotated:gh-refresh-{canary.marker}"
        assert (
            token_of(world.other(user)) == other_token
        )  # the neighbour's cursor is untouched

    assert sorted(presented) == sorted(f"gh-refresh-{c.marker}" for _, c, _ in accounts)


# ---------------------------------------------------------------------------
# Receive: messages, notifications and model prompts reach only their owner
# ---------------------------------------------------------------------------

#: Every outbound ``send_*`` function in production code, and what it delivers.
CHANNELS = {
    "push_notifier.send_push_notification": "web push, to the owner's registered devices",
    "telegram_notifier.send_telegram_message": "not wired into any production caller",
}


def test_a_shared_browser_belongs_to_one_account_at_a_time(world: tm.World) -> None:
    alice, bob = world.client(world.alice), world.client(world.bob)
    device = {
        "endpoint": "https://push.example.test/shared-browser",
        "keys": {"p256dh": "p", "auth": "a"},
    }
    phone = {
        "endpoint": "https://push.example.test/alices-phone",
        "keys": {"p256dh": "p", "auth": "a"},
    }
    assert alice.post("/api/settings/push-subscribe", json=device).status_code == 200
    assert alice.post("/api/settings/push-subscribe", json=phone).status_code == 200

    assert bob.post("/api/settings/push-subscribe", json=device).status_code == 200

    alice_devices = {
        s["endpoint"] for s in get_push_subscriptions(world.alice, world.db)
    }
    bob_devices = {s["endpoint"] for s in get_push_subscriptions(world.bob, world.db)}
    assert device["endpoint"] not in alice_devices  # the device moved to Bob
    assert phone["endpoint"] in alice_devices  # her other devices are untouched
    assert device["endpoint"] in bob_devices
    assert not alice_devices & bob_devices  # never two owners for one device


def test_every_account_is_notified_only_at_its_own_devices(world: tm.World) -> None:
    for user in (world.alice, world.bob):
        mine = {s["endpoint"] for s in get_push_subscriptions(user, world.db)}
        theirs = {
            s["endpoint"] for s in get_push_subscriptions(world.other(user), world.db)
        }
        assert mine, "the account has a device to receive on"
        assert not mine & theirs
        assert all(world.canaries(user).marker in endpoint for endpoint in mine)


def test_in_app_notifications_belong_to_the_account_they_were_written_for(
    world: tm.World,
) -> None:
    for user in (world.alice, world.bob):
        client = world.client(user)
        body = client.get("/api/notifications").text
        assert world.canaries(user).marker in body
        assert not world.leaked(world.other(user), body)
        assert client.post("/api/notifications/test-coach").status_code == 200
    for user in (world.alice, world.bob):
        mine = get_notifications(user, db_path=world.db)
        assert len(mine) == 2
        assert not world.leaked(world.other(user), json.dumps(mine))


def test_model_prompts_carry_only_the_requesting_accounts_data(
    world: tm.World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in ("insight-daily", "insight-weekly"):
        world.outbound.clear()
        _run_job(kind, world, monkeypatch)
        for user in (world.alice, world.bob):
            canary = world.canaries(user)
            own = [
                text for _, key, text in world.outbound.calls if key in canary.secrets()
            ]
            assert own, f"{kind}: {user} was not served"
            assert all(canary.marker in text or canary.weight in text for text in own)
            assert not any(world.leaked(world.other(user), text) for text in own)


def test_every_outbound_sender_is_a_known_channel() -> None:
    assert set(tm.outbound_senders(BACKEND)) == set(CHANNELS), (
        "a new send_* function needs a receive probe and an entry in CHANNELS"
    )


# ---------------------------------------------------------------------------
# Export: what leaves the system leaves under the owner's credential, for the owner
# ---------------------------------------------------------------------------


def test_retained_exports_live_in_a_directory_per_owner(world: tm.World) -> None:
    alice_dir = account_deletion.export_directory(world.db, world.alice)
    bob_dir = account_deletion.export_directory(world.db, world.bob)
    assert alice_dir is not None and bob_dir is not None and alice_dir != bob_dir
    assert alice_dir.parent == bob_dir.parent
    for hostile in ("../" + world.alice, world.alice + "/..", "", "a/b", "..", "."):
        assert account_deletion.export_directory(world.db, hostile) is None


def test_outbound_connector_calls_use_the_owners_own_credential(
    world: tm.World,
) -> None:
    import main

    config = _config(world)
    assert main._scope_config_to_user(config, world.bob).hevy_api_key == tm.BOB.hevy_key
    assert (
        main._scope_config_to_user(config, world.alice).hevy_api_key
        == tm.ALICE.hevy_key
    )

    keyless = get_or_create_user(
        "keyless-matrix@example.test", "Keyless", db_path=world.db
    )
    # Neither the server's key nor another account's is ever lent to an account.
    assert main._scope_config_to_user(config, str(keyless["id"])).hevy_api_key is None


def test_no_export_route_exists_without_a_probe(world: tm.World) -> None:
    exporting = [
        f"{method} {path}"
        for method, path in tm.discover_routes(world.module.app)
        if ("export" in path.lower() or "download" in path.lower())
        and (method, path) not in _ATTACKED
    ]
    assert not exporting, f"register an export probe for: {exporting}"


# ---------------------------------------------------------------------------
# Generation: every surface has a cell, and the matrix renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    operation: str
    surface: str
    probe: str
    route: tuple[str, str] | None = None


def build_cells() -> list[Cell]:
    read, change = (
        "test_read_never_returns_another_accounts_data",
        "test_change_never_reaches_another_accounts_data",
    )
    cells = [Cell("read", f"GET {path}", read, ("GET", path)) for path in GET_ROUTES]
    for attack in tm.ATTACKS:
        for operation in ("change", *attack.also):
            cells.append(Cell(operation, attack.id, change, attack.route))
    queue = "test_queued_jobs_run_only_for_their_own_owner"
    cells += [Cell("queue", f"scheduler job {kind}", queue) for kind in QUEUE_KINDS]
    cells += [
        Cell(
            "queue",
            "connector sync token and cursor",
            "test_connector_sync_uses_and_rotates_only_the_owners_token",
        ),
        Cell(
            "queue",
            "account deletion",
            "test_a_deletion_queued_by_one_account_runs_only_on_that_account",
        ),
        Cell(
            "receive",
            "web push: a shared browser",
            "test_a_shared_browser_belongs_to_one_account_at_a_time",
        ),
        Cell(
            "receive",
            "web push: registered devices",
            "test_every_account_is_notified_only_at_its_own_devices",
        ),
        Cell(
            "receive",
            "in-app notifications",
            "test_in_app_notifications_belong_to_the_account_they_were_written_for",
        ),
        Cell(
            "receive",
            "model prompts",
            "test_model_prompts_carry_only_the_requesting_accounts_data",
        ),
        Cell(
            "export",
            "retained exports",
            "test_retained_exports_live_in_a_directory_per_owner",
        ),
        Cell(
            "export",
            "connector credentials",
            "test_outbound_connector_calls_use_the_owners_own_credential",
        ),
    ]
    return cells


def test_the_matrix_is_complete(world: tm.World) -> None:
    cells = build_cells()

    routes = set(tm.discover_routes(world.module.app))
    covered = {cell.route for cell in cells if cell.route}
    assert not routes - covered, f"routes without a cell: {sorted(routes - covered)}"
    assert not covered - routes, (
        f"cells for routes that are gone: {sorted(covered - routes)}"
    )

    for operation in tm.OPERATIONS:
        assert any(cell.operation == operation for cell in cells), (
            f"no {operation} cell"
        )

    # Every owned store holds a canary for both accounts, so a leak from it is visible.
    assert set(tm.owned_tables(world.db)) == set(USER_OWNED_TABLES)
    for table in USER_OWNED_TABLES:
        for user, canary in ((world.alice, tm.ALICE), (world.bob, tm.BOB)):
            rows = world.rows(f"SELECT * FROM {table} WHERE user_id = ?", user)
            assert rows, f"{table} has no row for {canary.marker}"
            text = json.dumps([list(map(str, row)) for row in rows])
            if table == "user_api_keys":
                # Sealed at rest: the stored value is ciphertext, never the secret.
                assert not any(secret in text for secret in canary.secrets())
                assert all(is_sealed(str(row[3])) for row in rows), (
                    "a credential is stored in the clear"
                )
                continue
            assert any(token in text for token in canary.all()), (
                f"{table} carries no canary"
            )


def test_the_matrix_renders(
    world: tm.World, capsys: pytest.CaptureFixture[str]
) -> None:
    cells = build_cells()
    lines = ["# Tenant-isolation matrix", ""]
    for operation in tm.OPERATIONS:
        surfaces = sorted({c.surface for c in cells if c.operation == operation})
        lines += [
            f"## {operation} ({len(surfaces)} surfaces)",
            *[f"- {s}" for s in surfaces],
            "",
        ]
    report = "\n".join(lines)
    target = os.environ.get("TENANT_MATRIX_REPORT")
    if target:
        Path(target).write_text(report)
    with capsys.disabled():
        print(report)
    assert all(f"## {operation} (" in report for operation in tm.OPERATIONS)
