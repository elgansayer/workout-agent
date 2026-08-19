"""Freshness classification for user-facing connector and recovery state."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class Freshness(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    NEVER = "never"


def classify_freshness(last_success_at: datetime | None, *, now: datetime, fresh_for: timedelta = timedelta(hours=24), stale_after: timedelta = timedelta(hours=72)) -> Freshness:
    if last_success_at is None:
        return Freshness.NEVER
    age = now - last_success_at
    if age <= fresh_for:
        return Freshness.FRESH
    if age >= stale_after:
        return Freshness.STALE
    return Freshness.AGING
