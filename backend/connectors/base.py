"""Provider-neutral connector contracts for tenant-safe external integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


class ConnectorState(StrEnum):
    DISCONNECTED = "disconnected"
    PENDING = "pending"
    CONNECTED = "connected"
    ATTENTION = "attention"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    authorize: bool = False
    test: bool = True
    sync: bool = True
    refresh: bool = False
    disconnect: bool = True
    purge: bool = True
    backfill: bool = False
    webhooks: bool = False
    write: bool = False
    metrics: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ConnectorContext:
    user_id: int
    connection_id: str | None = None

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("connector context requires a positive user_id")


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    provider: str
    state: ConnectorState
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    message: str | None = None
    retryable: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SyncResult:
    provider: str
    started_at: datetime
    finished_at: datetime
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    cursor: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls, provider: str) -> "SyncResult":
        now = datetime.now(timezone.utc)
        return cls(provider=provider, started_at=now, finished_at=now)


class ConnectorError(RuntimeError):
    """Base error that providers map external failures into."""

    def __init__(self, message: str, *, code: str = "connector_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class Connector(ABC):
    """Contract implemented by every external connector.

    All operations receive an explicit ConnectorContext. Provider adapters must
    not infer tenant identity from process-global state or credentials.
    """

    provider: str
    capabilities: ConnectorCapabilities

    @abstractmethod
    def status(self, context: ConnectorContext) -> ConnectorStatus:
        raise NotImplementedError

    def authorize(self, context: ConnectorContext, **kwargs: Any) -> Mapping[str, Any]:
        raise ConnectorError("authorization is not supported", code="unsupported")

    @abstractmethod
    def test(self, context: ConnectorContext) -> ConnectorStatus:
        raise NotImplementedError

    @abstractmethod
    def sync(self, context: ConnectorContext, *, cursor: str | None = None) -> SyncResult:
        raise NotImplementedError

    def refresh(self, context: ConnectorContext) -> ConnectorStatus:
        raise ConnectorError("refresh is not supported", code="unsupported")

    @abstractmethod
    def disconnect(self, context: ConnectorContext) -> None:
        raise NotImplementedError

    @abstractmethod
    def purge(self, context: ConnectorContext) -> None:
        raise NotImplementedError
