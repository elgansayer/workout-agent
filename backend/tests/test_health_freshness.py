from datetime import datetime, timedelta, timezone

from health_freshness import Freshness, classify_freshness


NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def test_freshness_states_are_deterministic():
    assert classify_freshness(None, now=NOW) == Freshness.NEVER
    assert classify_freshness(NOW - timedelta(hours=1), now=NOW) == Freshness.FRESH
    assert classify_freshness(NOW - timedelta(hours=48), now=NOW) == Freshness.AGING
    assert classify_freshness(NOW - timedelta(hours=80), now=NOW) == Freshness.STALE
