from health_provider_catalog import PROVIDERS, ProviderAvailability


def test_provider_catalog_exposes_real_product_gates():
    providers = {item.provider: item for item in PROVIDERS}
    assert providers["garmin"].availability == ProviderAvailability.REQUIRES_APPROVAL
    assert providers["health_connect"].availability == ProviderAvailability.COMPANION_REQUIRED
    assert providers["fitbit"].availability == ProviderAvailability.PLATFORM_VALIDATION
