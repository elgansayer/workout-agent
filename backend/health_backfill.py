"""Bounds for user-requested health history backfills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


MAX_BACKFILL_DAYS = 365


@dataclass(frozen=True, slots=True)
class BackfillWindow:
    start: date
    end: date

    def validate(self) -> None:
        if self.end < self.start:
            raise ValueError("backfill end precedes start")
        if self.end - self.start > timedelta(days=MAX_BACKFILL_DAYS):
            raise ValueError("backfill exceeds maximum window")
