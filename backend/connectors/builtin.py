"""Built-in provider registry construction."""

from __future__ import annotations

from .garmin import GarminHealthConnector, GarminTrainingConnector
from .registry import ConnectorRegistry


def build_builtin_registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        (
            GarminHealthConnector(),
            GarminTrainingConnector(),
        )
    )
