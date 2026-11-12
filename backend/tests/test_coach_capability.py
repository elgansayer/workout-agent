from datetime import datetime, timedelta, timezone

from coach_capability import (
    CoachStatus,
    capability_prompt_block,
    derive_coach_capability,
)
from health_freshness import Freshness

NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def _state(**overrides):
    values = {
        "provider_name": "gemini",
        "provider_configured": False,
        "provider_supported": True,
        "sync_configured": False,
        "observed_at": {},
        "programme_available": False,
        "now": NOW,
    }
    values.update(overrides)
    return derive_coach_capability(**values)


def test_no_data_and_no_provider_claims_nothing():
    state = _state()

    assert state.status is CoachStatus.UNCONFIGURED
    assert not state.can_chat
    assert not state.has_training_data
    assert not state.has_fresh_training_data
    assert state.sync_state == "not_configured"
    assert all(not source.available for source in state.sources.values())
    assert state.provider_state == "missing_key"

    prompt = capability_prompt_block(state).lower()
    assert "no training data is available" in prompt
    assert "do not claim access to logs, metrics, records, or history" in prompt
    assert "unavailable" in prompt


def test_provider_without_data_is_ready_but_never_claims_data():
    state = _state(provider_configured=True)

    assert state.status is CoachStatus.NO_DATA
    assert state.can_chat
    assert not state.has_training_data
    assert all(not source.available for source in state.sources.values())


def test_data_without_provider_keeps_data_truthful_but_blocks_chat():
    state = _state(
        sync_configured=True,
        observed_at={"progress_history": NOW - timedelta(days=1)},
    )

    assert state.status is CoachStatus.PROVIDER_MISSING
    assert not state.can_chat
    assert state.has_training_data
    assert state.has_fresh_training_data
    assert state.sync_state == "synced"
    assert state.sources["progress_history"].available


def test_stale_training_data_is_reported_as_stale():
    state = _state(
        provider_configured=True,
        sync_configured=True,
        observed_at={"training_logs": NOW - timedelta(days=45)},
    )

    assert state.status is CoachStatus.STALE_DATA
    assert state.has_training_data
    assert not state.has_fresh_training_data
    assert state.sources["training_logs"].freshness is Freshness.STALE
    assert state.sync_state == "synced"


def test_fresh_full_capability_lists_every_available_source():
    state = _state(
        provider_configured=True,
        provider_test_status="ok",
        readiness="ready",
        sync_configured=True,
        observed_at={
            "training_logs": NOW - timedelta(days=1),
            "progress_history": NOW - timedelta(days=2),
            "personal_records": NOW - timedelta(days=2),
            "body_metrics": NOW - timedelta(days=1),
        },
        programme_available=True,
    )

    assert state.status is CoachStatus.READY
    assert state.can_chat
    assert state.provider_verified
    assert state.verified_data
    assert state.has_training_data
    assert state.has_fresh_training_data
    assert state.programme_available
    assert all(source.available for source in state.sources.values())

    prompt = capability_prompt_block(state)
    assert "training logs: available" in prompt
    assert "body composition: available" in prompt
    assert "active programme: available" in prompt
    assert "No training data is available" not in prompt


def test_sync_configured_without_data_is_never_synced():
    state = _state(sync_configured=True)

    assert state.sync_state == "never_synced"
    assert not state.has_training_data


def test_unsupported_provider_is_not_reported_as_ready():
    state = _state(provider_configured=True, provider_supported=False)

    assert state.provider_state == "unsupported"
    assert not state.can_chat
    assert not state.provider_verified


FRESH = {"progress_history": NOW - timedelta(days=1)}


def test_a_stored_key_that_was_never_tested_is_not_claimed_to_work():
    state = _state(
        provider_configured=True,
        sync_configured=True,
        observed_at=FRESH,
        readiness="ready",
    )

    assert state.provider_state == "unverified"
    assert not state.provider_verified
    assert state.status is CoachStatus.PROVIDER_UNVERIFIED
    assert state.status is not CoachStatus.READY
    # An untested key may well work, so Coach may try; it just never claims it.
    assert state.can_chat
    assert state.blocked_message() is None


def test_a_failed_connection_test_pauses_coach():
    state = _state(
        provider_configured=True,
        provider_test_status="rejected",
        sync_configured=True,
        observed_at=FRESH,
        readiness="ready",
    )

    assert state.provider_state == "failed"
    assert state.status is CoachStatus.PROVIDER_FAILED
    assert not state.can_chat
    assert not state.provider_verified
    assert "connection test failed" in (state.blocked_message() or "")


def test_ready_needs_both_a_verified_provider_and_current_data():
    providers = {"missing": {}, "untested": {"provider_configured": True}}
    providers["verified"] = {"provider_configured": True, "provider_test_status": "ok"}
    data = {
        "none": {},
        "stale": {"observed_at": {"progress_history": NOW - timedelta(days=45)}},
        "fresh": {"observed_at": FRESH},
    }

    ready = {
        (provider, kind)
        for provider, provider_facts in providers.items()
        for kind, data_facts in data.items()
        if _state(
            sync_configured=True, readiness="ready", **provider_facts, **data_facts
        ).status
        is CoachStatus.READY
    }

    assert ready == {("verified", "fresh")}


def test_a_running_sync_withholds_data_claims_until_it_finishes():
    for phase in ("connecting", "syncing"):
        state = _state(
            provider_configured=True,
            provider_test_status="ok",
            sync_configured=True,
            observed_at=FRESH,
            readiness=phase,
        )

        assert state.status is CoachStatus.SYNCING
        assert not state.verified_data
        assert state.has_training_data
        assert "may be incomplete" in capability_prompt_block(state)


def test_a_degraded_connection_is_reported_even_with_fresh_stored_data():
    state = _state(
        provider_configured=True,
        provider_test_status="ok",
        sync_configured=True,
        observed_at=FRESH,
        readiness="degraded",
    )

    assert state.status is CoachStatus.DEGRADED
    assert state.verified_data
    assert state.can_chat
    assert "last successful sync" in capability_prompt_block(state)


def test_the_public_contract_carries_every_verified_fact():
    payload = _state(
        provider_configured=True,
        provider_test_status="ok",
        provider_source="user",
        sync_configured=True,
        observed_at=FRESH,
        readiness="ready",
    ).as_dict()

    assert payload["provider_state"] == "verified"
    assert payload["provider_verified"] is True
    assert payload["provider_source"] == "user"
    assert payload["verified_data"] is True
    assert payload["readiness"] == "ready"
    assert payload["status"] == "ready"
    assert payload["can_chat"] is True
