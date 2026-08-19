"""Tenant-safe export envelope for canonical health data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class HealthExport:
    user_id: int
    generated_at: datetime
    schema_version: int
    data: Mapping[str, Any]

    def validate(self, *, authenticated_user_id: int) -> None:
        if self.user_id != authenticated_user_id:
            raise ValueError("cannot export another user's health data")
        if self.schema_version < 1:
            raise ValueError("health export schema version must be positive")
