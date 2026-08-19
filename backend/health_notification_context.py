"""Minimal health context allowed into notification templates."""

from __future__ import annotations

from dataclasses import dataclass

from adaptive_training import TrainingAdjustment
from health_readiness import ReadinessState


@dataclass(frozen=True, slots=True)
class HealthNotificationContext:
    readiness_band: str
    training_decision: str
    reasons: tuple[str, ...]


def notification_context(readiness: ReadinessState, adjustment: TrainingAdjustment) -> HealthNotificationContext:
    return HealthNotificationContext(readiness.band.value, adjustment.decision.value, adjustment.reasons[:3])
