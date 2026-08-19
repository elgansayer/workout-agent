from datetime import datetime, timezone

import pytest

from health_metric_validation import validate_metric_range
from health_models import HealthMetric, MetricType, SourceProvenance


def test_metric_range_rejects_impossible_percentage():
    metric = HealthMetric(1, MetricType.SPO2_PCT, 150, "%", datetime(2026, 8, 19, tzinfo=timezone.utc), SourceProvenance("oura", "c1"))
    with pytest.raises(ValueError, match="bounds"):
        validate_metric_range(metric)
