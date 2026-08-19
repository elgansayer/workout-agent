"""Built-in provider registry construction."""

from __future__ import annotations

from .fitbit import FitbitConnector
from .garmin import GarminHealthConnector, GarminTrainingConnector
from .oura import OuraConnector
from .polar import PolarConnector
from .registry import ConnectorRegistry
from .withings import WithingsConnector


def build_builtin_registry() -> ConnectorRegistry:
    return ConnectorRegistry(
        (
            FitbitConnector(),
            GarminHealthConnector(),
            GarminTrainingConnector(),
            OuraConnector(),
            PolarConnector(),
            WithingsConnector(),
        )
    )
