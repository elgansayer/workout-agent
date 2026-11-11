"""Deterministic coverage for the tenant-scoped readiness contract.

The six account states are ``setup_required``, ``connecting``, ``syncing``,
``ready``, ``stale`` and ``degraded``; each is reproducibly derived from the
facts passed in, never from a hidden clock or another tenant's records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from connector_operations import ConnectorStatus, OperationPhase, SyncOutcome
from setup_state import (
    SETUP_ACTIONS,
    SURFACE_REQUIREMENTS,
    ProviderState,
    SetupStateKind,
    derive_account_state,
    derive_provider_state,
    surface_setup,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _account(**overrides: object):
    facts = {
        "has_hevy_key": True,
        "has_programme": True,
        "has_training_data": True,
        "has_session_history": True,
        "has_ai_provider": True,
        "ai_provider_state": "verified",
        "last_activity_at": NOW - timedelta(days=1),
        "now": NOW,
    }
    facts.update(overrides)
    if not facts["has_ai_provider"] and "ai_provider_state" not in overrides:
        facts["ai_provider_state"] = "missing_key"
    return derive_account_state(**facts)  # type: ignore[arg-type]


def _connector(**overrides: object) -> ConnectorStatus:
    return ConnectorStatus(**overrides)  # type: ignore[arg-type]


def test_new_account_reports_nothing_configured() -> None:
    account = _account(
        has_hevy_key=False,
        has_programme=False,
        has_training_data=False,
        has_session_history=False,
        has_ai_provider=False,
        last_activity_at=None,
    )

    assert account.state is SetupStateKind.SETUP_REQUIRED
    assert account.first_run is True
    assert account.checks["hevy_connected"] is False
    assert "hevy_connected" in account.missing
    assert account.last_activity_at is None
    assert account.age_days is None
    assert account.stale is False


def test_account_with_hevy_but_no_programme_still_needs_setup() -> None:
    account = _account(has_programme=False, has_training_data=False, has_session_history=False)

    assert account.state is SetupStateKind.SETUP_REQUIRED
    assert account.first_run is False
    assert account.reason_code == "programme_active"


def test_ready_account_reports_fresh_data() -> None:
    account = _account()

    assert account.state is SetupStateKind.READY
    assert account.stale is False
    assert account.age_days == 1
    assert account.missing == ()


def test_stale_account_is_reported_after_the_stale_window() -> None:
    account = _account(last_activity_at=NOW - timedelta(days=9))

    assert account.state is SetupStateKind.STALE
    assert account.stale is True
    assert account.age_days == 9


def test_activity_inside_the_stale_window_stays_ready() -> None:
    account = _account(last_activity_at=NOW - timedelta(days=7))

    assert account.state is SetupStateKind.READY
    assert account.stale is False


def test_removed_hevy_key_is_reported_as_degraded() -> None:
    account = _account(has_hevy_key=False)

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == "credential_removed"
    assert "removed" in account.reason


def test_new_accounts_ask_for_the_hevy_connection_first() -> None:
    account = _account(
        has_hevy_key=False,
        has_programme=False,
        has_training_data=False,
        has_session_history=False,
        has_ai_provider=False,
        last_activity_at=None,
    )

    setup = surface_setup(account, "dashboard")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.first_run is True
    assert setup.label == "Finish setting up your training data"
    assert setup.action is not None
    assert setup.action.id == "connect_hevy"
    assert setup.action.href == "/settings"
    assert "Hevy" in setup.reason


def test_surfaces_without_a_programme_point_at_the_builder() -> None:
    account = _account(has_programme=False)

    setup = surface_setup(account, "plan")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.first_run is False
    assert setup.label == "Setup is not finished yet"
    assert setup.action is not None
    assert setup.action.id == "build_programme"
    assert setup.action.href == "/programmes"
    assert "programme_active" in setup.missing


def test_surfaces_without_training_data_point_at_the_sync() -> None:
    account = _account(has_training_data=False, has_session_history=False)

    setup = surface_setup(account, "progress")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.action is not None
    assert setup.action.id == "sync_hevy"
    assert setup.action.href == "/settings"


def test_history_requires_recorded_sessions_rather_than_lifts() -> None:
    account = _account(has_session_history=False)

    setup = surface_setup(account, "history")

    assert setup.action is not None
    assert setup.action.id == "sync_hevy"


def test_coach_requires_training_data_before_a_provider_key() -> None:
    account = _account(has_training_data=False, has_session_history=False, has_ai_provider=False)

    setup = surface_setup(account, "coach")

    assert setup.action is not None
    assert setup.action.id == "sync_hevy"


def test_coach_reports_a_missing_provider_once_data_exists() -> None:
    account = _account(has_ai_provider=False)

    setup = surface_setup(account, "coach")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.action is not None
    assert setup.action.id == "configure_ai"
    assert setup.action.href == "/settings"


def test_accounts_that_lost_their_key_are_reconnected_first() -> None:
    account = _account(has_hevy_key=False)

    setup = surface_setup(account, "progress")

    assert setup.state is SetupStateKind.DEGRADED
    assert setup.action is not None
    assert setup.action.id == "connect_hevy"
    assert "removed" in setup.reason


def test_stale_surfaces_offer_a_sync_instead_of_claiming_freshness() -> None:
    account = _account(last_activity_at=NOW - timedelta(days=30))

    setup = surface_setup(account, "stats")

    assert setup.state is SetupStateKind.STALE
    assert setup.stale is True
    assert setup.action is not None
    assert setup.action.id == "sync_hevy"
    assert "30 days ago" in setup.reason


def test_ready_surfaces_have_no_setup_action() -> None:
    account = _account()

    setup = surface_setup(account, "progress")

    assert setup.state is SetupStateKind.READY
    assert setup.action is None
    assert setup.reason == ""


def test_a_broken_programme_asks_for_a_rebuild() -> None:
    account = _account()

    setup = surface_setup(account, "dashboard", programme_status="needs_review")

    assert setup.action is not None
    assert setup.action.id == "rebuild_programme"
    assert setup.action.href == "/programmes"
    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.reason_code == "programme_needs_review"


def test_a_finished_programme_asks_for_the_next_cycle() -> None:
    account = _account()

    setup = surface_setup(account, "plan", programme_status="completed")

    assert setup.action is not None
    assert setup.action.id == "build_programme"


def test_a_programme_that_has_not_started_is_not_reported_as_broken() -> None:
    account = _account()

    setup = surface_setup(account, "plan", programme_status="not_started")

    assert setup.state is SetupStateKind.READY
    assert setup.action is None


def test_every_offered_action_points_at_a_known_route() -> None:
    known_routes = {"/settings", "/programmes"}

    for action in SETUP_ACTIONS.values():
        assert action.href in known_routes
        assert action.label
        assert action.description


def test_every_surface_has_requirements_and_a_serialisable_contract() -> None:
    account = _account()

    for surface in SURFACE_REQUIREMENTS:
        payload = surface_setup(account, surface).as_dict()
        assert payload["state"] in {str(state) for state in SetupStateKind}
        assert payload["missing"] is not None
        if payload["state"] != str(SetupStateKind.READY):
            assert payload["reason"]
            assert payload["action"]["href"] in {"/settings", "/programmes"}


# -- the six readiness states ------------------------------------------------


def test_a_running_connection_check_is_reported_as_connecting() -> None:
    account = _account(
        has_hevy_key=False,
        has_programme=False,
        has_training_data=False,
        has_session_history=False,
        has_ai_provider=False,
        last_activity_at=None,
        connector=_connector(
            operation=OperationPhase.CONNECTING,
            operation_started_at=NOW - timedelta(seconds=10),
        ),
    )

    assert account.state is SetupStateKind.CONNECTING
    assert account.reason_code == "connecting"
    assert "Checking your Hevy API key" in account.reason


def test_a_running_sync_is_reported_as_syncing_with_how_long_it_has_run() -> None:
    account = _account(
        connector=_connector(
            operation=OperationPhase.SYNCING,
            operation_started_at=NOW - timedelta(minutes=3),
        ),
    )

    assert account.state is SetupStateKind.SYNCING
    assert "3 minutes ago" in account.reason
    assert "incomplete" in account.reason


def test_live_operations_never_offer_a_setup_step_that_would_collide() -> None:
    for phase, state in (
        (OperationPhase.CONNECTING, SetupStateKind.CONNECTING),
        (OperationPhase.SYNCING, SetupStateKind.SYNCING),
    ):
        account = _account(
            has_training_data=False,
            has_session_history=False,
            connector=_connector(
                operation=phase,
                operation_started_at=NOW - timedelta(minutes=1),
            ),
        )

        for surface in SURFACE_REQUIREMENTS:
            setup = surface_setup(account, surface)
            assert setup.state is state, surface
            assert setup.action is None, surface
            assert setup.reason, surface


def test_a_live_operation_outranks_every_other_problem() -> None:
    account = _account(
        has_hevy_key=False,
        connection_state="invalid",
        connector=_connector(
            operation=OperationPhase.SYNCING,
            operation_started_at=NOW,
            last_sync_outcome=SyncOutcome.FAILED,
            last_sync_at=NOW - timedelta(days=2),
            last_sync_code="sync_failed",
        ),
    )

    assert account.state is SetupStateKind.SYNCING


def test_an_operation_that_never_finished_is_degraded_not_syncing_forever() -> None:
    account = _account(
        connector=_connector(
            operation=None,
            operation_started_at=NOW - timedelta(hours=3),
            operation_stalled=True,
        ),
    )

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == "operation_stalled"
    assert surface_setup(account, "stats").action.id == "retry_sync"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("connection_state", "code", "action"),
    [
        ("invalid", "credential_rejected", "reconnect_hevy"),
        ("insufficient_scope", "credential_insufficient_scope", "reconnect_hevy"),
        ("unverified", "connection_unverified", "verify_hevy"),
    ],
)
def test_a_connection_hevy_did_not_confirm_is_degraded(
    connection_state: str, code: str, action: str
) -> None:
    account = _account(
        connection_state=connection_state,
        connection_tested_at=NOW - timedelta(hours=1),
    )

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == code
    for surface in ("dashboard", "progress", "coach"):
        setup = surface_setup(account, surface)
        assert setup.state is SetupStateKind.DEGRADED, surface
        assert setup.action is not None and setup.action.id == action, surface
        assert setup.reason == account.reason, surface


def test_a_sync_that_succeeded_after_an_unreachable_check_clears_the_doubt() -> None:
    tested_at = NOW - timedelta(hours=5)
    account = _account(
        connection_state="unverified",
        connection_tested_at=tested_at,
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=tested_at + timedelta(hours=1),
            last_sync_records=12,
        ),
    )

    assert account.state is SetupStateKind.READY


def test_a_rejected_key_is_not_cleared_by_an_older_successful_sync() -> None:
    account = _account(
        connection_state="invalid",
        connection_tested_at=NOW - timedelta(hours=1),
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=NOW - timedelta(hours=9),
        ),
    )

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == "credential_rejected"


def test_a_failed_last_sync_is_degraded_and_says_when() -> None:
    account = _account(
        connector=_connector(
            last_sync_outcome=SyncOutcome.FAILED,
            last_sync_at=NOW - timedelta(days=1),
            last_sync_code="hevy_unavailable",
        ),
    )

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == "sync_failed"
    assert "2026-08-31" in account.reason
    setup = surface_setup(account, "history")
    assert setup.action is not None and setup.action.id == "retry_sync"
    assert setup.state is SetupStateKind.DEGRADED


def test_a_later_successful_sync_replaces_the_failure() -> None:
    account = _account(
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=NOW - timedelta(hours=1),
            last_sync_records=40,
        ),
    )

    assert account.state is SetupStateKind.READY
    assert account.reason == ""


def test_a_recent_sync_keeps_data_fresh_even_when_the_athlete_has_not_trained() -> None:
    quiet = _account(
        last_activity_at=NOW - timedelta(days=20),
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=NOW - timedelta(hours=2),
            last_sync_records=40,
        ),
    )
    assert quiet.state is SetupStateKind.READY
    assert quiet.stale is False
    assert quiet.age_days == 20

    abandoned = _account(
        last_activity_at=NOW - timedelta(days=20),
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=NOW - timedelta(days=15),
            last_sync_records=40,
        ),
    )
    assert abandoned.state is SetupStateKind.STALE


def test_a_sync_that_found_no_workouts_says_so_instead_of_asking_for_a_sync() -> None:
    account = _account(
        has_training_data=False,
        has_session_history=False,
        last_activity_at=None,
        connector=_connector(
            last_sync_outcome=SyncOutcome.SUCCEEDED,
            last_sync_at=NOW - timedelta(minutes=5),
            last_sync_records=0,
        ),
    )

    assert account.state is SetupStateKind.SETUP_REQUIRED
    assert "found no logged workouts" in account.reason
    setup = surface_setup(account, "history")
    assert "found no logged workouts" in setup.reason
    assert setup.action is not None and setup.action.id == "sync_hevy"


def test_a_surface_with_everything_it_needs_is_ready_while_setup_is_open_elsewhere() -> None:
    account = _account(has_programme=False)

    assert account.state is SetupStateKind.SETUP_REQUIRED
    progress = surface_setup(account, "progress")
    assert progress.state is SetupStateKind.READY
    assert progress.action is None
    assert surface_setup(account, "plan").state is SetupStateKind.SETUP_REQUIRED


# -- provider verification ---------------------------------------------------


@pytest.mark.parametrize(
    ("supported", "configured", "last_test", "expected"),
    [
        (False, True, "ok", ProviderState.UNSUPPORTED),
        (True, False, None, ProviderState.MISSING_KEY),
        (True, True, None, ProviderState.UNVERIFIED),
        (True, True, "", ProviderState.UNVERIFIED),
        (True, True, "ok", ProviderState.VERIFIED),
        (True, True, "OK", ProviderState.VERIFIED),
        (True, True, "rejected", ProviderState.FAILED),
        (True, True, "unreachable", ProviderState.FAILED),
    ],
)
def test_a_provider_is_verified_only_by_a_passing_connection_test(
    supported: bool, configured: bool, last_test: str | None, expected: ProviderState
) -> None:
    assert (
        derive_provider_state(
            supported=supported, configured=configured, last_test_status=last_test
        )
        is expected
    )


def test_coach_asks_for_a_connection_test_when_the_key_was_never_tested() -> None:
    account = _account(ai_provider_state="unverified")

    setup = surface_setup(account, "coach")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.reason_code == "ai_verified"
    assert setup.action is not None and setup.action.id == "verify_ai"
    # Every other surface is unaffected by the AI provider.
    assert surface_setup(account, "progress").state is SetupStateKind.READY


def test_coach_asks_for_a_fix_when_the_last_connection_test_failed() -> None:
    account = _account(ai_provider_state="failed")

    setup = surface_setup(account, "coach")

    assert setup.action is not None and setup.action.id == "fix_ai"
    assert "test failed" in setup.reason
    assert account.checks["ai_provider"] is True
    assert account.checks["ai_verified"] is False


def test_coach_is_ready_only_with_a_verified_provider() -> None:
    assert surface_setup(_account(), "coach").state is SetupStateKind.READY
    assert surface_setup(_account(ai_provider_state=None), "coach").state is not (
        SetupStateKind.READY
    )


# -- the contract ------------------------------------------------------------


def test_all_six_states_are_reachable_and_serialisable() -> None:
    syncing = _connector(
        operation=OperationPhase.SYNCING, operation_started_at=NOW
    )
    connecting = _connector(
        operation=OperationPhase.CONNECTING, operation_started_at=NOW
    )
    reached = {
        _account(has_hevy_key=False, has_programme=False, has_training_data=False,
                 has_session_history=False, has_ai_provider=False,
                 last_activity_at=None).state,
        _account(connector=connecting).state,
        _account(connector=syncing).state,
        _account().state,
        _account(last_activity_at=NOW - timedelta(days=30)).state,
        _account(has_hevy_key=False).state,
    }

    assert reached == set(SetupStateKind)
    assert {state.value for state in SetupStateKind} == {
        "setup_required",
        "connecting",
        "syncing",
        "ready",
        "stale",
        "degraded",
    }


def test_the_account_payload_exposes_the_connector_without_provider_text() -> None:
    account = _account(
        connector=_connector(
            last_sync_outcome=SyncOutcome.FAILED,
            last_sync_at=NOW - timedelta(hours=1),
            last_sync_code="hevy_unavailable",
        ),
        connection_state="connected",
    )

    payload = account.as_dict()

    assert payload["state"] == "degraded"
    assert payload["reason_code"] == "sync_failed"
    assert payload["first_run"] is False
    assert payload["connection_state"] == "connected"
    assert payload["connector"]["last_sync"]["code"] == "hevy_unavailable"
    assert payload["ai_provider_state"] == "verified"


def test_a_shared_ai_key_does_not_make_a_new_account_look_set_up() -> None:
    account = _account(
        has_hevy_key=False,
        has_programme=False,
        has_training_data=False,
        has_session_history=False,
        has_ai_provider=True,
        ai_provider_state="unverified",
        last_activity_at=None,
    )

    assert account.state is SetupStateKind.SETUP_REQUIRED
    assert account.first_run is True
    assert surface_setup(account, "dashboard").first_run is True


def test_the_shared_server_key_asks_for_no_test_it_cannot_take() -> None:
    shared = _account(ai_provider_state="unverified", ai_provider_source="server")
    own = _account(ai_provider_state="unverified", ai_provider_source="user")

    # Coach is not claimed to work in either case: the check stays honest.
    assert shared.checks["ai_verified"] is False
    assert own.checks["ai_verified"] is False
    assert shared.as_dict()["ai_provider_source"] == "server"
    # But only an account that owns a key is sent to test it.
    assert surface_setup(shared, "coach").state is SetupStateKind.READY
    assert surface_setup(shared, "coach").action is None
    asked = surface_setup(own, "coach")
    assert asked.state is SetupStateKind.SETUP_REQUIRED
    assert asked.action is not None and asked.action.id == "verify_ai"


def test_a_failing_shared_key_still_needs_attention() -> None:
    account = _account(ai_provider_state="failed", ai_provider_source="server")

    setup = surface_setup(account, "coach")

    assert setup.state is SetupStateKind.SETUP_REQUIRED
    assert setup.action is not None and setup.action.id == "fix_ai"
