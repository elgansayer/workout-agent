"""Registry for provider-neutral connectors."""

from __future__ import annotations

from collections.abc import Iterable

from .base import Connector


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[Connector] = ()) -> None:
        self._connectors: dict[str, Connector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: Connector) -> None:
        provider = connector.provider.strip().lower()
        if not provider:
            raise ValueError("connector provider must not be empty")
        if provider in self._connectors:
            raise ValueError(f"connector already registered: {provider}")
        self._connectors[provider] = connector

    def get(self, provider: str) -> Connector:
        key = provider.strip().lower()
        try:
            return self._connectors[key]
        except KeyError as exc:
            raise KeyError(f"unknown connector: {provider}") from exc

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

    def all(self) -> tuple[Connector, ...]:
        return tuple(self._connectors[key] for key in self.providers())


registry = ConnectorRegistry()
