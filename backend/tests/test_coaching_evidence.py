"""Evidence and provenance contracts for personalised coaching replies."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from coaching_evidence import (
    GroundingStatus,
    build_evidence_bundle,
    classify_freshness,
    resolve_grounding,
    validate_citations,
)

AS_OF = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)


def _bundle(*, window_start: date = date(2026, 3, 1), window_end: date = date(2026, 3, 20)):
    return build_evidence_bundle(
        user_id="user-a",
        logs=[
            {
                "id": 1,
                "date": "2026-03-18",
                "focus": "Back, Deadlifts & Chest",
                "carb_tier": "high",
            },
            {
                "id": 99,
                "date": "2026-01-01",
                "focus": "Out of window",
                "carb_tier": "low",
            },
        ],
        history={
            "Deadlift (Barbell)": [
                {
                    "id": 7,
                    "date": "2026-03-17",
                    "top_weight_kg": 140.0,
                    "top_reps": 5,
                    "sets": 4,
                },
                {
                    "id": 8,
                    "date": "2025-12-01",
                    "top_weight_kg": 100.0,
                    "top_reps": 5,
                    "sets": 3,
                },
            ],
        },
        biometrics=[
            {"id": 3, "date": "2026-03-19", "weight_kg": 82.0, "body_fat_pct": 16.0},
        ],
        as_of=AS_OF,
        window_start=window_start,
        window_end=window_end,
    )


def test_bundle_attaches_record_provenance_and_excludes_unavailable_dates() -> None:
    bundle = _bundle()

    assert bundle.user_id == "user-a"
    assert bundle.window_start == "2026-03-01"
    assert bundle.window_end == "2026-03-20"
    assert bundle.transformation_version == "coaching-context-v1"

    summaries = " ".join(item.summary for item in bundle.items)
    assert "Out of window" not in summaries
    assert "100.0kg" not in summaries
    assert {item.evidence_id for item in bundle.items} == {"E1", "E2", "E3"}
    for item in bundle.items:
        assert item.record_id.startswith(f"{item.source}:")
        assert item.date >= "2026-03-01"
        assert item.date <= "2026-03-20"
        assert item.freshness in {"fresh", "aging", "stale", "unknown"}
        assert item.transformation_version.endswith("-v1")


def test_freshness_classification_is_deterministic() -> None:
    assert classify_freshness(date(2026, 3, 19), as_of=AS_OF).value == "fresh"
    assert classify_freshness(date(2026, 3, 1), as_of=AS_OF).value == "aging"
    assert classify_freshness(date(2025, 12, 1), as_of=AS_OF).value == "stale"
    assert classify_freshness(date(2026, 3, 25), as_of=AS_OF).value == "unknown"


def test_validate_citations_strips_ids_outside_the_bundle() -> None:
    bundle = _bundle()
    text = "Deadlift is progressing [E1]. Another user's record says otherwise [E9]."

    sanitized, cited = validate_citations(text, bundle)

    assert "[E1]" in sanitized
    assert "[E9]" not in sanitized
    assert cited == ["E1"]
    assert resolve_grounding(sanitized, bundle, cited) is GroundingStatus.OBSERVED


def test_grounding_labels_unknown_and_inferred_replies() -> None:
    empty = build_evidence_bundle(
        user_id="user-a",
        as_of=AS_OF,
        window_start=date(2026, 3, 1),
        window_end=date(2026, 3, 20),
    )
    assert resolve_grounding("No data available.", empty, []) is GroundingStatus.UNKNOWN

    bundle = _bundle()
    assert resolve_grounding("You should deload.", bundle, []) is GroundingStatus.INFERRED
    assert resolve_grounding("", bundle, []) is GroundingStatus.INCOMPLETE


def test_minimum_window_end_does_not_overflow() -> None:
    """The default 90-day window must tolerate the earliest representable date."""
    bundle = build_evidence_bundle(user_id="user-a", as_of=AS_OF, window_end=date.min)

    assert bundle.window_start == date.min.isoformat()
    assert bundle.window_end == date.min.isoformat()
    assert bundle.items == ()


def test_stale_evidence_is_retained_but_labelled() -> None:
    recent = _bundle(
        window_start=date(2025, 1, 1),
        window_end=date(2026, 3, 20),
    )
    stale = [item for item in recent.items if item.freshness == "stale"]
    assert stale
    assert all(item.date < "2026-02-18" for item in stale)
    assert all(timedelta(0) <= AS_OF.date() - date.fromisoformat(item.date) for item in recent.items)
