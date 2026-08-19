from health_models import MetricType
from health_units import canonical_unit


def test_canonical_units_are_application_owned():
    assert canonical_unit(MetricType.WEIGHT_KG) == "kg"
    assert canonical_unit(MetricType.HRV_RMSSD_MS) == "ms"
    assert canonical_unit(MetricType.STEPS) == "count"
