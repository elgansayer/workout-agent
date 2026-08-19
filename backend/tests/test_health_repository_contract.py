from typing import get_type_hints

from health_repository import HealthRepository


def test_health_repository_exposes_tenant_scoped_write_contract():
    assert hasattr(HealthRepository, "save_metrics")
    assert hasattr(HealthRepository, "save_sleep")
    assert hasattr(HealthRepository, "save_activities")
    assert hasattr(HealthRepository, "save_sync_run")
