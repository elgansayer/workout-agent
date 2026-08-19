import pytest

from health_provider_identity import ProviderIdentity


def test_provider_identity_is_tenant_bound():
    identity = ProviderIdentity(1, "fitbit", "external-123")
    assert identity.external_user_id == "external-123"
    with pytest.raises(ValueError):
        ProviderIdentity(0, "fitbit", "external-123")
