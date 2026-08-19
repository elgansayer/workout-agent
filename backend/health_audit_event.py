"""Privacy-safe health connector audit event."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class HealthAuditEvent:
    user_id: int
    provider: str
    action: str
    occurred_at: datetime
    outcome: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.action.strip() or not self.outcome.strip():
            raise ValueError("health audit event identity is incomplete")
