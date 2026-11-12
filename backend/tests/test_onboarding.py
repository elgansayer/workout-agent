"""Deterministic coverage for resumable first-run onboarding."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import database
import pytest
from onboarding import (
    STEP_DEFINITIONS,
    OnboardingError,
    OnboardingStepStatus,
    build_onboarding,
    resume_step,
    skip_step,
)
from test_webapp_setup_states import _programme_definition

pytestmark = pytest.mark.usefixtures("encryption_key")


@pytest.fixture()
def encryption_key(monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )


@pytest.fixture()
def db_path(tmp_path: Any) -> str:
    path = str(tmp_path / "onboarding.db")
    database.init_db(path)
    return path


def _user(db_path: str, email: str = "athlete@example.test") -> str:
    return database.get_or_create_user(email, "Synthetic athlete", db_path)["id"]


def _connect_hevy(user_id: str, db_path: str) -> None:
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db_path)


def _activate_programme(user_id: str, db_path: str) -> None:
    today = datetime.now(tz=timezone.utc).date().isoformat()
    database.set_active_programme(
        user_id,
        "hevy",
        "synthetic-hevy",
        _programme_definition(today),
        db_path=db_path,
    )


def _connect_health(user_id: str, db_path: str) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO health_connections
                (id, user_id, provider, state, granted_capabilities,
                 cursor, last_sync_at, created_at, updated_at)
            VALUES (?, ?, 'google_health', 'connected', '[]', NULL, NULL, ?, ?)
            """,
            ("synthetic-connection", user_id, now, now),
        )


def _step(state: Any, step_id: str) -> Any:
    return next(step for step in state.steps if step.id == step_id)


def test_the_sequence_covers_every_documented_first_run_step() -> None:
    assert [step.id for step in STEP_DEFINITIONS] == [
        "preferences",
        "hevy",
        "health_data",
        "ai_provider",
        "programme",
        "notifications",
    ]
    required = {step.id for step in STEP_DEFINITIONS if not step.optional}
    assert required == {"hevy", "programme"}
    for step in STEP_DEFINITIONS:
        assert step.href.startswith("/")
        assert step.description
        if step.optional:
            assert step.skip_consequence


def test_a_new_account_is_sent_to_connect_hevy_first(db_path: str) -> None:
    user_id = _user(db_path)

    state = build_onboarding(user_id=user_id, db_path=db_path)

    assert state.account_ready is False
    assert state.completed_count == 0
    assert state.skipped_count == 0
    assert state.progress_percent == 0
    assert state.next_step is not None
    assert state.next_step.id == "hevy"
    assert _step(state, "hevy").status is OnboardingStepStatus.PENDING
    assert _step(state, "programme").status is OnboardingStepStatus.PENDING


def test_required_records_make_the_account_ready(db_path: str) -> None:
    user_id = _user(db_path)
    _connect_hevy(user_id, db_path)
    _activate_programme(user_id, db_path)

    state = build_onboarding(user_id=user_id, db_path=db_path)

    assert state.account_ready is True
    assert _step(state, "hevy").status is OnboardingStepStatus.COMPLETE
    assert _step(state, "programme").status is OnboardingStepStatus.COMPLETE
    assert state.completed_count == 2
    assert state.next_step is not None
    assert state.next_step.id == "preferences"


def test_optional_steps_are_verified_from_server_records(db_path: str) -> None:
    user_id = _user(db_path)
    database.save_user_preferences(user_id, goals=["strength"], db_path=db_path)
    _connect_health(user_id, db_path)
    database.save_push_subscription(
        user_id,
        endpoint="https://push.example.test/subscription",
        p256dh="synthetic-p256dh",
        auth="synthetic-auth",
        db_path=db_path,
    )

    state = build_onboarding(
        user_id=user_id,
        db_path=db_path,
        server_ai_key="synthetic-server-key",
    )

    for step_id in ("preferences", "health_data", "ai_provider", "notifications"):
        assert _step(state, step_id).status is OnboardingStepStatus.COMPLETE, step_id


def test_skipping_an_optional_step_is_persisted_for_the_tenant(db_path: str) -> None:
    user_id = _user(db_path)

    state = skip_step(user_id, "health_data", db_path=db_path)

    assert _step(state, "health_data").status is OnboardingStepStatus.SKIPPED
    assert state.skipped_count == 1
    assert state.completed_count == 0

    reloaded = build_onboarding(user_id=user_id, db_path=db_path)
    assert _step(reloaded, "health_data").status is OnboardingStepStatus.SKIPPED


def test_resuming_a_skipped_step_puts_it_back_in_the_sequence(db_path: str) -> None:
    user_id = _user(db_path)
    skip_step(user_id, "notifications", db_path=db_path)

    state = resume_step(user_id, "notifications", db_path=db_path)

    assert _step(state, "notifications").status is OnboardingStepStatus.PENDING
    assert state.skipped_count == 0


def test_a_required_step_cannot_be_skipped(db_path: str) -> None:
    user_id = _user(db_path)

    with pytest.raises(OnboardingError, match="required"):
        skip_step(user_id, "hevy", db_path=db_path)

    state = build_onboarding(user_id=user_id, db_path=db_path)
    assert _step(state, "hevy").status is OnboardingStepStatus.PENDING


def test_unknown_steps_are_rejected(db_path: str) -> None:
    user_id = _user(db_path)

    with pytest.raises(OnboardingError, match="Unknown onboarding step"):
        skip_step(user_id, "not_a_step", db_path=db_path)


def test_a_skip_belongs_to_one_tenant_only(db_path: str) -> None:
    first = _user(db_path, "first@example.test")
    second = _user(db_path, "second@example.test")

    skip_step(first, "health_data", db_path=db_path)

    first_state = build_onboarding(user_id=first, db_path=db_path)
    second_state = build_onboarding(user_id=second, db_path=db_path)
    assert _step(first_state, "health_data").status is OnboardingStepStatus.SKIPPED
    assert _step(second_state, "health_data").status is OnboardingStepStatus.PENDING


def test_a_verified_completion_wins_over_an_old_skip(db_path: str) -> None:
    user_id = _user(db_path)
    skip_step(user_id, "preferences", db_path=db_path)
    database.save_user_preferences(user_id, goals=["strength"], db_path=db_path)

    state = build_onboarding(user_id=user_id, db_path=db_path)

    assert _step(state, "preferences").status is OnboardingStepStatus.COMPLETE
    assert state.skipped_count == 0


def test_an_empty_user_id_is_rejected(db_path: str) -> None:
    with pytest.raises(OnboardingError):
        build_onboarding(user_id="", db_path=db_path)
