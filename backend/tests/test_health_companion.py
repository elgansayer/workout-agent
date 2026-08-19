from datetime import datetime, timezone

import pytest

from health_companion import CompanionDevice


def test_companion_request_is_user_and_device_bound():
    device = CompanionDevice(1, "pixel-10", datetime(2026, 8, 19, tzinfo=timezone.utc))
    device.validate_request(authenticated_user_id=1, device_id="pixel-10")
    with pytest.raises(ValueError, match="unpaired"):
        device.validate_request(authenticated_user_id=2, device_id="pixel-10")
