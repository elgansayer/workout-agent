"""Deterministic source selection and deduplication for normalized health data."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

from health_models import HealthMetric, MetricType


PRECEDENCE_VERSION = 1
DEFAULT_PROVIDER_ORDER = ("garmin", "oura", "fitbit", "withings", "polar", "health_connect")


def metric_fingerprint(metric: HealthMetric) -> str:
    """Return a stable identity without incorporating tenant secrets."""
    upstream_id = metric.provenance.upstream_id
    if upstream_id:
        raw = f"{metric.provenance.provider}|{metric.provenance.connection_id}|{upstream_id}"
    else:
        raw = "|".join(
            (
                metric.provenance.provider,
                metric.provenance.connection_id,
                metric.metric_type.value,
                metric.observed_at.isoformat(),
                format(metric.value, ".12g"),
                metric.unit,
                metric.provenance.data_origin or "",
            )
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deduplicate_metrics(metrics: Iterable[HealthMetric]) -> list[HealthMetric]:
    seen: set[tuple[int, str]] = set()
    result: list[HealthMetric] = []
    for metric in metrics:
        identity = (metric.user_id, metric_fingerprint(metric))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(metric)
    return result


def select_preferred_metric(
    metrics: Iterable[HealthMetric],
    *,
    provider_order: Mapping[MetricType, tuple[str, ...]] | None = None,
) -> HealthMetric | None:
    candidates = list(metrics)
    if not candidates:
        return None
    metric_type = candidates[0].metric_type
    if any(item.metric_type != metric_type for item in candidates):
        raise ValueError("source selection requires one metric type")
    if len({item.user_id for item in candidates}) != 1:
        raise ValueError("source selection cannot cross tenant boundaries")

    order = (provider_order or {}).get(metric_type, DEFAULT_PROVIDER_ORDER)
    ranks = {provider: index for index, provider in enumerate(order)}
    return min(
        candidates,
        key=lambda item: (
            ranks.get(item.provenance.provider, len(ranks)),
            -item.observed_at.timestamp(),
            item.provenance.provider,
            metric_fingerprint(item),
        ),
    )
