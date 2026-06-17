"""Tests for reading recovery metrics from a Health Connect export file."""

from __future__ import annotations

from health_connect import body_metrics_from_recovery, read_recovery_metrics


def test_missing_path_returns_none():
    assert read_recovery_metrics(None) is None


def test_nonexistent_file_returns_none(tmp_path):
    assert read_recovery_metrics(str(tmp_path / "nope.json")) is None


def test_bad_json_returns_none(tmp_path):
    path = tmp_path / "recovery.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_recovery_metrics(str(path)) is None


def test_non_object_json_returns_none(tmp_path):
    path = tmp_path / "recovery.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_recovery_metrics(str(path)) is None


def test_valid_file_returns_dict(tmp_path):
    path = tmp_path / "recovery.json"
    path.write_text(
        '{"date": "2026-06-17", "sleep_hours": 7.5, "weight_kg": 82.0}',
        encoding="utf-8",
    )
    data = read_recovery_metrics(str(path))
    assert data == {"date": "2026-06-17", "sleep_hours": 7.5, "weight_kg": 82.0}


def test_body_metrics_none_without_recovery():
    assert body_metrics_from_recovery(None) is None


def test_body_metrics_none_without_composition_fields():
    # Sleep and resting HR alone are not body composition.
    assert body_metrics_from_recovery({"sleep_hours": 7.5, "resting_hr": 58}) is None


def test_body_metrics_extracted_from_scale_reading():
    recovery = {
        "weight_kg": 82.0,
        "body_fat_pct": 14.2,
        "muscle_pct": 47.5,
        "resting_hr": 58,
        "sleep_hours": 7.5,
    }
    metrics = body_metrics_from_recovery(recovery)
    assert metrics == {
        "weight_kg": 82.0,
        "body_fat_pct": 14.2,
        "muscle_pct": 47.5,
        "resting_hr": 58,
    }
