from datetime import datetime, timedelta, timezone

from health_test_action import ConnectorTestLimiter


def test_connector_test_action_is_rate_limited_per_user_provider():
    limiter = ConnectorTestLimiter(timedelta(seconds=30))
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    assert limiter.allow(1, "oura", now=now)
    assert not limiter.allow(1, "oura", now=now + timedelta(seconds=10))
    assert limiter.allow(2, "oura", now=now + timedelta(seconds=10))
