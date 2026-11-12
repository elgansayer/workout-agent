"""Programme activation service.

The web layer never writes programme rows itself.  Validation, source
reconfirmation and persistence live here so every caller applies the same
rules:

* a preview with blocking violations is never activated;
* a preview whose token, engine version or Hevy source hashes no longer match
  is refused with an actionable conflict report;
* activation is atomic and tenant scoped: it persists an immutable revision
  through :class:`programme_domain.ProgrammeService`, archives the previous
  active programme, materialises the first week state and records the audit
  event and notification in one transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import database
from dynamic_programme import ENGINE_VERSION, validate_programme_preview


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


def _violations_for(preview: dict[str, Any]) -> list[dict[str, Any]]:
    violations = list(preview.get("blocking_violations") or [])
    for violation in validate_programme_preview(preview):
        if violation not in violations:
            violations.append(violation)
    return violations


def _tenant_timezone(user_id: str, db_path: str) -> str:
    try:
        user = database.get_user_by_id(user_id, db_path)
    except Exception:  # noqa: BLE001 - legacy settings may be unreadable
        user = None
    return str((dict(user) if user else {}).get("timezone") or "UTC")


def activate_programme(
    user_id: str,
    preview: dict[str, Any],
    *,
    db_path: str,
    presented_token: str | None = None,
    presented_source_hashes: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify and atomically activate a generated programme for one tenant.

    ``preview`` must be the server-regenerated preview for the tenant's *current*
    Hevy source.  ``presented_token`` / ``presented_source_hashes`` are what the
    client saw; when supplied they must match.  Raises
    :class:`ProgrammeValidationError` (422) for blocking violations and
    :class:`ProgrammeConflictError` (409) for a stale token, engine version or
    source hash.  Nothing is written unless every check passes, and a failure
    inside the transaction leaves the previous active programme untouched.
    """

    from programme_domain import ProgrammeService
    from programme_domain.activation import build_week_state, definition_to_draft
    from programme_domain.models import (
        STATE_CONFIRMED,
        AdaptationDecisionInput,
    )
    from programme_domain.models import (
        ProgrammeValidationError as DomainValidationError,
    )
    from programme_domain.weeks import local_date_of
    from programme_validator import validate_programme

    violations = _violations_for(preview)
    if violations:
        raise ProgrammeValidationError(
            "The programme preview violates blocking constraints.",
            details={"violations": violations},
        )

    current_hashes = [
        {"routine_id": item.get("id"), "source_hash": item.get("source_hash")}
        for item in preview.get("source_snapshots") or []
    ]
    titles = {
        str(item.get("id")): str(item.get("title"))
        for item in preview.get("source_snapshots") or []
    }
    conflict = (
        detect_source_conflicts(
            presented_source_hashes, current_hashes, titles=titles
        )
        if presented_source_hashes
        else {
            "changed_routines": [],
            "added_routines": [],
            "removed_routines": [],
            "has_conflicts": False,
        }
    )
    token = str(preview.get("preview_token") or "")
    engine_changed = preview.get("engine_version") != ENGINE_VERSION
    token_changed = presented_token is not None and token != presented_token
    if engine_changed or token_changed or conflict["has_conflicts"]:
        conflict = {**conflict, "engine_changed": engine_changed}
        raise ProgrammeConflictError(
            "The Hevy routines or programme settings changed after the preview. "
            "Generate a fresh preview before activation.",
            details={"conflict": conflict, "remediation": conflict_remediation(conflict)},
        )

    spec = preview.get("programme_spec") or {}
    timezone_name = _tenant_timezone(user_id, db_path)
    activated_at = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    today = local_date_of(activated_at, timezone_name) or activated_at.date()
    audit = {
        "event": "activation",
        "preview_token": token,
        "engine_version": preview.get("engine_version"),
        "source_hashes": current_hashes,
        "validator": {
            "blocking_violations": 0,
            "warnings": len(preview.get("warnings") or []),
        },
        "timezone": timezone_name,
        "activated_at": activated_at.isoformat(),
        "activated_by": user_id,
    }
    try:
        draft = definition_to_draft(
            preview, user_id, reason="activation", audit=audit
        )
        first_week = build_week_state(
            preview,
            start_date=draft.start_date,
            duration_weeks=draft.duration_weeks,
            week_number=1,
            timezone_name=timezone_name,
            today=today,
        )
    except (DomainValidationError, ValueError) as exc:
        raise ProgrammeValidationError(str(exc)) from exc
    next_exposure = first_week.planned.get("exposures") or []
    event = AdaptationDecisionInput(
        scope={
            "level": "programme",
            "user_id": user_id,
            "revision_number": 1,
            "week_number": 1,
        },
        inputs=audit,
        output={
            "decision_type": "activation",
            "name": draft.name,
            "goal": draft.goal,
            "duration_weeks": draft.duration_weeks,
            "start_date": draft.start_date.isoformat(),
            "cadence": first_week.planned.get("cadence"),
            "first_week": first_week.planned,
            "next_exposure": next_exposure[0] if next_exposure else None,
        },
        confidence=1.0,
        rule_version=str(preview.get("engine_version") or ENGINE_VERSION),
        explanation=(
            "Programme activated by the user from a verified preview; the "
            "previous active programme was archived."
        ),
        confirmed=True,
        decision_type="activation",
        application_state=STATE_CONFIRMED,
        week_number=1,
    )

    def guard(conn: sqlite3.Connection) -> None:
        profile_rules, training_rules = database.load_validation_rules(conn, user_id)
        result = validate_programme(
            preview, profile_rules=profile_rules, training_rules=training_rules
        )
        if result.blocking_violations:
            raise ProgrammeValidationError(
                "The programme preview violates blocking constraints.",
                details={"violations": list(result.blocking_violations)},
            )

    try:
        ProgrammeService(db_path).activate_generated(
            user_id,
            draft,
            guard=guard,
            first_week=first_week,
            event=event,
            notification={
                "type": "programme",
                "title": "Hevy programme activated",
                "message": (
                    f"{preview.get('name', 'Your programme')} is active for "
                    f"{preview.get('cycle_weeks', 0)} weeks using "
                    f"{preview.get('total_days', 0)} selected routines."
                ),
                "link": "/plan",
            },
            legacy_start_date=str(spec.get("start_date") or "") or None,
            template_key=f"hevy:{token[:20]}" if token else "hevy:programme",
        )
    except DomainValidationError as exc:
        raise ProgrammeValidationError(str(exc)) from exc
    return database.get_active_programme(user_id, db_path=db_path) or {}
