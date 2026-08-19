"""Provider-neutral scheduling policy for health synchronization."""

from __future__ import annotations

from datetime import datetime, timedelta


def sync_due(last_success_at: datetime | None, *, now: datetime, interval: timedelta = timedelta(hours=6)) -> bool:
    if last_success_at is None:
        return True
    return now - last_success_at >= interval
