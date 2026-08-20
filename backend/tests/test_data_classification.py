from __future__ import annotations

from data_classification import (
    FIELD_CLASSES,
    POLICIES,
    DataClass,
    classify_field,
    redact_for_log,
    safe_log_value,
    sanitize_for_export,
)


def test_every_data_class_has_complete_handling_policy() -> None:
    assert set(POLICIES) == set(DataClass)
    for policy in POLICIES.values():
        assert policy.storage
        assert policy.encryption
        assert policy.logging
        assert policy.retention
        assert policy.access


def test_sensitive_field_registry_covers_database_security_boundaries() -> None:
    expected = {
        "user_id": DataClass.IDENTIFIER,
        "email": DataClass.IDENTIFIER,
        "api_key": DataClass.CREDENTIAL,
        "client_secret": DataClass.CREDENTIAL,
        "refresh_token": DataClass.CREDENTIAL,
        "endpoint": DataClass.IDENTIFIER,
        "p256dh": DataClass.CREDENTIAL,
        "auth": DataClass.CREDENTIAL,
        "hevy_payload": DataClass.WORKOUT,
        "weight_kg": DataClass.HEALTH,
        "body_fat_pct": DataClass.HEALTH,
        "resting_hr": DataClass.HEALTH,
        "hrv": DataClass.HEALTH,
        "content": DataClass.PROMPT,
        "reasoning": DataClass.PROMPT,
        "insight_json": DataClass.ANALYTICS,
    }
    for field_name, classification in expected.items():
        assert FIELD_CLASSES[field_name] is classification


def test_unknown_fields_fail_closed_to_internal() -> None:
    assert classify_field("future_unreviewed_field") is DataClass.INTERNAL


def test_name_patterns_classify_new_sensitive_fields() -> None:
    assert classify_field("provider_access_token_v2") is DataClass.CREDENTIAL
    assert classify_field("nightly_sleep_score") is DataClass.HEALTH
    assert classify_field("latest_workout_payload") is DataClass.WORKOUT
    assert classify_field("coach_user_prompt") is DataClass.PROMPT
    assert classify_field("member_user_id") is DataClass.IDENTIFIER
    assert classify_field("retention_analytics_score") is DataClass.ANALYTICS


def test_safe_log_value_never_returns_sensitive_raw_value() -> None:
    secret = "super-secret-token"
    assert safe_log_value("refresh_token", secret) == "[REDACTED:CREDENTIAL]"
    assert secret not in str(safe_log_value("refresh_token", secret))
    assert safe_log_value("email", "person@example.com") == "[REDACTED:IDENTIFIER]"
    assert safe_log_value("weight_kg", 82.4) == "[REDACTED:HEALTH]"
    assert safe_log_value("workout", {"sets": 4}) == "[REDACTED:WORKOUT]"
    assert safe_log_value("prompt", "private prompt") == "[REDACTED:PROMPT]"


def test_recursive_log_redaction_handles_nested_payloads_and_lists() -> None:
    payload = {
        "provider": "google_health",
        "refresh_token": "token-123",
        "profile": {"email": "person@example.com", "weight_kg": 80.0},
        "workouts": [{"exercise_name": "Squat", "top_reps": 5}],
        "prompt": "Use my recent recovery data",
    }

    redacted = redact_for_log(payload)

    rendered = repr(redacted)
    assert "token-123" not in rendered
    assert "person@example.com" not in rendered
    assert "80.0" not in rendered
    assert "Squat" not in rendered
    assert "Use my recent recovery data" not in rendered
    assert redacted["refresh_token"] == "[REDACTED:CREDENTIAL]"
    assert redacted["profile"]["email"] == "[REDACTED:IDENTIFIER]"
    assert redacted["workouts"] == "[REDACTED:WORKOUT]"


def test_export_sanitizer_excludes_credentials_internal_and_derived_analytics() -> None:
    account_payload = {
        "email": "person@example.com",
        "weight_kg": 82.4,
        "workout": {"exercise_name": "Squat", "sets": 4},
        "user_prompt": "Help with my programme",
        "api_key": "sk-secret",
        "refresh_token": "refresh-secret",
        "system_prompt": "private operator instructions",
        "insight_json": {"risk_score": 0.91},
        "correlation": 0.72,
        "future_unreviewed_field": "must fail closed",
    }

    exported = sanitize_for_export(account_payload)

    assert exported == {
        "email": "person@example.com",
        "weight_kg": 82.4,
        "workout": {"exercise_name": "Squat", "sets": 4},
        "user_prompt": "Help with my programme",
    }
    rendered = repr(exported)
    assert "sk-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "private operator instructions" not in rendered
    assert "risk_score" not in rendered
    assert "must fail closed" not in rendered


def test_credentials_are_never_exportable() -> None:
    assert POLICIES[DataClass.CREDENTIAL].exportable is False
    assert POLICIES[DataClass.INTERNAL].exportable is False
    assert POLICIES[DataClass.ANALYTICS].exportable is False
