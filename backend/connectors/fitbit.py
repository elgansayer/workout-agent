"""Fitbit Web API connector contract.

Network behaviour remains gated on resolving the current platform decision in
issue #825; this module prevents legacy Google-health assumptions leaking into
new connector code.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import ConnectorCapabilities, ConnectorContext, ConnectorError, ConnectorState, ConnectorStatus, SyncResult
from .provider import HealthProviderConnector, NormalizedRecord


class FitbitConnector(HealthProviderConnector):
    provider = "fitbit"
    capabilities = ConnectorCapabilities(authorize=True, refresh=True, backfill=True, metrics=frozenset({"sleep", "activity", "heart_rate", "workouts", "body"}))

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(self.provider, ConnectorState.PENDING, message="Fitbit platform contract decision #825 must be resolved before production activation")

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise ConnectorError("Fitbit production activation is pending platform validation", code="platform_validation_required")

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None

    def normalize_record(self, context: ConnectorContext, payload: Mapping[str, Any]) -> Iterable[NormalizedRecord]:
        raise ConnectorError("Fitbit normalization requires validated contract fixtures", code="platform_validation_required")
