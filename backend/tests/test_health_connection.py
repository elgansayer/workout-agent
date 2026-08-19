import pytest

from health_connection import HealthConnection, HealthConnectionState


def test_connection_is_per_user_and_capability_scoped():
    connection = HealthConnection("c1", 1, "oura", HealthConnectionState.CONNECTED, frozenset({"sleep", "hrv"}))
    assert connection.user_id == 1
    assert connection.granted_capabilities == frozenset({"sleep", "hrv"})


def test_connection_rejects_missing_tenant_identity():
    with pytest.raises(ValueError):
        HealthConnection("c1", 0, "oura", HealthConnectionState.PENDING)
