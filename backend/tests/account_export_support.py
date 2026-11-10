"""Synthetic two-tenant data for the account export tests.

Every value is fictional. Each tenant's visible values contain its tag, and each
secret contains ``-secret-``, so tests can prove that one account's export holds
none of another account's records and none of anyone's credentials.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from database import (
    advance_day,
    get_or_create_user,
    save_body_metrics,
    save_chat_message,
    save_checkin,
    save_daily_log,
    save_dashboard_insight,
    save_deep_correlation,
    save_notification,
    save_progress,
    save_push_subscription,
    save_reasoning_log,
    save_routine_record,
    save_user_api_key,
    save_user_preferences,
    save_workout,
    set_active_programme,
    set_meta,
)
from hevy_parser import ExerciseSummary, WorkoutSummary

SESSION_SECRET = "account-export-test-secret-that-is-long-enough"


def secret_values(tag: str) -> list[str]:
    """Every credential-like value seeded for *tag*; none may reach an export."""

    return [
        f"{tag}-secret-hevy-key",
        f"{tag}-secret-client",
        f"{tag}-secret-refresh",
        f"{tag}-secret-hidden-extra",
        f"{tag}-secret-preview-token",
        f"{tag}-secret-nested-token",
        f"{tag}-secret-google-refresh",
        f"{tag}-secret-oauth-state",
        f"{tag}-secret-endpoint-path",
        f"{tag}-secret-p256dh",
        f"{tag}-secret-push-auth",
    ]


def seed_tenant(db_path: str, *, email: str, display_name: str, tag: str) -> str:
    """Populate every exported table for one synthetic account; return its id."""

    uid = get_or_create_user(email, display_name, db_path=db_path)["id"]
    save_user_preferences(
        uid,
        goals=[f"{tag}-goal"],
        constraints=[f"{tag}-constraint"],
        experience_level="advanced",
        coaching_style="direct",
        preferred_ai="claude",
        ai_model=f"{tag}-model",
        custom_rules=[f"{tag}-rule"],
        db_path=db_path,
    )
    save_user_api_key(
        uid,
        "hevy",
        f"{tag}-secret-hevy-key",
        client_id=f"{tag}-client-id",
        client_secret=f"{tag}-secret-client",
        refresh_token=f"{tag}-secret-refresh",
        extra={"model": f"{tag}-connector-model", "note": f"{tag}-secret-hidden-extra"},
        db_path=db_path,
    )
    set_active_programme(
        uid,
        source="hevy",
        template_key=f"hevy:{tag}",
        definition={
            "name": f"{tag} programme",
            "engine_version": "hevy-programme-v1",
            "preview_token": f"{tag}-secret-preview-token",
            "days": [{"title": f"{tag}-day-one"}],
        },
        db_path=db_path,
    )
    advance_day(db_path, user_id=uid)
    save_workout(
        {
            "title": f"{tag}-workout",
            "exercises": [
                {"title": f"{tag}-squat", "access_token": f"{tag}-secret-nested-token"}
            ],
        },
        db_path,
        "2026-10-01",
        user_id=uid,
    )
    save_progress(
        WorkoutSummary(
            title=f"{tag}-session",
            date="2026-10-01",
            duration_seconds=3600,
            total_volume_kg=2400.0,
            exercises=[ExerciseSummary(f"{tag}-squat", 100.0, 5, 3)],
        ),
        db_path,
        user_id=uid,
    )
    save_body_metrics(
        {
            "weight_kg": 80.5,
            "body_fat_pct": 15.0,
            "muscle_pct": 40.0,
            "resting_hr": 55,
            "hrv": 70.0,
        },
        "2026-10-01",
        db_path,
        user_id=uid,
    )
    save_checkin(1, "2026-10-02", 12, 4, f"{tag}-checkin-message", db_path, user_id=uid)
    save_notification(
        uid,
        f"{tag}-notification-title",
        f"{tag}-notification-message",
        type="coach",
        link="/dashboard",
        db_path=db_path,
    )
    save_chat_message("user", f"{tag}-chat-question", db_path, user_id=uid)
    save_chat_message("assistant", f"{tag}-chat-answer", db_path, user_id=uid)
    save_reasoning_log(
        f"2026-10-01_{tag}-squat",
        f"{tag}-squat",
        f"{tag}-reasoning",
        db_path=db_path,
        user_id=uid,
    )
    save_dashboard_insight(
        json.dumps(
            {
                "fatigue": f"{tag}-fatigue",
                "wins_stalls": f"{tag}-wins",
                "advice": f"{tag}-advice",
            }
        ),
        db_path=db_path,
        user_id=uid,
    )
    save_deep_correlation(f"# {tag}-correlation report", db_path=db_path, user_id=uid)
    save_daily_log(
        "2026-10-03",
        1,
        f"{tag}-focus",
        "high",
        f"{tag}-plan",
        f"{tag}-lifestyle",
        db_path,
        user_id=uid,
    )
    save_routine_record(
        f"{tag}-routine-key",
        f"{tag}-routine-id",
        f"{tag}-routine-hash",
        db_path,
        user_id=uid,
    )
    set_meta("programme_start_date", "2026-09-01", db_path, user_id=uid)
    set_meta("routine_folder_id", f"{tag}-folder", db_path, user_id=uid)
    set_meta("google_health_refresh_token", f"{tag}-secret-google-refresh", db_path, user_id=uid)
    set_meta("google_health_oauth_state", f"{tag}-secret-oauth-state", db_path, user_id=uid)
    save_push_subscription(
        uid,
        f"https://push.example.com/send/{tag}-secret-endpoint-path",
        f"{tag}-secret-p256dh",
        f"{tag}-secret-push-auth",
        db_path,
    )
    return uid


def archive_text(archive_path: Path) -> dict[str, str]:
    """Return every archive member's decoded text, verifying the CRCs."""

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def archive_json(archive_path: Path) -> dict[str, Any]:
    return {name: json.loads(text) for name, text in archive_text(archive_path).items()}
