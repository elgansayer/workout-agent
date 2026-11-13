"""Deterministic evidence and provenance for personalised coaching replies.

The coach may explain and interpret a user's data, but every factual claim must
be traceable to a tenant-scoped source record. This module builds a bounded
evidence bundle from already-authorised repository reads, stamps each item with
its record ID, date, freshness and transformation version, and validates any
citations the model emits against that bundle.

It is deliberately provider- and persistence-agnostic so the same rules apply
to the chat UI, stored history and tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

CONTEXT_TRANSFORMATION_VERSION = "coaching-context-v1"

TRANSFORMATION_VERSIONS: Mapping[str, str] = {
    "daily_log": "daily-log-v1",
    "exercise_progress": "exercise-progress-v1",
    "personal_record": "epley-e1rm-v1",
    "body_metrics": "body-metrics-v1",
}

FRESH_FOR = timedelta(days=7)
STALE_AFTER = timedelta(days=30)


class Freshness(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


class GroundingStatus(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    source: str
    record_id: str
    date: str
    freshness: str
    transformation_version: str
    summary: str

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "record_id": self.record_id,
            "date": self.date,
            "freshness": self.freshness,
            "transformation_version": self.transformation_version,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    user_id: str
    generated_at: str
    window_start: str
    window_end: str
    transformation_version: str
    items: tuple[EvidenceItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "transformation_version": self.transformation_version,
            "items": [item.to_dict() for item in self.items],
        }

    def prompt_context(self) -> str:
        if not self.items:
            return "No matching training records are available for this window."
        lines = []
        for item in self.items:
            lines.append(
                f"[{item.evidence_id}] ({item.source}, {item.date}, "
                f"freshness={item.freshness}, transform={item.transformation_version}) "
                f"{item.summary}"
            )
        return "\n".join(lines)


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def classify_freshness(record_date: date | None, *, as_of: datetime) -> Freshness:
    """Classify a record relative to the coaching request time."""
    if record_date is None:
        return Freshness.UNKNOWN
    age = as_of.date() - record_date
    if age < timedelta(0):
        return Freshness.UNKNOWN
    if age <= FRESH_FOR:
        return Freshness.FRESH
    if age > STALE_AFTER:
        return Freshness.STALE
    return Freshness.AGING


def _within_window(record_date: date | None, start: date, end: date) -> bool:
    if record_date is None:
        return False
    return start <= record_date <= end


def _clean_text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _log_item(record: Mapping[str, Any]) -> tuple[str, str]:
    focus = _clean_text(record.get("focus")) or "Training log"
    carb = _clean_text(record.get("carb_tier"))
    suffix = f" ({carb} carbs)" if carb else ""
    return "Daily log", f"Daily log: {focus}{suffix}"


def _progress_item(record: Mapping[str, Any]) -> tuple[str, str]:
    exercise = _clean_text(record.get("exercise_name")) or "Exercise"
    weight = record.get("top_weight_kg")
    reps = record.get("top_reps")
    sets = record.get("sets")
    return (
        exercise,
        f"{exercise} top set {weight}kg x {reps} reps x {sets} sets",
    )


def _pr_item(record: Mapping[str, Any]) -> tuple[str, str]:
    exercise = _clean_text(record.get("exercise")) or "Exercise"
    summary = (
        f"Estimated 1RM {record.get('e1rm')}kg from "
        f"{record.get('weight_kg')}kg x {record.get('reps')} reps"
    )
    return exercise, summary


def _body_item(record: Mapping[str, Any]) -> tuple[str, str]:
    parts = []
    if record.get("weight_kg") is not None:
        parts.append(f"weight {record.get('weight_kg')}kg")
    if record.get("body_fat_pct") is not None:
        parts.append(f"body fat {record.get('body_fat_pct')}%")
    if record.get("resting_hr") is not None:
        parts.append(f"resting HR {record.get('resting_hr')}bpm")
    if record.get("hrv") is not None:
        parts.append(f"HRV {record.get('hrv')}ms")
    summary = "Body metrics: " + (", ".join(parts) if parts else "reading recorded")
    return "Body metrics", summary


def _build_item(
    *,
    source: str,
    record: Mapping[str, Any],
    record_id: Any,
    record_date: date,
    as_of: datetime,
    summary_builder: Any,
) -> EvidenceItem:
    label, summary = summary_builder(record)
    return EvidenceItem(
        evidence_id="",
        source=source,
        record_id=f"{source}:{record_id}",
        date=record_date.isoformat(),
        freshness=classify_freshness(record_date, as_of=as_of).value,
        transformation_version=TRANSFORMATION_VERSIONS[source],
        summary=summary or label,
    )


def build_evidence_bundle(
    *,
    user_id: str,
    logs: Sequence[Mapping[str, Any]] = (),
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    biometrics: Sequence[Mapping[str, Any]] = (),
    personal_records: Sequence[Mapping[str, Any]] = (),
    as_of: datetime | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    max_items: int = 40,
) -> EvidenceBundle:
    """Build a bounded, tenant-scoped evidence bundle.

    Callers must pass records already read with the authenticated user's ID.
    Records outside ``window_start``..``window_end`` (or dated after ``as_of``)
    are excluded before they can be cited.
    """
    now = as_of or datetime.now(tz=timezone.utc)
    end = window_end or now.date()
    if window_start is not None:
        start = window_start
    else:
        try:
            start = end - timedelta(days=90)
        except OverflowError:
            start = date.min
    if start > end:
        raise ValueError("window_start must not be after window_end")

    collected: list[EvidenceItem] = []

    for record in logs:
        when = _coerce_date(record.get("date"))
        if when is None or not _within_window(when, start, end):
            continue
        collected.append(
            _build_item(
                source="daily_log",
                record=record,
                record_id=record.get("id", record.get("date")),
                record_date=when,
                as_of=now,
                summary_builder=_log_item,
            ),
        )

    for records in (history or {}).values():
        for record in records:
            when = _coerce_date(record.get("date"))
            if when is None or not _within_window(when, start, end):
                continue
            collected.append(
                _build_item(
                    source="exercise_progress",
                    record=record,
                    record_id=record.get("id", record.get("date")),
                    record_date=when,
                    as_of=now,
                    summary_builder=_progress_item,
                ),
            )

    for record in personal_records:
        when = _coerce_date(record.get("date"))
        if when is None or not _within_window(when, start, end):
            continue
        collected.append(
            _build_item(
                source="personal_record",
                record=record,
                record_id=record.get("id", record.get("date")),
                record_date=when,
                as_of=now,
                summary_builder=_pr_item,
            ),
        )

    for record in biometrics:
        when = _coerce_date(record.get("date"))
        if when is None or not _within_window(when, start, end):
            continue
        collected.append(
            _build_item(
                source="body_metrics",
                record=record,
                record_id=record.get("id", record.get("date")),
                record_date=when,
                as_of=now,
                summary_builder=_body_item,
            ),
        )

    collected.sort(key=lambda item: (item.date, item.source, item.record_id))
    bounded = collected[-max_items:] if max_items > 0 else []
    numbered = tuple(
        EvidenceItem(
            evidence_id=f"E{index}",
            source=item.source,
            record_id=item.record_id,
            date=item.date,
            freshness=item.freshness,
            transformation_version=item.transformation_version,
            summary=item.summary,
        )
        for index, item in enumerate(bounded, start=1)
    )
    return EvidenceBundle(
        user_id=user_id,
        generated_at=now.isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        transformation_version=CONTEXT_TRANSFORMATION_VERSION,
        items=numbered,
    )


_CITATION_RE = re.compile(r"\[E(\d+)\]")


def citation_ids(text: str, bundle: EvidenceBundle) -> list[str]:
    """Return valid evidence IDs cited by *text*, in first-seen order."""
    valid = {item.evidence_id for item in bundle.items}
    seen: list[str] = []
    for match in _CITATION_RE.finditer(text or ""):
        evidence_id = f"E{match.group(1)}"
        if evidence_id in valid and evidence_id not in seen:
            seen.append(evidence_id)
    return seen


def validate_citations(text: str, bundle: EvidenceBundle) -> tuple[str, list[str]]:
    """Strip citations that are not in the authenticated, time-bound bundle."""
    valid = {item.evidence_id for item in bundle.items}

    def _replace(match: re.Match[str]) -> str:
        evidence_id = f"E{match.group(1)}"
        return match.group(0) if evidence_id in valid else ""

    sanitized = _CITATION_RE.sub(_replace, text or "")
    return sanitized, citation_ids(sanitized, bundle)


def resolve_grounding(
    text: str,
    bundle: EvidenceBundle,
    cited: Iterable[str],
) -> GroundingStatus:
    """Label the reply so the UI never presents inference as observation."""
    cited_ids = list(cited)
    if not bundle.items:
        return GroundingStatus.UNKNOWN
    if not (text or "").strip():
        return GroundingStatus.INCOMPLETE
    if cited_ids:
        return GroundingStatus.OBSERVED
    return GroundingStatus.INFERRED


def evidence_prompt(bundle: EvidenceBundle) -> str:
    """Prompt fragment that constrains claims to the retrieved evidence."""
    return f"""Evidence rules:
- The evidence list below is the ONLY source of the user's personal training data.
- Cite supporting evidence inline using its bracketed ID, for example [E1]. Never invent IDs.
- Clearly separate observations (cited) from inference (your interpretation) and generic advice.
- If the evidence does not answer the question, say the conclusion is unknown or incomplete instead of guessing.
- Never mention records, users, or dates that are absent from this list.

Evidence window: {bundle.window_start} to {bundle.window_end} (transformation {bundle.transformation_version})
{bundle.prompt_context()}"""


def serialise_bundle(bundle: EvidenceBundle, *, grounding: GroundingStatus, cited: Sequence[str]) -> str:
    """Serialise the stored evidence payload for chat history."""
    payload = bundle.to_dict()
    payload["grounding"] = grounding.value
    payload["cited"] = list(cited)
    return json.dumps(payload, separators=(",", ":"))


__all__ = [
    "CONTEXT_TRANSFORMATION_VERSION",
    "TRANSFORMATION_VERSIONS",
    "EvidenceBundle",
    "EvidenceItem",
    "Freshness",
    "GroundingStatus",
    "build_evidence_bundle",
    "citation_ids",
    "classify_freshness",
    "evidence_prompt",
    "resolve_grounding",
    "serialise_bundle",
    "validate_citations",
]
