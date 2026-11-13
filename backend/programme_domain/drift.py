"""Hevy routine drift: an exact provider-versus-programme diff and reconciliation.

The active snapshot is never touched by detection.  A user decision (keep,
compare/merge, rebase or replace) is the only way a provider change reaches the
programme, and it always appends a new, validated, immutable revision.  Nothing
here talks to Hevy: original routines are read-only.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import adaptation as engine
from .activation import definition_from_revision, definition_to_draft
from .models import (
    STATE_ACKNOWLEDGED,
    STATE_CONFIRMED,
    AdaptationDecisionInput,
    Programme,
    ProgrammeNotFoundError,
    ProgrammeValidationError,
)
from .repository import ProgrammeRepository
from .weeks import local_date_of, week_for_date

ACTIONS = ("keep", "merge", "rebase", "replace")
_TARGET_FIELDS = (
    "set_targets",
    "rest_seconds",
    "notes",
    "superset_id",
    "target_weight_kg",
    "target_reps",
    "target_rep_range",
)


class DriftConflictError(ProgrammeValidationError):
    """The provider routine changed again after the diff the user reviewed."""


def source_hash(payload: Mapping[str, Any]) -> str:
    """Canonical hash of a routine payload (mirrors ``dynamic_programme``)."""

    body = {
        "id": payload.get("id"),
        "title": payload.get("title"),
        "folder_id": payload.get("folder_id"),
        "updated_at": payload.get("updated_at"),
        "exercises": payload.get("exercises") or [],
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _keyed(exercises: Sequence[Mapping[str, Any]]) -> list[tuple[str, Mapping[str, Any]]]:
    seen: dict[str, int] = {}
    keyed: list[tuple[str, Mapping[str, Any]]] = []
    for exercise in exercises:
        template = str(exercise.get("template_id") or "")
        seen[template] = seen.get(template, 0) + 1
        keyed.append((f"{template}#{seen[template]}", exercise))
    return keyed


def _supported(exercise: Mapping[str, Any]) -> bool:
    return not exercise.get("unsupported_fields") and str(
        exercise.get("exercise_type") or "unknown"
    ).lower() not in {"", "unknown"}


def _brief(exercise: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if exercise is None:
        return None
    return {
        "template_id": exercise.get("template_id"),
        "title": exercise.get("title"),
        "sets": exercise.get("sets"),
        "rest_seconds": exercise.get("rest_seconds"),
        "set_targets": exercise.get("set_targets"),
        "unsupported_fields": exercise.get("unsupported_fields") or [],
    }


def diff_routine(
    snapshot: Mapping[str, Any], live: Mapping[str, Any] | None, position: int
) -> dict[str, Any]:
    """The exact difference between the programme's snapshot and the provider."""

    routine_id = str(snapshot.get("id"))
    base = {
        "routine_id": routine_id,
        "position": position,
        "title": snapshot.get("title"),
        "source_hash": snapshot.get("source_hash"),
        "current_title": live.get("title") if live else None,
        "current_hash": live.get("source_hash") if live else None,
    }
    if live is None:
        return {
            **base,
            "state": "deleted",
            "changes": [],
            "available_actions": ["keep", "replace"],
        }
    old = _keyed(snapshot.get("exercises") or [])
    new = _keyed(live.get("exercises") or [])
    old_map, new_map = dict(old), dict(new)
    changes: list[dict[str, Any]] = []
    if snapshot.get("title") != live.get("title"):
        changes.append(
            {
                "id": "routine_renamed:title",
                "kind": "routine_renamed",
                "key": "title",
                "template_id": "",
                "before": snapshot.get("title"),
                "after": live.get("title"),
                "supported": True,
            }
        )

    def add(kind: str, key: str, before: Any, after: Any, *, supported: bool = True) -> None:
        changes.append(
            {
                "id": f"{kind}:{key}",
                "kind": kind,
                "key": key,
                "template_id": key.split("#")[0],
                "before": before,
                "after": after,
                "supported": supported,
            }
        )

    for key, exercise in old:
        if key not in new_map:
            add("removed", key, _brief(exercise), None)
    for key, exercise in new:
        if key not in old_map:
            add("added", key, None, _brief(exercise), supported=_supported(exercise))
    for key, exercise in old:
        other = new_map.get(key)
        if other is None:
            continue
        if exercise.get("title") != other.get("title"):
            add("renamed", key, exercise.get("title"), other.get("title"))
        if exercise.get("sets") != other.get("sets"):
            add("sets_changed", key, exercise.get("sets"), other.get("sets"), supported=_supported(other))
        elif any(exercise.get(field) != other.get(field) for field in _TARGET_FIELDS):
            add("targets_changed", key, _brief(exercise), _brief(other), supported=_supported(other))
    common_old = [key for key, _ in old if key in new_map]
    common_new = [key for key, _ in new if key in old_map]
    if common_old != common_new:
        add("reordered", "order", common_old, common_new)

    kinds = {item["kind"] for item in changes}
    if not changes:
        state = "unchanged"
    elif kinds == {"routine_renamed"}:
        state = "renamed"
    elif kinds <= {"routine_renamed", "renamed", "reordered"}:
        state = "compatible_edit"
    else:
        state = "structural_edit"
    return {
        **base,
        "state": state,
        "changes": changes,
        "available_actions": [] if state == "unchanged" else list(ACTIONS),
    }


def diff_programme(
    definition: Mapping[str, Any], live_source: Mapping[str, Any]
) -> dict[str, Any]:
    """Diff every selected routine; the definition is never modified."""

    live_by_id = {
        str(item.get("id")): item
        for item in live_source.get("routines") or []
        if isinstance(item, Mapping)
    }
    routines = []
    for position, snapshot in enumerate(definition.get("source_snapshots") or [], start=1):
        routines.append(
            diff_routine(snapshot, live_by_id.get(str(snapshot.get("id"))), position)
        )
    changed = [item for item in routines if item["state"] != "unchanged"]
    return {
        "has_drift": bool(changed),
        "status": (
            "unchanged"
            if not changed
            else "action_required"
            if any(item["state"] in {"structural_edit", "deleted"} for item in changed)
            else "review_recommended"
        ),
        "routines": routines,
        "summary": {
            state: sum(1 for item in routines if item["state"] == state)
            for state in ("renamed", "compatible_edit", "structural_edit", "deleted")
        },
        "writes_to_hevy": False,
    }


@dataclass(frozen=True)
class Resolution:
    """A user's decision about one drifted routine."""

    routine_id: str
    action: str
    accept: tuple[str, ...] = ()
    replacement_routine_id: str | None = None
    expected_hash: str | None = None
    acknowledge_unsupported: bool = False


# ---------------------------------------------------------------------------
# Building the reconciled definition
# ---------------------------------------------------------------------------


def _estimate_minutes(exercises: Sequence[Mapping[str, Any]]) -> int:
    work_sets = sum(int(item.get("sets") or 0) for item in exercises)
    rest = sum(
        max(0, int(item.get("rest_seconds") or 0)) * max(1, int(item.get("sets") or 0))
        for item in exercises
    )
    return max(10, round((8 * 60 + rest + work_sets * 45) / 60))


def _new_exercise_entry(
    source: Mapping[str, Any],
    position: int,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    """Generate a conservative, fully validated entry for a provider exercise."""

    from dynamic_programme import _prescriptions_for_exercise

    spec = definition.get("programme_spec") or {}
    role, reason, confidence, evidence, prescriptions = _prescriptions_for_exercise(
        dict(source),
        position,
        str(spec.get("goal") or "general_fitness"),
        list(definition.get("blocks") or []),
        priority_muscles=tuple(spec.get("priority_muscles") or ()),
        priority_exercises=tuple(spec.get("priority_exercises") or ()),
        experience=str(spec.get("experience") or "intermediate"),
        strength_model=str(spec.get("strength_model") or "auto"),
        aggressiveness=str(spec.get("adaptation_aggressiveness") or "balanced"),
        adaptation_bounds={"minimum": 0, "maximum": 0},
    )
    source_sets = max(1, int(source.get("sets") or 1))
    for item in prescriptions:
        # No history exists for an adopted exercise: start at the provider's sets.
        item["sets"] = source_sets
        item["confidence"] = confidence if confidence in {"low", "medium"} else "medium"
    opening = prescriptions[0]
    return {
        "name": source.get("title"),
        "template_id": source.get("template_id"),
        "exercise_type": source.get("exercise_type"),
        "equipment": source.get("equipment"),
        "role": role,
        "role_reason": reason,
        "role_confidence": confidence,
        "role_evidence": evidence,
        "role_override": False,
        "sets": opening["sets"],
        "rep_range": opening["rep_range"],
        "scheme": f"{opening['sets']} × {opening['rep_range']}",
        "note": source.get("notes") or "",
        "rest_seconds": source.get("rest_seconds"),
        "source_sets": source_sets,
        "source_targets": source.get("set_targets") or [],
        "unsupported_fields": source.get("unsupported_fields") or [],
        "prescriptions": prescriptions,
        "baseline_confidence": None,
        "baseline": None,
        "role_features": None,
    }


def _merge_day(
    definition: dict[str, Any],
    day: dict[str, Any],
    snapshot_index: int,
    snapshot: Mapping[str, Any],
    live: Mapping[str, Any],
    diff: Mapping[str, Any],
    accepted: set[str],
) -> None:
    """Apply the accepted changes of one routine to its day and snapshot."""

    old = _keyed(snapshot.get("exercises") or [])
    old_map = dict(old)
    new = _keyed(live.get("exercises") or [])
    new_map = dict(new)
    entries = dict(zip([key for key, _ in old], day.get("exercises") or [], strict=False))
    all_ids = {item["id"] for item in diff["changes"]}

    # Resulting order: provider order only when the reorder is accepted.
    if "reordered:order" in accepted:
        ordered = [key for key, _ in new if key in old_map or f"added:{key}" in accepted]
        ordered += [
            key for key, _ in old if key not in new_map and f"removed:{key}" not in accepted
        ]
    else:
        ordered = [
            key
            for key, _ in old
            if key in new_map or f"removed:{key}" not in accepted
        ]
        for index, (key, _exercise) in enumerate(new):
            if key in old_map or f"added:{key}" not in accepted:
                continue
            anchor = next((k for k, _ in reversed(new[:index]) if k in ordered), None)
            ordered.insert(ordered.index(anchor) + 1 if anchor else 0, key)

    exercises: list[dict[str, Any]] = []
    merged_sources: list[dict[str, Any]] = []
    for position, key in enumerate(ordered):
        old_source = old_map.get(key)
        new_source = new_map.get(key)
        if old_source is None:
            if new_source is None:  # pragma: no cover - ordered only holds known keys
                continue
            merged_sources.append(copy.deepcopy(dict(new_source)))
            exercises.append(_new_exercise_entry(new_source, position, definition))
            continue
        source_copy = copy.deepcopy(dict(old_source))
        entry = copy.deepcopy(entries.get(key) or {})
        if new_source is not None:
            if f"renamed:{key}" in accepted:
                source_copy["title"] = new_source.get("title")
                entry["name"] = new_source.get("title")
            if f"sets_changed:{key}" in accepted:
                source_copy = copy.deepcopy(dict(new_source))
                entry = _new_exercise_entry(new_source, position, definition)
            elif f"targets_changed:{key}" in accepted:
                for field_name in _TARGET_FIELDS:
                    source_copy[field_name] = copy.deepcopy(new_source.get(field_name))
                source_copy["unsupported_fields"] = new_source.get("unsupported_fields") or []
                entry["rest_seconds"] = new_source.get("rest_seconds")
                entry["source_targets"] = new_source.get("set_targets") or []
                entry["note"] = new_source.get("notes") or ""
                entry["unsupported_fields"] = new_source.get("unsupported_fields") or []
        merged_sources.append(source_copy)
        exercises.append(entry)

    if accepted >= all_ids:
        new_snapshot = copy.deepcopy(dict(live))
    else:
        new_snapshot = copy.deepcopy(dict(snapshot))
        if "routine_renamed:title" in accepted:
            new_snapshot["title"] = live.get("title")
        new_snapshot["exercises"] = merged_sources
        new_snapshot["exercise_count"] = len(merged_sources)
        new_snapshot["work_set_count"] = sum(int(item.get("sets") or 0) for item in merged_sources)
        new_snapshot["estimated_duration_minutes"] = _estimate_minutes(merged_sources)
        new_snapshot["source_hash"] = source_hash(new_snapshot)
    day["exercises"] = exercises
    day["focus"] = new_snapshot.get("title") or day.get("focus")
    day["source_hash"] = new_snapshot.get("source_hash")
    day["estimated_duration_minutes"] = new_snapshot.get("estimated_duration_minutes")
    definition["source_snapshots"][snapshot_index] = new_snapshot


def _replace_day(
    definition: dict[str, Any],
    day: dict[str, Any],
    snapshot_index: int,
    old_id: str,
    live: Mapping[str, Any],
) -> None:
    exercises = [
        _new_exercise_entry(exercise, position, definition)
        for position, exercise in enumerate(live.get("exercises") or [])
    ]
    day["routine_id"] = live.get("id")
    day["source_hash"] = live.get("source_hash")
    day["focus"] = live.get("title")
    day["muscles"] = live.get("muscle_summary") or []
    day["estimated_duration_minutes"] = live.get("estimated_duration_minutes")
    day["exercises"] = exercises
    definition["source_snapshots"][snapshot_index] = copy.deepcopy(dict(live))
    cadence = definition.get("cadence")
    if isinstance(cadence, dict):
        for entry in cadence.get("rotation") or []:
            if entry.get("routine_id") == old_id:
                entry["routine_id"] = live.get("id")
                entry["title"] = live.get("title")
    for entry in definition.get("schedule") or []:
        if entry.get("routine_id") == old_id:
            entry["routine_id"] = live.get("id")
            entry["routine_title"] = live.get("title")
    spec = dict(definition.get("programme_spec") or {})
    spec["selected_routine_ids"] = [
        live.get("id") if item == old_id else item
        for item in spec.get("selected_routine_ids") or []
    ]
    definition["programme_spec"] = spec


def reconcile_definition(
    definition: Mapping[str, Any],
    live_source: Mapping[str, Any],
    resolutions: Sequence[Resolution],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the reconciled definition copy and a per-routine report."""

    result = copy.deepcopy(dict(definition))
    live_by_id = {
        str(item.get("id")): item
        for item in live_source.get("routines") or []
        if isinstance(item, Mapping)
    }
    snapshots = result.get("source_snapshots") or []
    index_by_id = {str(item.get("id")): index for index, item in enumerate(snapshots)}
    report: list[dict[str, Any]] = []
    used = {str(item) for item in index_by_id}
    for resolution in resolutions:
        if resolution.action not in ACTIONS:
            raise ProgrammeValidationError(f"Unsupported drift action: {resolution.action}")
        index = index_by_id.get(resolution.routine_id)
        if index is None:
            raise ProgrammeValidationError("The routine is not part of this programme.")
        snapshot = copy.deepcopy(snapshots[index])
        live = live_by_id.get(resolution.routine_id)
        if (
            resolution.expected_hash is not None
            and (live.get("source_hash") if live else None) != resolution.expected_hash
        ):
            raise DriftConflictError(
                "The Hevy routine changed again after you reviewed it; review the new diff."
            )
        diff = diff_routine(snapshot, live, index + 1)
        day = next(
            (item for item in result.get("days") or [] if item.get("routine_id") == resolution.routine_id),
            None,
        )
        if day is None:
            raise ProgrammeValidationError("The routine has no programme day.")
        if resolution.action == "keep":
            report.append({"routine_id": resolution.routine_id, "action": "keep", "state": diff["state"], "source_hash": diff["current_hash"]})
            continue
        if resolution.action == "replace":
            target_id = resolution.replacement_routine_id or ""
            replacement = live_by_id.get(target_id)
            if replacement is None:
                raise ProgrammeValidationError("The replacement routine is not available in Hevy.")
            if target_id in used and target_id != resolution.routine_id:
                raise ProgrammeValidationError("The replacement routine is already in the programme.")
            if not all(_supported(item) for item in replacement.get("exercises") or []) and not resolution.acknowledge_unsupported:
                raise ProgrammeValidationError(
                    "The replacement contains exercises this engine cannot interpret; acknowledge them to continue."
                )
            _replace_day(result, day, index, resolution.routine_id, replacement)
            used.discard(resolution.routine_id)
            used.add(target_id)
            index_by_id[target_id] = index_by_id.pop(resolution.routine_id)
            report.append({"routine_id": resolution.routine_id, "action": "replace", "replacement_routine_id": target_id})
            continue
        if live is None:
            raise ProgrammeValidationError(
                "The Hevy routine no longer exists; keep it or replace it with another routine."
            )
        all_ids = [item["id"] for item in diff["changes"]]
        accepted = set(all_ids) if resolution.action == "rebase" else set(resolution.accept)
        unknown = accepted - set(all_ids)
        if unknown:
            raise ProgrammeValidationError(
                "Unknown change identifiers: " + ", ".join(sorted(unknown))
            )
        unsupported = [item["id"] for item in diff["changes"] if item["id"] in accepted and not item["supported"]]
        if unsupported and not resolution.acknowledge_unsupported:
            raise ProgrammeValidationError(
                "These changes involve exercises the engine cannot interpret; "
                "acknowledge them to continue: " + ", ".join(unsupported)
            )
        if not accepted:
            raise ProgrammeValidationError("Select at least one change to adopt.")
        _merge_day(result, day, index, snapshot, live, diff, accepted)
        report.append(
            {
                "routine_id": resolution.routine_id,
                "action": resolution.action,
                "accepted": sorted(accepted),
                "rejected": sorted(set(all_ids) - accepted),
            }
        )
    return result, report


class DriftService:
    """Tenant-scoped drift diff and reconciliation."""

    def __init__(self, db_path: str, repository: ProgrammeRepository | None = None) -> None:
        self.repository = repository or ProgrammeRepository(db_path)
        self.db_path = db_path

    def _timezone(self, conn: Any, user_id: str) -> str:
        row = conn.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()
        return str((row["timezone"] if row else None) or "UTC")

    def diff(
        self, user_id: str, programme_id: int, live_source: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.repository.transaction() as conn:
            programme = self.repository.programme_in(conn, user_id, programme_id)
            if programme.current_revision is None:
                raise ProgrammeNotFoundError("Programme has no materialised revision.")
            definition = definition_from_revision(
                programme, programme.current_revision, timezone_name=self._timezone(conn, user_id)
            )
            report = diff_programme(definition, live_source)
            report["programme_id"] = programme.id
            report["revision_number"] = programme.current_revision.revision_number
            return report

    def resolve(
        self,
        user_id: str,
        programme_id: int,
        live_source: Mapping[str, Any],
        resolutions: Sequence[Resolution],
        *,
        now: datetime,
        profile_rules: Any = None,
        training_rules: Any = None,
    ) -> dict[str, Any]:
        """Apply the user's explicit decisions; a change appends a new revision."""

        if not resolutions:
            raise ProgrammeValidationError("Choose how to resolve at least one routine.")
        from dynamic_programme import validate_programme_preview
        from programme_validator import validate_programme

        with self.repository.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            programme = self.repository.programme_in(conn, user_id, programme_id)
            revision = programme.current_revision
            if programme.status not in {"active", "paused"} or revision is None:
                raise ProgrammeValidationError("Only an active or paused programme can be reconciled.")
            timezone_name = self._timezone(conn, user_id)
            definition = definition_from_revision(programme, revision, timezone_name=timezone_name)
            reconciled, report = reconcile_definition(definition, live_source, resolutions)
            changed = [item for item in report if item["action"] != "keep"]
            today = local_date_of(now, timezone_name) or now.date()
            week = (
                week_for_date(programme.start_date, int(programme.duration_weeks or 0), today)
                if programme.start_date
                else None
            ) or 0
            if not changed:
                for item in report:
                    self.repository.record_decision_in(
                        conn, user_id, programme_id, revision.id, revision.id,
                        self._decision(
                            programme, user_id, "drift_keep", item, week,
                            state=STATE_ACKNOWLEDGED,
                            key=f"drift-keep:{programme_id}:{item['routine_id']}:{item.get('source_hash')}",
                        ),
                    )
                return {"revision_created": False, "revision_number": revision.revision_number, "resolved": report}
            if profile_rules is None and training_rules is None:
                from database import load_validation_rules

                profile_rules, training_rules = load_validation_rules(conn, user_id)
            violations = list(validate_programme_preview(reconciled))
            checked = validate_programme(
                reconciled, profile_rules=profile_rules, training_rules=training_rules
            )
            violations.extend(checked.blocking_violations)
            if violations:
                raise ProgrammeValidationError(
                    "The reconciled programme violates blocking constraints: "
                    + "; ".join(str(item.get("message") or item.get("rule") or item) for item in violations[:3])
                )
            reason = "rebase"
            kind = "replace" if all(i["action"] == "replace" for i in changed) else "rebase"
            draft = definition_to_draft(
                reconciled, user_id, reason=reason,
                audit={"event": "drift_resolution", "resolved": report, "from_revision": revision.revision_number},
            )
            new_revision, decision_id = self.repository.create_revision_in(
                conn, user_id, programme_id, draft, reason=reason,
                adaptation=self._decision(
                    programme, user_id, f"drift_{kind}", {"resolved": report}, week,
                    state=STATE_CONFIRMED, key="",
                ),
            )
            for item in report:
                if item["action"] == "keep":
                    self.repository.record_decision_in(
                        conn, user_id, programme_id, new_revision.id, new_revision.id,
                        self._decision(
                            programme, user_id, "drift_keep", item, week,
                            state=STATE_ACKNOWLEDGED,
                            key=f"drift-keep:{programme_id}:{item['routine_id']}:{item.get('source_hash')}",
                        ),
                    )
            return {
                "revision_created": True,
                "revision_number": new_revision.revision_number,
                "decision_id": decision_id,
                "resolved": report,
            }

    @staticmethod
    def _decision(
        programme: Programme,
        user_id: str,
        decision_type: str,
        detail: Mapping[str, Any],
        week: int,
        *,
        state: str,
        key: str,
    ) -> AdaptationDecisionInput:
        revision = programme.current_revision
        return AdaptationDecisionInput(
            scope={
                "level": "source_drift",
                "user_id": user_id,
                "programme_id": programme.id,
                "revision_number": revision.revision_number if revision else None,
                "week_number": week,
            },
            inputs={"detection": "hevy_routine_drift", "writes_to_hevy": False},
            output={"decision_type": decision_type, **dict(detail), "rationale_codes": []},
            confidence=1.0,
            rule_version=engine.RULE_VERSION,
            explanation=(
                "The user resolved Hevy routine drift; the previous revision is preserved "
                "and the original Hevy routine was not modified."
            ),
            confirmed=state == STATE_CONFIRMED,
            decision_type=decision_type,
            application_state=state,
            idempotency_key=key,
            week_number=week,
        )


__all__ = [
    "ACTIONS",
    "DriftConflictError",
    "DriftService",
    "Resolution",
    "diff_programme",
    "diff_routine",
    "reconcile_definition",
    "source_hash",
]
