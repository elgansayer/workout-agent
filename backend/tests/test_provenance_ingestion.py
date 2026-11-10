"""Provenance is captured where records enter the system (importers, generators)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import checkin
import google_health_client
import insight_cron
import main
import pytest
import requests
from config import Config
from database import (
    get_body_metrics,
    get_daily_logs,
    get_or_create_user,
    get_record_lineage,
    get_record_provenance,
    init_db,
)
from gemini_engine import (
    generate_checkin_message,
    generate_next_workout,
    generate_rest_day_message,
)
from hevy_parser import parse_workout, workout_identity
from program import BLOCKS
from provenance import GeneratedText, RecordType

# ---------------------------------------------------------------------------
# Hevy identity
# ---------------------------------------------------------------------------


def _hevy_payload(workout_id: Any = "hevy-1") -> dict[str, Any]:
    return {
        "workouts": [
            {
                "id": workout_id,
                "title": "Legs",
                "start_time": "2026-06-17T07:00:00Z",
                "end_time": "2026-06-17T08:00:00Z",
                "exercises": [{"title": "Squat", "sets": [{"weight_kg": 100, "reps": 5}]}],
            }
        ]
    }


def test_workout_identity_reads_the_provider_id_and_start_time() -> None:
    assert workout_identity(_hevy_payload()) == ("hevy-1", "2026-06-17T07:00:00Z")
    assert workout_identity(_hevy_payload(12345))[0] == "12345"
    assert workout_identity(_hevy_payload(""))[0] is None
    assert workout_identity(_hevy_payload(None))[0] is None
    assert workout_identity(None) == (None, None)
    assert workout_identity({"workouts": []}) == (None, None)


def test_parsed_summary_carries_the_workout_id() -> None:
    summary = parse_workout(_hevy_payload())
    assert summary is not None and summary.workout_id == "hevy-1"
    legacy = parse_workout(_hevy_payload(None))
    assert legacy is not None and legacy.workout_id is None


# ---------------------------------------------------------------------------
# Google Health sample times
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _points(key: str, field: str, samples: list[tuple[str, float]]) -> dict[str, Any]:
    return {
        "dataPoints": [
            {key: {field: value, "sampleTime": {"physicalTime": when}}} for when, value in samples
        ]
    }


def test_google_health_readings_expose_the_sample_time_of_each_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs: Any) -> _Response:
        if "body-fat" in url:
            return _Response(_points("bodyFat", "percentage", [("2026-06-17T08:05:00Z", 14.6)]))
        if "weight" in url:
            return _Response(
                _points(
                    "weight",
                    "weightGrams",
                    [("2026-06-15T08:00:00Z", 83000), ("2026-06-17T08:00:00Z", 81500)],
                )
            )
        return _Response({"dataPoints": []})

    monkeypatch.setattr(requests, "get", fake_get)

    metrics = google_health_client.fetch_body_metrics("token")

    assert metrics == {"weight_kg": 81.5, "body_fat_pct": 14.6}  # still a plain metrics dict
    assert isinstance(metrics, google_health_client.BodyMetricsReading)
    assert metrics.sample_times == {
        "weight_kg": "2026-06-17T08:00:00Z",
        "body_fat_pct": "2026-06-17T08:05:00Z",
    }
    assert json.loads(json.dumps(metrics)) == {"weight_kg": 81.5, "body_fat_pct": 14.6}


def test_google_health_without_samples_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", lambda url, **kw: _Response({"dataPoints": []}))
    assert google_health_client.fetch_body_metrics("token") is None


# ---------------------------------------------------------------------------
# Generators tag how their output was produced
# ---------------------------------------------------------------------------


def _provider(text: str = "AI plan", *, fail: bool = False) -> MagicMock:
    provider = MagicMock()
    provider.name.return_value = "Gemini (gemini-test)"
    if fail:
        provider.generate.side_effect = RuntimeError("boom")
    else:
        provider.generate.return_value = text
    return provider


@pytest.mark.parametrize("empty", [False, True])
def test_ai_generated_text_names_the_model_and_fallback_text_does_not(empty: bool) -> None:
    ai = generate_next_workout(_provider(), day=1, week=1, block=BLOCKS[1])
    assert ai == "AI plan" and ai.generator == "Gemini (gemini-test)" and not ai.from_fallback
    rest = generate_rest_day_message(_provider("Rest well"))
    assert rest.generator == "Gemini (gemini-test)"
    note = generate_checkin_message(
        _provider("Check-in text"), 1, 2, BLOCKS[1], 10, 4, "analysis", "fallback"
    )
    assert note.generator == "Gemini (gemini-test)"

    failing = _provider("" if empty else "x", fail=not empty)
    fallbacks = [
        generate_next_workout(failing, day=1, week=1, block=BLOCKS[1]),
        generate_rest_day_message(failing),
        generate_checkin_message(failing, 1, 2, BLOCKS[1], 10, 4, "analysis", "fallback text"),
    ]
    for text in fallbacks:
        assert isinstance(text, GeneratedText) and text.from_fallback and text.generator is None
    assert fallbacks[2] == "fallback text"


# ---------------------------------------------------------------------------
# main._body_metrics_provenance
# ---------------------------------------------------------------------------


def test_live_google_health_values_are_attributed_field_by_field() -> None:
    file_data = {"date": "2026-06-17", "weight_kg": 80.0, "muscle_pct": 40.0}
    synced = google_health_client.BodyMetricsReading(
        {"weight_kg": 81.5, "body_fat_pct": 14.6},
        {"weight_kg": "2026-06-17T08:00:00Z", "body_fat_pct": "2026-06-17T08:05:00Z"},
    )
    body = {**file_data, **synced}
    body.pop("date")

    provenance, field_sources = main._body_metrics_provenance(file_data, synced, body)

    assert provenance is not None
    assert provenance.source == "google_health"
    assert provenance.transformation_version == google_health_client.GOOGLE_HEALTH_TRANSFORM_VERSION
    assert provenance.source_timestamp == "2026-06-17T08:05:00+00:00"  # newest sample used
    assert field_sources == {
        "weight_kg": "google_health",
        "muscle_pct": "health_connect_file",
        "body_fat_pct": "google_health",
    }


def test_exported_file_values_are_attributed_to_the_file_with_its_date() -> None:
    body = {"weight_kg": 80.0}
    provenance, field_sources = main._body_metrics_provenance(
        {"date": "2026-06-17", "weight_kg": 80.0}, None, body
    )
    assert provenance is not None
    assert provenance.source == "health_connect_file"
    assert provenance.source_timestamp == "2026-06-17"
    assert provenance.external_id is None
    assert field_sources == {"weight_kg": "health_connect_file"}


def test_plain_dict_sync_results_are_still_attributed_without_sample_times() -> None:
    provenance, _ = main._body_metrics_provenance(None, {"weight_kg": 81.0}, {"weight_kg": 81.0})
    assert provenance is not None
    assert provenance.source == "google_health" and provenance.source_timestamp is None


def test_nothing_to_save_means_no_provenance() -> None:
    assert main._body_metrics_provenance({"date": "2026-06-17"}, None, None) == (None, {})


# ---------------------------------------------------------------------------
# End to end: the daily run records provenance for what it stores
# ---------------------------------------------------------------------------


def _run_daily_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, plan: Any, synced: Any
) -> tuple[str, str]:
    db = str(tmp_path / "daily.db")
    init_db(db)
    user_id = get_or_create_user("runner@example.com", "Runner", db_path=db)["id"]
    config = Config(
        hevy_api_key="hevy-key",
        gemini_api_key="g",
        gemini_model="gemini-test",
        health_connect_file=None,
        database_path=db,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )
    monkeypatch.setattr("main.Config.load", lambda: config)
    monkeypatch.setattr("main._resolve_run_user", lambda path: user_id)
    monkeypatch.setattr("main._scope_config_to_user", lambda cfg, uid: cfg)
    monkeypatch.setattr("main._resolve_provider", lambda cfg, **kw: _provider())
    monkeypatch.setattr("main._deliver", lambda *a, **kw: 0)
    monkeypatch.setattr("main.sync_routines", lambda cfg, **kw: [])
    monkeypatch.setattr(
        "main.datetime",
        MagicMock(now=lambda tz=None: datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr("main.get_programme_start_date", lambda *a, **kw: date(2026, 8, 3))
    monkeypatch.setattr(
        "main.read_recovery_metrics",
        lambda *a, **kw: {"date": "2026-08-03", "weight_kg": 82.0, "muscle_pct": 41.0},
    )
    monkeypatch.setattr("main.google_health_client.sync_body_metrics", lambda *a: synced)
    monkeypatch.setattr("main.fetch_latest_workout", lambda key: _hevy_payload("hevy-run-1"))
    monkeypatch.setattr("main.generate_next_workout", lambda *a, **kw: plan)
    assert main.run(preview=False) == 0
    return db, user_id


def test_daily_run_records_source_ai_and_hevy_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synced = google_health_client.BodyMetricsReading(
        {"weight_kg": 81.5}, {"weight_kg": "2026-08-03T06:30:00Z"}
    )
    plan = GeneratedText.from_provider("Back day\nDeadlift: 4 x 8", _provider())

    db, user_id = _run_daily_cycle(monkeypatch, tmp_path, plan=plan, synced=synced)

    # Imported body metrics: source, per-field sources and the provider sample time.
    reading = get_body_metrics(db_path=db, user_id=user_id, include_provenance=True)[0]
    assert reading["weight_kg"] == 81.5 and reading["muscle_pct"] == 41.0
    assert reading["provenance"]["kind"] == "imported"
    assert reading["provenance"]["source"] == "google_health"
    assert reading["provenance"]["source_timestamp"] == "2026-08-03T06:30:00+00:00"
    [revision] = get_record_lineage(user_id, RecordType.BODY_METRIC, "2026-08-03", db)
    assert revision["content"]["field_sources"]["muscle_pct"] == "health_connect_file"

    # Imported Hevy workout and the progress rows parsed from it.
    for family in (RecordType.WORKOUT, RecordType.EXERCISE_PROGRESS):
        view = get_record_provenance(user_id, family, "hevy-run-1", db)
        assert view is not None, family
        assert (view["kind"], view["source"], view["external_id"]) == ("imported", "hevy", "hevy-run-1")
        assert view["transformation_version"] == "hevy-parser/1"
        assert view["source_timestamp"] == "2026-06-17T07:00:00+00:00"

    # The generated plan is a derived AI recommendation naming the model.
    [log] = get_daily_logs(limit=5, db_path=db, user_id=user_id, include_provenance=True)
    assert log["provenance"]["kind"] == "derived"
    assert log["provenance"]["source"] == "ai:gemini/gemini-test"
    assert log["provenance"]["transformation_version"] == "daily-plan/1"
    assert log["provenance"]["authoritative"] is False


def test_fallback_plans_are_never_attributed_to_an_ai_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = GeneratedText.fallback("Back day\nDeadlift: 4 x 8")
    db, user_id = _run_daily_cycle(monkeypatch, tmp_path, plan=plan, synced=None)
    [log] = get_daily_logs(limit=5, db_path=db, user_id=user_id, include_provenance=True)
    assert log["provenance"]["source"] == "rules:fallback"
    assert log["provenance"]["label"] == "Calculated by rules"
    reading = get_body_metrics(db_path=db, user_id=user_id, include_provenance=True)[0]
    assert reading["provenance"]["source"] == "health_connect_file"
    assert reading["provenance"]["source_timestamp"] == "2026-08-03"


def test_a_user_correction_survives_the_next_daily_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from database import correct_body_metrics

    synced = google_health_client.BodyMetricsReading({"weight_kg": 91.0}, {})
    db, user_id = _run_daily_cycle(
        monkeypatch, tmp_path, plan=GeneratedText.fallback("Plan"), synced=synced
    )
    correct_body_metrics(user_id, "2026-08-03", {"weight_kg": 81.0}, db_path=db)

    assert main.run(preview=False) == 0  # the scheduled run imports the same bad reading again

    assert get_body_metrics(db_path=db, user_id=user_id)[0]["weight_kg"] == 81.0
    chain = get_record_lineage(user_id, RecordType.BODY_METRIC, "2026-08-03", db)
    assert [(c["kind"], c["applied"]) for c in chain] == [
        ("imported", False),
        ("user_entered", True),
        ("imported", True),
    ]


# ---------------------------------------------------------------------------
# Check-ins and insight generation record derived provenance
# ---------------------------------------------------------------------------


def _checkin_config(tmp_path: Path) -> Config:
    db = str(tmp_path / "checkin.db")
    init_db(db)
    return Config(
        hevy_api_key=None,
        gemini_api_key="g",
        gemini_model="m",
        health_connect_file=None,
        database_path=db,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=True,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )


@pytest.mark.parametrize(
    ("message", "source", "label"),
    [
        (GeneratedText.from_provider("Check-in 1", _provider()), "ai:gemini/gemini-test", "AI recommendation"),
        (GeneratedText.fallback("Check-in 1"), "rules:fallback", "Calculated by rules"),
        ("Check-in 1", "unattributed", "Derived by the app"),
    ],
)
def test_check_ins_are_recorded_as_derived_coaching_summaries(
    tmp_path: Path, message: str, source: str, label: str
) -> None:
    config = _checkin_config(tmp_path)
    user_id = get_or_create_user("c@example.com", "C", db_path=config.database_path)["id"]
    checkin.record(
        config,
        checkin.CheckinDue(1, 12, 4, 12),
        message,
        today=date(2026, 9, 1),
        user_id=user_id,
    )
    view = get_record_provenance(user_id, RecordType.CHECK_IN, "1", config.database_path)
    assert view is not None
    assert (view["kind"], view["source"], view["label"]) == ("derived", source, label)
    assert view["transformation_version"] == "checkin/1" and view["authoritative"] is False


def test_insight_generation_records_the_model_and_labels_its_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from database import correct_body_metrics, save_body_metrics, save_daily_log
    from provenance import Provenance

    config = _checkin_config(tmp_path)
    db = config.database_path
    user_id = get_or_create_user("i@example.com", "I", db_path=db)["id"]
    today = datetime.now(tz=timezone.utc).date().isoformat()
    save_body_metrics(
        {"weight_kg": 91.0}, today, db, user_id=user_id,
        provenance=Provenance.imported("google_health", "google-health-body/1"),
    )
    correct_body_metrics(user_id, today, {"weight_kg": 81.0}, reason="PRIVATE-NOTE", db_path=db)
    save_daily_log(
        today, 1, "Legs", "high", "Yesterday's AI plan", "life", db, user_id=user_id,
        provenance=Provenance.derived("ai:gemini/old", "daily-plan/1"),
    )
    provider = _provider(
        json.dumps({"fatigue": "Green", "wins_stalls": "Steady", "advice": "Go"})
    )
    monkeypatch.setattr(insight_cron, "resolve_provider", lambda **kw: provider)

    insight_cron.generate_daily_header(config, user_id=user_id)

    [prompt] = [call.args[0] for call in provider.generate.call_args_list]
    assert "Provenance rules:" in prompt
    assert '"origin": "user_entered"' in prompt and '"corrected_by_user": true' in prompt
    assert '"origin": "derived"' in prompt
    assert "PRIVATE-NOTE" not in prompt
    view = get_record_provenance(user_id, RecordType.DASHBOARD_INSIGHT, "current", db)
    assert view is not None
    assert (view["kind"], view["source"]) == ("derived", "ai:gemini/gemini-test")
    assert view["transformation_version"] == "dashboard-insight/1"

    provider.generate.reset_mock()
    provider.generate.return_value = "# Report"
    insight_cron.generate_weekly_correlations(config, user_id=user_id)
    view = get_record_provenance(user_id, RecordType.DEEP_CORRELATION, "current", db)
    assert view is not None and view["transformation_version"] == "deep-correlation/1"
    weekly_prompt = provider.generate.call_args.args[0]
    assert "Provenance rules:" in weekly_prompt and "exercise_history_provenance" in weekly_prompt
