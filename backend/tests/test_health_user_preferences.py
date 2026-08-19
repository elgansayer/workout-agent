import pytest

from health_models import MetricType
from health_user_preferences import HealthSourcePreference


def test_source_preferences_are_per_user_and_ordered():
    preference = HealthSourcePreference(1, MetricType.WEIGHT_KG, ("withings", "health_connect"))
    assert preference.providers[0] == "withings"
    with pytest.raises(ValueError, match="unique"):
        HealthSourcePreference(1, MetricType.WEIGHT_KG, ("withings", "withings"))
