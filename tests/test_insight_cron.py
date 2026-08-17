"""Tests for insight_cron.py: daily insight header and weekly deep correlations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
        daily_logs_calls.append(1)
        return [{"date": "2026-08-01"}]

    monkeypatch.setattr("insight_cron.get_body_metrics", _fake_body_metrics)
    monkeypatch.setattr("insight_cron.get_daily_logs", _fake_daily_logs)

    saved: list[str] = []
    monkeypatch.setattr(
        "insight_cron.save_dashboard_insight",
        lambda payload, **kw: saved.append(payload),
    )

    generate_daily_header(config)  # type: ignore[arg-type]
    assert len(saved) == 1


# ---------------------------------------------------------------------------
# generate_weekly_correlations
# ---------------------------------------------------------------------------


def test_generate_weekly_correlations_saves_markdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _FakeConfig(tmp_path)

    fake_provider = MagicMock()
    fake_provider.generate.return_value = (
        "## Correlation Report\n\nPull-ups stall after poor sleep."
    )
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr(
        "insight_cron._resolve_insight_user", lambda path: "test-user-id"
    )
    monkeypatch.setattr("insight_cron.resolve_provider", lambda **kw: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_progress_history", lambda **kw: {})

    saved: list[str] = []
    monkeypatch.setattr(
        "insight_cron.save_deep_correlation",
        lambda markdown, **kw: saved.append(markdown),
    )

    generate_weekly_correlations(config)  # type: ignore[arg-type]
    assert len(saved) == 1
    assert "Pull-ups stall" in saved[0]


def test_generate_weekly_correlations_empty_response_not_saved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _FakeConfig(tmp_path)

    fake_provider = MagicMock()
    fake_provider.generate.return_value = ""
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr(
        "insight_cron._resolve_insight_user", lambda path: "test-user-id"
    )
    monkeypatch.setattr("insight_cron.resolve_provider", lambda **kw: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_progress_history", lambda **kw: {})

    saved: list[str] = []
    monkeypatch.setattr(
        "insight_cron.save_deep_correlation",
        lambda markdown, **kw: saved.append(markdown),
    )

    generate_weekly_correlations(config)  # type: ignore[arg-type]
    assert len(saved) == 0


def test_generate_weekly_correlations_exception_is_handled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _FakeConfig(tmp_path)

    fake_provider = MagicMock()
    fake_provider.generate.side_effect = RuntimeError("Boom")
    monkeypatch.setattr(
        "insight_cron._resolve_insight_user", lambda path: "test-user-id"
    )
    monkeypatch.setattr("insight_cron.resolve_provider", lambda **kw: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_progress_history", lambda **kw: {})

    # Should not raise
    generate_weekly_correlations(config)  # type: ignore[arg-type]


def test_generate_weekly_correlations_filters_history_by_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _FakeConfig(tmp_path)

    fake_provider = MagicMock()
    fake_provider.generate.return_value = "## Report\nLooking good."
    fake_provider.name.return_value = "Gemini (test)"
    monkeypatch.setattr(
        "insight_cron._resolve_insight_user", lambda path: "test-user-id"
    )
    monkeypatch.setattr("insight_cron.resolve_provider", lambda **kw: fake_provider)
    monkeypatch.setattr("insight_cron.get_body_metrics", lambda **kw: [])
    monkeypatch.setattr("insight_cron.get_daily_logs", lambda **kw: [])

    # All history entries are newer than 60 days ago, should all be included
    monkeypatch.setattr(
        "insight_cron.get_progress_history",
        lambda **kw: {
            "Deadlift": [{"date": "2026-08-01", "top_weight_kg": 140, "top_reps": 5}],
        },
    )

    saved: list[str] = []
    monkeypatch.setattr(
        "insight_cron.save_deep_correlation",
        lambda markdown, **kw: saved.append(markdown),
    )

    generate_weekly_correlations(config)  # type: ignore[arg-type]
    assert len(saved) == 1
