"""Truthful health capability state for dashboard/coach surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from health_connection import HealthConnection, HealthConnectionState


@dataclass(frozen=True, slots=True)
class HealthCapabilityState:
    has_connected_source: bool
    has_fresh_data: bool
    capabilities: frozenset[str]


def derive_capability_state(connections: list[HealthConnection], *, fresh_connection_ids: set[str]) -> HealthCapabilityState:
    connected = [item for item in connections if item.state is HealthConnectionState.CONNECTED]
    return HealthCapabilityState(
        has_connected_source=bool(connected),
        has_fresh_data=any(item.id in fresh_connection_ids for item in connected),
        capabilities=frozenset(capability for item in connected for capability in item.granted_capabilities),
    )
