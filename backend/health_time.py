"""Timestamp normalization rules for health providers."""

from __future__ import annotations

from datetime import datetime, timezone


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("health timestamps must be timezone-aware")
    return value


def to_utc(value: datetime) -> datetime:
    return require_aware(value).astimezone(timezone.utc)
