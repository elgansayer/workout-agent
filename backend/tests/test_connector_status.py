from connectors.base import Connector, ConnectorCapabilities, ConnectorState, ConnectorStatus, SyncResult
from connectors.status import public_connector_status
from connectors import ConnectorContext


class StatusConnector(Connector):
    provider = "status"
    capabilities = ConnectorCapabilities(metrics=frozenset({"steps"}))

    def status(self, context):
        return ConnectorStatus(
            self.provider,
            ConnectorState.ATTENTION,
            message="Reconnect required",
            metadata={"scope": "activity", "access_token": "secret", "api_key": "secret2"},
        )

    def test(self, context):
        return self.status(context)

    def sync(self, context, *, cursor=None):
        return SyncResult.empty(self.provider)

    def disconnect(self, context):
        pass

    def purge(self, context):
        pass


def test_public_status_exposes_lifecycle_but_redacts_secrets():
    result = public_connector_status(StatusConnector(), ConnectorContext(1))
    assert result["state"] == "attention"
    assert result["metadata"] == {"scope": "activity"}
    assert result["capabilities"]["metrics"] == frozenset({"steps"})
