import pytest
from connectors.base import (
    Connector,
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorState,
    ConnectorStatus,
    SyncResult,
)
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


def test_retain_and_revoke_only_never_call_purge():
    for mode in (DisconnectMode.REVOKE_ONLY, DisconnectMode.RETAIN):
        connector = Fake()
        outcome = disconnect_connector(
            connector,
            ConnectorContext(1),
            DisconnectRequest(1, "c1", mode),
        )
        assert connector.calls == ["disconnect"]
        assert outcome.purged is None
        assert outcome.mode is mode


def test_cross_tenant_disconnect_does_not_touch_connector():
    connector = Fake()
    with pytest.raises(ValueError, match="another user"):
        disconnect_connector(
            connector,
            ConnectorContext(1),
            DisconnectRequest(2, "c1", DisconnectMode.REVOKE_ONLY),
        )
    assert connector.calls == []


def test_connector_without_purge_capability_fails_closed():
    class NoPurge(Fake):
        capabilities = ConnectorCapabilities(purge=False)

    connector = NoPurge()
    with pytest.raises(ValueError, match="does not support purge"):
        disconnect_connector(
            connector,
            ConnectorContext(1),
            DisconnectRequest(1, "c1", DisconnectMode.PURGE, confirmed=True),
        )
    assert connector.calls == []
