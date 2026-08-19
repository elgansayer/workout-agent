"""Versioned metadata for synthetic provider contract fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from connectors.fixtures import assert_safe_fixture
from health_provider_contracts import contract_version


@dataclass(frozen=True, slots=True)
class ContractFixture:
    provider: str
    payload: Mapping[str, Any]
    contract: str

    @classmethod
    def create(cls, provider: str, payload: Mapping[str, Any]) -> "ContractFixture":
        assert_safe_fixture(payload)
        return cls(provider, payload, contract_version(provider))
