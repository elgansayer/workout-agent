"""Guard provider write operations behind explicit capabilities and approval."""

from __future__ import annotations

from connectors.base import Connector


def require_write_permission(connector: Connector, *, explicit_user_approval: bool) -> None:
    if not connector.capabilities.write:
        raise ValueError(f"{connector.provider} does not support write operations")
    if not explicit_user_approval:
        raise ValueError("provider write operation requires explicit user approval")
