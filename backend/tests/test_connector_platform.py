from dataclasses import FrozenInstanceError

import pytest

from connectors import (
    Connector,
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorRegistry,
    ConnectorState,
    ConnectorStatus,
    SyncResult,
)


class FakeConnector(Connector):
    provider = "fake"
    capabilities = ConnectorCapabilities(metrics=frozenset({"weight_kg"}))

    def status(self, context):
        return ConnectorStatus(self.provider, ConnectorState.CONNECTED)

    def test(self, context):
        return self.status(context)

    def sync(self, context, *, cursor=None):
        return SyncResult.empty(self.provider)

    def disconnect(self, context):
        return None

    def purge(self, context):
        return None


def test_context_requires_explicit_positive_user():
    with pytest.raises(ValueError):
        ConnectorContext(user_id=0)
    assert ConnectorContext(user_id=42).user_id == 42


def test_registry_is_case_insensitive_and_deterministic():
    connector = FakeConnector()
    registry = ConnectorRegistry([connector])
    assert registry.get("FAKE") is connector
    assert registry.providers() == ("fake",)


def test_registry_rejects_duplicate_provider():
    registry = ConnectorRegistry([FakeConnector()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeConnector())


def test_connector_capabilities_are_immutable():
    capabilities = FakeConnector.capabilities
    with pytest.raises(FrozenInstanceError):
        capabilities.sync = False
