"""Deterministic coverage for the tenant-scoped setup-state contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from setup_state import (
    SETUP_ACTIONS,
    SURFACE_REQUIREMENTS,
    SetupStateKind,
    derive_account_state,
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
        "last_activity_at": NOW - timedelta(days=1),
        "now": NOW,
    }
    facts.update(overrides)
    return derive_account_state(**facts)  # type: ignore[arg-type]


def test_new_account_reports_nothing_configured() -> None:
    account = _account(
        has_hevy_key=False,
        has_programme=False,
        has_training_data=False,
        has_session_history=False,
        has_ai_provider=False,
        last_activity_at=None,
    )

    assert account.state is SetupStateKind.NEW
    assert account.checks["hevy_connected"] is False
    assert "hevy_connected" in account.missing
    assert account.last_activity_at is None
    assert account.age_days is None
    assert account.stale is False


def test_partially_configured_account_has_hevy_but_no_programme() -> None:
    account = _account(has_programme=False, has_training_data=False, has_session_history=False)

    assert account.state is SetupStateKind.PARTIALLY_CONFIGURED


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


def test_removed_hevy_key_is_reported_as_disconnected() -> None:
    account = _account(has_hevy_key=False)

    assert account.state is SetupStateKind.DISCONNECTED


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

    assert setup.state is SetupStateKind.NEW
    assert setup.action is not None
    assert setup.action.id == "connect_hevy"
    assert setup.action.href == "/settings"
    assert "Hevy" in setup.reason


def test_surfaces_without_a_programme_point_at_the_builder() -> None:
    account = _account(has_programme=False)

    setup = surface_setup(account, "plan")

    assert setup.state is SetupStateKind.PARTIALLY_CONFIGURED
    assert setup.action is not None
    assert setup.action.id == "build_programme"
    assert setup.action.href == "/programmes"
    assert "programme_active" in setup.missing


def test_surfaces_without_training_data_point_at_the_sync() -> None:
    account = _account(has_training_data=False, has_session_history=False)

    setup = surface_setup(account, "progress")

    assert setup.state is SetupStateKind.PARTIALLY_CONFIGURED
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

    assert setup.state is SetupStateKind.PARTIALLY_CONFIGURED
    assert setup.action is not None
    assert setup.action.id == "configure_ai"
    assert setup.action.href == "/settings"


def test_disconnected_accounts_are_reconnected_first() -> None:
    account = _account(has_hevy_key=False)

    setup = surface_setup(account, "progress")

    assert setup.state is SetupStateKind.DISCONNECTED
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
    assert setup.state is SetupStateKind.PARTIALLY_CONFIGURED


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
