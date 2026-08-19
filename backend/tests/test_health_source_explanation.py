from datetime import datetime, timezone

from health_models import HealthMetric, MetricType, SourceProvenance
from health_source_explanation import explain_source


def metric(provider):
    return HealthMetric(1, MetricType.STEPS, 100, "count", datetime(2026, 8, 19, tzinfo=timezone.utc), SourceProvenance(provider, provider + "-c"))


def test_source_explanation_names_selected_and_available_providers():
    selected = metric("garmin")
    text = explain_source(selected, [selected, metric("health_connect")])
    assert "garmin" in text
    assert "health_connect" in text
