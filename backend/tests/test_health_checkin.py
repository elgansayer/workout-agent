import pytest

from health_checkin import RecoveryCheckin


def test_recovery_checkin_uses_bounded_scale():
    RecoveryCheckin(1, energy=4, soreness=2, motivation=5)
    with pytest.raises(ValueError, match="energy"):
        RecoveryCheckin(1, energy=6, soreness=2, motivation=5)
