"""Purpose-specific health connector consent representation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class HealthConsent:
    user_id: int
    provider: str
    capabilities: frozenset[str]
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.policy_version.strip():
            raise ValueError("health consent requires tenant, provider and policy version")
