"""Minimal health context projection for AI coaching."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from adaptive_training import TrainingAdjustment
from health_readiness import ReadinessState


def coaching_health_context(readiness: ReadinessState, adjustment: TrainingAdjustment) -> dict[str, Any]:
    """Expose derived signals, not raw provider health payloads, to the coach."""
    return {
        "readiness": {
            "band": readiness.band.value,
            "confidence": readiness.confidence,
            "reasons": list(readiness.reasons),
            "algorithm_version": readiness.algorithm_version,
        },
        "training_adjustment": {
            "decision": adjustment.decision.value,
            "volume_multiplier": adjustment.volume_multiplier,
            "intensity_multiplier": adjustment.intensity_multiplier,
            "confidence": adjustment.confidence,
            "reasons": list(adjustment.reasons),
            "rule_version": adjustment.rule_version,
        },
    }
