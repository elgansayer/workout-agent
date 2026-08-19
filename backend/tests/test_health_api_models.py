from datetime import datetime, timezone

from health_api_models import connection_view
from health_connection import HealthConnection, HealthConnectionState
from health_freshness import Freshness


def test_connection_view_exposes_status_without_credentials():
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    connection = HealthConnection("c1", 1, "oura", HealthConnectionState.CONNECTED, frozenset({"sleep"}), last_sync_at=now)
    view = connection_view(connection, now=now)
    assert view.provider == "oura"
    assert view.freshness == Freshness.FRESH
    assert not hasattr(view, "token")
