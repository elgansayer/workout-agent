import pytest

from health_retention import DisconnectMode, DisconnectRequest


def test_disconnect_is_tenant_bound():
    request = DisconnectRequest(1, "c1", DisconnectMode.REVOKE_ONLY)
    request.validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        request.validate(authenticated_user_id=2)


def test_purge_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="confirmation"):
        DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE).validate(authenticated_user_id=1)
    DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE, confirmed=True).validate(authenticated_user_id=1)
