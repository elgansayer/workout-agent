"""Feature gates derived from provider availability and server configuration."""

from __future__ import annotations

from health_provider_catalog import PROVIDERS, ProviderAvailability


def provider_enabled(provider: str, *, configured: bool) -> bool:
    definition = next((item for item in PROVIDERS if item.provider == provider), None)
    if definition is None:
        return False
    if definition.availability in {ProviderAvailability.REQUIRES_APPROVAL, ProviderAvailability.PLATFORM_VALIDATION}:
        return configured
    if definition.availability is ProviderAvailability.COMPANION_REQUIRED:
        return True
    return configured
