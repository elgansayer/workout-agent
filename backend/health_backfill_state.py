"""Resumable per-connection health backfill state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from health_backfill import BackfillWindow


@dataclass(frozen=True, slots=True)
class BackfillState:
    user_id: int
    connection_id: str
    window: BackfillWindow
    cursor: str | None = None

    def validate(self) -> None:
        if self.user_id <= 0 or not self.connection_id.strip():
            raise ValueError("backfill state requires tenant and connection")
        self.window.validate()
