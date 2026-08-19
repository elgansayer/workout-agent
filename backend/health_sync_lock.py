"""Per-user/per-connection sync lock identity."""

from __future__ import annotations


def sync_lock_key(user_id: int, provider: str, connection_id: str) -> str:
    if user_id <= 0 or not provider.strip() or not connection_id.strip():
        raise ValueError("sync lock requires tenant, provider and connection")
    return f"health-sync:{user_id}:{provider}:{connection_id}"
