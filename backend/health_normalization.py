"""Validation helpers for provider normalization adapters."""

from __future__ import annotations

from health_models import HealthMetric
from health_units import canonical_unit


def validate_normalized_metric(metric: HealthMetric) -> HealthMetric:
    expected = canonical_unit(metric.metric_type)
    if metric.unit != expected:
        raise ValueError(f"{metric.metric_type.value} must use canonical unit {expected!r}, got {metric.unit!r}")
    return metric
