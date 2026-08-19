import pytest

from health_models import MetricType
from health_source_policy import SourcePolicy


def test_source_policy_allows_metric_specific_precedence():
    policy = SourcePolicy(overrides={MetricType.WEIGHT_KG: ("withings", "health_connect")})
    assert policy.overrides[MetricType.WEIGHT_KG][0] == "withings"


def test_source_policy_rejects_duplicate_provider_entries():
    with pytest.raises(ValueError, match="duplicate"):
        SourcePolicy(overrides={MetricType.WEIGHT_KG: ("withings", "withings")})
