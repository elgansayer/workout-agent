"""Stable user-facing health connector attention reasons."""

from __future__ import annotations

from enum import StrEnum


class ConnectionAttention(StrEnum):
    REAUTHENTICATE = "reauthenticate"
    PERMISSION_CHANGED = "permission_changed"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    APPROVAL_REQUIRED = "approval_required"
    COMPANION_OFFLINE = "companion_offline"
