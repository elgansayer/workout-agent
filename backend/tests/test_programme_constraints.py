"""Deterministic tests for profile constraint validation and activation blocking.

Verifies:
1. Validator runs on create, import, edit, inference, selection, and activation.
2. Blocking violations prevent activation and identify the exact exercise,
   prescription, rule, and permitted remediation.
3. The active programme and all seeded defaults pass current profile rules.
"""

from __future__ import annotations

import base64
import importlib
import json
from datetime import date
from typing import Any

import database
import pytest
from dynamic_programme import (
    ProgrammePreviewRequest,
    build_programme_preview,
    serialise_hevy_source,
)
from hevy_reader import (
    CompletedWorkout,
    ExerciseTemplate,
    HevyTrainingData,
    Routine,
    RoutineExercise,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from itsdangerous import TimestampSigner
from programme_inference import infer_programme
from programme_validator import (
    validate_programme,
)
from secret_vault import ResolvedCredential, SecretValue
from session_helpers import signed_session_cookie
from starlette.testclient import TestClient

_SECRET = "constraints-test-secret-that-is-long-enough"


def _session_cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _training_data(include_banned: bool = False) -> HevyTrainingData:
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
    exercises_a = [
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
    ]
    if include_banned:
        templates["bss"] = ExerciseTemplate(
            id="bss",
            title="Bulgarian Split Squats",
            exercise_type="weight_reps",
            primary_muscle_group="quadriceps",
        )
        exercises_a.append(
            RoutineExercise(
                template_id="bss",
                title="Bulgarian Split Squats",
                sets=3,
                rest_seconds=90,
                target_reps=10,
                set_targets=[
                    {"type": "normal", "reps": 10, "weight_kg": 20.0},
                    {"type": "normal", "reps": 10, "weight_kg": 20.0},
                    {"type": "normal", "reps": 10, "weight_kg": 20.0},
                ],
            )
        )

    routines = [
        Routine(
            id="routine-a",
            title="Upper A",
            exercises=exercises_a,
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


def _client(tmp_path: Any, monkeypatch: Any, training_data: HevyTrainingData | None = None) -> tuple[TestClient, Any, str]:
    db_file = str(tmp_path / "test.db")
    database.init_db(db_file)
    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "constraints-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "constraints-client-secret")

    import webapp.app as app_module

    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
    monkeypatch.setattr(
        app_module,
        "_vault_credential",
        lambda user_id, provider, **kwargs: ResolvedCredential(
            provider="hevy",
            version=1,
            api_key=SecretValue("fixture-hevy-key"),
        ),
    )
    t_data = training_data or _training_data()
    monkeypatch.setattr(
        app_module,
        "_load_hevy_training_for_user",
        lambda *args, **kwargs: t_data,
    )
    client = TestClient(app_module.app)
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


def _payload(goal: str = "hypertrophy", constraints: list[Any] | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {
        "selected_routine_ids": ["routine-b", "routine-a"],
        "duration_weeks": 12,
        "goal": goal,
        "start_date": date(2026, 9, 1).isoformat(),
        "sessions_per_week": 4,
        "experience": "intermediate",
        "max_session_minutes": 90,
        "adaptation_aggressiveness": "balanced",
    }
    if constraints is not None:
        p["constraints"] = constraints
    return p


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Seeded defaults and active programme pass profile rules
# ---------------------------------------------------------------------------


def test_seeded_defaults_pass_current_profile_rules(tmp_path: Any) -> None:
    """All seeded defaults in a fresh database pass profile rules."""
    db_file = str(tmp_path / "seeded.db")
    database.init_db(db_file)
    legacy_uid = database.get_legacy_user_id(db_file)

    # 1. Default user preferences constraints are empty by default
    prefs = database.get_user_preferences(legacy_uid, db_path=db_file)
    assert prefs["constraints"] == []

    # 2. A fresh database has no active programme until the user activates one
    assert database.get_active_programme(legacy_uid, db_path=db_file) is None


def test_active_programme_passes_current_profile_rules(tmp_path: Any, monkeypatch: Any) -> None:
    """An activated programme passes the user's current profile rules."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)

    preview_res = client.post("/api/programmes/preview", json=_payload())
    assert preview_res.status_code == 200
    preview = preview_res.json()["preview"]
    assert preview["blocking_violations"] == []

    activate_res = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": preview["preview_token"]},
    )
    assert activate_res.status_code == 200

    active = database.get_active_programme(legacy_uid, db_path=db_file)
    assert active is not None
    validation = validate_programme(active["definition"], profile_rules=[])
    assert validation.is_valid is True
    assert validation.blocking_violations == []


# ---------------------------------------------------------------------------
# Acceptance Criterion 1: Validator runs on create, import, edit, inference, selection, activation
# ---------------------------------------------------------------------------


def test_validator_runs_on_create_preview() -> None:
    """Validator runs on programme preview creation."""
    data = _training_data()
    # Maximal strength prescribes 1-4 reps in expression/realisation
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="maximal_strength",
        duration_weeks=12,
        constraints=["min_reps: 6"],
    )
    preview = build_programme_preview(data, req)
    assert len(preview["blocking_violations"]) > 0
    first = preview["blocking_violations"][0]
    assert first["exercise"] == "Bench Press"
    assert "reps" in first["prescription"]
    assert "min_reps: 6" in first["rule"]
    assert "Permitted remediation:" in first["permitted_remediation"] or "Increase" in first["permitted_remediation"]


def test_validator_runs_on_import_routines() -> None:
    """Validator runs when importing/serialising Hevy routines."""
    banned_data = _training_data(include_banned=True)
    rules = ["No Bulgarian split squats (bad toes). Use leg extensions or the flat-foot leg press instead."]
    source = serialise_hevy_source(banned_data, profile_rules=rules)
    assert "blocking_violations" in source
    assert len(source["blocking_violations"]) == 1
    v = source["blocking_violations"][0]
    assert v["exercise"] == "Bulgarian Split Squats"
    assert "No Bulgarian split squats" in v["rule"]
    assert "Use leg extensions or the flat-foot leg press instead" in v["permitted_remediation"]


def test_validator_runs_on_edit_preferences(tmp_path: Any, monkeypatch: Any) -> None:
    """Validator runs when user edits preferences/constraints, checking active programme."""
    client, _app, db_file = _client(tmp_path, monkeypatch)

    # Activate a programme first
    preview_res = client.post("/api/programmes/preview", json=_payload())
    token = preview_res.json()["preview"]["preview_token"]
    client.post("/api/programmes/activate", json={**_payload(), "preview_token": token})

    # Edit preferences with a constraint that conflicts with the active programme (Bench Press min_reps: 12)
    pref_res = client.post(
        "/api/settings/preferences",
        json={"constraints": ["min_reps: 12"]},
    )
    assert pref_res.status_code == 200
    data = pref_res.json()
    assert "blocking_violations" in data
    assert len(data["blocking_violations"]) > 0
    assert any(v["exercise"] in {"Bench Press", "Row"} for v in data["blocking_violations"])
    assert data["reconciliation"]["status"] == "deactivated_requires_remediation"
    legacy_uid = database.get_legacy_user_id(db_file)
    assert database.get_active_programme(legacy_uid, db_file) is None


def test_validator_runs_on_inference() -> None:
    """Validator runs during programme inference from Hevy training data."""
    banned_data = _training_data(include_banned=True)
    inferred = infer_programme(banned_data, profile_rules=["No Bulgarian split squats"])
    assert hasattr(inferred, "blocking_violations")
    assert len(inferred.blocking_violations) == 1
    assert inferred.blocking_violations[0]["exercise"] == "Bulgarian Split Squats"


def test_validator_runs_on_activation_and_blocks(tmp_path: Any, monkeypatch: Any) -> None:
    """Validator runs on /api/programmes/activate and blocks violating activation."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)

    # Save user constraint
    database.save_user_preferences(legacy_uid, constraints=["min_reps: 6"], db_path=db_file)

    # Preview maximal_strength (contains 1-4 reps)
    preview_res = client.post("/api/programmes/preview", json=_payload(goal="maximal_strength"))
    preview = preview_res.json()["preview"]
    assert len(preview["blocking_violations"]) > 0

    # Activation must fail with 422
    activate_res = client.post(
        "/api/programmes/activate",
        json={**_payload(goal="maximal_strength"), "preview_token": preview["preview_token"]},
    )
    assert activate_res.status_code == 422
    err_detail = activate_res.json()["detail"]
    assert "Activation blocked by profile constraint" in err_detail
    assert ("Bench Press" in err_detail or "Row" in err_detail)
    assert "Permitted remediation:" in err_detail


def test_structured_rules_block_activation_with_readable_detail(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A prohibited exercise from structured rules blocks activation with a string detail."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)
    database.save_training_rules(
        legacy_uid,
        {
            "version": 1,
            "prohibited_exercises": [{"exercise": "Bench Press", "reason": "Shoulder"}],
        },
        db_path=db_file,
    )

    preview_res = client.post("/api/programmes/preview", json=_payload())
    preview = preview_res.json()["preview"]
    assert any(
        v["exercise"] == "Bench Press"
        and "Prohibited exercise" in v["rule"]
        and v["permitted_remediation"]
        for v in preview["blocking_violations"]
    )

    activate_res = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": preview["preview_token"]},
    )
    assert activate_res.status_code == 422
    detail = activate_res.json()["detail"]
    assert "Activation blocked by profile constraint" in detail
    assert "Bench Press" in detail
    assert "Permitted remediation:" in detail


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Blocking violations identify exercise, prescription, rule, remediation
# ---------------------------------------------------------------------------


def test_blocking_violation_exact_fields_rep_range() -> None:
    """Blocking rep range violation identifies exact exercise, prescription, rule, and permitted remediation."""
    data = _training_data()
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="maximal_strength",
        duration_weeks=12,
        constraints=["reps >= 6"],
    )
    preview = build_programme_preview(data, req)
    violations = preview["blocking_violations"]
    assert len(violations) > 0

    for v in violations:
        assert "exercise" in v and isinstance(v["exercise"], str) and len(v["exercise"]) > 0
        assert "prescription" in v and isinstance(v["prescription"], str) and len(v["prescription"]) > 0
        assert "rule" in v and isinstance(v["rule"], str) and len(v["rule"]) > 0
        assert "permitted_remediation" in v and isinstance(v["permitted_remediation"], str) and len(v["permitted_remediation"]) > 0
        assert "remediation" in v and v["remediation"] == v["permitted_remediation"]


def test_blocking_violation_intensity_target_rir() -> None:
    """Intensity / RIR constraint violation identifies exact exercise, prescription, rule, and remediation."""
    data = _training_data()
    # Hypertrophy overload block prescribes target_rir = "1-3" (min RIR 1 < 2)
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="hypertrophy",
        duration_weeks=12,
        constraints=["min_rir: 2"],
    )
    preview = build_programme_preview(data, req)
    violations = preview["blocking_violations"]
    assert len(violations) > 0

    rir_violations = [v for v in violations if "RIR" in v["prescription"]]
    assert len(rir_violations) > 0
    first = rir_violations[0]
    assert first["exercise"] == "Bench Press"
    assert "Target RIR 1-3" in first["prescription"]
    assert "min_rir: 2" in first["rule"]
    assert "Adjust target RIR" in first["permitted_remediation"]


def test_blocking_violation_excluded_exercise_and_remediation() -> None:
    """Excluded exercise constraint identifies exact exercise, rule, and permitted remediation."""
    data = _training_data(include_banned=True)
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="general_fitness",
        duration_weeks=12,
        constraints=[
            "No Bulgarian split squats (bad toes). Use leg extensions or the flat-foot leg press instead."
        ],
    )
    preview = build_programme_preview(data, req)
    violations = [v for v in preview["blocking_violations"] if v["exercise"] == "Bulgarian Split Squats"]
    assert len(violations) == 1
    v = violations[0]
    assert v["exercise"] == "Bulgarian Split Squats"
    assert "No Bulgarian split squats" in v["rule"]
    assert v["permitted_remediation"] == "Use leg extensions or the flat-foot leg press instead."


def test_database_set_active_programme_enforces_validation(tmp_path: Any) -> None:
    """Database-level set_active_programme raises ValueError on blocking constraint violations."""
    db_file = str(tmp_path / "db_enforce.db")
    database.init_db(db_file)
    legacy_uid = database.get_legacy_user_id(db_file)

    database.save_user_preferences(legacy_uid, constraints=["min_reps: 8"], db_path=db_file)

    data = _training_data()
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="maximal_strength",
        duration_weeks=12,
        constraints=["min_reps: 8"],
    )
    preview = build_programme_preview(data, req)
    assert len(preview["blocking_violations"]) > 0

    with pytest.raises(ValueError, match="Cannot activate programme with blocking violations"):
        database.set_active_programme(
            legacy_uid,
            source="hevy",
            template_key="hevy:test-violating",
            definition=preview,
            db_path=db_file,
        )

    # Also verify that if someone clears blocking_violations from the dict, set_active_programme still validates against user preferences
    tampered = dict(preview)
    tampered["blocking_violations"] = []
    with pytest.raises(ValueError, match="Activation blocked by profile constraint"):
        database.set_active_programme(
            legacy_uid,
            source="hevy",
            template_key="hevy:test-tampered",
            definition=tampered,
            db_path=db_file,
        )


def test_permitted_remediation_allows_clean_activation(tmp_path: Any, monkeypatch: Any) -> None:
    """Remediating constraint violations permits clean programme activation."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)

    # Set constraint: min_reps 5
    database.save_user_preferences(legacy_uid, constraints=["min_reps: 5"], db_path=db_file)

    # 1. Violating preview (maximal_strength: prescribes 1-4 reps)
    bad_res = client.post("/api/programmes/preview", json=_payload(goal="maximal_strength"))
    assert len(bad_res.json()["preview"]["blocking_violations"]) > 0

    # 2. Remediated preview (hypertrophy: prescribes 5-10 and 6-10 reps)
    good_res = client.post("/api/programmes/preview", json=_payload(goal="hypertrophy"))
    good_preview = good_res.json()["preview"]
    assert good_preview["blocking_violations"] == []

    # 3. Activation succeeds cleanly
    act_res = client.post(
        "/api/programmes/activate",
        json={**_payload(goal="hypertrophy"), "preview_token": good_preview["preview_token"]},
    )
    assert act_res.status_code == 200

    active = database.get_active_programme(legacy_uid, db_path=db_file)
    assert active is not None
    assert active["definition"]["preview_token"] == good_preview["preview_token"]


def test_request_constraints_cannot_override_stored_profile_rules(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """A client-supplied empty constraints list must not bypass stored profile rules."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)
    database.save_user_preferences(legacy_uid, constraints=["min_reps: 6"], db_path=db_file)

    bypass = {**_payload(goal="maximal_strength"), "constraints": []}
    preview_res = client.post("/api/programmes/preview", json=bypass)
    assert preview_res.status_code == 200
    preview = preview_res.json()["preview"]
    assert preview["blocking_violations"]
    assert all(v["rule"] == "min_reps: 6" for v in preview["blocking_violations"])

    activate_res = client.post(
        "/api/programmes/activate",
        json={**bypass, "preview_token": preview["preview_token"]},
    )
    assert activate_res.status_code == 422
    assert "Activation blocked by profile constraint" in activate_res.json()["detail"]
    assert database.get_active_programme(legacy_uid, db_path=db_file) is None


def test_request_constraints_add_to_stored_profile_rules(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Request constraints may only tighten the stored profile rules."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)
    database.save_user_preferences(legacy_uid, constraints=["min_reps: 1"], db_path=db_file)

    stricter = {**_payload(goal="maximal_strength"), "constraints": ["min_reps: 6"]}
    preview = client.post("/api/programmes/preview", json=stricter).json()["preview"]
    assert any(v["rule"] == "min_reps: 6" for v in preview["blocking_violations"])


# ---------------------------------------------------------------------------
# Structured (versioned) training rules are enforced by the shared validator
# ---------------------------------------------------------------------------


def test_structured_rep_bounds_block_preview_with_exact_fields() -> None:
    """Versioned rep bounds block preview with exact prescription and remediation."""
    data = _training_data()
    rules = {
        "version": 2,
        "rep_intensity_bounds": {"min_reps": 6, "max_reps": 20},
    }
    req = ProgrammePreviewRequest(
        selected_routine_ids=["routine-a", "routine-b"],
        goal="maximal_strength",
        duration_weeks=12,
        rules=rules,
    )
    preview = build_programme_preview(data, req)
    assert preview["blocking_violations"]
    for violation in preview["blocking_violations"]:
        assert violation["exercise"]
        assert violation["prescription"]
        assert violation["rule"]
        assert violation["permitted_remediation"]
        assert violation["remediation"] == violation["permitted_remediation"]
    assert any(
        "Structured rule" in v["rule"] and "reps" in v["prescription"]
        for v in preview["blocking_violations"]
    )


def test_structured_volume_frequency_equipment_and_intensity_rules() -> None:
    """Structured volume, frequency, equipment, and RPE rules all block deterministically."""
    programme = {
        "days": [
            {
                "focus": "Push",
                "exercises": [
                    {
                        "name": "Bench Press",
                        "equipment": "barbell",
                        "prescriptions": [
                            {"sets": 5, "rep_range": "8-12", "target_rpe": 9}
                        ],
                    }
                ],
            },
            {
                "focus": "Pull",
                "exercises": [
                    {
                        "name": "Chest-supported row",
                        "equipment": "machine",
                        "prescriptions": [{"sets": 4, "rep_range": "10-12"}],
                    }
                ],
            },
        ]
    }
    rules = {
        "version": 1,
        "rep_intensity_bounds": {"max_rpe": 8},
        "volume": {"max_sets_per_exercise": 3, "max_sets_per_workout": 4},
        "frequency": {"max_sessions_per_week": 1},
        "equipment": {"prohibited_equipment": ["machine"]},
    }
    result = validate_programme(programme, training_rules=rules)
    assert result.is_valid is False
    rules_hit = [v["rule"] for v in result.blocking_violations]
    assert any("per exercise" in rule for rule in rules_hit)
    assert any("per workout" in rule for rule in rules_hit)
    assert any("sessions per week" in rule for rule in rules_hit)
    assert any("Prohibited equipment" in rule for rule in rules_hit)
    assert any("maximum RPE" in rule for rule in rules_hit)
    for violation in result.blocking_violations:
        assert violation["exercise"]
        assert violation["prescription"]
        assert violation["permitted_remediation"]


def test_structured_substitution_blocks_until_applied() -> None:
    """A mandatory substitution blocks activation until the source exercise is replaced."""
    programme = {
        "days": [
            {
                "focus": "Push",
                "exercises": [
                    {
                        "name": "Bench Press",
                        "prescriptions": [{"sets": 3, "rep_range": "8-12"}],
                    }
                ],
            }
        ]
    }
    rules = {
        "version": 1,
        "substitutions": [
            {"from_exercise": "Bench Press", "to_exercise": "Dumbbell Bench Press"}
        ],
        "contextual_notes": [
            {
                "category": "joint",
                "note": "Right shoulder impingement",
                "severity": "warning",
            }
        ],
    }
    result = validate_programme(programme, training_rules=rules)
    assert result.is_valid is False
    assert result.blocking_violations == [
        {
            "exercise": "Bench Press",
            "prescription": "Prescribed in routine 'Push'",
            "rule": "Substitute Bench Press with Dumbbell Bench Press",
            "permitted_remediation": "Replace Bench Press with Dumbbell Bench Press.",
            "remediation": "Replace Bench Press with Dumbbell Bench Press.",
            "block_number": None,
            "routine": "Push",
        }
    ]
    assert any(
        "Right shoulder impingement" in warning for warning in result.warnings
    )


def test_structured_rules_apply_to_inference_and_source_import() -> None:
    """The same structured validator runs for inference and source serialisation."""
    data = _training_data(include_banned=True)
    rules = {
        "version": 1,
        "prohibited_exercises": [
            {"exercise": "Bulgarian Split Squats", "reason": "Knees"}
        ],
    }
    inferred = infer_programme(data, training_rules=rules)
    assert inferred.blocking_violations
    assert inferred.blocking_violations[0]["exercise"] == "Bulgarian Split Squats"

    source = serialise_hevy_source(data, training_rules=rules)
    assert source["blocking_violations"]
    assert source["blocking_violations"][0]["exercise"] == "Bulgarian Split Squats"
    assert source["blocking_violations"][0]["permitted_remediation"]


def test_request_structured_rules_cannot_bypass_stored_rules(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Client-supplied rules cannot clear the user's stored structured rules."""
    client, _app, db_file = _client(tmp_path, monkeypatch)
    legacy_uid = database.get_legacy_user_id(db_file)
    database.save_training_rules(
        legacy_uid,
        {
            "version": 1,
            "prohibited_exercises": [
                {"exercise": "Bench Press", "reason": "Shoulder"}
            ],
        },
        db_path=db_file,
    )

    preview_res = client.post(
        "/api/programmes/preview",
        json={**_payload(), "rules": {}},
    )
    assert preview_res.status_code == 200
    preview = preview_res.json()["preview"]
    assert any(
        v["exercise"] == "Bench Press"
        for v in preview["blocking_violations"]
    )


def test_edit_training_rules_reconciles_active_programme(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Saving structured rules reconciles the live programme and reports violations."""
    client, _app, db_file = _client(tmp_path, monkeypatch)

    preview_res = client.post("/api/programmes/preview", json=_payload())
    token = preview_res.json()["preview"]["preview_token"]
    activate_res = client.post(
        "/api/programmes/activate",
        json={**_payload(), "preview_token": token},
    )
    assert activate_res.status_code == 200

    conflict = client.post(
        "/api/settings/rules",
        json={
            "version": 1,
            "prohibited_exercises": [
                {"exercise": "Bench Press", "reason": "Shoulder"}
            ],
        },
    )
    assert conflict.status_code == 200
    body = conflict.json()
    assert any(
        v["exercise"] == "Bench Press" and v["permitted_remediation"]
        for v in body["blocking_violations"]
    )
    assert body["reconciliation"]["status"] == "deactivated_requires_remediation"
    legacy_uid = database.get_legacy_user_id(db_file)
    assert database.get_active_programme(legacy_uid, db_file) is None

    reconciled = client.post(
        "/api/settings/rules",
        json={"version": 2, "frequency": {"max_sessions_per_week": 7}},
    )
    assert reconciled.status_code == 200
    assert "blocking_violations" not in reconciled.json()


def test_seeded_canonical_programme_reconciled_with_structured_rules(
    tmp_path: Any,
) -> None:
    """Seeded canonical defaults pass compatible rules and refuse contradictory ones."""
    db_file = str(tmp_path / "canonical.db")
    database.init_db(db_file)
    legacy_uid = database.get_legacy_user_id(db_file)

    prog_id = database.seed_canonical_programme(legacy_uid, db_file)
    assert prog_id > 0

    database.save_training_rules(
        legacy_uid,
        {"version": 1, "frequency": {"max_sessions_per_week": 1}},
        db_path=db_file,
    )
    assert database.get_active_programme(legacy_uid, db_file) is None
    with pytest.raises(ValueError, match="Activation blocked by profile constraint"):
        database.seed_canonical_programme(legacy_uid, db_file)


@settings(max_examples=60, deadline=None)
@given(
    min_reps=st.integers(min_value=1, max_value=20),
    reps=st.integers(min_value=1, max_value=30),
    sets=st.integers(min_value=1, max_value=10),
    max_sets=st.integers(min_value=1, max_value=10),
)
def test_validator_property_violations_are_exact_and_deterministic(
    min_reps: int,
    reps: int,
    sets: int,
    max_sets: int,
) -> None:
    """Validator output is deterministic and every violation identifies its prescription."""
    programme = {
        "days": [
            {
                "focus": "Property day",
                "exercises": [
                    {
                        "name": "Bench Press",
                        "prescriptions": [
                            {"sets": sets, "rep_range": str(reps)}
                        ],
                    }
                ],
            }
        ]
    }
    rules = {
        "version": 1,
        "rep_intensity_bounds": {"min_reps": min_reps},
        "volume": {"max_sets_per_exercise": max_sets},
    }

    first = validate_programme(programme, training_rules=rules)
    second = validate_programme(programme, training_rules=rules)
    assert first.model_dump() == second.model_dump()

    rep_violated = reps < min_reps
    sets_violated = sets > max_sets
    assert first.is_valid is (not rep_violated and not sets_violated)

    for violation in first.blocking_violations:
        for key in ("exercise", "prescription", "rule", "permitted_remediation"):
            assert isinstance(violation[key], str) and violation[key]

    if rep_violated:
        assert any(
            "reps" in violation["prescription"]
            and "Structured rule" in violation["rule"]
            for violation in first.blocking_violations
        )
    if sets_violated:
        assert any(
            "sets" in violation["prescription"]
            and "Structured rule" in violation["rule"]
            for violation in first.blocking_violations
        )
