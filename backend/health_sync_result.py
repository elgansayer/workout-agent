"""Aggregate isolated connector sync outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from health_sync_orchestrator import SyncOutcome


@dataclass(frozen=True, slots=True)
class MultiProviderSyncSummary:
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    retryable: tuple[str, ...]


def summarize_sync(outcomes: list[SyncOutcome]) -> MultiProviderSyncSummary:
    return MultiProviderSyncSummary(
        succeeded=tuple(sorted(item.provider for item in outcomes if item.result is not None)),
        failed=tuple(sorted(item.provider for item in outcomes if item.error_code is not None)),
        retryable=tuple(sorted(item.provider for item in outcomes if item.error_code is not None and item.retryable)),
    )
