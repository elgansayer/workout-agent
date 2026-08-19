import pytest

from connectors.garmin import GarminHealthConnector, GarminTrainingConnector
from health_write_guard import require_write_permission


def test_read_only_provider_cannot_write():
    with pytest.raises(ValueError, match="does not support"):
        require_write_permission(GarminHealthConnector(), explicit_user_approval=True)


def test_write_provider_still_requires_explicit_approval():
    with pytest.raises(ValueError, match="explicit"):
        require_write_permission(GarminTrainingConnector(), explicit_user_approval=False)
    require_write_permission(GarminTrainingConnector(), explicit_user_approval=True)
