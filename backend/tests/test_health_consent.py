from datetime import datetime, timezone

from health_consent import HealthConsent


def test_health_consent_is_versioned_and_revocable():
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    active = HealthConsent(1, "oura", frozenset({"sleep"}), "v1", now)
    revoked = HealthConsent(1, "oura", frozenset({"sleep"}), "v1", now, revoked_at=now)
    assert active.active
    assert not revoked.active
