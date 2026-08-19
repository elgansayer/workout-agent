"""Shared invariants for provider normalization output."""

from __future__ import annotations

from collections.abc import Iterable

from connectors.base import ConnectorContext
from connectors.provider import NormalizedRecord
from health_normalization import validate_normalized_metric
from health_models import HealthMetric


def validate_normalized_records(context: ConnectorContext, records: Iterable[NormalizedRecord]) -> tuple[NormalizedRecord, ...]:
    result = []
    for record in records:
        if record.user_id != context.user_id:
            raise ValueError("provider adapter emitted a record for another user")
        if isinstance(record, HealthMetric):
            validate_normalized_metric(record)
        result.append(record)
    return tuple(result)
