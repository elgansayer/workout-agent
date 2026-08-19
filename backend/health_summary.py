"""Canonical daily summary construction from normalized health metrics."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Iterable, Mapping

from health_dedup import PRECEDENCE_VERSION, deduplicate_metrics, select_preferred_metric
from health_models import DailyHealthSummary, HealthMetric, MetricType


def build_daily_summary(
    *,
    user_id: int,
    day: date,
    metrics: Iterable[HealthMetric],
    provider_order: Mapping[MetricType, tuple[str, ...]] | None = None,
) -> DailyHealthSummary:
    grouped: dict[MetricType, list[HealthMetric]] = defaultdict(list)
    for metric in deduplicate_metrics(metrics):
        if metric.user_id != user_id:
            raise ValueError("daily summary cannot include another user's metric")
        if metric.observed_at.date() != day:
            continue
        grouped[metric.metric_type].append(metric)

    values: dict[MetricType, float] = {}
    sources = {}
    for metric_type, candidates in grouped.items():
        selected = select_preferred_metric(candidates, provider_order=provider_order)
        if selected is None:
            continue
        values[metric_type] = selected.value
        sources[metric_type] = selected.provenance

    return DailyHealthSummary(
        user_id=user_id,
        day=day,
        metrics=values,
        selected_sources=sources,
        precedence_version=PRECEDENCE_VERSION,
        computed_at=datetime.now(timezone.utc),
    )
