from datetime import date, datetime, timezone

import pytest

from health_models import HealthMetric, MetricType, SourceProvenance
from health_summary import build_daily_summary


DAY = date(2026, 8, 19)
WHEN = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def metric(user_id, provider, value):
    return HealthMetric(
        user_id,
        MetricType.WEIGHT_KG,
        value,
        "kg",
        WHEN,
        SourceProvenance(provider, provider + "-connection", upstream_id=provider + "-1"),
    )


def test_summary_uses_precedence_and_preserves_selected_source():
    summary = build_daily_summary(user_id=1, day=DAY, metrics=[metric(1, "health_connect", 80), metric(1, "garmin", 81)])
    assert summary.metrics[MetricType.WEIGHT_KG] == 81
    assert summary.selected_sources[MetricType.WEIGHT_KG].provider == "garmin"


def test_summary_rejects_cross_tenant_input():
    with pytest.raises(ValueError, match="another user"):
        build_daily_summary(user_id=1, day=DAY, metrics=[metric(2, "garmin", 81)])
