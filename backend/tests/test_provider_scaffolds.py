import pytest

from connectors import ConnectorContext, ConnectorError
from connectors.fitbit import FitbitConnector
from connectors.oura import OuraConnector
from connectors.polar import PolarConnector
from connectors.withings import WithingsConnector


@pytest.mark.parametrize(
    ("connector", "metric"),
    [
        (FitbitConnector(), "sleep"),
        (OuraConnector(), "readiness"),
        (PolarConnector(), "training"),
        (WithingsConnector(), "body_composition"),
    ],
)
def test_provider_scaffolds_publish_capabilities_without_network_calls(connector, metric):
    assert connector.capabilities.authorize
    assert connector.capabilities.refresh
    assert metric in connector.capabilities.metrics
    with pytest.raises(ConnectorError):
        connector.sync(ConnectorContext(1))
