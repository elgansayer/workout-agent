"""Tests for the provenance contract (kinds, validation, projections)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from provenance import (
    ALLOWED_KINDS,
    MANUAL_SOURCE,
    UNATTRIBUTED_SOURCE,
    GeneratedText,
    Provenance,
    RecordKind,
    RecordType,
    ai_provenance,
    ai_source,
    annotate_for_retrieval,
    api_view,
    content_hash,
    describe,
    generated_text_provenance,
    normalise_timestamp,
    programme_view,
    retrieval_view,
    rules_source,
    source_label,
    unattributed,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_imported_provenance_stores_every_required_field() -> None:
    prov = Provenance.imported(
        "hevy",
        "hevy-parser/1",
        external_id="workout-123",
        source_timestamp="2030-01-01T08:00:00Z",
        ingested_at=NOW,
        confidence=0.9,
    )
    assert prov.kind is RecordKind.IMPORTED
    assert prov.source == "hevy"
    assert prov.external_id == "workout-123"
    assert prov.source_timestamp == "2030-01-01T08:00:00+00:00"
    assert prov.ingested_at == "2030-01-02T03:04:05+00:00"
    assert prov.transformation_version == "hevy-parser/1"
    assert prov.confidence == 0.9
    assert prov.authoritative is True


def test_timestamps_are_normalised_to_utc() -> None:
    offset = datetime(2030, 1, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    assert normalise_timestamp(offset) == "2030-01-01T08:00:00+00:00"
    assert normalise_timestamp("2030-01-01T10:00:00+02:00") == "2030-01-01T08:00:00+00:00"
    # Date-only input keeps date precision instead of inventing a time of day.
    assert normalise_timestamp("2030-01-01") == "2030-01-01"
    assert normalise_timestamp(date(2030, 1, 1)) == "2030-01-01"


@pytest.mark.parametrize(
    "value", [None, "", "  ", "not a date", "2030-01-01T10:00:00", 42, "x" * 80]
)
def test_unusable_source_timestamps_become_none(value: Any) -> None:
    assert normalise_timestamp(value) is None
    # Best-effort metadata must never block ingestion.
    prov = Provenance.imported("hevy", "v/1", source_timestamp=value, ingested_at=NOW)
    assert prov.source_timestamp is None


def test_ingestion_time_must_be_a_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="ingested_at"):
        Provenance(RecordKind.IMPORTED, "hevy", "v/1", "2030-01-01")
    with pytest.raises(ValueError, match="ingested_at"):
        Provenance(RecordKind.IMPORTED, "hevy", "v/1", "2030-01-01T00:00:00")


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"])
def test_confidence_must_be_a_probability(confidence: Any) -> None:
    with pytest.raises(ValueError, match="confidence"):
        Provenance(
            RecordKind.DERIVED,
            "ai:gemini",
            "plan/1",
            NOW.isoformat(),
            confidence=confidence,
        )


def test_confidence_is_optional_and_bounded_inclusive() -> None:
    assert Provenance.derived("ai:gemini", "plan/1", ingested_at=NOW).confidence is None
    assert Provenance.derived("ai:gemini", "plan/1", confidence=0, ingested_at=NOW).confidence == 0.0
    assert Provenance.derived("ai:gemini", "plan/1", confidence=1, ingested_at=NOW).confidence == 1.0


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        (RecordKind.IMPORTED, MANUAL_SOURCE),
        (RecordKind.IMPORTED, "ai:gemini"),
        (RecordKind.IMPORTED, "rules:fallback"),
        (RecordKind.USER_ENTERED, "hevy"),
        (RecordKind.USER_ENTERED, "ai:gemini"),
        (RecordKind.DERIVED, "hevy"),
        (RecordKind.DERIVED, MANUAL_SOURCE),
    ],
)
def test_kind_and_source_must_agree(kind: RecordKind, source: str) -> None:
    """AI output can never be labelled as source data, nor imports as advice."""
    with pytest.raises(ValueError):
        Provenance(kind, source, "v/1", NOW.isoformat())


@pytest.mark.parametrize("source", ["", "Hevy", "has space", "x" * 81, "../etc"])
def test_source_must_be_a_short_identifier(source: str) -> None:
    with pytest.raises(ValueError, match="source"):
        Provenance(RecordKind.IMPORTED, source, "v/1", NOW.isoformat())


@pytest.mark.parametrize("version", ["", " ", "bad version", "x" * 65, "\n1"])
def test_transformation_version_must_be_a_label(version: str) -> None:
    with pytest.raises(ValueError, match="transformation_version"):
        Provenance(RecordKind.IMPORTED, "hevy", version, NOW.isoformat())


def test_external_id_is_bounded_printable_text() -> None:
    assert Provenance.imported("hevy", "v/1", external_id="  abc  ", ingested_at=NOW).external_id == "abc"
    assert Provenance.imported("hevy", "v/1", external_id="   ", ingested_at=NOW).external_id is None
    with pytest.raises(ValueError, match="external_id"):
        Provenance.imported("hevy", "v/1", external_id="a\nb", ingested_at=NOW)
    with pytest.raises(ValueError, match="external_id"):
        Provenance.imported("hevy", "v/1", external_id="x" * 201, ingested_at=NOW)


def test_authority_ranks_user_entries_above_imports_above_derived() -> None:
    user = Provenance.user_entered(ingested_at=NOW)
    imported = Provenance.imported("hevy", "v/1", ingested_at=NOW)
    derived = Provenance.derived("ai:gemini", "v/1", ingested_at=NOW)
    assert user.source == MANUAL_SOURCE
    assert user.authoritative and imported.authoritative
    assert not derived.authoritative
    assert user.may_replace(RecordKind.IMPORTED)
    assert user.may_replace(RecordKind.USER_ENTERED)
    assert imported.may_replace(RecordKind.IMPORTED)
    # An automated import must never overwrite what the user typed.
    assert not imported.may_replace(RecordKind.USER_ENTERED)
    assert not derived.may_replace(RecordKind.IMPORTED)


def test_record_families_restrict_which_kinds_they_accept() -> None:
    assert set(ALLOWED_KINDS) == set(RecordType)
    assert ALLOWED_KINDS[RecordType.BODY_METRIC] == {
        RecordKind.IMPORTED,
        RecordKind.USER_ENTERED,
    }
    for family in (
        RecordType.DAILY_PLAN,
        RecordType.CHECK_IN,
        RecordType.DASHBOARD_INSIGHT,
        RecordType.DEEP_CORRELATION,
        RecordType.REASONING,
    ):
        assert ALLOWED_KINDS[family] == {RecordKind.DERIVED}
    for family in (RecordType.WORKOUT, RecordType.EXERCISE_PROGRESS):
        assert ALLOWED_KINDS[family] == {RecordKind.IMPORTED}


def test_ai_source_normalises_provider_labels() -> None:
    assert ai_source("Gemini (gemini-2.5-flash)") == "ai:gemini/gemini-2.5-flash"
    assert ai_source("Claude (claude-sonnet-5-5)") == "ai:claude/claude-sonnet-5-5"
    assert ai_source("DeepSeek") == "ai:deepseek"
    assert ai_source(None) == "ai:unknown"
    assert ai_source("   ") == "ai:unknown"
    assert ai_source("(only a model)") == "ai:unknown"
    # Hostile labels still produce a valid source identifier.
    hostile = ai_source("Evil<script> (a b\nc/../d)")
    assert Provenance.derived(hostile, "v/1", ingested_at=NOW).source == hostile
    assert rules_source("Fallback Plan!") == "rules:fallback-plan"


def test_labels_distinguish_the_three_kinds() -> None:
    assert describe(RecordKind.IMPORTED, "hevy") == "Imported from Hevy"
    assert describe(RecordKind.IMPORTED, "google_health") == "Imported from Google Health"
    assert describe(RecordKind.IMPORTED, UNATTRIBUTED_SOURCE) == "Imported, source not recorded"
    assert describe(RecordKind.USER_ENTERED, MANUAL_SOURCE) == "Entered by you"
    assert describe(RecordKind.USER_ENTERED, MANUAL_SOURCE, corrected=True) == "Corrected by you"
    assert describe(RecordKind.DERIVED, "ai:gemini/x") == "AI recommendation"
    assert describe(RecordKind.DERIVED, "rules:fallback") == "Calculated by rules"
    assert describe(RecordKind.DERIVED, UNATTRIBUTED_SOURCE) == "Derived by the app"
    assert source_label("ai:gemini/gemini-2.5-flash") == "AI coach (Gemini gemini-2.5-flash)"
    assert source_label("custom-source") == "custom-source"


def test_api_view_exposes_the_full_owner_facing_contract() -> None:
    prov = Provenance.imported(
        "hevy",
        "hevy-parser/1",
        external_id="w1",
        source_timestamp="2030-01-01T08:00:00Z",
        ingested_at=NOW,
    )
    view = api_view(prov, revision=2, supersedes_revision=1, note="n")
    assert view == {
        "contract_version": 1,
        "kind": "imported",
        "source": "hevy",
        "source_label": "Hevy",
        "label": "Imported from Hevy",
        "authoritative": True,
        "corrected": False,
        "applied": True,
        "revision": 2,
        "supersedes_revision": 1,
        "external_id": "w1",
        "source_timestamp": "2030-01-01T08:00:00+00:00",
        "ingested_at": "2030-01-02T03:04:05+00:00",
        "transformation_version": "hevy-parser/1",
        "confidence": None,
        "note": "n",
    }


def test_retrieval_view_is_compact_and_leaks_no_identifiers() -> None:
    prov = Provenance.imported(
        "hevy", "hevy-parser/1", external_id="secret-id", ingested_at=NOW, confidence=0.876
    )
    compact = retrieval_view(api_view(prov, note="private note"))
    assert compact == {
        "origin": "imported",
        "source": "Hevy",
        "authoritative": True,
        "as_of": "2030-01-02",
        "confidence": 0.88,
    }
    flattened = str(compact)
    assert "secret-id" not in flattened and "private note" not in flattened
    corrected = retrieval_view(
        api_view(Provenance.user_entered(ingested_at=NOW), corrected=True)
    )
    assert corrected["corrected_by_user"] is True
    assert corrected["origin"] == "user_entered"


def test_missing_provenance_is_labelled_unrecorded_not_assumed_authoritative() -> None:
    assert retrieval_view(None) == {
        "origin": "unrecorded",
        "source": "not recorded",
        "authoritative": False,
    }
    records = [{"date": "2030-01-01", "weight_kg": 80.0}]
    annotated = annotate_for_retrieval(records)
    assert annotated[0]["provenance"]["origin"] == "unrecorded"
    assert "provenance" not in records[0]  # the input is not mutated


def test_annotate_replaces_the_full_view_with_the_compact_one() -> None:
    view = api_view(Provenance.derived("ai:gemini", "plan/1", ingested_at=NOW))
    [record] = annotate_for_retrieval([{"date": "2030-01-01", "provenance": view}])
    assert record["provenance"]["origin"] == "derived"
    assert record["provenance"]["authoritative"] is False
    assert "transformation_version" not in record["provenance"]


def test_content_hash_is_stable_and_order_insensitive() -> None:
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


class _Provider:
    def __init__(self, label: Any) -> None:
        self._label = label

    def name(self) -> Any:
        if isinstance(self._label, Exception):
            raise self._label
        return self._label


def test_generated_text_tracks_how_it_was_produced() -> None:
    ai_text = GeneratedText.from_provider("plan", _Provider("Gemini (flash)"))
    assert ai_text == "plan" and isinstance(ai_text, str)
    prov = generated_text_provenance(ai_text, "daily-plan/1")
    assert prov.kind is RecordKind.DERIVED and prov.source == "ai:gemini/flash"

    fallback = generated_text_provenance(GeneratedText.fallback("baseline"), "daily-plan/1")
    assert fallback.source == "rules:fallback"

    plain = generated_text_provenance("untagged", "daily-plan/1")
    assert plain.source == UNATTRIBUTED_SOURCE and plain.kind is RecordKind.DERIVED


@pytest.mark.parametrize("label", [RuntimeError("boom"), 42, None, "  "])
def test_provider_label_failures_never_block_provenance(label: Any) -> None:
    prov = ai_provenance(_Provider(label), "dashboard-insight/1")
    assert prov.source == "ai:unknown"


def test_unattributed_provenance_is_explicit_about_the_missing_source() -> None:
    prov = unattributed(RecordKind.IMPORTED, ingested_at=NOW)
    assert prov.source == UNATTRIBUTED_SOURCE
    assert prov.transformation_version == "unspecified/0"
    assert prov.external_id is None and prov.confidence is None


def _programme(**definition: Any) -> dict[str, Any]:
    return {
        "created_at": "2026-09-01T10:00:00+00:00",
        "template_key": "hevy:abc123",
        "definition": {
            "engine_version": "hevy-programme-v1",
            "analysis": {"confidence": "medium"},
            "source_snapshots": [
                {"id": "a", "updated_at": "2026-08-01T10:00:00Z"},
                {"id": "b", "updated_at": "2026-08-20T10:00:00Z"},
                {"id": "c", "updated_at": None},
            ],
            **definition,
        },
    }


def test_programme_view_reports_a_derived_recommendation_from_stored_fields() -> None:
    view = programme_view(_programme())
    assert view is not None
    assert view["kind"] == "derived" and view["authoritative"] is False
    assert view["source"] == "rules:hevy-programme"
    assert view["label"] == "Calculated by rules"
    assert view["external_id"] == "hevy:abc123"
    assert view["transformation_version"] == "hevy-programme-v1"
    assert view["ingested_at"] == "2026-09-01T10:00:00+00:00"
    assert view["source_timestamp"] == "2026-08-20T10:00:00+00:00"  # newest Hevy routine edit
    # The engine reports a level, not a probability: nothing numeric is invented.
    assert view["confidence"] is None and view["confidence_label"] == "medium"


def test_programme_view_tolerates_sparse_or_malformed_definitions() -> None:
    assert programme_view(None) is None
    assert programme_view({}) is None
    assert programme_view({"created_at": "not a timestamp"}) is None
    sparse = programme_view({"created_at": "2026-09-01T10:00:00+00:00", "definition": "junk"})
    assert sparse is not None
    assert sparse["transformation_version"] == "unspecified/0"
    assert sparse["source_timestamp"] is None and sparse["confidence_label"] is None
    odd = programme_view(_programme(analysis={"confidence": "certain"}, source_snapshots="bad"))
    assert odd is not None and odd["confidence_label"] is None and odd["source_timestamp"] is None
