"""Deterministic provider sync simulator for connector contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SimulatedPage:
    records: tuple[Mapping[str, Any], ...]
    next_cursor: str | None = None
    error_code: str | None = None


class SyncSimulator:
    def __init__(self, pages: Mapping[str | None, SimulatedPage]):
        self._pages = dict(pages)
        self.calls: list[str | None] = []

    def fetch(self, cursor: str | None = None) -> SimulatedPage:
        self.calls.append(cursor)
        if cursor not in self._pages:
            raise KeyError(f"no simulated page for cursor {cursor!r}")
        page = self._pages[cursor]
        if page.error_code:
            raise RuntimeError(page.error_code)
        return page
