"""Tenant-scoped integration status: persisted facts, run recording, alerts."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import database
import integration_status as svc
import pytest
from connectors.base import ConnectorState, ConnectorStatus
from health_sync import SyncState
from integration_state import IntegrationState, NextAction, Reason, RunKind

# Far in the future so rows stamped with the wall clock (credential saves) are
# always older than the explicit timestamps used for runs.
NOW = datetime(2035, 1, 10, 12, 0, tzinfo=timezone.utc)
S = IntegrationState
R = Reason


@pytest.fixture()
def env(tmp_path: Path) -> tuple[svc.StatusContext, str, str]:
    db = str(tmp_path / "status.db")
    database.init_db(db)
    first = database.get_or_create_user("a@example.test", "A", db_path=db)["id"]
    second = database.get_or_create_user("b@example.test", "B", db_path=db)["id"]
    return svc.StatusContext(db_path=db, google_health_configured=True), first, second


def stamp_key(ctx: svc.StatusContext, user_id: str, provider: str, when: datetime) -> None:
    with sqlite3.connect(ctx.db_path) as conn:
        conn.execute(
            "UPDATE user_api_keys SET updated_at = ? WHERE user_id = ? AND provider = ?",
            (when.isoformat(), user_id, provider),
        )


def save_hevy_key(ctx: svc.StatusContext, user_id: str, when: datetime | None = None) -> None:
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=ctx.db_path)
    stamp_key(ctx, user_id, "hevy", when or NOW - timedelta(days=30))


def hevy(ctx: svc.StatusContext, user_id: str, now: datetime = NOW) -> Any:
    return svc.component_status(user_id, svc.HEVY, ctx, now=now)


def sync_run(
    ctx: svc.StatusContext,
    user_id: str,
    status: SyncState,
    when: datetime,
    **kwargs: Any,
) -> Any:
    return svc.record_run(user_id, svc.HEVY, RunKind.SYNC, status, ctx, now=when, **kwargs)


def notifications(ctx: svc.StatusContext, user_id: str) -> list[dict[str, Any]]:
    return database.get_notifications(user_id, db_path=ctx.db_path)


def test_new_account_requires_setup_for_every_required_component(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    account = svc.build_account_status(user, ctx, now=NOW)
    assert account.state is S.SETUP_REQUIRED
    assert account.blocking == ("hevy", "programme", "ai_provider")
    assert account.components["hevy"].reason is R.CREDENTIAL_MISSING
    assert account.components["programme"].reason is R.PROGRAMME_MISSING
    assert account.components["ai_provider"].reason is R.CREDENTIAL_MISSING
    assert account.components["google_health"].reason is R.CREDENTIAL_MISSING
    assert account.message == "Hevy is not set up yet."  # first blocker in dependency order
    assert account.action_href == "/settings?section=connections"


def test_unconfigured_google_health_is_unavailable_not_failed(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    off = svc.StatusContext(db_path=ctx.db_path, google_health_configured=False)
    status = svc.component_status(user, svc.GOOGLE_HEALTH, off, now=NOW)
    assert (status.state, status.reason, status.next_action) == (
        S.SETUP_REQUIRED,
        R.NOT_AVAILABLE,
        NextAction.NONE,
    )
    assert "not set on the server" in status.message


def test_hevy_lifecycle_is_derived_from_persisted_facts_and_time(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    assert hevy(ctx, user).state is S.SETUP_REQUIRED

    save_hevy_key(ctx, user)
    status = hevy(ctx, user)
    assert (status.state, status.reason) == (S.CONNECTING, R.AWAITING_VERIFICATION)

    svc.record_run(user, svc.HEVY, RunKind.TEST, SyncState.SUCCEEDED, ctx, now=NOW)
    assert hevy(ctx, user).reason is R.AWAITING_FIRST_SYNC

    sync_run(ctx, user, SyncState.SUCCEEDED, NOW + timedelta(minutes=1), fetched=12, written=12)
    ready = hevy(ctx, user, NOW + timedelta(hours=1))
    assert (ready.state, ready.reason) == (S.READY, R.UP_TO_DATE)
    assert ready.last_run_counts == {"fetched": 12, "written": 12, "skipped": 0}

    # No event happens, yet the derived state changes purely with the clock.
    assert hevy(ctx, user, NOW + timedelta(days=2)).state is S.READY
    stale = hevy(ctx, user, NOW + timedelta(days=4))
    assert (stale.state, stale.next_action) == (S.STALE, NextAction.SYNC)
    # Same facts and same clock always give the same answer.
    assert hevy(ctx, user, NOW + timedelta(days=4)) == stale


def test_syncing_is_observable_while_a_run_is_open(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    handle = svc.begin_run(user, svc.HEVY, RunKind.SYNC, ctx, now=NOW)
    assert handle is not None
    assert hevy(ctx, user, NOW + timedelta(minutes=1)).state is S.SYNCING

    transition = svc.complete_run(
        handle, SyncState.SUCCEEDED, ctx, fetched=5, written=5, now=NOW + timedelta(minutes=2)
    )
    assert (transition.before.state, transition.after.state) == (S.CONNECTING, S.READY)
    assert transition.changed


def test_double_submit_and_duplicate_completion_are_idempotent(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    first = svc.begin_run(user, svc.HEVY, RunKind.SYNC, ctx, now=NOW)
    assert first is not None
    assert svc.begin_run(user, svc.HEVY, RunKind.SYNC, ctx, now=NOW + timedelta(seconds=5)) is None

    done = svc.complete_run(first, SyncState.FAILED, ctx, error_code="forbidden", now=NOW + timedelta(minutes=1))
    assert done.after.state is S.SETUP_REQUIRED
    replay = svc.complete_run(first, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(minutes=2))
    assert not replay.changed  # a retried completion cannot flip the recorded outcome
    assert hevy(ctx, user, NOW + timedelta(minutes=3)).reason is R.CREDENTIAL_REJECTED
    assert len(notifications(ctx, user)) == 1  # and cannot alert twice


def test_crashed_sync_does_not_block_or_stick(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    crashed = svc.begin_run(user, svc.HEVY, RunKind.SYNC, ctx, now=NOW + timedelta(hours=1))
    assert crashed is not None
    later = NOW + timedelta(hours=1, minutes=20)
    status = hevy(ctx, user, later)
    assert (status.state, status.last_error_code) == (S.READY, "interrupted")
    retry = svc.begin_run(user, svc.HEVY, RunKind.SYNC, ctx, now=later)
    assert retry is not None and retry.run_id != crashed.run_id


def test_expired_token_degrades_alerts_once_and_recovers(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)

    expired = svc.record_run(
        user, svc.HEVY, RunKind.TEST, SyncState.FAILED, ctx,
        error_code="unauthorized", now=NOW + timedelta(hours=1),
    )
    assert (expired.after.state, expired.after.reason) == (S.DEGRADED, R.CREDENTIAL_REJECTED)
    alerts = notifications(ctx, user)
    assert [item["title"] for item in alerts] == ["⚠️ Hevy needs attention"]
    assert alerts[0]["link"] == "/settings?section=connections"
    assert "rejected your credentials" in alerts[0]["message"]

    again = svc.record_run(
        user, svc.HEVY, RunKind.TEST, SyncState.FAILED, ctx,
        error_code="unauthorized", now=NOW + timedelta(hours=2),
    )
    assert not again.changed
    assert len(notifications(ctx, user)) == 1  # repeated failure, no repeated alert

    # The user supplies a new key: old evidence stops applying immediately.
    save_hevy_key(ctx, user, NOW + timedelta(hours=3))
    renewed = hevy(ctx, user, NOW + timedelta(hours=3, minutes=1))
    assert (renewed.state, renewed.reason) == (S.CONNECTING, R.AWAITING_VERIFICATION)

    svc.record_run(user, svc.HEVY, RunKind.TEST, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(hours=4))
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW + timedelta(hours=4, minutes=1))
    assert hevy(ctx, user, NOW + timedelta(hours=5)).state is S.READY
    # Replacing the key is user-initiated: no "working again" alert is needed.
    assert len(notifications(ctx, user)) == 1


def test_recovery_without_a_new_credential_alerts_once(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    svc.record_run(
        user, svc.HEVY, RunKind.TEST, SyncState.FAILED, ctx,
        error_code="unauthorized", now=NOW + timedelta(hours=1),
    )
    recovered = svc.record_run(
        user, svc.HEVY, RunKind.TEST, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(hours=2)
    )
    assert (recovered.before.state, recovered.after.state) == (S.DEGRADED, S.READY)
    assert notifications(ctx, user)[0]["title"] == "✓ Hevy is working again"
    svc.record_run(user, svc.HEVY, RunKind.TEST, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(hours=3))
    assert len(notifications(ctx, user)) == 2


def test_partial_import_is_degraded_until_a_full_sync_succeeds(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    partial = sync_run(
        ctx, user, SyncState.PARTIAL, NOW + timedelta(hours=1),
        fetched=100, written=60, skipped=3, error_code="partial_import", retryable=True,
    )
    assert (partial.after.state, partial.after.reason) == (S.DEGRADED, R.PARTIAL_IMPORT)
    assert "60 of 100" in partial.after.message

    svc.record_run(user, svc.HEVY, RunKind.TEST, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(hours=2))
    assert hevy(ctx, user, NOW + timedelta(hours=2, minutes=1)).reason is R.PARTIAL_IMPORT

    healed = sync_run(ctx, user, SyncState.SUCCEEDED, NOW + timedelta(hours=3), fetched=100, written=100)
    assert healed.after.state is S.READY


def test_transient_failures_do_not_flap_the_state_or_spam_alerts(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    one = sync_run(
        ctx, user, SyncState.FAILED, NOW + timedelta(hours=1),
        error_code="rate_limited", retryable=True,
    )
    assert one.after.state is S.READY and not one.changed
    two = sync_run(
        ctx, user, SyncState.FAILED, NOW + timedelta(hours=2),
        error_code="rate_limited", retryable=True,
    )
    assert (two.after.state, two.after.consecutive_failures) == (S.DEGRADED, 2)
    sync_run(
        ctx, user, SyncState.FAILED, NOW + timedelta(hours=3),
        error_code="rate_limited", retryable=True,
    )
    assert len(notifications(ctx, user)) == 1


def test_notify_false_records_without_alerting(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    result = sync_run(
        ctx, user, SyncState.FAILED, NOW, error_code="unauthorized", notify=False
    )
    assert result.changed and notifications(ctx, user) == []


def test_status_is_tenant_isolated(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, alice, bob = env
    save_hevy_key(ctx, alice)
    save_hevy_key(ctx, bob)
    sync_run(ctx, alice, SyncState.SUCCEEDED, NOW)
    sync_run(ctx, bob, SyncState.FAILED, NOW, error_code="unauthorized")
    assert hevy(ctx, alice, NOW + timedelta(minutes=5)).state is S.READY
    assert hevy(ctx, bob, NOW + timedelta(minutes=5)).reason is R.CREDENTIAL_REJECTED
    assert [item["title"] for item in notifications(ctx, alice)] == []
    assert len(notifications(ctx, bob)) == 1

    # A handle can only ever close a row owned by its own tenant.
    handle = svc.begin_run(alice, svc.HEVY, RunKind.SYNC, ctx, now=NOW + timedelta(hours=1))
    assert handle is not None
    forged = svc.RunHandle(bob, svc.HEVY, "hevy", RunKind.SYNC, handle.run_id, NOW, handle.before)
    assert not svc.complete_run(forged, SyncState.FAILED, ctx, error_code="x").changed
    assert hevy(ctx, alice, NOW + timedelta(hours=1, minutes=1)).state is S.SYNCING


def test_removing_a_credential_returns_to_setup_and_forgets_history(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    database.delete_user_api_key(user, "hevy", db_path=ctx.db_path)
    assert hevy(ctx, user, NOW + timedelta(minutes=1)).reason is R.CREDENTIAL_MISSING
    assert svc.forget_connector_history(user, svc.HEVY, ctx) == 1
    assert database.get_connector_runs(user, "hevy", ctx.db_path) == []


def test_ai_provider_state_follows_the_selected_provider(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, other = env

    def ai(uid: str, now: datetime = NOW) -> Any:
        return svc.component_status(uid, svc.AI_PROVIDER, ctx, now=now)

    assert ai(user).reason is R.CREDENTIAL_MISSING

    fallback = svc.StatusContext(ctx.db_path, server_gemini_key="server-key")
    assert svc.component_status(user, svc.AI_PROVIDER, fallback, now=NOW).state is S.READY

    database.save_user_preferences(user, preferred_ai="claude", db_path=ctx.db_path)
    # The Gemini fallback never covers another provider.
    assert svc.component_status(user, svc.AI_PROVIDER, fallback, now=NOW).state is S.SETUP_REQUIRED

    database.save_user_api_key(user, "claude", "synthetic-claude-key", db_path=ctx.db_path)
    stamp_key(ctx, user, "claude", NOW - timedelta(days=1))
    status = ai(user)
    assert (status.state, status.reason, status.label) == (
        S.READY,
        R.CONFIGURED,
        "AI coach (Anthropic Claude)",
    )

    svc.record_run(user, svc.AI_PROVIDER, RunKind.REQUEST, SyncState.FAILED, ctx,
                   error_code="unauthorized", now=NOW + timedelta(minutes=1))
    assert ai(user, NOW + timedelta(minutes=2)).reason is R.CREDENTIAL_REJECTED

    # Evidence is per provider and per tenant: switching away isolates it.
    database.save_user_api_key(user, "openai", "synthetic-openai-key", db_path=ctx.db_path)
    stamp_key(ctx, user, "openai", NOW - timedelta(days=1))
    database.save_user_preferences(user, preferred_ai="openai", db_path=ctx.db_path)
    assert ai(user, NOW + timedelta(minutes=2)).state is S.READY
    database.save_user_api_key(other, "claude", "synthetic-claude-key", db_path=ctx.db_path)
    stamp_key(ctx, other, "claude", NOW - timedelta(days=1))
    database.save_user_preferences(other, preferred_ai="claude", db_path=ctx.db_path)
    assert ai(other, NOW + timedelta(minutes=2)).state is S.READY

    assert svc.forget_connector_history(user, svc.ai_connector_id("claude"), ctx) == 1


def test_google_health_authorization_lifecycle(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, user, _ = env

    def gh(now: datetime) -> Any:
        return svc.component_status(user, svc.GOOGLE_HEALTH, ctx, now=now)

    first = svc.begin_run(user, svc.GOOGLE_HEALTH, RunKind.AUTHORIZE, ctx, now=NOW)
    assert first is not None
    assert (gh(NOW + timedelta(minutes=1)).state, gh(NOW + timedelta(minutes=1)).reason) == (
        S.CONNECTING,
        R.AUTHORIZING,
    )
    # Re-clicking Connect supersedes the abandoned attempt instead of blocking.
    second = svc.begin_run(user, svc.GOOGLE_HEALTH, RunKind.AUTHORIZE, ctx, now=NOW + timedelta(minutes=2))
    assert second is not None

    denied = svc.complete_run(second, SyncState.FAILED, ctx, error_code="authorization_denied",
                              now=NOW + timedelta(minutes=3))
    assert denied.after.reason is R.AUTHORIZATION_FAILED
    assert denied.after.last_error_code == "authorization_denied"

    third = svc.begin_run(user, svc.GOOGLE_HEALTH, RunKind.AUTHORIZE, ctx, now=NOW + timedelta(minutes=10))
    assert third is not None
    database.set_meta(svc.GOOGLE_HEALTH_TOKEN_KEY, "synthetic-refresh-token", ctx.db_path, user_id=user)
    svc.complete_run(third, SyncState.SUCCEEDED, ctx, now=NOW + timedelta(minutes=11))
    connected = gh(NOW + timedelta(minutes=12))
    assert (connected.state, connected.reason) == (S.CONNECTING, R.AWAITING_FIRST_SYNC)
    assert connected.next_action is NextAction.NONE  # no manual sync exists for it

    svc.record_run(user, svc.GOOGLE_HEALTH, RunKind.SYNC, SyncState.SUCCEEDED, ctx,
                   written=3, now=NOW + timedelta(hours=1))
    assert gh(NOW + timedelta(hours=2)).state is S.READY
    assert gh(NOW + timedelta(days=5)).state is S.STALE

    database.set_meta(svc.GOOGLE_HEALTH_TOKEN_KEY, "", ctx.db_path, user_id=user)
    assert gh(NOW + timedelta(hours=3)).reason is R.CREDENTIAL_MISSING


def test_programme_component_follows_the_active_hevy_programme(
    env: tuple[svc.StatusContext, str, str],
) -> None:
    ctx, user, other = env
    definition = {
        "name": "Synthetic",
        "programme_spec": {"start_date": "2035-01-01", "duration_weeks": 12},
        "blocks": [{"number": 1, "name": "Base", "start_week": 1, "end_week": 12}],
        "days": [{"number": 1, "focus": "Upper", "exercises": []}],
    }
    database.set_active_programme(user, "hevy", "synthetic", definition, db_path=ctx.db_path)
    status = svc.component_status(user, svc.PROGRAMME, ctx, now=NOW)
    assert (status.state, status.reason) == (S.READY, R.PROGRAMME_ACTIVE)
    assert svc.component_status(other, svc.PROGRAMME, ctx, now=NOW).state is S.SETUP_REQUIRED

    finished = svc.component_status(user, svc.PROGRAMME, ctx, now=NOW + timedelta(weeks=14))
    assert (finished.state, finished.reason) == (S.STALE, R.PROGRAMME_COMPLETED)

    broken = {**definition, "programme_spec": {"start_date": "not-a-date", "duration_weeks": 12}}
    database.set_active_programme(user, "hevy", "broken", broken, db_path=ctx.db_path)
    review = svc.component_status(user, svc.PROGRAMME, ctx, now=NOW)
    assert (review.state, review.reason) == (S.DEGRADED, R.PROGRAMME_NEEDS_REVIEW)


def test_run_ledger_cannot_be_written_for_programme(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, user, _ = env
    with pytest.raises(ValueError):
        svc.begin_run(user, svc.PROGRAMME, RunKind.SYNC, ctx, now=NOW)


def test_unknown_ledger_values_are_ignored_not_fatal(env: tuple[svc.StatusContext, str, str]) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
    with sqlite3.connect(ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO connector_runs (user_id, connector, kind, status, started_at) "
            "VALUES (?, 'hevy', 'bogus', 'exploded', ?)",
            (user, NOW.isoformat()),
        )
    assert hevy(ctx, user, NOW + timedelta(minutes=1)).state is S.READY


def test_transitions_are_logged_without_tenant_identifiers(
    env: tuple[svc.StatusContext, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    ctx, user, _ = env
    save_hevy_key(ctx, user)
    with caplog.at_level(logging.INFO, logger="integration_status"):
        sync_run(ctx, user, SyncState.SUCCEEDED, NOW)
        sync_run(ctx, user, SyncState.FAILED, NOW + timedelta(hours=1),
                 error_code="unauthorized")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "integration_state_transition connector=hevy from=connecting/awaiting_verification" in text
    assert "to=degraded/credential_rejected error_code=unauthorized" in text
    assert "connector_run" in text and "'outcome': 'succeeded'" in text
    assert user not in text and "synthetic-hevy-key" not in text


@pytest.mark.parametrize(
    ("state", "last_success", "expected"),
    [
        (ConnectorState.DISCONNECTED, None, (S.SETUP_REQUIRED, R.CREDENTIAL_MISSING)),
        (ConnectorState.PENDING, None, (S.CONNECTING, R.AUTHORIZING)),
        (ConnectorState.CONNECTED, None, (S.CONNECTING, R.AWAITING_FIRST_SYNC)),
        (ConnectorState.CONNECTED, NOW - timedelta(hours=2), (S.READY, R.UP_TO_DATE)),
        (ConnectorState.CONNECTED, NOW - timedelta(days=5), (S.STALE, R.DATA_STALE)),
        (ConnectorState.ATTENTION, None, (S.DEGRADED, R.PROVIDER_FAILING)),
        (ConnectorState.ERROR, None, (S.DEGRADED, R.PROVIDER_FAILING)),
    ],
)
def test_registry_connector_status_uses_the_shared_vocabulary(
    state: ConnectorState, last_success: datetime | None, expected: tuple[IntegrationState, Reason]
) -> None:
    projected = svc.status_from_connector(
        ConnectorStatus("garmin", state, last_success_at=last_success, message="Provider says hi"),
        label="Garmin",
        now=NOW,
    )
    assert (projected.state, projected.reason) == expected
    assert projected.message == "Provider says hi"


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("secret provider text")
        self.status_code = status_code


class _CodeError(Exception):
    code = 429


class ReadTimeout(Exception):
    pass


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_StatusError(401), ("unauthorized", False)),
        (_StatusError(403), ("forbidden", False)),
        (_StatusError(429), ("rate_limited", True)),
        (_StatusError(503), ("provider_unavailable", True)),
        (_StatusError(400), ("provider_error", False)),
        (_CodeError(), ("rate_limited", True)),
        (ReadTimeout(), ("provider_unavailable", True)),
        (RuntimeError("anything"), ("provider_error", True)),
    ],
)
def test_provider_exceptions_are_classified_without_their_text(
    error: BaseException, expected: tuple[str, bool]
) -> None:
    assert svc.classify_provider_error(error) == expected
