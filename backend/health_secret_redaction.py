"""Recursive redaction for health connector diagnostics."""

from __future__ import annotations

from typing import Any


SECRET_KEYS = {"authorization", "access_token", "refresh_token", "api_key", "client_secret", "cookie"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if str(key).lower() in SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
