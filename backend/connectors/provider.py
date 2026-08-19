"""Base class for health providers that normalize external records."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import Any, Mapping

from health_models import HealthMetric, SleepSession, WearableActivity

from .base import Connector, ConnectorContext

NormalizedRecord = HealthMetric | SleepSession | WearableActivity


class HealthProviderConnector(Connector):
    """Connector extension for providers that emit canonical health records."""

    @abstractmethod
    def normalize_record(
        self,
        context: ConnectorContext,
        payload: Mapping[str, Any],
    ) -> Iterable[NormalizedRecord]:
        """Normalize one provider payload while preserving source provenance."""
        raise NotImplementedError
