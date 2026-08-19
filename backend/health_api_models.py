"""Secret-free API projections for health integration UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from health_connection import HealthConnection
from health_freshness import Freshness, classify_freshness


@dataclass(frozen=True, slots=True)
class HealthConnectionView:
    provider: str
    state: str
    capabilities: tuple[str, ...]
    last_sync_at: str | None
    freshness: Freshness


def connection_view(connection: HealthConnection, *, now: datetime) -> HealthConnectionView:
    return HealthConnectionView(
        provider=connection.provider,
        state=connection.state.value,
        capabilities=tuple(sorted(connection.granted_capabilities)),
        last_sync_at=connection.last_sync_at.isoformat() if connection.last_sync_at else None,
        freshness=classify_freshness(connection.last_sync_at, now=now),
    )
