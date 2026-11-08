"""Readiness states must never overstate data or provider capability."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from product_readiness import ReadinessPhase, derive_product_readiness

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _derive(**overrides):
    defaults = {
        "has_source_connected": False,
        "has_training_data": False,
        "last_data_at": None,
        "now": NOW,
        "provider_configured": False,
        "provider_available": False,
    }
    defaults.update(overrides)
    return derive_product_readiness(**defaults)


def test_no_source_and_no_data_requires_setup():
    readiness = _derive()
    assert readiness.phase is ReadinessPhase.SETUP_REQUIRED
    assert readiness.data.available is False
    assert readiness.coach_grounded is False
    assert readiness.setup_steps[0].complete is False


def test_connected_source_without_data_is_connecting():
    readiness = _derive(has_source_connected=True)
    assert readiness.phase is ReadinessPhase.CONNECTING
    assert readiness.data.available is False


def test_in_progress_sync_is_syncing():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=1),
        provider_configured=True,
        provider_available=True,
        sync_in_progress=True,
    )
    assert readiness.phase is ReadinessPhase.SYNCING


def test_fresh_data_and_provider_is_ready_and_grounded():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=2),
        provider_configured=True,
        provider_available=True,
    )
    assert readiness.phase is ReadinessPhase.READY
    assert readiness.coach_grounded is True
    assert readiness.provider.verified is True
    assert all(step.complete for step in readiness.setup_steps)


def test_old_data_is_stale_not_ready():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(days=10),
        provider_configured=True,
        provider_available=True,
    )
    assert readiness.phase is ReadinessPhase.STALE
    assert readiness.data.available is True
    assert "old" in (readiness.data.reason or "").lower()


def test_historical_data_without_source_is_stale():
    readiness = _derive(
        has_source_connected=False,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=2),
        provider_configured=True,
        provider_available=True,
    )
    assert readiness.phase is ReadinessPhase.STALE
    assert readiness.data.available is True


def test_source_error_is_degraded():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=False,
        source_error="Hevy rejected the API key.",
        provider_configured=True,
        provider_available=True,
    )
    assert readiness.phase is ReadinessPhase.DEGRADED
    assert "Hevy rejected the API key." in readiness.reasons


def test_provider_error_is_degraded():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=2),
        provider_configured=True,
        provider_available=False,
        provider_error="The gemini provider SDK is unavailable on this server.",
    )
    assert readiness.phase is ReadinessPhase.DEGRADED


def test_data_without_provider_never_claims_grounded_coach():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=2),
        provider_configured=False,
        provider_available=False,
    )
    assert readiness.phase is ReadinessPhase.SETUP_REQUIRED
    assert readiness.coach_grounded is False
    assert readiness.data.available is True
    assert readiness.provider.available is False
    assert "provider" in (readiness.provider.reason or "").lower()


def test_payload_exposes_each_capability_without_secrets():
    readiness = _derive(
        has_source_connected=True,
        has_training_data=True,
        last_data_at=NOW - timedelta(hours=2),
        provider_configured=True,
        provider_available=True,
    )
    payload = readiness.as_dict()

    assert payload["phase"] == "ready"
    assert payload["data"] == {"available": True, "verified": True, "reason": None}
    assert payload["provider"]["available"] is True
    assert payload["coach"]["grounded"] is True
    assert [step["key"] for step in payload["setup_steps"]] == [
        "connect_source",
        "sync_source",
        "configure_provider",
        "coach_ready",
    ]
    assert "api_key" not in str(payload).lower()
