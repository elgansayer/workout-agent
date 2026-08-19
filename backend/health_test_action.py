"""Simple per-user connector test-action rate limiter."""

from __future__ import annotations

from datetime import datetime, timedelta


class ConnectorTestLimiter:
    def __init__(self, cooldown: timedelta = timedelta(seconds=30)):
        self.cooldown = cooldown
        self._last: dict[tuple[int, str], datetime] = {}

    def allow(self, user_id: int, provider: str, *, now: datetime) -> bool:
        key = (user_id, provider)
        previous = self._last.get(key)
        if previous is not None and now - previous < self.cooldown:
            return False
        self._last[key] = now
        return True
