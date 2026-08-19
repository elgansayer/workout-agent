from datetime import date, datetime, timedelta, timezone

import pytest

from health_models import (
    DailyHealthSummary,
    HealthMetric,
    MetricType,
    SleepSession,
    SourceProvenance,
    WearableActivity,
)


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
SOURCE = SourceProvenance(provider="garmin", connection_id="c1", upstream_id="r1")


def test_health_metric_requires_tenant_and_unit():
    metric = HealthMetric(1, MetricType.WEIGHT_KG, 80.2, "kg", NOW, SOURCE)
    assert metric.provenance.provider == "garmin"
    with pytest.raises(ValueError):
        HealthMetric(0, MetricType.WEIGHT_KG, 80.2, "kg", NOW, SOURCE)
    with pytest.raises(ValueError):
        HealthMetric(1, MetricType.WEIGHT_KG, 80.2, "", NOW, SOURCE)


def test_sleep_and_activity_validate_time_ranges():
    SleepSession(1, NOW, NOW + timedelta(hours=8), SOURCE)
    WearableActivity(1, "strength", NOW, NOW + timedelta(hours=1), SOURCE)
    with pytest.raises(ValueError):
        SleepSession(1, NOW, NOW, SOURCE)
    with pytest.raises(ValueError):
        WearableActivity(1, "run", NOW, NOW - timedelta(minutes=1), SOURCE)


def test_daily_summary_requires_provenance_for_every_metric():
    summary = DailyHealthSummary(
        user_id=1,
        day=date(2026, 8, 19),
        metrics={MetricType.RESTING_HR_BPM: 58.0},
        selected_sources={MetricType.RESTING_HR_BPM: SOURCE},
    )
    assert summary.precedence_version == 1
    with pytest.raises(ValueError, match="missing provenance"):
        DailyHealthSummary(
            user_id=1,
            day=date(2026, 8, 19),
            metrics={MetricType.STEPS: 9000},
            selected_sources={},
        )
