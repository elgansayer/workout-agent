"""Disconnect workflow boundary for provider connectors."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from connectors.base import Connector, ConnectorContext
from health_purge import PurgeResult, purge_imported_data
from health_retention import DisconnectMode, DisconnectRequest


@dataclass(frozen=True, slots=True)
class DisconnectOutcome:
    provider: str
    mode: DisconnectMode
    purged: PurgeResult | None = None


def disconnect_connector(
    connector: Connector,
    context: ConnectorContext,
    request: DisconnectRequest,
    *,
    connection: sqlite3.Connection | None = None,
) -> DisconnectOutcome:
    """Revoke upstream access, then purge local data only when requested.

    The connector's own ``purge`` hook releases provider-specific state. When a
    database connection is supplied, the shared purge service also removes the
    provider's imported records and invalidates derived insights for the same
    tenant. Both steps are idempotent.
    """

    request.validate(authenticated_user_id=context.user_id)
    if request.mode.purges_imported_data and not connector.capabilities.purge:
        raise ValueError(f"connector {connector.provider} does not support purge")

    connector.disconnect(context)

    purged: PurgeResult | None = None
    if request.mode.purges_imported_data:
        connector.purge(context)
        if connection is not None:
            purged = purge_imported_data(
                connection,
                user_id=context.user_id,
                provider=connector.provider,
                connection_id=context.connection_id or request.connection_id,
            )
    return DisconnectOutcome(provider=connector.provider, mode=request.mode, purged=purged)
