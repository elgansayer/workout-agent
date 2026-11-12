"""Public connector status projections never expose credentials or other tenants."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import pytest
from connectors.base import (
    Connector,
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorState,
    ConnectorStatus,
    SyncResult,
)
from connectors.status import SENSITIVE_KEYS, public_connector_status

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


class _FakeConnector(Connector):
    provider = "synthetic"
    capabilities = ConnectorCapabilities(
        authorize=True,
        metrics=frozenset({"weight_kg"}),
    )

    def __init__(self, status: ConnectorStatus) -> None:
        self._status = status
        self.contexts: list[ConnectorContext] = []

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        self.contexts.append(context)
        return self._status

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self._status

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        return SyncResult.empty(self.provider)

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None


def _status(metadata: Mapping[str, Any] | None = None, **overrides: Any) -> ConnectorStatus:
    values: dict[str, Any] = {
        "provider": "synthetic",
        "state": ConnectorState.CONNECTED,
        "last_success_at": NOW,
        "last_attempt_at": NOW,
        "message": "Connected.",
        "retryable": False,
        "metadata": metadata if metadata is not None else {},
    }
    values.update(overrides)
    return ConnectorStatus(**values)


def test_the_projection_is_plain_serialisable_data() -> None:
    connector = _FakeConnector(_status({"account": "synthetic-account"}))

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["provider"] == "synthetic"
    assert payload["state"] == "connected"
    assert payload["last_success_at"] == NOW.isoformat()
    assert payload["last_attempt_at"] == NOW.isoformat()
    assert payload["message"] == "Connected."
    assert payload["retryable"] is False
    assert payload["metadata"] == {"account": "synthetic-account"}
    assert payload["capabilities"]["authorize"] is True
    assert payload["capabilities"]["metrics"] == frozenset({"weight_kg"})


def test_a_connector_that_never_ran_has_no_timestamps() -> None:
    connector = _FakeConnector(
        _status(
            state=ConnectorState.DISCONNECTED,
            last_success_at=None,
            last_attempt_at=None,
            message=None,
        )
    )

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["state"] == "disconnected"
    assert payload["last_success_at"] is None
    assert payload["last_attempt_at"] is None
    assert payload["message"] is None


@pytest.mark.parametrize("state", list(ConnectorState))
def test_every_state_is_reported_by_its_value(state: ConnectorState) -> None:
    connector = _FakeConnector(_status(state=state, retryable=state is ConnectorState.ERROR))

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["state"] == state.value
    assert payload["retryable"] is (state is ConnectorState.ERROR)


def test_credential_metadata_is_stripped_whatever_its_case() -> None:
    secrets = {
        "token": "synthetic-token",
        "ACCESS_TOKEN": "synthetic-access",
        "Refresh_Token": "synthetic-refresh",
        "api_key": "synthetic-key",
        "Client_Secret": "synthetic-client-secret",
        "Authorization": "Bearer synthetic",
        "COOKIE": "session=synthetic",
    }
    connector = _FakeConnector(_status({**secrets, "account": "kept", "records": 4}))

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["metadata"] == {"account": "kept", "records": 4}
    rendered = repr(payload)
    for value in secrets.values():
        assert value not in rendered


def test_every_documented_sensitive_key_is_covered() -> None:
    assert SENSITIVE_KEYS >= {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "client_secret",
        "authorization",
        "cookie",
    }
    metadata = {key: "synthetic-secret" for key in SENSITIVE_KEYS}
    connector = _FakeConnector(_status(metadata))

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["metadata"] == {}


def test_read_only_mappings_are_accepted_and_filtered() -> None:
    connector = _FakeConnector(
        _status(MappingProxyType({"api_key": "synthetic-key", "account": "kept"}))
    )

    payload = public_connector_status(connector, ConnectorContext(user_id="user-1"))

    assert payload["metadata"] == {"account": "kept"}


def test_the_status_is_read_for_exactly_the_requesting_tenant() -> None:
    connector = _FakeConnector(_status())
    context = ConnectorContext(user_id="user-1", connection_id="conn-1")

    public_connector_status(connector, context)

    assert connector.contexts == [context]


def test_a_tenantless_context_cannot_be_built() -> None:
    with pytest.raises(ValueError):
        ConnectorContext(user_id="")
