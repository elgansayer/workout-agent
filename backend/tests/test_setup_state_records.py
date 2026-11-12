"""Readiness derived from a tenant's real records, never from another tenant's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import database
import pytest
from connector_operations import (
    OperationPhase,
    SyncOutcome,
    begin_operation,
    record_sync_outcome,
)
from secret_vault import SecretVault, VerificationResult
from setup_state import (
    ProviderState,
    SetupStateKind,
    ai_provider_configured,
    ai_provider_status,
    build_account_setup,
    surface_setup,
)

_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: Any) -> str:
    monkeypatch.setenv("ENCRYPTION_KEY", _ENCRYPTION_KEY)
    path = str(tmp_path / "setup-records.db")
    database.init_db(path)
    return path


def _user(db: str, name: str) -> str:
    return database.get_or_create_user(f"{name}@example.test", name, db)["id"]


def _verdict(ok: bool, code: str = "ok"):
    return lambda _credential: VerificationResult(ok, code, code)


def _status(db: str, user_id: str, server_key: str | None = None):
    return ai_provider_status(user_id=user_id, db_path=db, server_api_key=server_key)


# -- AI provider status ------------------------------------------------------


def test_no_key_anywhere_is_missing(db: str) -> None:
    status = _status(db, _user(db, "alice"))

    assert status.state is ProviderState.MISSING_KEY
    assert status.configured is False
    assert status.source == "none"
    assert ai_provider_configured(user_id=_user(db, "alice"), db_path=db) is False


def test_the_shared_server_key_is_usable_but_never_verified(db: str) -> None:
    status = _status(db, _user(db, "alice"), server_key="synthetic-server-key")

    assert status.state is ProviderState.UNVERIFIED
    assert status.configured is True
    assert status.source == "server"
    assert status.last_tested_at is None
    assert status.as_dict()["verified"] is False


def test_a_stored_key_is_unverified_until_a_connection_test_passes(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)

    untested = _status(db, alice)
    assert untested.state is ProviderState.UNVERIFIED
    assert untested.source == "user"
    assert untested.last_test_status is None

    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(True))
    verified = _status(db, alice)
    assert verified.state is ProviderState.VERIFIED
    assert verified.last_test_status == "ok"
    assert verified.last_tested_at
    assert verified.as_dict()["verified"] is True


def test_a_failed_connection_test_is_remembered_with_its_code_only(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)

    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(False, "rejected"))

    status = _status(db, alice, server_key="synthetic-server-key")
    # A failing own key is never papered over by the shared server key.
    assert status.state is ProviderState.FAILED
    assert status.source == "user"
    assert status.last_test_status == "rejected"
    assert "synthetic-ai-key" not in repr(status.as_dict())


def test_replacing_the_key_clears_the_old_keys_verification(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)
    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(True))
    assert _status(db, alice).state is ProviderState.VERIFIED

    SecretVault(db).rotate(alice, "gemini", api_key="synthetic-new-key")

    assert _status(db, alice).state is ProviderState.UNVERIFIED


def test_a_revoked_key_no_longer_counts_as_configured(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)
    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(True))

    SecretVault(db).revoke(alice, "gemini")

    status = _status(db, alice)
    assert status.state is ProviderState.MISSING_KEY
    assert status.configured is False


def test_an_unsupported_preferred_provider_is_not_papered_over_by_gemini(
    db: str,
) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)
    database.save_user_preferences(alice, preferred_ai="not-a-provider", db_path=db)

    status = _status(db, alice, server_key="synthetic-server-key")

    assert status.state is ProviderState.UNSUPPORTED
    assert status.configured is False
    assert ai_provider_configured(user_id=alice, db_path=db, server_api_key="x") is False


def test_another_tenants_verified_key_never_counts(db: str) -> None:
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)
    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(True))

    assert _status(db, alice).state is ProviderState.VERIFIED
    assert _status(db, bob).state is ProviderState.MISSING_KEY


def test_a_non_gemini_provider_never_borrows_the_server_gemini_key(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_preferences(alice, preferred_ai="claude", db_path=db)

    status = _status(db, alice, server_key="synthetic-server-key")

    assert status.state is ProviderState.MISSING_KEY
    assert status.configured is False


# -- account readiness from stored records ------------------------------------


def _connected(db: str, name: str) -> str:
    user_id = _user(db, name)
    database.save_user_api_key(user_id, "hevy", "synthetic-hevy-key", db_path=db)
    return user_id


def test_a_fresh_account_reads_as_setup_required_and_first_run(db: str) -> None:
    account = build_account_setup(user_id=_user(db, "alice"), db_path=db, now=NOW)

    assert account.state is SetupStateKind.SETUP_REQUIRED
    assert account.first_run is True
    assert account.connector is not None
    assert account.connector.operation is None


def test_a_stored_key_hevy_rejected_reads_as_degraded(db: str) -> None:
    alice = _connected(db, "alice")
    database.save_hevy_connection(
        alice,
        state="invalid",
        api_version="v1",
        capabilities={},
        tested_at=(NOW - timedelta(hours=1)).isoformat(),
        db_path=db,
    )

    account = build_account_setup(user_id=alice, db_path=db, now=NOW)

    assert account.state is SetupStateKind.DEGRADED
    assert account.reason_code == "credential_rejected"
    assert account.connection_state == "invalid"


def test_a_connection_record_without_a_key_is_never_trusted(db: str) -> None:
    alice = _user(db, "alice")
    database.save_hevy_connection(
        alice,
        state="connected",
        api_version="v1",
        capabilities={},
        tested_at=NOW.isoformat(),
        db_path=db,
    )

    account = build_account_setup(user_id=alice, db_path=db, now=NOW)

    assert account.connection_state is None
    assert account.checks["hevy_connected"] is False
    assert account.state is SetupStateKind.SETUP_REQUIRED


def test_a_live_operation_is_read_back_as_the_account_state(db: str) -> None:
    alice = _connected(db, "alice")
    bob = _connected(db, "bob")
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)

    live = build_account_setup(user_id=alice, db_path=db, now=NOW + timedelta(minutes=2))
    other = build_account_setup(user_id=bob, db_path=db, now=NOW + timedelta(minutes=2))

    assert live.state is SetupStateKind.SYNCING
    assert "2 minutes ago" in live.reason
    assert other.state is not SetupStateKind.SYNCING
    assert other.connector is not None and other.connector.operation is None


def test_a_recorded_failure_is_read_back_and_a_success_replaces_it(db: str) -> None:
    alice = _connected(db, "alice")
    record_sync_outcome(
        alice, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db, now=NOW
    )

    failed = build_account_setup(user_id=alice, db_path=db, now=NOW)
    assert failed.state is SetupStateKind.DEGRADED
    assert failed.reason_code == "sync_failed"

    record_sync_outcome(alice, SyncOutcome.SUCCEEDED, records=0, db_path=db, now=NOW)
    recovered = build_account_setup(user_id=alice, db_path=db, now=NOW)
    assert recovered.state is SetupStateKind.SETUP_REQUIRED
    # The account-level step is the programme; the surfaces that need history say
    # that the sync finished but Hevy has no workouts, instead of "sync again".
    assert recovered.reason_code == "programme_active"
    history = surface_setup(recovered, "history")
    assert "found no logged workouts" in history.reason
    assert history.action is not None and history.action.id == "sync_hevy"


def test_the_provider_verification_reaches_the_account_payload(db: str) -> None:
    alice = _user(db, "alice")
    database.save_user_api_key(alice, "gemini", "synthetic-ai-key", db_path=db)
    SecretVault(db).verify_stored(alice, "gemini", verifier=_verdict(True))

    account = build_account_setup(user_id=alice, db_path=db, now=NOW)

    assert account.ai_provider_state == "verified"
    assert account.ai_provider_source == "user"
    assert account.checks["ai_provider"] is True
    assert account.checks["ai_verified"] is True
    assert account.as_dict()["ai_provider_state"] == "verified"


def test_the_shared_server_key_is_reported_with_its_source(db: str) -> None:
    alice = _user(db, "alice")

    account = build_account_setup(
        user_id=alice, db_path=db, now=NOW, server_ai_key="synthetic-server-key"
    )

    assert account.ai_provider_state == "unverified"
    assert account.ai_provider_source == "server"
    assert account.checks["ai_provider"] is True
    assert account.checks["ai_verified"] is False
