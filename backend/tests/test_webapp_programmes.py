from __future__ import annotations

import base64
import importlib
import json
from datetime import date
from typing import Any

import database
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "programmes-test-secret-that-is-long-enough"


def _session_cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _training_data() -> HevyTrainingData:
    templates = {
        "bench": ExerciseTemplate(
            id="bench",
            title="Bench Press",
            exercise_type="weight_reps",
            primary_muscle_group="chest",
        ),
        "row": ExerciseTemplate(
            id="row",
            title="Row",
            exercise_type="weight_reps",
            primary_muscle_group="upper_back",
        ),
    }
    routines = [
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
                    set_targets=[
                        {"type": "normal", "reps": 8, "weight_kg": 60.0},
                        {"type": "normal", "reps": 8, "weight_kg": 60.0},
                        {"type": "normal", "reps": 8, "weight_kg": 60.0},
                    ],
                )
            ],
        ),
        Routine(
            id="routine-b",
            title="Upper B",
            exercises=[
                RoutineExercise(
                    template_id="row",
                    title="Row",
                    sets=3,
                    rest_seconds=90,
                    target_reps=10,
                    set_targets=[
                        {"type": "normal", "reps": 10, "weight_kg": 50.0},
                        {"type": "normal", "reps": 10, "weight_kg": 50.0},
                        {"type": "normal", "reps": 10, "weight_kg": 50.0},
                    ],
                )
            ],
        ),
    ]
    workouts = [
        CompletedWorkout(
            id=f"workout-{index}",
            title="Upper A" if index % 2 == 0 else "Upper B",
            start_time=f"2026-08-{10 + index:02d}T10:00:00Z",
        )
        for index in range(8)
    ]
    return HevyTrainingData(
        username="fixture-user",
        workout_count=42,
        exercise_templates=templates,
        routines=routines,
        recent_workouts=workouts,
        folders={},
    )


def _client(tmp_path: Any, monkeypatch: Any) -> tuple[TestClient, Any, str]:
    db_file = str(tmp_path / "test.db")
    database.init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "programmes-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "programmes-client-secret")

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
    monkeypatch.setattr(
        app_module,
        "get_user_api_keys",
        lambda *args, **kwargs: {"hevy": {"api_key": "fixture-hevy-key"}},
    )
    monkeypatch.setattr(
        app_module,
        "_load_hevy_training_for_user",
        lambda *args, **kwargs: _training_data(),
    )
    client = TestClient(app_module.app)
    client.cookies.set("session", _session_cookie(database.get_legacy_user_id(db_file)))
    return client, app_module, db_file


def _payload() -> dict[str, Any]:
    return {
        "selected_routine_ids": ["routine-b", "routine-a"],
        "duration_weeks": 12,
        "goal": "hypertrophy",
        "start_date": date(2026, 9, 1).isoformat(),
        "sessions_per_week": 4,
        "experience": "intermediate",
        "max_session_minutes": 90,
        "adaptation_aggressiveness": "balanced",
    }


def test_programme_builder_returns_hevy_source_without_templates(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.get("/api/programmes")
    assert response.status_code == 200
    data = response.json()

    assert data["templates"] == []
    assert data["connection"]["state"] == "connected"
    assert [routine["id"] for routine in data["source"]["routines"]] == [
        "routine-a",
        "routine-b",
    ]
    assert data["source"]["routines"][0]["exercises"][0]["set_targets"]


def test_preview_activation_and_plan_round_trip(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, db_file = _client(tmp_path, monkeypatch)

    before = client.get("/api/dashboard")
    assert before.status_code == 200
    assert before.json()["setup_required"] is True

    preview_response = client.post("/api/programmes/preview", json=_payload())
    assert preview_response.status_code == 200
    preview = preview_response.json()["preview"]
    assert preview["cycle_weeks"] == 12
    assert sum(block["duration_weeks"] for block in preview["blocks"]) == 12
    assert [day["routine_id"] for day in preview["days"]] == [
        "routine-b",
        "routine-a",
    ]

    activation_response = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": preview["preview_token"]},
    )
    assert activation_response.status_code == 200

    legacy_uid = database.get_legacy_user_id(db_file)
    active = database.get_active_programme(legacy_uid, db_path=db_file)
    assert active is not None
    assert active["source"] == "hevy"
    assert active["definition"]["preview_token"] == preview["preview_token"]

    plan_response = client.get("/api/plan")
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["setup_required"] is False
    assert plan["source"] == "hevy"
    assert plan["cycle_weeks"] == 12
    assert plan["blocks"]
    assert plan["days"]

    dashboard_response = client.get("/api/dashboard")
    assert dashboard_response.status_code == 200
    dashboard = dashboard_response.json()
    assert dashboard["setup_required"] is False
    assert dashboard["block"]["number"] >= 1


def test_activation_rejects_changed_preview_token(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": "0" * 64},
    )
    assert response.status_code == 409


def test_static_template_compatibility_key_is_gone(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/programmes/select",
        json={"template_key": "hybrid_powerbuilding"},
    )
    assert response.status_code == 410
    assert "removed" in response.json()["detail"].lower()


def test_disconnected_builder_does_not_return_static_fallback(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, _db_file = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "get_user_api_keys",
        lambda *args, **kwargs: {},
    )

    response = client.get("/api/programmes")
    assert response.status_code == 200
    data = response.json()
    assert data["connection"]["state"] == "disconnected"
    assert data["templates"] == []
    assert data["source"]["routines"] == []

    plan = client.get("/api/plan").json()
    assert plan["setup_required"] is True
    assert plan["blocks"] == []
    assert plan["days"] == []
