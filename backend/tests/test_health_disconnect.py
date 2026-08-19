from connectors.base import Connector, ConnectorCapabilities, ConnectorContext, ConnectorState, ConnectorStatus, SyncResult
from health_disconnect import disconnect_connector
from health_retention import DisconnectMode, DisconnectRequest


class Fake(Connector):
    provider = "fake"
    capabilities = ConnectorCapabilities()
    def __init__(self): self.calls = []
    def status(self, context): return ConnectorStatus(self.provider, ConnectorState.CONNECTED)
    def test(self, context): return self.status(context)
    def sync(self, context, *, cursor=None): return SyncResult.empty(self.provider)
    def disconnect(self, context): self.calls.append("disconnect")
    def purge(self, context): self.calls.append("purge")


def test_disconnect_then_purge_only_when_confirmed():
    connector = Fake()
    disconnect_connector(connector, ConnectorContext(1), DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE, confirmed=True))
    assert connector.calls == ["disconnect", "purge"]
