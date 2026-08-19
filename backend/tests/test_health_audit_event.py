from datetime import datetime, timezone

from health_audit_event import HealthAuditEvent


def test_health_audit_event_records_action_not_payload():
    event = HealthAuditEvent(1, "oura", "disconnect", datetime(2026, 8, 19, tzinfo=timezone.utc), "success")
    assert event.action == "disconnect"
    assert not hasattr(event, "payload")
    assert not hasattr(event, "token")
