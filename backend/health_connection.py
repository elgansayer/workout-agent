"""Canonical per-user health connection lifecycle model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HealthConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    PENDING = "pending"
    CONNECTED = "connected"
    ATTENTION = "attention"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class HealthConnection:
    id: str
    user_id: int
    provider: str
    state: HealthConnectionState
    granted_capabilities: frozenset[str] = frozenset()
    last_sync_at: datetime | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip() or self.user_id <= 0 or not self.provider.strip():
            raise ValueError("health connection requires id, positive user_id and provider")
