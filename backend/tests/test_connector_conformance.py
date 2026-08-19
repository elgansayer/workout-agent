from connectors.base import Connector, ConnectorCapabilities, ConnectorState, ConnectorStatus, SyncResult
from connectors.conformance import run_conformance


class ConformingConnector(Connector):
    provider = "conforming"
    capabilities = ConnectorCapabilities()

    def status(self, context):
        return ConnectorStatus(self.provider, ConnectorState.CONNECTED)

    def test(self, context):
        return ConnectorStatus(self.provider, ConnectorState.CONNECTED)

    def sync(self, context, *, cursor=None):
        return SyncResult.empty(self.provider)

    def disconnect(self, context):
        pass

    def purge(self, context):
        pass


def test_conforming_connector_passes_shared_suite():
    result = run_conformance(ConformingConnector())
    assert result.passed
    assert result.provider == "conforming"
