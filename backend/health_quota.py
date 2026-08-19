"""Provider quota state for scheduling and UI diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class QuotaState:
    provider: str
    remaining: int | None
    reset_at: datetime | None

    @property
    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0
