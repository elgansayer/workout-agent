from datetime import datetime, timedelta, timezone

from health_connection import HealthConnection, HealthConnectionState
from health_sync_plan import build_sync_plan


def test_sync_plan_contains_only_due_connected_sources():
    now = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
    connections = [
        HealthConnection("old", 1, "oura", HealthConnectionState.CONNECTED, last_sync_at=now - timedelta(hours=7)),
        HealthConnection("fresh", 1, "polar", HealthConnectionState.CONNECTED, last_sync_at=now - timedelta(hours=1)),
        HealthConnection("off", 1, "withings", HealthConnectionState.DISCONNECTED),
    ]
    plan = build_sync_plan(connections, now=now)
    assert [(item.provider, item.connection_id) for item in plan] == [("oura", "old")]
