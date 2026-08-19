"""Vendor score representation that never masquerades as a universal score."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProviderScore:
    user_id: int
    provider: str
    score_name: str
    value: float
    observed_at: datetime
    connection_id: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.score_name.strip() or not self.connection_id.strip():
            raise ValueError("provider score requires tenant and source identity")
