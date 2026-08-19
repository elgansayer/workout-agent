"""Product-facing availability states for health providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProviderAvailability(StrEnum):
    AVAILABLE = "available"
    REQUIRES_APPROVAL = "requires_approval"
    PLATFORM_VALIDATION = "platform_validation"
    COMPANION_REQUIRED = "companion_required"


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider: str
    availability: ProviderAvailability
    note: str


PROVIDERS = (
    ProviderDefinition("garmin", ProviderAvailability.REQUIRES_APPROVAL, "Garmin Developer Program approval is required for production API access."),
    ProviderDefinition("health_connect", ProviderAvailability.COMPANION_REQUIRED, "Health Connect data is read on Android and uploaded by a paired companion."),
    ProviderDefinition("fitbit", ProviderAvailability.PLATFORM_VALIDATION, "Production activation follows the current Fitbit platform decision."),
    ProviderDefinition("oura", ProviderAvailability.AVAILABLE, "Requires a configured Oura OAuth application."),
    ProviderDefinition("polar", ProviderAvailability.AVAILABLE, "Requires a configured Polar OAuth application."),
    ProviderDefinition("withings", ProviderAvailability.AVAILABLE, "Requires a configured Withings application."),
)
