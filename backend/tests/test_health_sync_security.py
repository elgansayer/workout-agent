import pytest

from health_connection import HealthConnection, HealthConnectionState
from health_sync_security import require_connection_owner


def test_connection_owner_check_fails_closed():
    connection = HealthConnection("c1", 1, "oura", HealthConnectionState.CONNECTED)
    assert require_connection_owner(connection, user_id=1) is connection
    with pytest.raises(ValueError, match="another user"):
        require_connection_owner(connection, user_id=2)
