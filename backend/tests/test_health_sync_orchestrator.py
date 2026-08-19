from connectors import ConnectorContext
from connectors.oura import OuraConnector
from health_sync_orchestrator import run_connector_sync


def test_connector_failure_becomes_isolated_sync_outcome():
    outcome = run_connector_sync(OuraConnector(), ConnectorContext(1))
    assert outcome.provider == "oura"
    assert outcome.result is None
    assert outcome.error_code == "not_configured"
