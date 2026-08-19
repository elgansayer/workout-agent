"""Explicit disconnect/purge intent for health connections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DisconnectMode(StrEnum):
    REVOKE_ONLY = "revoke_only"
    REVOKE_AND_PURGE = "revoke_and_purge"


@dataclass(frozen=True, slots=True)
class DisconnectRequest:
    user_id: int
    connection_id: str
    mode: DisconnectMode
    confirmed: bool = False

    def validate(self, *, authenticated_user_id: int) -> None:
        if self.user_id != authenticated_user_id:
            raise ValueError("cannot disconnect another user's health connection")
        if not self.connection_id.strip():
            raise ValueError("connection_id is required")
        if self.mode is DisconnectMode.REVOKE_AND_PURGE and not self.confirmed:
            raise ValueError("health-data purge requires explicit confirmation")
