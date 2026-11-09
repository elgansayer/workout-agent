"""Programme activation service.

The web layer never writes programme rows itself.  Validation, source
reconfirmation and persistence live here so every caller applies the same
rules:

* a preview with blocking violations is never activated;
* a preview whose Hevy source hashes no longer match is refused with an
  actionable conflict report;
* activation is atomic and tenant scoped.
"""

from __future__ import annotations

from typing import Any

import database
from dynamic_programme import validate_programme_preview


class ProgrammeActivationError(Exception):
    """Base class for a refused programme activation."""

    status_code = 422

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ProgrammeValidationError(ProgrammeActivationError):
    """The generated programme violates a blocking constraint."""


class ProgrammeConflictError(ProgrammeActivationError):
    """The Hevy source changed after the preview was generated."""

    status_code = 409


def detect_source_conflicts(
    preview_hashes: list[dict[str, Any]],
    current_hashes: list[dict[str, Any]],
    *,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare the previewed source hashes with the current Hevy source."""

    names = titles or {}

    def _index(items: list[dict[str, Any]]) -> dict[str, str]:
        return {
            str(item.get("routine_id")): str(item.get("source_hash") or "")
            for item in items
            if item.get("routine_id")
        }

    preview_by_id = _index(preview_hashes)
    current_by_id = _index(current_hashes)

    changed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for routine_id, current_hash in current_by_id.items():
        previous = preview_by_id.get(routine_id)
        if previous is None:
            added.append(
                {
                    "routine_id": routine_id,
                    "title": names.get(routine_id),
                    "previous_source_hash": None,
                    "current_source_hash": current_hash,
                }
            )
        elif previous != current_hash:
            changed.append(
                {
                    "routine_id": routine_id,
                    "title": names.get(routine_id),
                    "previous_source_hash": previous,
                    "current_source_hash": current_hash,
                }
            )

    for routine_id, previous_hash in preview_by_id.items():
        if routine_id in current_by_id:
            continue
        removed.append(
            {
                "routine_id": routine_id,
                "title": names.get(routine_id),
                "previous_source_hash": previous_hash,
                "current_source_hash": None,
            }
        )

    return {
        "changed_routines": changed,
        "added_routines": added,
        "removed_routines": removed,
        "has_conflicts": bool(changed or added or removed),
    }


def conflict_remediation(conflict: dict[str, Any]) -> list[str]:
    """Return actionable remediation steps for a source conflict."""

    steps = [
        "Refresh from Hevy to load the current routine revisions.",
        "Review the changed routines in the routine library.",
        "Generate a fresh preview and activate that version.",
    ]
    if conflict.get("removed_routines"):
        steps.insert(
            1,
            "A routine used by the preview no longer exists in Hevy; remove it "
            "from the selection or choose a replacement.",
        )
    return steps


def activate_programme(
    user_id: str,
    preview: dict[str, Any],
    *,
    db_path: str,
) -> dict[str, Any]:
    """Validate and activate a generated programme for one tenant.

    Raises :class:`ProgrammeValidationError` when the preview still contains
    blocking violations.
    """

    violations = list(preview.get("blocking_violations") or [])
    for violation in validate_programme_preview(preview):
        if violation not in violations:
            violations.append(violation)
    if violations:
        raise ProgrammeValidationError(
            "The programme preview violates blocking constraints.",
            details={"violations": violations},
        )

    spec = preview.get("programme_spec") or {}
    start_date = str(spec.get("start_date") or "")
    token = str(preview.get("preview_token") or "")
    template_key = f"hevy:{token[:20]}" if token else "hevy:programme"

    database.activate_programme_definition(
        user_id,
        "hevy",
        template_key,
        preview,
        start_date=start_date or None,
        db_path=db_path,
    )
    database.save_notification(
        user_id,
        title="Hevy programme activated",
        message=(
            f"{preview.get('name', 'Your programme')} is active for "
            f"{preview.get('cycle_weeks', 0)} weeks using "
            f"{preview.get('total_days', 0)} selected routines."
        ),
        type="programme",
        link="/plan",
        db_path=db_path,
    )
    return database.get_active_programme(user_id, db_path=db_path)
