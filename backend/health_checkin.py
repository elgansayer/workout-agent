"""Subjective user check-in input for adaptive training."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoveryCheckin:
    user_id: int
    energy: int
    soreness: int
    motivation: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("check-in requires a positive user_id")
        for name, value in (("energy", self.energy), ("soreness", self.soreness), ("motivation", self.motivation)):
            if not 1 <= value <= 5:
                raise ValueError(f"{name} must be between 1 and 5")
