from __future__ import annotations

import importlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import database
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevySourceSnapshot,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from secret_vault import ResolvedCredential, SecretValue
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "programmes-test-secret-that-is-long-enough"


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


def _snapshot(
    data: HevyTrainingData | None = None,
    *,
    status: str = "ok",
    warnings: list[str] | None = None,
    fetched_at: str | None = None,
) -> HevySourceSnapshot:
    training = data or _training_data()
    return HevySourceSnapshot(
        data=training,
        status=status,
        fetched_at=fetched_at or datetime.now(tz=UTC).isoformat(),
        warnings=list(warnings or []),
        counts={
            "routines": len(training.routines),
            "folders": len(training.folders),
            "exercise_templates": len(training.exercise_templates),
            "recent_workouts": len(training.recent_workouts),
            "workouts": int(training.workout_count or 0),
        },
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
        "_vault_credential",
        lambda *args, **kwargs: ResolvedCredential(
            provider="hevy",
            version=1,
            api_key=SecretValue("fixture-hevy-key"),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        lambda *args, **kwargs: _snapshot(),
    )
    client = TestClient(app_module.app)
    # Register a real server-side session, exactly as login does.
    user_id = database.get_legacy_user_id(db_file)
    session_id = app_module.SESSION_STORE.create(user_id)
    client.cookies.set(
        app_module.SESSION_SECURITY.cookie.name,
        signed_session_cookie(
            _SECRET,
            user="athlete@example.test",
            user_id=user_id,
            session_id=session_id,
        ),
    )
    return client, app_module, db_file


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "selected_routine_ids": ["routine-b", "routine-a"],
        "duration_weeks": 12,
        "goal": "hypertrophy",
        "start_date": date(2026, 9, 1).isoformat(),
        "sessions_per_week": 4,
        "experience": "intermediate",
        "max_session_minutes": 90,
        "adaptation_aggressiveness": "balanced",
    }
    payload.update(overrides)
    return payload


def test_programme_builder_returns_hevy_source_without_templates(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.get("/api/programmes")
    assert response.status_code == 200
    data = response.json()

    assert data["connection"]["state"] == "connected"
    assert [routine["id"] for routine in data["source"]["routines"]] == [
        "routine-a",
        "routine-b",
    ]
    assert data["source"]["routines"][0]["exercises"][0]["set_targets"]


def test_builder_reports_owner_sync_state_and_counts(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, db_file = _client(tmp_path, monkeypatch)

    data = client.get("/api/programmes").json()
    connection = data["connection"]

    user_id = database.get_legacy_user_id(db_file)
    assert connection["owner"] == database.get_user_by_id(user_id, db_file)["email"]
    assert connection["username"] == "fixture-user"
    assert connection["counts"]["routines"] == 2
    assert connection["last_successful_sync"] is not None
    assert connection["stale"] is False

    stored = database.get_meta(
        "hevy_source_sync", db_file, user_id=database.get_legacy_user_id(db_file)
    )
    assert stored is not None
    assert json.loads(stored)["counts"]["routines"] == 2


def test_builder_reports_partial_source_state(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, _db_file = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        lambda *args, **kwargs: _snapshot(
            status="partial",
            warnings=["Routine folders could not be read from Hevy: timeout"],
        ),
    )

    data = client.get("/api/programmes").json()
    assert data["connection"]["state"] == "partial"
    assert data["connection"]["warnings"]


def test_refresh_endpoint_reports_partial_success(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, _db_file = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        lambda *args, **kwargs: _snapshot(status="partial", warnings=["folders stale"]),
    )

    response = client.post("/api/programmes/refresh")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "partial"
    assert body["warnings"] == ["folders stale"]
    assert body["counts"]["routines"] == 2


def test_refresh_endpoint_reports_auth_and_rate_limit_states(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from fastapi import HTTPException

    client, app_module, _db_file = _client(tmp_path, monkeypatch)

    def _raise(status_code: int, detail: str):
        def _loader(*args: Any, **kwargs: Any) -> None:
            raise HTTPException(status_code=status_code, detail=detail)

        return _loader

    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        _raise(401, "Hevy rejected your API key. Update it in Settings and refresh."),
    )
    auth = client.post("/api/programmes/refresh").json()
    assert auth["state"] == "auth_error"
    assert "rejected" in auth["detail"]

    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        _raise(429, "Hevy is rate limiting requests. Try again in a few minutes."),
    )
    limited = client.post("/api/programmes/refresh").json()
    assert limited["state"] == "rate_limited"

    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        _raise(502, "Hevy could not be reached. Check the connection and try again."),
    )
    failed = client.post("/api/programmes/refresh").json()
    assert failed["state"] == "error"


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
        json={
            **_payload(),
            "preview_token": preview["preview_token"],
            "source_hashes": [
                {
                    "routine_id": snapshot["id"],
                    "source_hash": snapshot["source_hash"],
                }
                for snapshot in preview["source_snapshots"]
            ],
        },
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


def test_preview_reports_cadence_schedule_and_muscle_sets(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/programmes/preview",
        json=_payload(
            cadence={"mode": "weekly", "weekdays": [0, 2, 4]},
            routine_selections=[
                {"routine_id": "routine-a", "role": "required"},
                {"routine_id": "routine-b", "role": "rotating"},
            ],
            sessions_per_week=3,
        ),
    )
    assert response.status_code == 200
    preview = response.json()["preview"]

    assert preview["cadence"]["mode"] == "weekly"
    assert preview["cadence"]["training_day_names"] == [
        "Monday",
        "Wednesday",
        "Friday",
    ]
    assert preview["cadence"]["sessions_per_week"] == 3
    assert len(preview["schedule"]) == 36
    assert preview["schedule"][0]["date"] == "2026-09-02"
    assert preview["muscle_sets"]["weekly_sets"]["chest"] == 3.0
    assert preview["blocking_violations"] == []


def test_preview_reports_blocking_violation_when_a_routine_cannot_be_scheduled(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/programmes/preview",
        json=_payload(
            cadence={"mode": "weekly", "weekdays": [0]},
            routine_selections=[
                {"routine_id": "routine-a", "role": "required"},
                {"routine_id": "routine-b", "role": "required"},
            ],
            sessions_per_week=1,
        ),
    )
    assert response.status_code == 200
    preview = response.json()["preview"]
    codes = {item["code"] for item in preview["blocking_violations"]}
    assert "routine_not_scheduled" in codes


def test_activation_refuses_a_preview_with_blocking_violations(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, db_file = _client(tmp_path, monkeypatch)

    payload = _payload(
        cadence={"mode": "weekly", "weekdays": [0]},
        routine_selections=[
            {"routine_id": "routine-a", "role": "required"},
            {"routine_id": "routine-b", "role": "required"},
        ],
        sessions_per_week=1,
    )
    preview = client.post("/api/programmes/preview", json=payload).json()["preview"]

    response = client.post(
        "/api/programmes/activate",
        json={**payload, "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["violations"]
    assert database.get_active_programme(
        database.get_legacy_user_id(db_file), db_path=db_file
    ) is None


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
    assert "changed" in response.json()["detail"]["message"].lower()


def test_activation_reports_which_routine_changed(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    preview = client.post("/api/programmes/preview", json=_payload()).json()["preview"]
    response = client.post(
        "/api/programmes/activate",
        json={
            **_payload(),
            "preview_token": preview["preview_token"],
            "source_hashes": [
                {
                    "routine_id": snapshot["id"],
                    "source_hash": "stale-hash",
                }
                for snapshot in preview["source_snapshots"]
            ],
        },
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    changed = detail["conflict"]["changed_routines"]
    assert {item["routine_id"] for item in changed} == {"routine-a", "routine-b"}
    assert detail["remediation"]


def test_preview_rejects_routines_from_another_account(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, _db_file = _client(tmp_path, monkeypatch)

    response = client.post(
        "/api/programmes/preview",
        json=_payload(selected_routine_ids=["someone-elses-routine"]),
    )
    assert response.status_code == 422
    assert "not available" in response.json()["detail"]


def test_disconnected_builder_does_not_return_static_fallback(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, _db_file = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        app_module,
        "_vault_credential",
        lambda *args, **kwargs: None,
    )

    response = client.get("/api/programmes")
    assert response.status_code == 200
    data = response.json()
    assert data["connection"]["state"] == "disconnected"
    assert data["source"]["routines"] == []

    plan = client.get("/api/plan").json()
    assert plan["setup_required"] is True
    assert plan["blocks"] == []
    assert plan["days"] == []


def test_dashboard_and_plan_share_current_prescriptions_and_tenant_rotation(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from dynamic_programme import ProgrammePreviewRequest, build_programme_preview

    client, _app_module, db_file = _client(tmp_path, monkeypatch)
    user_id = database.get_legacy_user_id(db_file)
    definition = build_programme_preview(
        _training_data(),
        ProgrammePreviewRequest(**_payload()),
    )
    database.set_active_programme(
        user_id, "hevy", "synthetic-rotation", definition, db_file
    )
    second_block = definition["blocks"][1]
    today = date(2026, 9, 1) + timedelta(weeks=second_block["start_week"] - 1)
    monkeypatch.setattr("programme_runtime.local_today", lambda timezone_name: today)
    database.save_workout(
        {
            "id": "synthetic-completion",
            "routine_id": "routine-b",
            "end_time": f"{today.isoformat()}T08:00:00Z",
        },
        db_file,
        user_id=user_id,
    )
    other = database.get_or_create_user("other@example.test", db_path=db_file)
    database.save_workout(
        {
            "id": "other-completion",
            "routine_id": "routine-a",
            "end_time": f"{today.isoformat()}T09:00:00Z",
        },
        db_file,
        user_id=other["id"],
    )

    dashboard = client.get("/api/dashboard").json()
    plan = client.get("/api/plan").json()
    start_date = date.fromisoformat(definition["programme_spec"]["start_date"])
    position = (today - start_date).days % len(definition["days"])
    selected_day = definition["days"][position]
    expected = selected_day["exercises"][0]["prescriptions"][1]
    assert dashboard["focus"] == selected_day["focus"]
    assert (
        dashboard["next_routine_id"]
        == plan["next_routine_id"]
        == selected_day["routine_id"]
    )
    assert (
        dashboard["rows"][0]["planned"]
        == f"{expected['sets']} × {expected['rep_range']}"
    )
    assert plan["days"][position]["exercises"][0]["sets"] == expected["sets"]
    assert plan["week"] == dashboard["week"] == second_block["start_week"]
    stored = database.get_active_programme(user_id, db_file)
    assert stored is not None
    assert stored["definition"] == definition


def test_stats_distribution_uses_programme_routine_ids_not_free_text(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    from dynamic_programme import ProgrammePreviewRequest, build_programme_preview

    client, _app_module, db_file = _client(tmp_path, monkeypatch)
    user_id = database.get_legacy_user_id(db_file)
    definition = build_programme_preview(
        _training_data(),
        ProgrammePreviewRequest(**_payload()),
    )
    database.set_active_programme(
        user_id, "hevy", "synthetic-distribution", definition, db_file
    )
    for index, routine_id in enumerate(("routine-a", "routine-b", "routine-a")):
        database.save_workout(
            {
                "id": f"workout-{index}",
                "routine_id": routine_id,
                "title": "Renamed provider title",
                "end_time": f"2026-09-{10 + index:02d}T08:00:00Z",
            },
            db_file,
            user_id=user_id,
        )

    stats = client.get("/api/stats").json()

    assert "Upper A" in stats["distribution"]
    assert "Upper B" in stats["distribution"]
    assert "Renamed provider title" not in stats["distribution"]


def _activate(training_data: Any, client: Any, db_file: str) -> None:
    preview = client.post("/api/programmes/preview", json=_payload())
    assert preview.status_code == 200
    token = preview.json()["preview"]["preview_token"]
    activation = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": token},
    )
    assert activation.status_code == 200


def test_programme_builder_reports_renamed_and_edited_routines(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, db_file = _client(tmp_path, monkeypatch)
    _activate(_training_data(), client, db_file)

    edited = _training_data()
    edited.routines[0].title = "Push A"
    edited.routines[1].exercises[0].sets = 5
    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        lambda *args, **kwargs: _snapshot(edited),
    )

    data = client.get("/api/programmes").json()
    drift = data["source_drift"]
    assert drift["status"] == "action_required"
    assert drift["has_changes"] is True
    assert drift["summary"] == {
        "renamed": 1,
        "content_changed": 1,
        "removed": 0,
    }
    by_id = {row["routine_id"]: row for row in drift["routines"]}
    assert by_id["routine-a"]["state"] == "renamed"
    assert by_id["routine-a"]["current_title"] == "Push A"
    assert by_id["routine-b"]["state"] == "content_changed"


def test_programme_builder_reports_deleted_routine_as_removed(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, db_file = _client(tmp_path, monkeypatch)
    _activate(_training_data(), client, db_file)

    edited = _training_data()
    edited.routines = [routine for routine in edited.routines if routine.id != "routine-b"]
    monkeypatch.setattr(
        app_module,
        "_load_hevy_snapshot_for_user",
        lambda *args, **kwargs: _snapshot(edited),
    )

    drift = client.get("/api/programmes").json()["source_drift"]
    assert drift["status"] == "action_required"
    assert drift["summary"]["removed"] == 1
    by_id = {row["routine_id"]: row for row in drift["routines"]}
    assert by_id["routine-b"]["state"] == "removed"
    assert by_id["routine-b"]["current_hash"] is None


def test_programme_builder_reports_unavailable_source_without_leaking_snapshot(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, app_module, db_file = _client(tmp_path, monkeypatch)
    _activate(_training_data(), client, db_file)
    monkeypatch.setattr(
        app_module,
        "_vault_credential",
        lambda *args, **kwargs: None,
    )

    data = client.get("/api/programmes").json()
    drift = data["source_drift"]
    assert data["connection"]["state"] == "disconnected"
    assert drift["status"] == "source_unavailable"
    assert drift["has_changes"] is False
    assert drift["routines"] == []


def test_activation_is_blocked_by_structured_constraint_violation(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, db_file = _client(tmp_path, monkeypatch)

    payload = {
        **_payload(),
        "constraints": {"max_sessions_per_week": 1},
    }

    preview_response = client.post("/api/programmes/preview", json=payload)
    assert preview_response.status_code == 200
    preview = preview_response.json()["preview"]
    assert preview["blocking_violations"]
    assert any(
        item["code"] == "session_limit_exceeded"
        for item in preview["blocking_violations"]
    )

    activation_response = client.post(
        "/api/programmes/activate",
        json={**payload, "preview_token": preview["preview_token"]},
    )
    assert activation_response.status_code == 422
    detail = activation_response.json()["detail"]
    assert detail["violations"]

    legacy_uid = database.get_legacy_user_id(db_file)
    assert database.get_active_programme(legacy_uid, db_path=db_file) is None


def test_history_endpoint_exposes_matches_and_accepts_user_override(
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    client, _app_module, db_file = _client(tmp_path, monkeypatch)

    history_response = client.get("/api/programmes/history")
    assert history_response.status_code == 200
    history = history_response.json()["history"]
    assert history["matches"]
    assert "baselines" in history
    assert history["rule_versions"]["matcher"]

    override_response = client.post(
        "/api/programmes/history/override",
        json={
            "pattern_type": "workout",
            "pattern_value": "workout-0",
            "routine_id": "routine-a",
        },
    )
    assert override_response.status_code == 200

    refreshed = client.get("/api/programmes/history").json()["history"]
    corrected = next(
        match
        for match in refreshed["matches"]
        if match["workout_id"] == "workout-0"
    )
    assert corrected["routine_id"] == "routine-a"
    assert corrected["user_confirmed"] is True
    assert corrected["confidence"] == "high"

    user_id = database.get_legacy_user_id(db_file)
    overrides = database.get_routine_match_overrides(user_id, db_file)
    assert overrides[0]["pattern_value"] == "workout-0"
    assert overrides[0]["routine_id"] == "routine-a"



