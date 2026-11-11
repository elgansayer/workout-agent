"""Deterministic tests for the integration readiness state machine.

Every scenario id below corresponds to a row in
``docs/INTEGRATION_STATE_MACHINE.md``. Fixtures are synthetic; no network,
clock or database is used.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import integration_state
import pytest
from health_freshness import Freshness
from hypothesis import given, settings
from hypothesis import strategies as st
from integration_state import (
    AUTH_ERROR_CODES,
    IntegrationFacts,
    IntegrationPolicy,
    IntegrationState,
    IntegrationStatus,
    NextAction,
    Reason,
    RunFact,
    RunKind,
    RunStatus,
    Transition,
    coach_data_notice,
    derive_account_status,
    derive_integration_state,
    derive_programme_state,
    notification_for,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SYNC_POLICY = IntegrationPolicy(manual_sync=True, manual_verify=True)
REQUEST_POLICY = IntegrationPolicy(requires_sync=False)

S = IntegrationState
R = Reason


def run(
    kind: RunKind,
    status: RunStatus,
    minutes_ago: float,
    *,
    error: str | None = None,
    retryable: bool = False,
    counts: tuple[int, int, int] = (0, 0, 0),
) -> RunFact:
    started = NOW - timedelta(minutes=minutes_ago)
    running = status is RunStatus.RUNNING
    return RunFact(
        kind=kind,
        status=status,
        started_at=started,
        finished_at=None if running else started + timedelta(seconds=30),
        error_code=error,
        retryable=retryable,
        fetched=counts[0],
        written=counts[1],
        skipped=counts[2],
    )


def sync_ok(minutes_ago: float) -> RunFact:
    return run(RunKind.SYNC, RunStatus.SUCCEEDED, minutes_ago, counts=(10, 10, 0))


def sync_fail(
    minutes_ago: float, error: str = "provider_unavailable", retryable: bool = True
) -> RunFact:
    return run(RunKind.SYNC, RunStatus.FAILED, minutes_ago, error=error, retryable=retryable)


def verify_ok(minutes_ago: float) -> RunFact:
    return run(RunKind.TEST, RunStatus.SUCCEEDED, minutes_ago)


def make(*runs: RunFact, **overrides: object) -> IntegrationFacts:
    params: dict[str, object] = {
        "connector": "hevy",
        "label": "Hevy",
        "policy": SYNC_POLICY,
        "credential_present": True,
        "runs": tuple(runs),
    }
    params.update(overrides)
    return IntegrationFacts(**params)  # type: ignore[arg-type]


def derive(facts: IntegrationFacts, now: datetime = NOW) -> IntegrationStatus:
    return derive_integration_state(facts, now=now)


HOUR = 60
DAY = 24 * HOUR

# (id, facts, state, reason, next action)
SYNC_SCENARIOS = [
    (
        "unavailable",
        make(available=False, credential_present=False),
        S.SETUP_REQUIRED,
        R.NOT_AVAILABLE,
        NextAction.NONE,
    ),
    ("no-credential", make(credential_present=False), S.SETUP_REQUIRED, R.CREDENTIAL_MISSING, NextAction.CONNECT),
    (
        "authorizing",
        make(run(RunKind.AUTHORIZE, RunStatus.RUNNING, 2), credential_present=False),
        S.CONNECTING,
        R.AUTHORIZING,
        NextAction.NONE,
    ),
    (
        "authorization-abandoned",
        make(run(RunKind.AUTHORIZE, RunStatus.RUNNING, 45), credential_present=False),
        S.SETUP_REQUIRED,
        R.AUTHORIZATION_FAILED,
        NextAction.CONNECT,
    ),
    (
        "authorization-denied",
        make(
            run(RunKind.AUTHORIZE, RunStatus.FAILED, 5, error="authorization_denied"),
            credential_present=False,
        ),
        S.SETUP_REQUIRED,
        R.AUTHORIZATION_FAILED,
        NextAction.CONNECT,
    ),
    (
        "disconnected-after-authorization",
        make(run(RunKind.AUTHORIZE, RunStatus.SUCCEEDED, 5), credential_present=False),
        S.SETUP_REQUIRED,
        R.CREDENTIAL_MISSING,
        NextAction.CONNECT,
    ),
    ("unverified", make(), S.CONNECTING, R.AWAITING_VERIFICATION, NextAction.VERIFY),
    ("verifying", make(run(RunKind.TEST, RunStatus.RUNNING, 1)), S.CONNECTING, R.VERIFYING, NextAction.NONE),
    ("verified-no-sync", make(verify_ok(5)), S.CONNECTING, R.AWAITING_FIRST_SYNC, NextAction.SYNC),
    ("first-sync-running", make(run(RunKind.SYNC, RunStatus.RUNNING, 1)), S.SYNCING, R.SYNC_IN_PROGRESS, NextAction.NONE),
    ("fresh", make(sync_ok(60)), S.READY, R.UP_TO_DATE, NextAction.NONE),
    ("aging-is-still-ready", make(sync_ok(30 * HOUR)), S.READY, R.UP_TO_DATE, NextAction.NONE),
    ("stale", make(sync_ok(80 * HOUR)), S.STALE, R.DATA_STALE, NextAction.SYNC),
    (
        "stale-resync-running",
        make(sync_ok(80 * HOUR), run(RunKind.SYNC, RunStatus.RUNNING, 2)),
        S.SYNCING,
        R.SYNC_IN_PROGRESS,
        NextAction.NONE,
    ),
    (
        "transient-failure-absorbed",
        make(sync_ok(2 * HOUR), sync_fail(30, "rate_limited")),
        S.READY,
        R.UP_TO_DATE,
        NextAction.NONE,
    ),
    (
        "transient-failure-when-aging",
        make(sync_ok(30 * HOUR), sync_fail(30, "rate_limited")),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.SYNC,
    ),
    (
        "repeated-failures",
        make(sync_ok(2 * HOUR), sync_fail(60), sync_fail(30)),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.SYNC,
    ),
    (
        "non-retryable-failure",
        make(sync_ok(2 * HOUR), sync_fail(30, "provider_error", retryable=False)),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.SYNC,
    ),
    (
        "failure-without-any-success",
        make(sync_fail(30)),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.SYNC,
    ),
    (
        "recovery-after-failures",
        make(sync_fail(120), sync_fail(90), sync_ok(10)),
        S.READY,
        R.UP_TO_DATE,
        NextAction.NONE,
    ),
    (
        "partial-import",
        make(run(RunKind.SYNC, RunStatus.PARTIAL, 20, counts=(100, 60, 0), error="partial_import", retryable=True)),
        S.DEGRADED,
        R.PARTIAL_IMPORT,
        NextAction.SYNC,
    ),
    (
        "partial-then-success",
        make(
            run(RunKind.SYNC, RunStatus.PARTIAL, 90, counts=(100, 60, 0), retryable=True),
            sync_ok(10),
        ),
        S.READY,
        R.UP_TO_DATE,
        NextAction.NONE,
    ),
    (
        "partial-not-cleared-by-connection-test",
        make(
            run(RunKind.SYNC, RunStatus.PARTIAL, 90, counts=(100, 60, 0), retryable=True),
            verify_ok(10),
        ),
        S.DEGRADED,
        R.PARTIAL_IMPORT,
        NextAction.SYNC,
    ),
    (
        "expired-token-after-success",
        make(sync_ok(5 * HOUR), run(RunKind.TEST, RunStatus.FAILED, 10, error="unauthorized")),
        S.DEGRADED,
        R.CREDENTIAL_REJECTED,
        NextAction.RECONNECT,
    ),
    (
        "rejected-key-never-worked",
        make(run(RunKind.TEST, RunStatus.FAILED, 10, error="unauthorized")),
        S.SETUP_REQUIRED,
        R.CREDENTIAL_REJECTED,
        NextAction.RECONNECT,
    ),
    (
        "revoked-scope",
        make(sync_ok(5 * HOUR), sync_fail(10, "forbidden", retryable=False)),
        S.DEGRADED,
        R.CREDENTIAL_REJECTED,
        NextAction.RECONNECT,
    ),
    (
        "token-recovered-without-new-credential",
        make(run(RunKind.TEST, RunStatus.FAILED, 30, error="unauthorized"), verify_ok(5)),
        S.CONNECTING,
        R.AWAITING_FIRST_SYNC,
        NextAction.SYNC,
    ),
    (
        "retry-while-rejected-shows-syncing",
        make(
            sync_ok(5 * HOUR),
            run(RunKind.TEST, RunStatus.FAILED, 30, error="unauthorized"),
            run(RunKind.SYNC, RunStatus.RUNNING, 1),
        ),
        S.SYNCING,
        R.SYNC_IN_PROGRESS,
        NextAction.NONE,
    ),
    (
        "old-failures-ignored-after-credential-change",
        make(
            sync_ok(10 * HOUR),
            run(RunKind.TEST, RunStatus.FAILED, 8 * HOUR, error="unauthorized"),
            credential_changed_at=NOW - timedelta(hours=2),
        ),
        S.CONNECTING,
        R.AWAITING_VERIFICATION,
        NextAction.VERIFY,
    ),
    (
        "test-failure-does-not-degrade-fresh-sync",
        make(sync_ok(2 * HOUR), run(RunKind.TEST, RunStatus.FAILED, 10, error="provider_unavailable", retryable=True)),
        S.READY,
        R.UP_TO_DATE,
        NextAction.NONE,
    ),
]


@pytest.mark.parametrize(
    ("facts", "state", "reason", "action"),
    [pytest.param(*row[1:], id=row[0]) for row in SYNC_SCENARIOS],
)
def test_sync_based_connector_transitions(
    facts: IntegrationFacts, state: IntegrationState, reason: Reason, action: NextAction
) -> None:
    status = derive(facts)
    assert (status.state, status.reason, status.next_action) == (state, reason, action)
    assert status.action_label is None if action is NextAction.NONE else status.action_label
    assert ("{" not in status.message) and status.message


def test_unavailable_connector_uses_the_supplied_detail_verbatim() -> None:
    status = derive(make(available=False, unavailable_detail="Not set {up} here."))
    assert status.message == "Not set {up} here."


def test_freshness_and_timestamps_are_reported() -> None:
    status = derive(make(sync_ok(30 * HOUR), sync_fail(10, "rate_limited")))
    assert status.freshness is Freshness.AGING
    assert status.last_success_at == NOW - timedelta(minutes=30 * HOUR) + timedelta(seconds=30)
    assert status.last_attempt_at == NOW - timedelta(minutes=10) + timedelta(seconds=30)
    assert status.last_error_code == "rate_limited"
    assert status.consecutive_failures == 1
    assert "29 hours ago" in derive(make(sync_ok(30 * HOUR))).message  # floor of 29h59m30s


def test_partial_import_message_reports_counts() -> None:
    status = derive(
        make(run(RunKind.SYNC, RunStatus.PARTIAL, 5, counts=(100, 60, 3), retryable=True))
    )
    assert "60 of 100" in status.message
    assert status.last_run_counts == {"fetched": 100, "written": 60, "skipped": 3}
    assert status.retryable is True


def test_abandoned_sync_is_interrupted_not_syncing_forever() -> None:
    absorbed = derive(make(sync_ok(2 * HOUR), run(RunKind.SYNC, RunStatus.RUNNING, 40)))
    assert (absorbed.state, absorbed.last_error_code) == (S.READY, "interrupted")
    never_worked = derive(make(run(RunKind.SYNC, RunStatus.RUNNING, 40)))
    assert (never_worked.state, never_worked.reason) == (S.DEGRADED, R.PROVIDER_FAILING)
    assert "did not finish" in never_worked.message


def test_known_failure_codes_get_specific_safe_copy() -> None:
    limited = derive(make(sync_fail(30, "rate_limited"), sync_fail(20, "rate_limited")))
    assert "limiting requests" in limited.message
    down = derive(make(sync_fail(30), sync_fail(20)))
    assert "temporarily unavailable" in down.message
    other = derive(make(sync_fail(30, "weird_code"), sync_fail(20, "weird_code")))
    assert "returned an error" in other.message and "weird_code" not in other.message


@pytest.mark.parametrize("code", sorted(AUTH_ERROR_CODES))
def test_every_credential_error_code_requires_reconnecting(code: str) -> None:
    status = derive(make(sync_ok(HOUR), run(RunKind.SYNC, RunStatus.FAILED, 5, error=code)))
    assert (status.state, status.reason, status.retryable) == (
        S.DEGRADED,
        R.CREDENTIAL_REJECTED,
        False,
    )
    assert status.last_error_code == code


def test_connector_without_manual_controls_never_offers_them() -> None:
    policy = IntegrationPolicy()
    awaiting = derive(make(policy=policy, credential_verified=True))
    assert (awaiting.reason, awaiting.next_action, awaiting.action_href) == (
        R.AWAITING_FIRST_SYNC,
        NextAction.NONE,
        None,
    )
    stale = derive(make(sync_ok(80 * HOUR), policy=policy))
    assert (stale.state, stale.next_action) == (S.STALE, NextAction.NONE)
    unverified = derive(make(policy=policy))
    assert unverified.next_action is NextAction.NONE


def test_externally_verified_credential_is_not_unverified() -> None:
    status = derive(make(credential_verified=True))
    assert (status.state, status.reason) == (S.CONNECTING, R.AWAITING_FIRST_SYNC)
    rejected = derive(
        make(
            run(RunKind.SYNC, RunStatus.FAILED, 5, error="invalid_grant"),
            credential_verified=True,
        )
    )
    assert (rejected.state, rejected.reason) == (S.DEGRADED, R.CREDENTIAL_REJECTED)


def test_naive_and_offset_datetimes_match_utc() -> None:
    utc = make(sync_ok(2 * HOUR), sync_fail(10, "rate_limited"))
    naive_runs = tuple(
        RunFact(
            kind=item.kind,
            status=item.status,
            started_at=item.started_at.replace(tzinfo=None),
            finished_at=item.finished_at.replace(tzinfo=None) if item.finished_at else None,
            error_code=item.error_code,
            retryable=item.retryable,
        )
        for item in utc.runs
    )
    plus_two = timezone(timedelta(hours=2))
    shifted_runs = tuple(
        RunFact(
            kind=item.kind,
            status=item.status,
            started_at=item.started_at.astimezone(plus_two),
            finished_at=item.finished_at.astimezone(plus_two) if item.finished_at else None,
            error_code=item.error_code,
            retryable=item.retryable,
        )
        for item in utc.runs
    )
    expected = derive(utc)
    assert derive(make(*naive_runs)) == expected
    assert derive(make(*shifted_runs), NOW.astimezone(plus_two)) == expected


# --- request-based connectors (AI provider) --------------------------------


def ai(*runs: RunFact, **overrides: object) -> IntegrationFacts:
    return make(*runs, connector="ai:claude", label="AI coach (Claude)", policy=REQUEST_POLICY, **overrides)


def request(status: RunStatus, minutes_ago: float, error: str | None = None, retryable: bool = False) -> RunFact:
    return run(RunKind.REQUEST, status, minutes_ago, error=error, retryable=retryable)


REQUEST_SCENARIOS = [
    ("no-key", ai(credential_present=False), S.SETUP_REQUIRED, R.CREDENTIAL_MISSING, NextAction.CONNECT),
    ("configured", ai(), S.READY, R.CONFIGURED, NextAction.NONE),
    (
        "one-transient-failure",
        ai(request(RunStatus.FAILED, 5, "provider_unavailable", True)),
        S.READY,
        R.CONFIGURED,
        NextAction.NONE,
    ),
    (
        "repeated-transient-failures",
        ai(
            request(RunStatus.FAILED, 9, "rate_limited", True),
            request(RunStatus.FAILED, 5, "rate_limited", True),
        ),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.NONE,
    ),
    (
        "non-retryable-failure",
        ai(request(RunStatus.FAILED, 5, "provider_error", False)),
        S.DEGRADED,
        R.PROVIDER_FAILING,
        NextAction.RECONNECT,
    ),
    (
        "recovered",
        ai(request(RunStatus.FAILED, 9, "provider_error"), request(RunStatus.SUCCEEDED, 5)),
        S.READY,
        R.CONFIGURED,
        NextAction.NONE,
    ),
    (
        "key-rejected-first-use",
        ai(request(RunStatus.FAILED, 5, "unauthorized")),
        S.SETUP_REQUIRED,
        R.CREDENTIAL_REJECTED,
        NextAction.RECONNECT,
    ),
    (
        "key-revoked-after-use",
        ai(request(RunStatus.SUCCEEDED, 60), request(RunStatus.FAILED, 5, "unauthorized")),
        S.DEGRADED,
        R.CREDENTIAL_REJECTED,
        NextAction.RECONNECT,
    ),
    (
        "new-key-clears-old-failures",
        ai(
            request(RunStatus.FAILED, 60, "unauthorized"),
            credential_changed_at=NOW - timedelta(minutes=10),
        ),
        S.READY,
        R.CONFIGURED,
        NextAction.NONE,
    ),
    (
        "in-flight-requests-are-ignored",
        ai(request(RunStatus.RUNNING, 1)),
        S.READY,
        R.CONFIGURED,
        NextAction.NONE,
    ),
]


@pytest.mark.parametrize(
    ("facts", "state", "reason", "action"),
    [pytest.param(*row[1:], id=row[0]) for row in REQUEST_SCENARIOS],
)
def test_request_based_connector_transitions(
    facts: IntegrationFacts, state: IntegrationState, reason: Reason, action: NextAction
) -> None:
    status = derive(facts)
    assert (status.state, status.reason, status.next_action) == (state, reason, action)
    assert status.freshness is None  # staleness does not apply to on-demand use


# --- programme and account --------------------------------------------------


@pytest.mark.parametrize(
    ("active", "runtime", "state", "reason", "action"),
    [
        (False, "setup_required", S.SETUP_REQUIRED, R.PROGRAMME_MISSING, NextAction.BUILD_PROGRAMME),
        (False, "active", S.SETUP_REQUIRED, R.PROGRAMME_MISSING, NextAction.BUILD_PROGRAMME),
        (True, "setup_required", S.SETUP_REQUIRED, R.PROGRAMME_MISSING, NextAction.BUILD_PROGRAMME),
        (True, "needs_review", S.DEGRADED, R.PROGRAMME_NEEDS_REVIEW, NextAction.REVIEW_PROGRAMME),
        (True, "completed", S.STALE, R.PROGRAMME_COMPLETED, NextAction.BUILD_PROGRAMME),
        (True, "not_started", S.READY, R.PROGRAMME_ACTIVE, NextAction.NONE),
        (True, "active", S.READY, R.PROGRAMME_ACTIVE, NextAction.NONE),
    ],
)
def test_programme_state_mapping(
    active: bool, runtime: str, state: IntegrationState, reason: Reason, action: NextAction
) -> None:
    status = derive_programme_state(
        has_active_programme=active, runtime_status=runtime, runtime_message="Runtime says {hi}."
    )
    assert (status.state, status.reason, status.next_action) == (state, reason, action)
    if action is not NextAction.NONE:
        assert status.action_href == "/programmes"


def components(**states: IntegrationState) -> dict[str, IntegrationStatus]:
    return {
        name: IntegrationStatus(name, name.title(), state, Reason.UP_TO_DATE, f"{name} {state.value}")
        for name, state in states.items()
    }


def test_account_state_is_the_least_ready_required_component() -> None:
    ordered = [S.SETUP_REQUIRED, S.DEGRADED, S.CONNECTING, S.STALE, S.SYNCING, S.READY]
    for index, worse in enumerate(ordered[:-1]):
        for better in ordered[index + 1 :]:
            account = derive_account_status(
                components(hevy=better, programme=worse, ai_provider=S.READY),
                required=("hevy", "programme", "ai_provider"),
                now=NOW,
            )
            assert account.state is worse
            assert account.message == f"programme {worse.value}"


def test_account_ties_break_by_dependency_order_and_blocking_is_reported() -> None:
    account = derive_account_status(
        components(hevy=S.DEGRADED, programme=S.DEGRADED, ai_provider=S.CONNECTING, extra=S.SETUP_REQUIRED),
        required=("hevy", "programme", "ai_provider"),
        now=NOW,
    )
    assert account.message == "hevy degraded"
    assert account.blocking == ("hevy", "programme", "ai_provider")
    payload = account.to_payload()
    assert payload["components"]["extra"]["required"] is False
    assert payload["components"]["hevy"]["required"] is True


def test_optional_components_never_change_the_account_state() -> None:
    account = derive_account_status(
        components(hevy=S.READY, programme=S.READY, ai_provider=S.READY, google_health=S.DEGRADED),
        required=("hevy", "programme", "ai_provider"),
        now=NOW,
    )
    assert (account.state, account.blocking) == (S.READY, ())


def test_stale_and_syncing_do_not_block() -> None:
    account = derive_account_status(
        components(hevy=S.STALE, programme=S.SYNCING, ai_provider=S.READY),
        required=("hevy", "programme", "ai_provider"),
        now=NOW,
    )
    assert (account.state, account.blocking) == (S.STALE, ())


def test_account_requires_every_named_component() -> None:
    with pytest.raises(ValueError):
        derive_account_status(components(hevy=S.READY), required=("hevy", "programme"), now=NOW)
    with pytest.raises(ValueError):
        derive_account_status(components(hevy=S.READY), required=(), now=NOW)


def test_policy_rejects_inconsistent_thresholds() -> None:
    with pytest.raises(ValueError):
        IntegrationPolicy(fresh_for=timedelta(days=4))
    with pytest.raises(ValueError):
        IntegrationPolicy(degrade_after_failures=0)


# --- transitions and notifications -----------------------------------------


def status_for(*runs: RunFact, **overrides: object) -> IntegrationStatus:
    return derive(make(*runs, **overrides))


def test_notifications_only_fire_on_meaningful_changes() -> None:
    ready = status_for(sync_ok(HOUR))
    degraded = status_for(sync_ok(5 * HOUR), sync_fail(20, "rate_limited"), sync_fail(10, "rate_limited"))
    rejected = status_for(sync_ok(5 * HOUR), run(RunKind.TEST, RunStatus.FAILED, 5, error="unauthorized"))
    stale = status_for(sync_ok(80 * HOUR))
    never_worked = status_for(run(RunKind.TEST, RunStatus.FAILED, 5, error="unauthorized"))
    missing = status_for(credential_present=False)
    connecting = status_for()

    assert notification_for(Transition("hevy", ready, ready)) is None
    assert notification_for(Transition("hevy", degraded, degraded)) is None  # no repeat alerts

    alert = notification_for(Transition("hevy", ready, degraded))
    assert alert is not None and alert.title == "⚠️ Hevy needs attention"
    assert alert.message == degraded.message and alert.link == "/settings"

    expired = notification_for(Transition("hevy", ready, rejected))
    assert expired is not None and "rejected your credentials" in expired.message

    assert notification_for(Transition("hevy", missing, never_worked)) is not None
    assert notification_for(Transition("hevy", ready, stale)) is None  # time-based, no event
    assert notification_for(Transition("hevy", missing, connecting)) is None

    for broken in (degraded, rejected, never_worked):
        recovered = notification_for(Transition("hevy", broken, ready))
        assert recovered is not None and recovered.title == "✓ Hevy is working again"
    assert notification_for(Transition("hevy", stale, ready)) is None

    reason_change = notification_for(Transition("hevy", degraded, rejected))
    assert reason_change is not None  # a different problem is worth a new alert


# --- Coach grounding ---------------------------------------------------------


def test_coach_notice_is_empty_when_data_is_current() -> None:
    ready = status_for(sync_ok(HOUR))
    assert coach_data_notice(ready, has_local_data=True) == ""
    assert coach_data_notice(ready, has_local_data=False) == ""


def test_coach_is_told_not_to_invent_data_that_was_never_imported() -> None:
    notice = coach_data_notice(status_for(credential_present=False), has_local_data=False)
    assert "No workout history has been imported from Hevy yet" in notice
    assert "Do not invent training figures" in notice


def test_history_from_before_sync_tracking_is_unknown_age_not_missing() -> None:
    unverified = status_for()  # a saved key with no recorded run
    notice = coach_data_notice(unverified, has_local_data=True)
    assert "age of the workout history below is unknown" in notice
    assert "Do not invent" not in notice


def test_coach_is_told_when_syncing_is_failing_or_data_is_stale() -> None:
    failing = status_for(sync_ok(5 * HOUR), sync_fail(20), sync_fail(10))
    notice = coach_data_notice(failing, has_local_data=True)
    assert "Hevy syncing is failing" in notice and "only current to 2026-11-11" in notice
    stale = status_for(sync_ok(80 * HOUR))
    assert "last synced on 2026-11-08" in coach_data_notice(stale, has_local_data=True)
    refreshing = status_for(sync_ok(80 * HOUR), run(RunKind.SYNC, RunStatus.RUNNING, 1))
    assert refreshing.state is S.SYNCING
    assert "last synced on 2026-11-08" in coach_data_notice(refreshing, has_local_data=True)
    fresh_syncing = status_for(sync_ok(HOUR), run(RunKind.SYNC, RunStatus.RUNNING, 1))
    assert coach_data_notice(fresh_syncing, has_local_data=True) == ""


def test_every_reason_and_action_has_user_facing_copy() -> None:
    assert set(integration_state._MESSAGES) == set(Reason)
    assert set(integration_state._ACTION_LABELS) == set(NextAction) - {NextAction.NONE}
    for template in integration_state._MESSAGES.values():
        assert template.strip().endswith((".", ")"))


# --- payload ----------------------------------------------------------------


def test_payload_is_json_safe_and_secret_free() -> None:
    status = status_for(sync_ok(2 * HOUR), sync_fail(10, "rate_limited"))
    payload = status.to_payload()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["state"] == "ready" and payload["freshness"] == "fresh"
    assert payload["last_run"] == {"fetched": 0, "written": 0, "skipped": 0}
    assert not {key for key in payload if "token" in key or "key" in key or "secret" in key}
    account = derive_account_status(
        {"hevy": status}, required=("hevy",), now=NOW
    ).to_payload()
    assert json.loads(json.dumps(account)) == account
    assert account["generated_at"] == NOW.isoformat()


def test_docs_describe_every_state_reason_and_action() -> None:
    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "INTEGRATION_STATE_MACHINE.md"
    ).read_text(encoding="utf-8")
    for enum in (IntegrationState, Reason, NextAction, RunKind):
        for member in enum:
            assert f"`{member.value}`" in doc, f"{enum.__name__}.{member.name} is undocumented"


# --- properties -------------------------------------------------------------

KINDS = st.sampled_from(list(RunKind))
STATUSES = st.sampled_from(list(RunStatus))
ERRORS = st.sampled_from([None, "unauthorized", "forbidden", "rate_limited", "provider_unavailable", "provider_error"])


@st.composite
def run_sets(draw: st.DrawFn) -> tuple[RunFact, ...]:
    minutes = draw(st.lists(st.integers(1, 20 * DAY), min_size=0, max_size=12, unique=True))
    items = []
    for offset in minutes:
        status = draw(STATUSES)
        items.append(
            run(
                draw(KINDS),
                status,
                offset,
                error=draw(ERRORS) if status is not RunStatus.SUCCEEDED else None,
                retryable=draw(st.booleans()),
                counts=(draw(st.integers(0, 50)), draw(st.integers(0, 50)), draw(st.integers(0, 5))),
            )
        )
    return tuple(items)


@st.composite
def fact_sets(draw: st.DrawFn) -> IntegrationFacts:
    runs = draw(run_sets())
    changed = draw(st.one_of(st.none(), st.integers(1, 20 * DAY).map(lambda m: NOW - timedelta(minutes=m))))
    return make(
        *runs,
        policy=draw(st.sampled_from([SYNC_POLICY, REQUEST_POLICY, IntegrationPolicy()])),
        available=draw(st.booleans()),
        credential_present=draw(st.booleans()),
        credential_verified=draw(st.booleans()),
        credential_changed_at=changed,
    )


@settings(max_examples=250, deadline=None, derandomize=True)
@given(fact_sets(), st.randoms(use_true_random=False))
def test_derivation_is_deterministic_and_order_independent(
    facts: IntegrationFacts, rng: random.Random
) -> None:
    first = derive(facts)
    assert derive(facts) == first
    shuffled = list(facts.runs)
    rng.shuffle(shuffled)
    reordered = IntegrationFacts(
        **{**{f: getattr(facts, f) for f in facts.__slots__}, "runs": tuple(shuffled)}  # type: ignore[attr-defined]
    )
    assert derive(reordered) == first


@settings(max_examples=250, deadline=None, derandomize=True)
@given(fact_sets())
def test_state_invariants_hold_for_any_facts(facts: IntegrationFacts) -> None:
    status = derive(facts)
    assert json.loads(json.dumps(status.to_payload()))["state"] == status.state.value
    assert status.message and "{" not in status.message and "}" not in status.message
    if not facts.available or not facts.credential_present:
        assert status.state in {S.SETUP_REQUIRED, S.CONNECTING}
        assert status.state is not S.CONNECTING or status.reason is R.AUTHORIZING
    if status.state is S.READY and facts.policy.requires_sync:
        assert status.last_success_at is not None
        assert status.freshness in {Freshness.FRESH, Freshness.AGING}
    if status.state is S.STALE:
        assert status.freshness is Freshness.STALE
    if status.reason is R.PARTIAL_IMPORT:
        assert status.state is S.DEGRADED and status.retryable
    if status.reason is R.CREDENTIAL_REJECTED:
        assert status.state in {S.DEGRADED, S.SETUP_REQUIRED}
        assert status.last_error_code in AUTH_ERROR_CODES
    if status.state is S.SYNCING:
        assert facts.credential_present and facts.available


@settings(max_examples=150, deadline=None, derandomize=True)
@given(fact_sets(), run_sets())
def test_evidence_older_than_the_credential_never_changes_state(
    facts: IntegrationFacts, extra: tuple[RunFact, ...]
) -> None:
    changed = NOW - timedelta(minutes=5)
    base = IntegrationFacts(
        **{**{f: getattr(facts, f) for f in facts.__slots__}, "credential_changed_at": changed, "runs": ()}  # type: ignore[attr-defined]
    )
    older = tuple(
        RunFact(
            kind=item.kind,
            status=item.status,
            started_at=min(item.started_at, changed - timedelta(minutes=1)),
            finished_at=item.finished_at,
            error_code=item.error_code,
            retryable=item.retryable,
        )
        for item in extra
    )
    with_old = IntegrationFacts(
        **{**{f: getattr(base, f) for f in base.__slots__}, "runs": older}  # type: ignore[attr-defined]
    )
    clean, noisy = derive(base), derive(with_old)
    assert (clean.state, clean.reason, clean.next_action) == (noisy.state, noisy.reason, noisy.next_action)


@settings(max_examples=150, deadline=None, derandomize=True)
@given(fact_sets(), st.booleans())
def test_coach_notice_is_prompt_safe_for_any_facts(facts: IntegrationFacts, local: bool) -> None:
    notice = coach_data_notice(derive(facts), has_local_data=local)
    assert "{" not in notice and "}" not in notice and "\n" not in notice
