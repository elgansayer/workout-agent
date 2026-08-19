"""Garmin connector boundaries.

Production network/OAuth behaviour is intentionally gated on Garmin Developer
Program approval. These classes establish capabilities and normalization
boundaries without inventing undocumented credentials or endpoints.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import ConnectorCapabilities, ConnectorContext, ConnectorError, ConnectorState, ConnectorStatus, SyncResult
from .provider import HealthProviderConnector, NormalizedRecord


class GarminHealthConnector(HealthProviderConnector):
    provider = "garmin"
    capabilities = ConnectorCapabilities(
        authorize=True,
        refresh=True,
        backfill=True,
        webhooks=True,
        metrics=frozenset({"sleep", "heart_rate", "hrv", "steps", "stress", "spo2", "respiration", "body_composition"}),
    )

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(self.provider, ConnectorState.PENDING, message="Garmin production access requires approved credentials")

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise ConnectorError("Garmin sync is unavailable until approved credentials are configured", code="approval_required")

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None

    def normalize_record(self, context: ConnectorContext, payload: Mapping[str, Any]) -> Iterable[NormalizedRecord]:
        raise ConnectorError("Garmin payload normalization requires approved contract fixtures", code="approval_required")


class GarminTrainingConnector(GarminHealthConnector):
    provider = "garmin_training"
    capabilities = ConnectorCapabilities(authorize=True, test=True, sync=False, refresh=True, disconnect=True, purge=True, write=True)
