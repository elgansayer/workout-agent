"""Safe user-facing connector status projections."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .base import Connector, ConnectorContext


SENSITIVE_KEYS = frozenset({"token", "access_token", "refresh_token", "api_key", "client_secret", "authorization", "cookie"})


def _safe_metadata(metadata: dict[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        metadata = dict(metadata)
    return {key: value for key, value in metadata.items() if key.lower() not in SENSITIVE_KEYS}


def public_connector_status(connector: Connector, context: ConnectorContext) -> dict[str, Any]:
    status = connector.status(context)
    return {
        "provider": connector.provider,
        "state": status.state.value,
        "last_success_at": status.last_success_at.isoformat() if status.last_success_at else None,
        "last_attempt_at": status.last_attempt_at.isoformat() if status.last_attempt_at else None,
        "message": status.message,
        "retryable": status.retryable,
        "capabilities": asdict(connector.capabilities),
        "metadata": _safe_metadata(dict(status.metadata)),
    }
