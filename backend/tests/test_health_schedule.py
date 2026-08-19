from datetime import datetime, timedelta, timezone

from health_schedule import sync_due


def test_sync_schedule_is_due_for_never_or_old_sync():
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    assert sync_due(None, now=now)
    assert sync_due(now - timedelta(hours=7), now=now)
    assert not sync_due(now - timedelta(hours=1), now=now)
