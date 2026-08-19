"""Failure-isolated health connector synchronization orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from connectors.base import Connector, ConnectorContext, ConnectorError, SyncResult


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    provider: str
    result: SyncResult | None
    error_code: str | None
    retryable: bool = False


def run_connector_sync(connector: Connector, context: ConnectorContext, *, cursor: str | None = None) -> SyncOutcome:
    try:
        return SyncOutcome(connector.provider, connector.sync(context, cursor=cursor), None)
    except ConnectorError as exc:
        return SyncOutcome(connector.provider, None, exc.code, exc.retryable)
