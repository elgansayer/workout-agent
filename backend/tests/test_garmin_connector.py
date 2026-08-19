import pytest

from connectors import ConnectorContext, ConnectorError
from connectors.garmin import GarminHealthConnector, GarminTrainingConnector


def test_garmin_health_is_explicitly_approval_gated():
    connector = GarminHealthConnector()
    status = connector.status(ConnectorContext(1))
    assert status.state.value == "pending"
    assert "approval" in status.message.lower()
    with pytest.raises(ConnectorError) as exc:
        connector.sync(ConnectorContext(1))
    assert exc.value.code == "approval_required"


def test_garmin_capabilities_separate_health_and_training_write():
    assert not GarminHealthConnector.capabilities.write
    assert GarminTrainingConnector.capabilities.write
    assert "sleep" in GarminHealthConnector.capabilities.metrics
