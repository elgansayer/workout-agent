"""Oura API v2 connector contract and capability boundary."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import ConnectorCapabilities, ConnectorContext, ConnectorError, ConnectorState, ConnectorStatus, SyncResult
from .provider import HealthProviderConnector, NormalizedRecord


class OuraConnector(HealthProviderConnector):
    provider = "oura"
    capabilities = ConnectorCapabilities(
        authorize=True,
        refresh=True,
        backfill=True,
        metrics=frozenset({"sleep", "readiness", "heart_rate", "hrv", "workouts", "spo2"}),
    )

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(self.provider, ConnectorState.DISCONNECTED)

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise ConnectorError("Oura OAuth credentials are not configured", code="not_configured")

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None

    def normalize_record(self, context: ConnectorContext, payload: Mapping[str, Any]) -> Iterable[NormalizedRecord]:
        raise ConnectorError("Oura normalization requires API v2 fixture mapping", code="not_implemented")
