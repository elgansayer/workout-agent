from datetime import datetime, timezone

import pytest

from health_ingestion import HealthIngestionBatch
from health_models import HealthMetric, MetricType, SourceProvenance


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
SOURCE = SourceProvenance("garmin", "connection", upstream_id="same")


def make_metric(user_id):
    return HealthMetric(user_id, MetricType.STEPS, 1000, "count", NOW, SOURCE)


def test_batch_rejects_cross_user_records():
    batch = HealthIngestionBatch(user_id=1)
    with pytest.raises(ValueError, match="another user"):
        batch.add(make_metric(2))


def test_batch_deduplicates_metrics_within_user():
    batch = HealthIngestionBatch(user_id=1)
    batch.extend([make_metric(1), make_metric(1)])
    assert batch.normalized() == [make_metric(1)]
