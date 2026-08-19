"""Audit representation for adaptive training decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from adaptive_training import TrainingAdjustment


@dataclass(frozen=True, slots=True)
class AdaptiveDecisionRecord:
    user_id: int
    decided_at: datetime
    adjustment: TrainingAdjustment
    input_summary_id: str

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.input_summary_id.strip():
            raise ValueError("adaptive decision requires tenant and input summary identity")
