"""Non-destructive bridge from legacy body metric values to canonical records."""

from __future__ import annotations

from datetime import datetime

from health_models import HealthMetric, MetricType, SourceProvenance


def legacy_body_metrics(
    *,
    user_id: int,
    observed_at: datetime,
    connection_id: str,
    weight_kg: float | None = None,
    body_fat_pct: float | None = None,
) -> tuple[HealthMetric, ...]:
    source = SourceProvenance("legacy_body_metrics", connection_id, normalization_version=1)
    records = []
    if weight_kg is not None:
        records.append(HealthMetric(user_id, MetricType.WEIGHT_KG, weight_kg, "kg", observed_at, source))
    if body_fat_pct is not None:
        records.append(HealthMetric(user_id, MetricType.BODY_FAT_PCT, body_fat_pct, "%", observed_at, source))
    return tuple(records)
