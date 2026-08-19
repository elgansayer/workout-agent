"""Shared connector operation limits for retries and test actions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_delay_seconds <= 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("invalid retry policy")

    def delay(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt numbers start at one")
        return min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
