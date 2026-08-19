"""External provider identity binding."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    user_id: int
    provider: str
    external_user_id: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.external_user_id.strip():
            raise ValueError("provider identity requires tenant and external identity")
