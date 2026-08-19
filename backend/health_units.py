"""Canonical health metric units and basic validation."""

from __future__ import annotations

from health_models import MetricType


CANONICAL_UNITS = {
    MetricType.WEIGHT_KG: "kg",
    MetricType.BODY_FAT_PCT: "%",
    MetricType.RESTING_HR_BPM: "bpm",
    MetricType.HRV_RMSSD_MS: "ms",
    MetricType.STEPS: "count",
    MetricType.SLEEP_DURATION_MIN: "min",
    MetricType.SPO2_PCT: "%",
    MetricType.RESPIRATION_BPM: "breaths/min",
    MetricType.STRESS_SCORE: "score",
    MetricType.RECOVERY_SCORE: "score",
}


def canonical_unit(metric_type: MetricType) -> str:
    return CANONICAL_UNITS[metric_type]
