from datetime import datetime, timezone

from health_conflicts import detect_conflict
from health_models import HealthMetric, MetricType, SourceProvenance


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def metric(provider, value):
    return HealthMetric(1, MetricType.WEIGHT_KG, value, "kg", NOW, SourceProvenance(provider, provider + "-c"))


def test_material_provider_disagreement_is_explainable():
    conflict = detect_conflict([metric("withings", 80), metric("health_connect", 90)])
    assert conflict is not None
    assert conflict.providers == ("withings", "health_connect")


def test_small_provider_difference_is_not_a_conflict():
    assert detect_conflict([metric("withings", 80), metric("health_connect", 80.2)]) is None
