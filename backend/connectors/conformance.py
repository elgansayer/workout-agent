"""Reusable connector contract checks used by provider test suites."""

from __future__ import annotations

from dataclasses import dataclass

from .base import Connector, ConnectorContext, ConnectorStatus, SyncResult


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    provider: str
    status_ok: bool
    test_ok: bool
    sync_ok: bool

    @property
    def passed(self) -> bool:
        return self.status_ok and self.test_ok and self.sync_ok


def run_conformance(connector: Connector, *, user_id: int = 1) -> ConformanceResult:
    """Run non-destructive lifecycle checks against a connector test double.

    Real provider suites should supply sandbox/fake adapters. Disconnect and
    purge are deliberately excluded here because they are destructive and need
    provider-specific fixture assertions.
    """
    context = ConnectorContext(user_id=user_id, connection_id="conformance")
    status = connector.status(context)
    tested = connector.test(context)
    synced = connector.sync(context)
    return ConformanceResult(
        provider=connector.provider,
        status_ok=isinstance(status, ConnectorStatus) and status.provider == connector.provider,
        test_ok=isinstance(tested, ConnectorStatus) and tested.provider == connector.provider,
        sync_ok=isinstance(synced, SyncResult) and synced.provider == connector.provider,
    )
