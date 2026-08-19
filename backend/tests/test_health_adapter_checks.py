from datetime import datetime, timezone

import pytest

from connectors import ConnectorContext
from health_adapter_checks import validate_normalized_records
from health_models import HealthMetric, MetricType, SourceProvenance


def test_adapter_output_must_match_context_tenant():
    metric = HealthMetric(2, MetricType.STEPS, 100, "count", datetime(2026, 8, 19, tzinfo=timezone.utc), SourceProvenance("garmin", "c1"))
    with pytest.raises(ValueError, match="another user"):
        validate_normalized_records(ConnectorContext(1), [metric])
