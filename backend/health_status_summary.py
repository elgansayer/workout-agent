"""Aggregate health integration state for onboarding/dashboard."""

from __future__ import annotations

from dataclasses import dataclass

from health_capability_state import HealthCapabilityState


@dataclass(frozen=True, slots=True)
class HealthSetupSummary:
    ready: bool
    message: str


def setup_summary(state: HealthCapabilityState) -> HealthSetupSummary:
    if not state.has_connected_source:
        return HealthSetupSummary(False, "Connect a health source to enable recovery-aware coaching.")
    if not state.has_fresh_data:
        return HealthSetupSummary(False, "Your health source is connected but does not have fresh data yet.")
    return HealthSetupSummary(True, "Recovery-aware coaching has fresh connected health data.")
