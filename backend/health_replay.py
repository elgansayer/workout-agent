"""Replay protection abstraction for client-pushed health batches."""

from __future__ import annotations


class ReplayStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, key: str) -> bool:
        if not key.strip():
            raise ValueError("replay key is required")
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
