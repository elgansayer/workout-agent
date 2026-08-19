"""Withings public health API connector contract."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import ConnectorCapabilities, ConnectorContext, ConnectorError, ConnectorState, ConnectorStatus, SyncResult
from .provider import HealthProviderConnector, NormalizedRecord


class WithingsConnector(HealthProviderConnector):
    provider = "withings"
    capabilities = ConnectorCapabilities(authorize=True, refresh=True, backfill=True, metrics=frozenset({"weight", "body_composition", "heart_rate", "sleep"}))

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(self.provider, ConnectorState.DISCONNECTED)

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise ConnectorError("Withings OAuth credentials are not configured", code="not_configured")

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None

    def normalize_record(self, context: ConnectorContext, payload: Mapping[str, Any]) -> Iterable[NormalizedRecord]:
        raise ConnectorError("Withings normalization requires provider fixtures", code="not_implemented")
