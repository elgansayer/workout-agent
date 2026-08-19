from datetime import datetime, timezone

import pytest

from health_models import HealthMetric, MetricType, SourceProvenance
from health_normalization import validate_normalized_metric


SOURCE = SourceProvenance("withings", "c1")
NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def test_normalization_accepts_canonical_unit():
    metric = HealthMetric(1, MetricType.WEIGHT_KG, 80, "kg", NOW, SOURCE)
    assert validate_normalized_metric(metric) is metric


def test_normalization_rejects_provider_specific_unit_leakage():
    metric = HealthMetric(1, MetricType.WEIGHT_KG, 176, "lb", NOW, SOURCE)
    with pytest.raises(ValueError, match="canonical unit"):
        validate_normalized_metric(metric)
