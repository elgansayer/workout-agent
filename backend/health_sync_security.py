"""Ownership checks shared by scheduled and user-triggered health syncs."""

from __future__ import annotations

from health_connection import HealthConnection


def require_connection_owner(connection: HealthConnection, *, user_id: int) -> HealthConnection:
    if connection.user_id != user_id:
        raise ValueError("health connection is owned by another user")
    return connection
