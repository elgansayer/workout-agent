"""Android Health Connect companion connector boundary.

Health Connect is device-local. The server receives authenticated normalized
uploads from a companion app rather than attempting server-side Google OAuth.
"""

from __future__ import annotations

from .base import Connector, ConnectorCapabilities, ConnectorContext, ConnectorError, ConnectorState, ConnectorStatus, SyncResult


class HealthConnectConnector(Connector):
    provider = "health_connect"
    capabilities = ConnectorCapabilities(
        authorize=False,
        refresh=False,
        backfill=True,
        metrics=frozenset({"sleep", "heart_rate", "hrv", "steps", "body", "activity", "spo2"}),
    )

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(self.provider, ConnectorState.DISCONNECTED, message="Pair an Android companion device to sync Health Connect")

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise ConnectorError("Health Connect sync is initiated by the paired Android companion", code="client_push_required")

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None
