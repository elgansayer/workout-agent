"""Concurrent multi-user jobs: no data, credential or output crosses accounts.

Every test runs real worker code (``main.run``, ``insight_cron``, ``scheduler``
and the Google Health sync) against a real SQLite database holding several
synthetic tenants. Only the network edges are faked: the AI SDK, Google, Hevy
and Web Push. Barriers force the jobs to be in flight at the same moment, so a
shared lock, cache or process-global would deadlock or mix tenants.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ai_provider
import google_health_client
import insight_cron
import main
import pytest
import requests
import scheduler
from ai_provider import AIProvider
from config import Config
from cryptography.fernet import Fernet
from database import (
    get_daily_logs,
    get_dashboard_insight,
    get_deep_correlation,
    get_notifications,
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_daily_log,
    save_push_subscription,
    save_user_api_key,
    save_user_preferences,
)
from secret_vault import CredentialUnavailableError, SecretVault
from tenant_isolation import (
    JobStatus,
    OwnedJob,
    OwnerIdentity,
    OwnerLocks,
    run_owned_job,
)

TENANTS = 4
WAIT_SECONDS = 10
_MARKER = re.compile(r"[A-Z]+-MARKER-\d+")


@dataclass(frozen=True)
class Tenant:
    index: int
    user_id: str

    @property
    def ai_key(self) -> str:
        return f"synthetic-ai-key-{self.index}"

    @property
    def ai_model(self) -> str:
        return f"synthetic-model-{self.index}"

    @property
    def hevy_key(self) -> str:
        return f"synthetic-hevy-key-{self.index}"

    @property
    def endpoint(self) -> str:
        return f"https://push.example.test/{self.index}"

    @property
    def plan_marker(self) -> str:
        return f"PLAN-MARKER-{self.index}"

    @property
    def lifestyle_marker(self) -> str:
        return f"LIFESTYLE-MARKER-{self.index}"

    @property
    def weight(self) -> float:
        return 80.0 + self.index


@dataclass(frozen=True)
class World:
    db: str
    config: Config
    tenants: list[Tenant]
    keyless: Tenant  # prefers an AI provider but has stored no key for it


def _make_user(db: str, index: int) -> Tenant:
    user = get_or_create_user(f"synthetic-{index}@example.com", f"Tenant {index}", db)
    return Tenant(index, user["id"])


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db = str(tmp_path / "world.db")
    init_db(db)
    today = datetime.now(tz=UTC).date().isoformat()

    tenants = [_make_user(db, index) for index in range(TENANTS)]
    for tenant in tenants:
        uid = tenant.user_id
        save_user_preferences(
            uid, preferred_ai="openai", ai_model=tenant.ai_model, db_path=db
        )
        save_user_api_key(uid, "openai", tenant.ai_key, db_path=db)
        save_user_api_key(uid, "hevy", tenant.hevy_key, db_path=db)
        save_body_metrics({"weight_kg": tenant.weight}, today, db, user_id=uid)
        save_daily_log(
            today,
            1,
            f"Focus {tenant.index}",
            "high",
            tenant.plan_marker,
            tenant.lifestyle_marker,
            db,
            uid,
        )
        save_push_subscription(uid, tenant.endpoint, f"p256dh-{uid}", f"auth-{uid}", db)

    keyless = _make_user(db, TENANTS)
    save_user_preferences(keyless.user_id, preferred_ai="openai", db_path=db)

    config = Config(
        hevy_api_key="synthetic-server-hevy-key",
        gemini_api_key="synthetic-server-gemini-key",
        gemini_model="gemini-3.8-flash",
        health_connect_file=None,
        database_path=db,
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
    return World(db, config, tenants, keyless)


# ---------------------------------------------------------------------------
# Test doubles for the external edges
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    api_key: str
    model: str
    prompt: str


class _ProviderRecorder:
    """Records every AI call and holds callers at a barrier so they overlap."""

    def __init__(self, parties: int) -> None:
        self._lock = threading.Lock()
        self._gate = threading.Barrier(parties, timeout=WAIT_SECONDS)
        self.calls: list[_Call] = []
        self.gate_broken = False

    def respond(self, api_key: str, model: str, prompt: str) -> str:
        with self._lock:
            self.calls.append(_Call(api_key, model, prompt))
        try:
            self._gate.wait()
        except threading.BrokenBarrierError:
            self.gate_broken = True
            raise
        markers = " ".join(sorted(set(_MARKER.findall(prompt))))
        if "Output ONLY valid JSON" in prompt:
            return json.dumps(
                {
                    "fatigue": f"Green via {api_key}",
                    "wins_stalls": markers,
                    "advice": f"model={model}",
                }
            )
        return f"REPORT via {api_key} model={model} saw {markers}"

    def calls_for(self, api_key: str) -> list[_Call]:
        return [call for call in self.calls if call.api_key == api_key]


def _install_provider(
    monkeypatch: pytest.MonkeyPatch, parties: int
) -> _ProviderRecorder:
    recorder = _ProviderRecorder(parties)

    class Recording(AIProvider):
        def __init__(self, api_key: str, model: str = "") -> None:
            self._api_key = api_key
            self._model = model

        def generate(self, prompt: str, *, stream: bool = False) -> str:
            return recorder.respond(self._api_key, self._model, prompt)

        def name(self) -> str:
            return "Recording"

    monkeypatch.setitem(
        ai_provider.PROVIDERS, "openai", {"class": Recording, "default_model": "unused"}
    )
    return recorder


def _run_concurrently(jobs: list[Callable[[], Any]]) -> list[Any]:
    """Run each job on its own thread; return its result or the exception raised."""
    outcomes: list[Any] = [None] * len(jobs)

    def _wrap(slot: int, job: Callable[[], Any]) -> None:
        try:
            outcomes[slot] = job()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcomes[slot] = exc

    threads = [
        threading.Thread(target=_wrap, args=(slot, job), daemon=True)
        for slot, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_SECONDS * 3)
        assert not thread.is_alive(), "a job never finished (deadlock?)"
    return outcomes


def _other_markers(world: World, tenant: Tenant) -> list[str]:
    return [
        marker
        for other in world.tenants
        if other is not tenant
        for marker in (other.plan_marker, other.lifestyle_marker)
    ]


# ---------------------------------------------------------------------------
# AI insight jobs
# ---------------------------------------------------------------------------


def test_concurrent_daily_insights_never_cross_accounts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install_provider(monkeypatch, parties=len(world.tenants))
    everyone = [*world.tenants, world.keyless]

    outcomes = _run_concurrently(
        [
            lambda t=t: insight_cron.generate_daily_header(  # type: ignore[misc]
                world.config, user_id=t.user_id
            )
            for t in everyone
        ]
    )

    assert not recorder.gate_broken
    for tenant, outcome in zip(world.tenants, outcomes):
        assert outcome is None, outcome
        calls = recorder.calls_for(tenant.ai_key)
        assert len(calls) == 1
        assert calls[0].model == tenant.ai_model
        assert tenant.plan_marker in calls[0].prompt
        assert f'"weight_kg": {tenant.weight}' in calls[0].prompt
        for other in world.tenants:
            if other is not tenant:
                assert other.plan_marker not in calls[0].prompt
                assert other.lifestyle_marker not in calls[0].prompt
                assert f'"weight_kg": {other.weight}' not in calls[0].prompt
        assert get_dashboard_insight(world.db, user_id=tenant.user_id) == {
            "fatigue": f"Green via {tenant.ai_key}",
            "wins_stalls": f"{tenant.lifestyle_marker} {tenant.plan_marker}",
            "advice": f"model={tenant.ai_model}",
        }
        notes = get_notifications(tenant.user_id, db_path=world.db)
        assert len(notes) == 1
        assert tenant.ai_key in notes[0]["message"]
        assert not any(
            o.ai_key in notes[0]["message"] for o in world.tenants if o is not tenant
        )

    # The tenant with no stored key fails on its own and never borrows another's.
    assert isinstance(outcomes[-1], ValueError)
    assert len(recorder.calls) == len(world.tenants)
    assert get_dashboard_insight(world.db, user_id=world.keyless.user_id) is None
    assert get_notifications(world.keyless.user_id, db_path=world.db) == []


def test_concurrent_weekly_correlations_never_cross_accounts(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _install_provider(monkeypatch, parties=len(world.tenants))
    everyone = [*world.tenants, world.keyless]

    outcomes = _run_concurrently(
        [
            lambda t=t: insight_cron.generate_weekly_correlations(  # type: ignore[misc]
                world.config, user_id=t.user_id
            )
            for t in everyone
        ]
    )

    assert not recorder.gate_broken
    for tenant, outcome in zip(world.tenants, outcomes):
        assert outcome is None, outcome
        calls = recorder.calls_for(tenant.ai_key)
        assert len(calls) == 1
        assert tenant.plan_marker in calls[0].prompt
        assert not any(
            marker in calls[0].prompt for marker in _other_markers(world, tenant)
        )
        report = get_deep_correlation(world.db, user_id=tenant.user_id)
        assert report == (
            f"REPORT via {tenant.ai_key} model={tenant.ai_model} "
            f"saw {tenant.lifestyle_marker} {tenant.plan_marker}"
        )

    assert isinstance(outcomes[-1], ValueError)
    assert get_deep_correlation(world.db, user_id=world.keyless.user_id) is None
    assert get_notifications(world.keyless.user_id, db_path=world.db) == []


# ---------------------------------------------------------------------------
# The full daily coaching run
# ---------------------------------------------------------------------------


def test_concurrent_daily_runs_isolate_credentials_data_and_output(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    parties = len(world.tenants)
    recorder = _install_provider(monkeypatch, parties=parties)
    hevy_gate = threading.Barrier(parties, timeout=WAIT_SECONDS)
    hevy_fetches: list[str] = []
    routine_syncs: dict[str, str | None] = {}
    deliveries: list[tuple[str, str]] = []

    def fake_fetch(api_key: str) -> dict[str, Any]:
        hevy_fetches.append(api_key)
        hevy_gate.wait()
        return {"workouts": [{"id": api_key, "title": f"Workout {api_key}"}]}

    def fake_sync_routines(config: Config, *, user_id: str) -> list[str]:
        routine_syncs[user_id] = config.hevy_api_key
        return []

    def fake_push(
        subscription: dict[str, Any], payload: dict[str, Any], vapid_key: str
    ) -> bool:
        deliveries.append((subscription["endpoint"], payload["body"]))
        return True

    monkeypatch.setattr("main.Config.load", lambda: world.config)
    monkeypatch.setattr("main.init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr("main.fetch_latest_workout", fake_fetch)
    monkeypatch.setattr("main.sync_routines", fake_sync_routines)
    monkeypatch.setattr("main.send_push_notification", fake_push)
    monkeypatch.setattr("main.today_day", lambda today: 1)
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda *args, **kwargs: datetime.now(tz=UTC).date(),
    )
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "synthetic-vapid-key")

    codes = _run_concurrently(
        [lambda t=t: main.run(preview=False, user_id=t.user_id) for t in world.tenants]  # type: ignore[misc]
    )

    assert codes == [0] * parties
    assert not recorder.gate_broken

    # Credentials: each run used only its own user's Hevy and AI keys.
    assert sorted(hevy_fetches) == sorted(t.hevy_key for t in world.tenants)
    assert "synthetic-server-hevy-key" not in hevy_fetches
    assert routine_syncs == {t.user_id: t.hevy_key for t in world.tenants}
    assert sorted(call.api_key for call in recorder.calls) == sorted(
        t.ai_key for t in world.tenants
    )

    for tenant in world.tenants:
        # Data: the prompt saw this user's last plan and nobody else's.
        prompt = recorder.calls_for(tenant.ai_key)[0].prompt
        assert tenant.plan_marker in prompt
        assert not any(marker in prompt for marker in _other_markers(world, tenant))

        # Output: the generated plan was stored, notified and pushed to this user only.
        plan = get_daily_logs(limit=1, db_path=world.db, user_id=tenant.user_id)[0][
            "plan"
        ]
        assert plan.startswith(f"REPORT via {tenant.ai_key} ")
        notes = get_notifications(tenant.user_id, db_path=world.db)
        assert [tenant.ai_key in note["message"] for note in notes] == [True]
        bodies = [body for endpoint, body in deliveries if endpoint == tenant.endpoint]
        assert len(bodies) == 1
        assert f"via {tenant.ai_key} " in bodies[0]
        assert not any(o.ai_key in bodies[0] for o in world.tenants if o is not tenant)

    assert len(deliveries) == parties


def test_run_never_touches_the_keyless_tenant_with_anothers_credentials(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed = world.tenants[0]
    recorder = _install_provider(monkeypatch, parties=1)
    fetches: list[str] = []
    monkeypatch.setattr("main.Config.load", lambda: world.config)
    monkeypatch.setattr("main.init_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "main.fetch_latest_workout",
        lambda api_key: fetches.append(api_key) or {"workouts": []},
    )
    monkeypatch.setattr("main.sync_routines", lambda config, *, user_id: [])
    monkeypatch.setattr("main.today_day", lambda today: 1)
    monkeypatch.setattr(
        "main.get_programme_start_date",
        lambda *args, **kwargs: datetime.now(tz=UTC).date(),
    )

    # A user with no stored AI key stops with a configuration error ...
    assert main.run(preview=True, user_id=world.keyless.user_id) == 1
    assert recorder.calls == []
    # ... and the next user's run is unaffected by that failure.
    assert main.run(preview=True, user_id=keyed.user_id) == 0
    assert [call.api_key for call in recorder.calls] == [keyed.ai_key]


# ---------------------------------------------------------------------------
# Connector credentials and cursors
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _user_token(db: str, user_id: str) -> str | None:
    try:
        credential = SecretVault(db).resolve(
            user_id, "google_health", purpose="test_readback"
        )
    except CredentialUnavailableError:
        return None
    return (
        credential.refresh_token.reveal()
        if credential.refresh_token is not None
        else None
    )


def test_concurrent_google_health_syncs_use_only_each_users_token(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Barrier(len(world.tenants), timeout=WAIT_SECONDS)
    presented: list[str] = []
    for tenant in world.tenants:
        SecretVault(world.db).store(
            tenant.user_id,
            "google_health",
            refresh_token=f"gh-refresh-{tenant.index}",
        )

    def fake_post(url: str, **kwargs: Any) -> _Response:
        token = kwargs["data"]["refresh_token"]
        presented.append(token)
        gate.wait()
        return _Response(
            {"access_token": f"access:{token}", "refresh_token": f"rotated:{token}"}
        )

    def fake_get(url: str, **kwargs: Any) -> _Response:
        index = int(kwargs["headers"]["Authorization"].rsplit("-", 1)[1])
        if "/dataTypes/weight/" not in url:
            return _Response({"dataPoints": []})
        sample = {"weightGrams": 80000 + 1000 * index}
        sample["sampleTime"] = {"physicalTime": "2026-08-01T08:00:00Z"}  # type: ignore[assignment]
        return _Response({"dataPoints": [{"weight": sample}]})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)

    unlinked = world.keyless
    results = _run_concurrently(
        [
            lambda t=t: google_health_client.sync_body_metrics(  # type: ignore[misc]
                "id", "secret", None, world.db, user_id=t.user_id
            )
            for t in [*world.tenants, unlinked]
        ]
    )

    assert sorted(presented) == sorted(f"gh-refresh-{t.index}" for t in world.tenants)
    for tenant, result in zip(world.tenants, results):
        assert result == {"weight_kg": tenant.weight}
        assert (
            _user_token(world.db, tenant.user_id)
            == f"rotated:gh-refresh-{tenant.index}"
        )
    # A user who never linked an account does not inherit anyone's token.
    assert results[-1] is None
    assert _user_token(world.db, unlinked.user_id) is None


# ---------------------------------------------------------------------------
# Owner-scoped job execution and locks
# ---------------------------------------------------------------------------


def test_owned_jobs_for_many_owners_run_in_parallel_and_share_no_state(
    world: World,
) -> None:
    gate = threading.Barrier(len(world.tenants), timeout=WAIT_SECONDS)
    locks = OwnerLocks()
    seen: dict[str, str] = {}

    def make_work(tenant: Tenant) -> Callable[[OwnerIdentity], str]:
        def work(owner: OwnerIdentity) -> str:
            gate.wait()  # every owner's job must be running at once
            seen[tenant.user_id] = owner.user_id
            return owner.user_id

        return work

    outcomes = _run_concurrently(
        [
            lambda t=t: run_owned_job(  # type: ignore[misc]
                OwnedJob("coaching", OwnerIdentity(t.user_id)),
                world.db,
                make_work(t),
                locks=locks,
            )
            for t in world.tenants
        ]
    )

    for tenant, outcome in zip(world.tenants, outcomes):
        assert outcome.status is JobStatus.COMPLETED, outcome
        assert outcome.result == tenant.user_id
    assert seen == {t.user_id: t.user_id for t in world.tenants}


def test_overlapping_duplicate_jobs_for_one_owner_are_skipped_not_raced(
    world: World,
) -> None:
    tenant = world.tenants[0]
    locks = OwnerLocks()
    entered = threading.Event()
    release = threading.Event()
    runs: list[str] = []

    def slow_work(owner: OwnerIdentity) -> str:
        runs.append(owner.user_id)
        entered.set()
        assert release.wait(WAIT_SECONDS)
        return "done"

    job = OwnedJob("coaching", OwnerIdentity(tenant.user_id))
    first: list[Any] = []
    thread = threading.Thread(
        target=lambda: first.append(
            run_owned_job(job, world.db, slow_work, locks=locks)
        ),
        daemon=True,
    )
    thread.start()
    assert entered.wait(WAIT_SECONDS)

    duplicate = run_owned_job(job, world.db, slow_work, locks=locks)
    release.set()
    thread.join(timeout=WAIT_SECONDS)

    assert duplicate.status is JobStatus.SKIPPED
    assert first[0].status is JobStatus.COMPLETED
    assert runs == [tenant.user_id]


def test_overlapping_scheduler_cycles_skip_a_busy_owner_but_serve_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    busy, free = world.tenants[0], world.tenants[1]
    entered = threading.Event()
    release = threading.Event()
    calls: list[str | None] = []

    def fake_run(*, preview: bool, user_id: str | None = None) -> int:
        calls.append(user_id)
        if user_id == busy.user_id:
            entered.set()
            assert release.wait(WAIT_SECONDS)
        return 0

    monkeypatch.setattr(
        scheduler,
        "get_all_users",
        lambda _db: [{"id": busy.user_id}, {"id": free.user_id}],
    )
    monkeypatch.setattr("main.run", fake_run)

    first_cycle: list[int] = []
    thread = threading.Thread(
        target=lambda: first_cycle.append(scheduler.run_coaching(world.config)),
        daemon=True,
    )
    thread.start()
    assert entered.wait(WAIT_SECONDS)

    second_cycle = scheduler.run_coaching(world.config)
    release.set()
    thread.join(timeout=WAIT_SECONDS)

    # The overlapping cycle neither raced the busy owner nor failed the free one.
    assert second_cycle == 0
    assert first_cycle == [0]
    assert calls.count(busy.user_id) == 1
    assert calls.count(free.user_id) == 2
