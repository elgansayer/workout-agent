"""Tenant-safe export envelope for canonical health data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from localisation import normalise_timezone, normalise_unit_system


@dataclass(frozen=True, slots=True)
class HealthExport:
    """Source data plus the owner's preferences for reading it.

    ``data`` carries stored values unchanged (UTC instants, kilograms).
    ``preferences`` is the owner's ``user_settings.export_preferences`` block,
    so a consumer renders local days and units the way the owner sees them
    instead of guessing from deployment settings.
    """

    user_id: int
    generated_at: datetime
    schema_version: int
    data: Mapping[str, Any]
    preferences: Mapping[str, Any] = field(default_factory=dict)

    def validate(self, *, authenticated_user_id: int) -> None:
        if self.user_id != authenticated_user_id:
            raise ValueError("cannot export another user's health data")
        if self.schema_version < 1:
            raise ValueError("health export schema version must be positive")
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("health export timestamps must be timezone-aware")
        if self.preferences:
            if normalise_timezone(self.preferences.get("timezone")) is None:
                raise ValueError("health export preferences need a supported time zone")
            if normalise_unit_system(self.preferences.get("unit_system")) is None:
                raise ValueError("health export preferences need a supported unit system")
