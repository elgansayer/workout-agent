import pytest

from connectors import ConnectorContext, ConnectorError
from connectors.health_connect import HealthConnectConnector


def test_health_connect_is_client_push_not_server_oauth():
    connector = HealthConnectConnector()
    assert not connector.capabilities.authorize
    assert connector.capabilities.backfill
    assert "sleep" in connector.capabilities.metrics
    with pytest.raises(ConnectorError) as exc:
        connector.sync(ConnectorContext(1))
    assert exc.value.code == "client_push_required"
