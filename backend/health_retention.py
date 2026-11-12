"""Explicit disconnect/purge intent for health connections.

Disconnecting a connector always revokes upstream access where the provider
supports it. The user separately decides what happens to data that was already
imported. The three canonical choices are deliberately explicit:

* ``revoke_only`` - cut the provider credential and stop syncing. Nothing
  stored in Workout Agent is removed.
* ``retain`` - disconnect the connector but keep imported records and the
  insights derived from them as part of the user's history.
* ``purge`` - permanently remove the records imported by this connector and
  invalidate the insights/aggregates derived from them. Records the user
  entered elsewhere (workouts, check-ins, notes) are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DisconnectMode(StrEnum):
    """What happens to imported and derived data when a connector is removed."""

    REVOKE_ONLY = "revoke_only"
    RETAIN = "retain"
    PURGE = "purge"
    # Compatibility alias for the original two-state contract. Treat as purge.
    REVOKE_AND_PURGE = "revoke_and_purge"

    @property
    def purges_imported_data(self) -> bool:
        return self in {DisconnectMode.PURGE, DisconnectMode.REVOKE_AND_PURGE}


@dataclass(frozen=True, slots=True)
class RetentionOption:
    """One user-selectable retention choice with a product explanation."""

    mode: DisconnectMode
    label: str
    explanation: str
    deletes_imported_data: bool
    deletes_derived_data: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "label": self.label,
            "explanation": self.explanation,
            "deletes_imported_data": self.deletes_imported_data,
            "deletes_derived_data": self.deletes_derived_data,
        }


RETENTION_OPTIONS: tuple[RetentionOption, ...] = (
    RetentionOption(
        mode=DisconnectMode.REVOKE_ONLY,
        label="Revoke access only",
        explanation=(
            "Revoke this connector's credential and stop future syncs. Your "
            "imported records and derived insights remain available."
        ),
        deletes_imported_data=False,
        deletes_derived_data=False,
    ),
    RetentionOption(
        mode=DisconnectMode.RETAIN,
        label="Disconnect and retain imported data",
        explanation=(
            "Disconnect this connector but keep the records it imported and the "
            "insights derived from them in your history."
        ),
        deletes_imported_data=False,
        deletes_derived_data=False,
    ),
    RetentionOption(
        mode=DisconnectMode.PURGE,
        label="Disconnect and purge imported data",
        explanation=(
            "Disconnect this connector and permanently delete the records it "
            "imported plus insights and aggregates derived from them. Workouts, "
            "check-ins and other records you entered yourself are kept."
        ),
        deletes_imported_data=True,
        deletes_derived_data=True,
    ),
)

RETENTION_EXPLANATIONS: dict[str, str] = {
    option.mode.value: option.explanation for option in RETENTION_OPTIONS
}


def retention_options() -> tuple[RetentionOption, ...]:
    """Return the ordered retention choices offered to every connector."""

    return RETENTION_OPTIONS


def retention_options_payload() -> list[dict[str, object]]:
    """Serialisable retention choices for the settings/API layer."""

    return [option.as_dict() for option in RETENTION_OPTIONS]


def parse_mode(value: str | DisconnectMode | None) -> DisconnectMode:
    """Parse a user-supplied retention mode, failing closed on junk."""

    if isinstance(value, DisconnectMode):
        return value
    try:
        return DisconnectMode((value or "").strip().lower())
    except ValueError as exc:
        allowed = ", ".join(option.mode.value for option in RETENTION_OPTIONS)
        raise ValueError(f"unknown retention mode; use one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class DisconnectRequest:
    user_id: int | str
    connection_id: str
    mode: DisconnectMode
    confirmed: bool = False

    @property
    def effective_mode(self) -> DisconnectMode:
        """Collapse the compatibility alias onto the canonical purge mode."""

        return DisconnectMode.PURGE if self.mode.purges_imported_data else self.mode

    def validate(self, *, authenticated_user_id: int | str) -> None:
        if str(self.user_id) != str(authenticated_user_id):
            raise ValueError("cannot disconnect another user's health connection")
        if not str(self.connection_id).strip():
            raise ValueError("connection_id is required")
        if self.mode.purges_imported_data and not self.confirmed:
            raise ValueError("health-data purge requires explicit confirmation")
