"""External connector platform."""

from .base import (
    Connector,
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorError,
    ConnectorState,
    ConnectorStatus,
    SyncResult,
)
from .registry import ConnectorRegistry, registry

__all__ = [
    "Connector",
    "ConnectorCapabilities",
    "ConnectorContext",
    "ConnectorError",
    "ConnectorRegistry",
    "ConnectorState",
    "ConnectorStatus",
    "SyncResult",
    "registry",
]
