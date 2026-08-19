"""Conservative physical bounds for obviously malformed normalized metrics."""

from __future__ import annotations

from health_models import HealthMetric, MetricType


RANGES = {
    MetricType.WEIGHT_KG: (10.0, 500.0),
    MetricType.BODY_FAT_PCT: (0.0, 80.0),
    MetricType.RESTING_HR_BPM: (20.0, 220.0),
    MetricType.HRV_RMSSD_MS: (0.0, 1000.0),
    MetricType.STEPS: (0.0, 250000.0),
    MetricType.SLEEP_DURATION_MIN: (0.0, 1440.0),
    MetricType.SPO2_PCT: (0.0, 100.0),
}


def validate_metric_range(metric: HealthMetric) -> HealthMetric:
    bounds = RANGES.get(metric.metric_type)
    if bounds is not None and not bounds[0] <= metric.value <= bounds[1]:
        raise ValueError(f"{metric.metric_type.value} is outside supported normalization bounds")
    return metric
