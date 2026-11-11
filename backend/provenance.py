"""Typed, versioned provenance contract for core user-owned records.

Every core record is exactly one of three kinds:

``imported``
    Source data received from an external provider (Hevy, Google Health, a
    Health Connect export). It records what the provider said.
``user_entered``
    A value typed or corrected by the user. It is authoritative and outranks
    the imported value it corrects, which stays in the record's lineage.
``derived``
    A summary or recommendation computed by this application, by rules or by
    an AI model. It is never evidence of what the user did or measured.

The kind/source pairing is validated so AI output can never be labelled as
source data. See ``docs/PROVENANCE.md`` for the persistence and lineage rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Self

PROVENANCE_CONTRACT_VERSION = 1

MANUAL_SOURCE = "manual"
UNATTRIBUTED_SOURCE = "unattributed"
MANUAL_ENTRY_VERSION = "manual-entry/1"
LEGACY_TRANSFORMATION_VERSION = "legacy/0"
UNSPECIFIED_TRANSFORMATION_VERSION = "unspecified/0"

_AI_PREFIX = "ai:"
_RULES_PREFIX = "rules:"
_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.+:/-]{0,79}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:/-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_EXTERNAL_ID = 200


class RecordKind(StrEnum):
    IMPORTED = "imported"
    USER_ENTERED = "user_entered"
    DERIVED = "derived"


class RecordType(StrEnum):
    """Record families that carry provenance (the owning table names)."""

    WORKOUT = "workout_history"
    EXERCISE_PROGRESS = "exercise_progress"
    BODY_METRIC = "body_metrics"
    DAILY_PLAN = "daily_log"
    CHECK_IN = "check_ins"
    DASHBOARD_INSIGHT = "dashboard_insights"
    DEEP_CORRELATION = "deep_correlations"
    REASONING = "reasoning_logs"


# Which kinds each record family may carry. This keeps AI output from being filed
# as source data (and imports from posing as recommendations) at the storage layer.
ALLOWED_KINDS: dict[RecordType, frozenset[RecordKind]] = {
    RecordType.WORKOUT: frozenset({RecordKind.IMPORTED}),
    RecordType.EXERCISE_PROGRESS: frozenset({RecordKind.IMPORTED}),
    RecordType.BODY_METRIC: frozenset({RecordKind.IMPORTED, RecordKind.USER_ENTERED}),
    RecordType.DAILY_PLAN: frozenset({RecordKind.DERIVED}),
    RecordType.CHECK_IN: frozenset({RecordKind.DERIVED}),
    RecordType.DASHBOARD_INSIGHT: frozenset({RecordKind.DERIVED}),
    RecordType.DEEP_CORRELATION: frozenset({RecordKind.DERIVED}),
    RecordType.REASONING: frozenset({RecordKind.DERIVED}),
}

_AUTHORITY_RANK = {
    RecordKind.USER_ENTERED: 3,
    RecordKind.IMPORTED: 2,
    RecordKind.DERIVED: 1,
}

_SOURCE_LABELS = {
    "hevy": "Hevy",
    "google_health": "Google Health",
    "health_connect_file": "Health Connect export",
    MANUAL_SOURCE: "You",
    UNATTRIBUTED_SOURCE: "Unknown source",
}


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def normalise_timestamp(value: datetime | date | str | None) -> str | None:
    """Return an ISO-8601 UTC timestamp, or a calendar date for date-only input.

    Blank, unparseable or timezone-less date-times return ``None``: source
    metadata is best effort and must never block ingestion or invent a time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date().isoformat() if len(text) == 10 else None
    return parsed.astimezone(timezone.utc).isoformat()


def content_hash(content: Any) -> str:
    """Stable SHA-256 of the logical value, used to ignore identical re-imports."""
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean_text(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    text = _CONTROL_RE.sub(" ", str(value))
    text = " ".join(text.split())
    return text[:limit] or None


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a record's value came from and how it reached the database."""

    kind: RecordKind
    source: str
    transformation_version: str
    ingested_at: str
    external_id: str | None = None
    source_timestamp: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        kind = RecordKind(self.kind)
        object.__setattr__(self, "kind", kind)

        source = self.source
        if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
            raise ValueError("source must be a short lower-case identifier")
        if kind is RecordKind.USER_ENTERED and source != MANUAL_SOURCE:
            raise ValueError("user-entered records must use the manual source")
        if kind is RecordKind.DERIVED and not (
            source.startswith((_AI_PREFIX, _RULES_PREFIX))
            or source == UNATTRIBUTED_SOURCE
        ):
            raise ValueError("derived records must name an ai: or rules: source")
        if kind is RecordKind.IMPORTED and (
            source == MANUAL_SOURCE or source.startswith((_AI_PREFIX, _RULES_PREFIX))
        ):
            raise ValueError("imported records cannot use a manual, ai: or rules: source")

        if not isinstance(self.transformation_version, str) or not _VERSION_RE.fullmatch(
            self.transformation_version
        ):
            raise ValueError("transformation_version must be a short version label")

        ingested = normalise_timestamp(self.ingested_at)
        if ingested is None or "T" not in ingested:
            raise ValueError("ingested_at must be a timezone-aware timestamp")
        object.__setattr__(self, "ingested_at", ingested)

        external_id = self.external_id
        if external_id is not None:
            if not isinstance(external_id, str) or _CONTROL_RE.search(external_id):
                raise ValueError("external_id must be printable text")
            external_id = external_id.strip() or None
            if external_id is not None and len(external_id) > _MAX_EXTERNAL_ID:
                raise ValueError("external_id is too long")
        object.__setattr__(self, "external_id", external_id)

        if self.source_timestamp is not None:
            source_timestamp = normalise_timestamp(self.source_timestamp)
            if source_timestamp is None:
                raise ValueError("source_timestamp must be an ISO-8601 timestamp")
            object.__setattr__(self, "source_timestamp", source_timestamp)

        if self.confidence is not None:
            confidence = self.confidence
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0.0 <= confidence <= 1.0
            ):
                raise ValueError("confidence must be a number between 0 and 1")
            object.__setattr__(self, "confidence", float(confidence))

    @classmethod
    def _build(
        cls,
        kind: RecordKind,
        source: str,
        transformation_version: str,
        *,
        external_id: str | None,
        source_timestamp: datetime | date | str | None,
        ingested_at: datetime | str | None,
        confidence: float | None,
    ) -> Provenance:
        return cls(
            kind=kind,
            source=source,
            transformation_version=transformation_version,
            ingested_at=normalise_timestamp(ingested_at or utc_now()) or "",
            external_id=external_id,
            source_timestamp=normalise_timestamp(source_timestamp),
            confidence=confidence,
        )

    @classmethod
    def imported(
        cls,
        source: str,
        transformation_version: str,
        *,
        external_id: str | None = None,
        source_timestamp: datetime | date | str | None = None,
        ingested_at: datetime | str | None = None,
        confidence: float | None = None,
    ) -> Provenance:
        return cls._build(
            RecordKind.IMPORTED,
            source,
            transformation_version,
            external_id=external_id,
            source_timestamp=source_timestamp,
            ingested_at=ingested_at,
            confidence=confidence,
        )

    @classmethod
    def user_entered(
        cls,
        transformation_version: str = MANUAL_ENTRY_VERSION,
        *,
        ingested_at: datetime | str | None = None,
    ) -> Provenance:
        return cls._build(
            RecordKind.USER_ENTERED,
            MANUAL_SOURCE,
            transformation_version,
            external_id=None,
            source_timestamp=None,
            ingested_at=ingested_at,
            confidence=None,
        )

    @classmethod
    def derived(
        cls,
        source: str,
        transformation_version: str,
        *,
        confidence: float | None = None,
        ingested_at: datetime | str | None = None,
    ) -> Provenance:
        return cls._build(
            RecordKind.DERIVED,
            source,
            transformation_version,
            external_id=None,
            source_timestamp=None,
            ingested_at=ingested_at,
            confidence=confidence,
        )

    @property
    def authority_rank(self) -> int:
        return _AUTHORITY_RANK[self.kind]

    @property
    def authoritative(self) -> bool:
        """Source data and user entries are evidence; derived records are not."""
        return self.kind is not RecordKind.DERIVED

    def may_replace(self, current_kind: RecordKind | str) -> bool:
        """An automated import may not overwrite a user-entered value."""
        return self.authority_rank >= _AUTHORITY_RANK[RecordKind(current_kind)]


def unattributed(
    kind: RecordKind,
    *,
    transformation_version: str = UNSPECIFIED_TRANSFORMATION_VERSION,
    ingested_at: datetime | str | None = None,
) -> Provenance:
    """Provenance for a writer that did not declare where its value came from."""
    return Provenance._build(
        kind,
        UNATTRIBUTED_SOURCE,
        transformation_version,
        external_id=None,
        source_timestamp=None,
        ingested_at=ingested_at,
        confidence=None,
    )


def ai_source(provider_label: str | None) -> str:
    """Map a provider label such as ``Gemini (gemini-2.5-flash)`` to a source id."""
    text = (provider_label or "").strip().lower()
    match = re.match(r"^([a-z0-9][a-z0-9._-]*)\s*(?:\(([^)]*)\))?", text)
    if not match:
        return f"{_AI_PREFIX}unknown"
    vendor = match.group(1)[:30]
    model = re.sub(r"[^a-z0-9._-]+", "-", (match.group(2) or "").strip()).strip("-")[:40]
    return f"{_AI_PREFIX}{vendor}/{model}" if model else f"{_AI_PREFIX}{vendor}"


class GeneratedText(str):
    """Generator output tagged with how it was produced.

    Behaves exactly like ``str``; ``generator`` is the AI provider label and
    ``from_fallback`` marks deterministic fallback text, so a saved plan is never
    attributed to an AI model that did not write it.
    """

    generator: str | None
    from_fallback: bool

    def __new__(
        cls, value: str, *, generator: str | None = None, from_fallback: bool = False
    ) -> Self:
        instance = super().__new__(cls, value)
        instance.generator = generator
        instance.from_fallback = from_fallback
        return instance

    @classmethod
    def from_provider(cls, value: str, provider: object) -> Self:
        return cls(value, generator=provider_label(provider))

    @classmethod
    def fallback(cls, value: str) -> Self:
        return cls(value, from_fallback=True)


def provider_label(provider: object) -> str | None:
    """Best-effort display name of an AI provider; never raises."""
    try:
        label = provider.name()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - provenance must never block a save
        return None
    return label if isinstance(label, str) and label.strip() else None


def ai_provenance(
    provider: object,
    transformation_version: str,
    *,
    confidence: float | None = None,
) -> Provenance:
    """Derived provenance for output generated by an AI *provider*."""
    return Provenance.derived(
        ai_source(provider_label(provider)),
        transformation_version,
        confidence=confidence,
    )


def generated_text_provenance(text: object, transformation_version: str) -> Provenance:
    """Derived provenance for generator output: AI, rule fallback, or unknown."""
    if getattr(text, "from_fallback", False):
        return Provenance.derived(rules_source("fallback"), transformation_version)
    generator = getattr(text, "generator", None)
    if isinstance(generator, str) and generator.strip():
        return Provenance.derived(ai_source(generator), transformation_version)
    return unattributed(
        RecordKind.DERIVED, transformation_version=transformation_version
    )


def rules_source(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-")[:40]
    return f"{_RULES_PREFIX}{slug or 'engine'}"


def clean_note(note: str | None, *, limit: int = 280) -> str | None:
    """Bound and de-control free text (a correction reason) before storage."""
    return _clean_text(note, limit=limit)


def source_label(source: str) -> str:
    if source in _SOURCE_LABELS:
        return _SOURCE_LABELS[source]
    if source.startswith(_AI_PREFIX):
        vendor, _, model = source[len(_AI_PREFIX):].partition("/")
        return f"AI coach ({' '.join(part for part in (vendor.title(), model) if part)})"
    if source.startswith(_RULES_PREFIX):
        return "Rules engine"
    return source


def describe(kind: RecordKind | str, source: str, *, corrected: bool = False) -> str:
    kind = RecordKind(kind)
    if kind is RecordKind.IMPORTED:
        if source == UNATTRIBUTED_SOURCE:
            return "Imported, source not recorded"
        return f"Imported from {source_label(source)}"
    if kind is RecordKind.USER_ENTERED:
        return "Corrected by you" if corrected else "Entered by you"
    if source.startswith(_AI_PREFIX):
        return "AI recommendation"
    if source.startswith(_RULES_PREFIX):
        return "Calculated by rules"
    return "Derived by the app"


def api_view(
    provenance: Provenance,
    *,
    revision: int = 1,
    corrected: bool = False,
    applied: bool = True,
    supersedes_revision: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Full owner-facing projection used by the web API and lineage views."""
    return {
        "contract_version": PROVENANCE_CONTRACT_VERSION,
        "kind": provenance.kind.value,
        "source": provenance.source,
        "source_label": source_label(provenance.source),
        "label": describe(provenance.kind, provenance.source, corrected=corrected),
        "authoritative": provenance.authoritative,
        "corrected": corrected,
        "applied": applied,
        "revision": revision,
        "supersedes_revision": supersedes_revision,
        "external_id": provenance.external_id,
        "source_timestamp": provenance.source_timestamp,
        "ingested_at": provenance.ingested_at,
        "transformation_version": provenance.transformation_version,
        "confidence": provenance.confidence,
        "note": note,
    }


def retrieval_view(view: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compact AI-context projection of an ``api_view``: no identifiers or notes."""
    if not view:
        return {"origin": "unrecorded", "source": "not recorded", "authoritative": False}
    compact: dict[str, Any] = {
        "origin": view["kind"],
        "source": view["source_label"],
        "authoritative": bool(view["authoritative"]),
        "as_of": str(view.get("source_timestamp") or view["ingested_at"])[:10],
    }
    if view.get("corrected"):
        compact["corrected_by_user"] = True
    if view.get("confidence") is not None:
        compact["confidence"] = round(float(view["confidence"]), 2)
    return compact


_PROGRAMME_CONFIDENCE_LABELS = frozenset({"high", "medium", "low"})


def programme_view(programme: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Provenance view of an activated programme, from the fields it already stores.

    A programme is a deterministic recommendation generated from the user's Hevy
    routines. Its row is itself an immutable, versioned record (a new preview
    token creates a new row and the previous one stays inactive), so no ledger
    revision is needed: the engine version, the Hevy routine edit times, the
    preview identifier and the inference confidence are already stored on it.
    """
    if not programme:
        return None
    definition = programme.get("definition")
    if not isinstance(definition, Mapping):
        definition = {}
    snapshots = definition.get("source_snapshots")
    stamps = [
        stamp
        for item in (snapshots if isinstance(snapshots, list) else [])
        if isinstance(item, Mapping) and (stamp := normalise_timestamp(item.get("updated_at")))
    ]
    try:
        provenance = Provenance(
            kind=RecordKind.DERIVED,
            source=rules_source("hevy-programme"),
            transformation_version=str(
                definition.get("engine_version") or UNSPECIFIED_TRANSFORMATION_VERSION
            ),
            ingested_at=str(programme.get("created_at") or ""),
            external_id=str(programme.get("template_key") or "") or None,
            source_timestamp=max(stamps) if stamps else None,
        )
    except ValueError:
        return None
    view = api_view(provenance)
    analysis = definition.get("analysis")
    label = analysis.get("confidence") if isinstance(analysis, Mapping) else None
    # The engine reports a qualitative level; no numeric probability is invented.
    view["confidence_label"] = label if label in _PROGRAMME_CONFIDENCE_LABELS else None
    return view


def annotate_for_retrieval(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy records for an AI prompt, replacing ``provenance`` with the compact view.

    Records without provenance are labelled ``unrecorded`` rather than assumed to
    be source data.
    """
    annotated: list[dict[str, Any]] = []
    for record in records:
        item = {key: value for key, value in record.items() if key != "provenance"}
        item["provenance"] = retrieval_view(record.get("provenance"))
        annotated.append(item)
    return annotated


RETRIEVAL_GUIDANCE = (
    "Provenance rules: every record carries an origin. 'imported' is source data "
    "received from an external provider. 'user_entered' was typed or corrected by "
    "the user, is authoritative and overrides the imported value it corrects. "
    "'derived' is a recommendation or summary produced by this app or an AI model: "
    "it is never evidence of what the user did or measured. 'unrecorded' means the "
    "origin was not captured, so treat it as unverified. Cite imported and "
    "user_entered records as facts, call derived records recommendations, and state "
    "when a claim rests only on derived or unrecorded data."
)
