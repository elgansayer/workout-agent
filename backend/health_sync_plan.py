"""Deterministic health connector sync planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from health_connection import HealthConnection, HealthConnectionState
from health_schedule import sync_due


@dataclass(frozen=True, slots=True)
class SyncPlanItem:
    connection_id: str
    provider: str
    cursor: str | None


def build_sync_plan(connections: list[HealthConnection], *, now: datetime) -> tuple[SyncPlanItem, ...]:
    return tuple(
        SyncPlanItem(item.id, item.provider, item.cursor)
        for item in sorted(connections, key=lambda value: (value.provider, value.id))
        if item.state is HealthConnectionState.CONNECTED and sync_due(item.last_sync_at, now=now)
    )
