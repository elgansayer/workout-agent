"""Pairing model for authenticated Android Health Connect companions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CompanionDevice:
    user_id: int
    device_id: str
    paired_at: datetime
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def validate_request(self, *, authenticated_user_id: int, device_id: str) -> None:
        if self.user_id != authenticated_user_id or self.device_id != device_id or not self.active:
            raise ValueError("unpaired or revoked Health Connect companion")
