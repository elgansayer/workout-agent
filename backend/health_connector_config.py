"""Configuration boundaries for health connectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderAppConfig:
    provider: str
    client_id: str
    client_secret_ref: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.client_id.strip() or not self.client_secret_ref.strip():
            raise ValueError("provider app configuration is incomplete")


@dataclass(frozen=True, slots=True)
class UserConnectorCredentialRef:
    user_id: int
    provider: str
    credential_id: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.credential_id.strip():
            raise ValueError("user connector credential reference is incomplete")
