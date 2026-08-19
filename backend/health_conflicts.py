"""Explain cross-provider source conflicts without silently averaging values."""

from __future__ import annotations

from dataclasses import dataclass

from health_models import HealthMetric


@dataclass(frozen=True, slots=True)
class MetricConflict:
    metric_type: str
    providers: tuple[str, ...]
    values: tuple[float, ...]


def detect_conflict(metrics: list[HealthMetric], *, relative_tolerance: float = 0.05) -> MetricConflict | None:
    if len(metrics) < 2:
        return None
    if len({m.user_id for m in metrics}) != 1 or len({m.metric_type for m in metrics}) != 1:
        raise ValueError("conflict detection requires one user and metric type")
    values = [m.value for m in metrics]
    scale = max(abs(value) for value in values) or 1.0
    if (max(values) - min(values)) / scale <= relative_tolerance:
        return None
    return MetricConflict(metrics[0].metric_type.value, tuple(m.provenance.provider for m in metrics), tuple(values))
