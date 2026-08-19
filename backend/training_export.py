"""Provider-neutral validation for exporting structured workouts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExportedWorkout:
    user_id: int
    provider: str
    name: str
    source_programme_id: str
    explicit_user_approval: bool

    def validate(self, *, authenticated_user_id: int) -> None:
        if self.user_id != authenticated_user_id:
            raise ValueError("cannot export a workout to another user's provider")
        if not self.explicit_user_approval:
            raise ValueError("workout export requires explicit user approval")
        if not self.provider.strip() or not self.name.strip() or not self.source_programme_id.strip():
            raise ValueError("workout export identity is incomplete")
