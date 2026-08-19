"""Disconnect workflow boundary for provider connectors."""

from __future__ import annotations

from connectors.base import Connector, ConnectorContext
from health_retention import DisconnectMode, DisconnectRequest


def disconnect_connector(connector: Connector, context: ConnectorContext, request: DisconnectRequest) -> None:
    request.validate(authenticated_user_id=context.user_id)
    connector.disconnect(context)
    if request.mode is DisconnectMode.REVOKE_AND_PURGE:
        connector.purge(context)
