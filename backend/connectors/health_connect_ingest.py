"""Validation boundary for Android Health Connect companion uploads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


MAX_CLOCK_SKEW = timedelta(minutes=10)
MAX_BATCH_RECORDS = 5000


@dataclass(frozen=True, slots=True)
class HealthConnectUpload:
    user_id: int
    device_id: str
    batch_id: str
    generated_at: datetime
    record_count: int

    def validate(self, *, authenticated_user_id: int, now: datetime | None = None) -> None:
        if self.user_id != authenticated_user_id:
            raise ValueError("Health Connect upload tenant mismatch")
        if not self.device_id.strip() or not self.batch_id.strip():
            raise ValueError("device_id and batch_id are required")
        if not 0 <= self.record_count <= MAX_BATCH_RECORDS:
            raise ValueError("Health Connect batch is outside allowed bounds")
        now = now or datetime.now(timezone.utc)
        generated_at = self.generated_at
        if generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if generated_at > now + MAX_CLOCK_SKEW:
            raise ValueError("Health Connect batch timestamp is too far in the future")


def replay_key(upload: HealthConnectUpload) -> str:
    """Stable key suitable for a server-side uniqueness constraint/cache."""
    return f"{upload.user_id}:{upload.device_id}:{upload.batch_id}"
