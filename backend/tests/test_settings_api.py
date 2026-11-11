"""HTTP contract and page consumers for per-user regional/accessibility settings."""

from __future__ import annotations

import base64
import importlib
import json
from collections.abc import Callable, Generator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("authlib")
pytest.importorskip("httpx")

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from session_helpers import signed_session_cookie
from starlette.middleware.sessions import SessionMiddleware

from database import (
    get_legacy_user_id,
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_progress,
)
from hevy_parser import ExerciseSummary, WorkoutSummary
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from user_settings import UserSettings, get_user_settings, update_user_settings

SECRET = "settings-api-test-secret-that-is-long-enough"


@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    db_path = str(tmp_path / "settings-api.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_HEALTH_CLIENT_SECRET", raising=False)
    import webapp.app as module

    importlib.reload(module)
    module._RATE_LIMITS.clear()
    yield module


def _session_app(module: Any) -> FastAPI:
    """The real handlers behind a signed-session boundary, one login per user."""
    app = FastAPI()

    @app.get("/test/session/{user_id}")
    def login(user_id: str, request: Request) -> dict[str, str]:
        request.session["user"] = f"{user_id}@example.test"
        request.session["user_id"] = user_id
        return {"status": "ok"}

    routes: list[tuple[str, Callable[..., Any], list[str]]] = [
        ("/api/settings/display", module.api_display_settings, ["GET"]),
        ("/api/settings/display", module.save_display_settings, ["POST"]),
        ("/api/progress", module.api_progress, ["GET"]),
        ("/api/stats", module.api_stats, ["GET"]),
        ("/api/dashboard", module.api_dashboard, ["GET"]),
    ]
    for path, handler, methods in routes:
        app.add_api_route(path, handler, methods=methods)
    app.add_middleware(SessionMiddleware, secret_key=SECRET)
    return app


def _client_for(module: Any, user_id: str | None) -> TestClient:
    client = TestClient(_session_app(module))
    if user_id is not None:
        assert client.get(f"/test/session/{user_id}").status_code == 200
    return client


def _workout(started_at: str, name: str, weight: float, reps: int = 5) -> WorkoutSummary:
    return WorkoutSummary(
        title="Synthetic session",
        date=started_at,
        exercises=[ExerciseSummary(name, weight, reps, 3)],
    )


def _new_user(module: Any, email: str) -> str:
    return get_or_create_user(email, "Synthetic", module.DB_PATH)["id"]


# ---------------------------------------------------------------------------
# GET / POST contract
# ---------------------------------------------------------------------------


def test_get_returns_defaults_and_every_accepted_option(app_module: Any) -> None:
    uid = _new_user(app_module, "defaults@example.test")
    body = _client_for(app_module, uid).get("/api/settings/display").json()

    assert body["settings"] == UserSettings().as_dict()
    assert "Europe/London" in body["options"]["timezones"]
    assert body["options"]["locales"][0]["value"] == "en-GB"
    assert body["options"]["defaults"] == body["settings"]


def test_get_works_in_the_read_only_anonymous_dev_mode(app_module: Any) -> None:
    with TestClient(app_module.app) as client:
        response = client.get("/api/settings/display")
    assert response.status_code == 200
    assert response.json()["settings"] == UserSettings().as_dict()


def test_post_persists_and_reports_what_changed(app_module: Any) -> None:
    uid = _new_user(app_module, "save@example.test")
    client = _client_for(app_module, uid)

    response = client.post(
        "/api/settings/display",
        json={"timezone": "America/New_York", "unit_system": "imperial", "text_size": "large"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["changed"]) == {"timezone", "unit_system", "text_size"}
    assert body["settings"]["timezone"] == "America/New_York"
    assert client.get("/api/settings/display").json()["settings"] == body["settings"]
    assert get_user_settings(uid, app_module.DB_PATH).unit_system == "imperial"


def test_post_only_ever_touches_the_authenticated_account(app_module: Any) -> None:
    alice = _new_user(app_module, "alice@example.test")
    bob = _new_user(app_module, "bob@example.test")

    _client_for(app_module, alice).post(
        "/api/settings/display", json={"unit_system": "imperial", "timezone": "Asia/Tokyo"}
    )

    assert _client_for(app_module, bob).get("/api/settings/display").json()["settings"] == (
        UserSettings().as_dict()
    )
    assert get_user_settings(bob, app_module.DB_PATH) == UserSettings()


def test_post_rejects_unsupported_locale_with_the_supported_list(app_module: Any) -> None:
    uid = _new_user(app_module, "locale@example.test")
    response = _client_for(app_module, uid).post(
        "/api/settings/display", json={"locale": "fr-FR"}
    )
    assert response.status_code == 400
    assert "Unsupported locale" in response.json()["detail"]
    assert "en-GB" in response.json()["detail"]
    assert get_user_settings(uid, app_module.DB_PATH).locale == "en-GB"


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"timezone": "Mars/Base"}, "time zone"),
        ({"unit_system": "stone"}, "unit system"),
        ({"motion": "warp"}, "motion"),
        ({"contrast": "extreme"}, "contrast"),
        ({"text_size": "huge"}, "text size"),
        ({"timezone": None}, "time zone"),
        ({}, "No settings"),
    ],
)
def test_post_rejects_invalid_values_with_a_400(
    app_module: Any, payload: dict[str, Any], needle: str
) -> None:
    uid = _new_user(app_module, "bad@example.test")
    response = _client_for(app_module, uid).post("/api/settings/display", json=payload)
    assert response.status_code == 400
    assert needle in response.json()["detail"]
    assert get_user_settings(uid, app_module.DB_PATH) == UserSettings()


def test_post_rejects_unknown_fields_including_owner_claims(app_module: Any) -> None:
    uid = _new_user(app_module, "extra@example.test")
    client = _client_for(app_module, uid)

    assert client.post("/api/settings/display", json={"theme": "dark"}).status_code == 422
    # An owner id is never an input: a claim for someone else is a 404 from the
    # mutation boundary and even a matching claim is not an accepted field.
    assert client.post("/api/settings/display", json={"user_id": "someone-else"}).status_code == 404
    assert client.post("/api/settings/display", json={"user_id": uid}).status_code == 422
    assert get_user_settings(uid, app_module.DB_PATH) == UserSettings()


def test_post_rejects_non_string_values(app_module: Any) -> None:
    uid = _new_user(app_module, "types@example.test")
    response = _client_for(app_module, uid).post("/api/settings/display", json={"timezone": 5})
    assert response.status_code == 422


def test_anonymous_callers_cannot_change_settings(app_module: Any) -> None:
    response = _client_for(app_module, None).post(
        "/api/settings/display", json={"unit_system": "imperial"}
    )
    assert response.status_code == 401


def test_settings_for_a_vanished_account_are_not_created(app_module: Any) -> None:
    response = _client_for(app_module, "ghost-account").post(
        "/api/settings/display", json={"unit_system": "imperial"}
    )
    # Fails closed: a session naming a missing account is simply unauthorised.
    assert response.status_code == 401


def test_changing_time_zone_reports_how_many_days_were_regrouped(app_module: Any) -> None:
    uid = _new_user(app_module, "regroup@example.test")
    save_progress(_workout("2026-03-02T20:30:00+00:00", "Squat", 100.0), app_module.DB_PATH, user_id=uid)

    response = _client_for(app_module, uid).post(
        "/api/settings/display", json={"timezone": "Pacific/Auckland"}
    )

    assert response.json()["regrouped_days"] == 1


# ---------------------------------------------------------------------------
# Pages use the owner's units
# ---------------------------------------------------------------------------


def _seed_lifts(module: Any, user_id: str) -> None:
    save_progress(_workout("2026-03-02T09:00:00+00:00", "Deadlift", 140.0), module.DB_PATH, user_id=user_id)
    save_progress(_workout("2026-03-09T09:00:00+00:00", "Deadlift", 150.0), module.DB_PATH, user_id=user_id)
    save_body_metrics({"weight_kg": 82.0}, "2026-03-02", module.DB_PATH, user_id=user_id)
    save_body_metrics({"weight_kg": 81.0}, "2026-03-09", module.DB_PATH, user_id=user_id)


def test_progress_page_data_is_metric_by_default(app_module: Any) -> None:
    uid = _new_user(app_module, "metric@example.test")
    _seed_lifts(app_module, uid)

    body = _client_for(app_module, uid).get("/api/progress").json()

    assert body["weight_unit"] == "kg"
    chart = body["charts"][0]
    assert chart["name"] == "Deadlift"
    assert " kg" in chart["svg"] and "150 kg x 5" in chart["svg"]
    assert chart["best_e1rm"] == pytest.approx(150 * (1 + 5 / 30))
    assert "kg" in body["body"]["weight"]


def test_progress_page_data_follows_the_imperial_choice(app_module: Any) -> None:
    uid = _new_user(app_module, "imperial@example.test")
    _seed_lifts(app_module, uid)
    update_user_settings(uid, {"unit_system": "imperial"}, app_module.DB_PATH)

    body = _client_for(app_module, uid).get("/api/progress").json()

    assert body["weight_unit"] == "lb"
    chart = body["charts"][0]
    assert "330.7 lb x 5" in chart["svg"]  # 150 kg
    assert "kg" not in chart["svg"]
    assert chart["best_e1rm"] == round(150 * (1 + 5 / 30) * 2.2046226218, 1)
    assert "lb" in body["body"]["weight"] and "kg" not in body["body"]["weight"]
    # Stored values are untouched.
    assert get_user_settings(uid, app_module.DB_PATH).unit_system == "imperial"


def test_stats_page_data_converts_volume_records_and_projections(app_module: Any) -> None:
    uid = _new_user(app_module, "stats@example.test")
    _seed_lifts(app_module, uid)
    client = _client_for(app_module, uid)

    metric = client.get("/api/stats").json()
    update_user_settings(uid, {"unit_system": "imperial"}, app_module.DB_PATH)
    imperial = client.get("/api/stats").json()

    assert (metric["weight_unit"], imperial["weight_unit"]) == ("kg", "lb")
    assert metric["total_volume"] == 140 * 5 * 3 + 150 * 5 * 3
    assert imperial["total_volume"] == round(metric["total_volume"] * 2.2046226218, 1)
    assert metric["prs"][0]["detail"] == "150 kg x 5"
    assert imperial["prs"][0]["detail"] == "330.7 lb x 5"
    assert float(imperial["prs"][0]["e1rm"]) == round(float(metric["prs"][0]["e1rm"]) * 2.2046226218, 1)
    assert "kg" in metric["volume_bars"] and "lb" in imperial["volume_bars"]
    assert "kg" not in imperial["volume_bars"]


def test_dashboard_weight_and_unit_follow_the_owner(app_module: Any) -> None:
    uid = _new_user(app_module, "dash@example.test")
    _seed_lifts(app_module, uid)
    client = _client_for(app_module, uid)

    assert client.get("/api/dashboard").json()["weight"] == "81 kg"
    update_user_settings(uid, {"unit_system": "imperial"}, app_module.DB_PATH)
    dashboard = client.get("/api/dashboard").json()

    assert dashboard["weight"] == "178.6 lb"  # 81 kg
    assert dashboard["weight_unit"] == "lb"
    assert "kg" not in dashboard["review_recovery"]
    assert "lb" in dashboard["review_recovery"]


def test_one_users_units_never_change_anothers_pages(app_module: Any) -> None:
    alice = _new_user(app_module, "alice-units@example.test")
    bob = _new_user(app_module, "bob-units@example.test")
    for uid in (alice, bob):
        _seed_lifts(app_module, uid)
    update_user_settings(alice, {"unit_system": "imperial"}, app_module.DB_PATH)

    assert _client_for(app_module, bob).get("/api/progress").json()["weight_unit"] == "kg"
    assert _client_for(app_module, alice).get("/api/progress").json()["weight_unit"] == "lb"


def test_overload_nudge_and_last_best_use_the_users_unit(app_module: Any) -> None:
    best = {"top_weight_kg": 100.0, "top_reps": 8}
    assert app_module._format_best(best) == "100 kg x 8"
    assert app_module._format_best(best, "imperial") == "220.5 lb x 8"
    assert "Add 2.5 kg" in app_module._overload_nudge("6-8", best)
    assert "Add 5 lb" in app_module._overload_nudge("6-8", best, "imperial")
    assert app_module._format_best(None, "imperial") == "No data yet"


def test_projection_cards_use_the_users_unit(app_module: Any) -> None:
    entries = [
        {"date": "2026-03-02", "top_weight_kg": 100.0, "top_reps": 5},
        {"date": "2026-03-09", "top_weight_kg": 110.0, "top_reps": 5},
    ]
    target = date(2026, 6, 1).toordinal()
    metric = app_module._project_lift("Deadlift", entries, target, metric="e1rm")
    imperial = app_module._project_lift(
        "Deadlift", entries, target, metric="e1rm", unit_system="imperial"
    )
    assert metric is not None and imperial is not None
    assert metric["current"].endswith(" kg") and imperial["current"].endswith(" lb")
    assert float(imperial["current"].split()[0]) == round(
        float(metric["current"].split()[0]) * 2.2046226218, 1
    )
    reps = app_module._project_lift("Pull-ups", entries, target, unit_system="imperial")
    assert reps is not None


# ---------------------------------------------------------------------------
# Pages use the owner's time zone
# ---------------------------------------------------------------------------


def test_session_counts_follow_the_owners_time_zone(app_module: Any) -> None:
    uid = _new_user(app_module, "days@example.test")
    save_progress(_workout("2026-03-02T09:00:00+00:00", "Squat", 100.0), app_module.DB_PATH, user_id=uid)
    save_progress(_workout("2026-03-02T20:30:00+00:00", "Bench", 80.0), app_module.DB_PATH, user_id=uid)
    client = _client_for(app_module, uid)

    assert client.get("/api/stats").json()["total_sessions"] == 1
    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, app_module.DB_PATH)
    assert client.get("/api/stats").json()["total_sessions"] == 2


def test_streak_counts_from_the_viewers_local_today(app_module: Any) -> None:
    levels = {"2026-03-08": 4, "2026-03-07": 4, "2026-03-06": 4}
    assert app_module._current_streak(levels, date(2026, 3, 8)) == 3
    # For someone already on the 9th, an empty "today" is allowed but the
    # streak still continues from yesterday.
    assert app_module._current_streak(levels, date(2026, 3, 9)) == 3
    # Two empty days break it.
    assert app_module._current_streak(levels, date(2026, 3, 10)) == 0


def test_streak_defaults_to_utc_today_only_as_a_fallback(app_module: Any) -> None:
    today = datetime.now(tz=timezone.utc).date().isoformat()
    assert app_module._current_streak({today: 4}) == 1


# ---------------------------------------------------------------------------
# AI prompts
# ---------------------------------------------------------------------------


class _CapturingProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, stream: bool = False) -> Any:
        self.prompts.append(prompt)
        return iter(["ok"]) if stream else "ok"


def _chat_prompt(module: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    provider = _CapturingProvider()
    monkeypatch.setattr(module, "_resolve_provider_for_request", lambda _r, _c: provider)
    with TestClient(module.app) as client:
        assert client.get("/api/rag_search", params={"q": "How heavy?"}).status_code == 200
    return provider.prompts[0]


def test_coach_chat_prompt_defaults_keep_british_english_and_kilograms(
    app_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = _chat_prompt(app_module, monkeypatch)
    assert "Use British English." in prompt
    assert "Time zone: UTC" in prompt
    assert "Quote every weight in kilograms" in prompt


def test_coach_chat_prompt_follows_the_owners_locale_zone_and_units(
    app_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    update_user_settings(
        get_legacy_user_id(app_module.DB_PATH),
        {"locale": "en-US", "timezone": "America/Chicago", "unit_system": "imperial"},
        app_module.DB_PATH,
    )
    prompt = _chat_prompt(app_module, monkeypatch)
    assert "Use American English." in prompt
    assert "British English" not in prompt
    assert "Time zone: America/Chicago" in prompt
    assert "Convert to pounds" in prompt


def test_other_ai_endpoints_carry_the_same_regional_context(
    app_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = get_legacy_user_id(app_module.DB_PATH)
    update_user_settings(uid, {"unit_system": "imperial", "locale": "en-AU"}, app_module.DB_PATH)
    provider = _CapturingProvider()
    monkeypatch.setattr(app_module, "_resolve_provider_for_request", lambda _r, _c: provider)
    with TestClient(app_module.app) as client:
        client.get("/api/xai_reasoning/2026-03-02_Deadlift")
        # project_peak only calls the model once deterministic projections exist.
        client.get("/api/project_peak")
    assert len(provider.prompts) == 1
    for prompt in provider.prompts:
        assert "Australian English" in prompt
        assert "Convert to pounds" in prompt


# ---------------------------------------------------------------------------
# The real application: routing, signed sessions and the programme start date
# ---------------------------------------------------------------------------

REAL_APP_SECRET = "settings-real-app-secret-that-is-long-enough"


def _real_client(module: Any, user_id: str) -> TestClient:
    """A client holding a genuine registry-backed session, as login creates."""
    client = TestClient(module.app)
    client.cookies.set(
        module.SESSION_SECURITY.cookie.name,
        signed_session_cookie(
            REAL_APP_SECRET,
            user="athlete@example.test",
            user_id=user_id,
            session_id=module.SESSION_STORE.create(user_id),
        ),
    )
    return client


@pytest.fixture()
def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db_path = str(tmp_path / "real-app.db")
    init_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", REAL_APP_SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "settings-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "settings-client-secret")
    import webapp.app as module

    importlib.reload(module)
    monkeypatch.setattr(module, "ALLOW_ANONYMOUS_WEB", True)
    module._RATE_LIMITS.clear()
    return module


def test_real_application_serves_and_persists_display_settings(real_app: Any) -> None:
    alice = _new_user(real_app, "alice-real@example.test")
    bob = _new_user(real_app, "bob-real@example.test")
    alice_client = _real_client(real_app, alice)

    assert alice_client.get("/api/settings/display").json()["settings"] == (
        UserSettings().as_dict()
    )
    saved = alice_client.post(
        "/api/settings/display",
        json={"timezone": "Europe/London", "unit_system": "imperial", "contrast": "more"},
    )
    assert saved.status_code == 200
    assert saved.json()["settings"]["unit_system"] == "imperial"

    assert alice_client.get("/api/settings/display").json()["settings"]["contrast"] == "more"
    assert _real_client(real_app, bob).get("/api/settings/display").json()["settings"] == (
        UserSettings().as_dict()
    )


def test_real_application_rejects_bad_values_and_anonymous_writes(real_app: Any) -> None:
    uid = _new_user(real_app, "reject@example.test")
    client = _real_client(real_app, uid)

    assert client.post("/api/settings/display", json={"locale": "xx"}).status_code == 400
    assert client.post("/api/settings/display", json={"surprise": "x"}).status_code == 422
    assert TestClient(real_app.app).post(
        "/api/settings/display", json={"unit_system": "imperial"}
    ).status_code == 401
    assert get_user_settings(uid, real_app.DB_PATH) == UserSettings()


def _training_data() -> HevyTrainingData:
    return HevyTrainingData(
        username="fixture-user",
        workout_count=8,
        exercise_templates={
            "bench": ExerciseTemplate(
                id="bench",
                title="Bench Press",
                exercise_type="weight_reps",
                primary_muscle_group="chest",
            )
        },
        routines=[
            Routine(
                id="routine-a",
                title="Upper A",
                exercises=[
                    RoutineExercise(
                        template_id="bench",
                        title="Bench Press",
                        sets=3,
                        rest_seconds=120,
                        target_reps=8,
                        set_targets=[{"type": "normal", "reps": 8, "weight_kg": 60.0}] * 3,
                    )
                ],
            )
        ],
        recent_workouts=[
            CompletedWorkout(
                id=f"workout-{index}", title="Upper A", start_time=f"2026-08-{10 + index:02d}T10:00:00Z"
            )
            for index in range(8)
        ],
        folders={},
    )


def test_programme_start_date_defaults_to_the_users_local_today(
    real_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = _new_user(real_app, "start@example.test")
    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, real_app.DB_PATH)
    zones: list[str] = []

    def fake_today(zone: str, now: datetime | None = None) -> date:
        zones.append(zone)
        return date(2026, 9, 3)  # already the 3rd in Auckland while UTC is still the 2nd

    monkeypatch.setattr(real_app, "local_today", fake_today)
    monkeypatch.setattr(
        real_app, "_load_hevy_training_for_user", lambda *_a, **_k: _training_data()
    )
    client = _real_client(real_app, uid)

    omitted = client.post(
        "/api/programmes/preview", json={"selected_routine_ids": ["routine-a"]}
    )
    explicit = client.post(
        "/api/programmes/preview",
        json={"selected_routine_ids": ["routine-a"], "start_date": "2026-09-10"},
    )

    assert omitted.status_code == 200 and explicit.status_code == 200
    assert omitted.json()["preview"]["programme_spec"]["start_date"] == "2026-09-03"
    assert explicit.json()["preview"]["programme_spec"]["start_date"] == "2026-09-10"
    assert zones == ["Pacific/Auckland"]
