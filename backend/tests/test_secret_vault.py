"""Lifecycle, isolation and audit tests for the per-user credential vault."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from cryptography.fernet import Fernet
from database import get_or_create_user, init_db, set_meta
from secret_vault import (
    CredentialExistsError,
    CredentialUnavailableError,
    InvalidSecretError,
    SecretValue,
    SecretVault,
    UnavailableReason,
    VaultUnavailableError,
    VerificationResult,
)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())


def _db(tmp_path: Any) -> str:
    path = str(tmp_path / "vault.db")
    init_db(path)
    return path


def _user(db: str, email: str) -> str:
    return get_or_create_user(email, db_path=db)["id"]


def _row(db: str, user_id: str, provider: str) -> tuple[Any, ...] | None:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT api_key, client_secret, refresh_token, key_hint, key_version, "
            "status FROM user_api_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()


def _hevy_ok(credential: Any) -> VerificationResult:
    assert credential.api_key is not None
    if credential.api_key.reveal() == "synthetic-rejected-key":
        return VerificationResult(False, "rejected", "The key was rejected.")
    return VerificationResult(True, "ok", "Connected.", {"workout_count": 7})


def test_lifecycle_masks_and_never_exposes_plaintext(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    other = _user(db, "other@example.test")
    vault = SecretVault(db)
    secret = "synthetic-hevy-key-1234"

    summary = vault.store(owner, "hevy", api_key=secret)

    assert summary.configured is True
    assert summary.masked == "••••••••1234"
    assert secret not in repr(summary)
    assert secret not in repr(summary.to_public_dict())

    raw = _row(db, owner, "hevy")
    assert raw is not None
    assert raw[0].startswith("wv2.")
    assert raw[0] != secret
    assert raw[3] == "1234"

    resolved = vault.resolve(owner, "hevy", purpose="hevy_sync")
    assert resolved.api_key is not None
    assert resolved.api_key.reveal() == secret
    assert secret not in repr(resolved)
    assert secret not in str(resolved.api_key)
    assert secret not in f"{resolved.api_key}"

    with pytest.raises(CredentialUnavailableError) as missing:
        vault.resolve(other, "hevy", purpose="hevy_sync")
    assert missing.value.reason is UnavailableReason.NOT_CONFIGURED

    vault.store(other, "hevy", api_key="synthetic-other-key-5678")
    assert (
        vault.resolve(other, "hevy", purpose="hevy_sync").api_key.reveal()
        == "synthetic-other-key-5678"
    )


def test_secret_value_refuses_serialisation_and_copy(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    resolved = SecretVault(db).store(owner, "hevy", api_key="synthetic-key-abcdef")
    assert resolved.configured

    credential = SecretVault(db).resolve(owner, "hevy", purpose="hevy_sync")
    with pytest.raises(TypeError):
        credential.api_key.__reduce__()
    assert SecretValue("a") == SecretValue("a")


def test_rotate_replaces_material_and_increments_version(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-old-key-1234")

    summary = vault.rotate(owner, "hevy", api_key="synthetic-new-key-5678")

    assert summary.version == 2
    assert summary.masked == "••••••••5678"
    assert (
        vault.resolve(owner, "hevy", purpose="hevy_sync").api_key.reveal()
        == "synthetic-new-key-5678"
    )
    actions = [event.action for event in vault.recent_events(owner)]
    assert actions == ["rotate", "save"]
    assert all(event.outcome == "success" for event in vault.recent_events(owner))


def test_store_rejects_duplicate_and_requires_valid_secret(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-key-1234")

    with pytest.raises(CredentialExistsError):
        vault.store(owner, "hevy", api_key="synthetic-key-5678")
    with pytest.raises(InvalidSecretError):
        vault.store(owner, "hevy", api_key="contains a space")
    assert vault.recent_events(owner)[0].outcome == "failure"


def test_test_operations_are_audited_and_candidate_is_not_stored(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-key-1234")

    stored = vault.verify_stored(owner, "hevy", verifier=_hevy_ok)
    assert stored.ok is True
    candidate = vault.verify_candidate(
        owner,
        "hevy",
        verifier=_hevy_ok,
        api_key="synthetic-candidate-key-9999",
    )
    assert candidate.ok is True
    assert _row(db, owner, "hevy")[0] != "synthetic-candidate-key-9999"

    events = vault.recent_events(owner)
    assert [event.action for event in events] == ["test", "test", "save"]
    assert {event.detail for event in events if event.action == "test"} >= {
        "ok",
        "candidate:ok",
    }

    rejected = vault.verify_candidate(
        owner,
        "hevy",
        verifier=_hevy_ok,
        api_key="synthetic-rejected-key",
    )
    assert rejected.ok is False
    assert vault.describe(owner, "hevy").last_test_status == "ok"


def test_revoke_destroys_material_and_delete_keeps_audit(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-key-1234")

    revoked = vault.revoke(owner, "hevy")
    assert revoked.status.value == "revoked"
    assert revoked.configured is False
    raw = _row(db, owner, "hevy")
    assert raw is not None
    assert raw[0] == ""
    assert raw[1] is None and raw[2] is None
    with pytest.raises(CredentialUnavailableError) as unavailable:
        vault.resolve(owner, "hevy", purpose="hevy_sync")
    assert unavailable.value.reason is UnavailableReason.REVOKED

    # Revoking again is idempotent and does not create a second event.
    vault.revoke(owner, "hevy")
    assert [e.action for e in vault.recent_events(owner)] == ["revoke", "save"]

    assert vault.delete(owner, "hevy") is True
    assert _row(db, owner, "hevy") is None
    assert vault.delete(owner, "hevy") is False
    assert [e.action for e in vault.recent_events(owner)] == [
        "delete",
        "revoke",
        "save",
    ]


def test_revoked_credential_can_be_replaced(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-old-key-1234")
    vault.revoke(owner, "hevy")

    summary = vault.store(owner, "hevy", api_key="synthetic-new-key-5678")

    assert summary.configured is True
    assert (
        vault.resolve(owner, "hevy", purpose="hevy_sync").api_key.reveal()
        == "synthetic-new-key-5678"
    )


def test_ciphertext_copied_to_another_user_fails_closed(tmp_path: Any) -> None:
    db = _db(tmp_path)
    alice = _user(db, "alice@example.test")
    bob = _user(db, "bob@example.test")
    vault = SecretVault(db)
    vault.store(alice, "hevy", api_key="synthetic-alice-key-1234")
    vault.store(bob, "hevy", api_key="synthetic-bob-key-5678")
    alice_token = _row(db, alice, "hevy")[0]

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE user_api_keys SET api_key = ? WHERE user_id = ? AND provider = ?",
            (alice_token, bob, "hevy"),
        )

    with pytest.raises(CredentialUnavailableError) as caught:
        vault.resolve(bob, "hevy", purpose="hevy_sync")
    assert caught.value.reason is UnavailableReason.DECRYPTION_FAILED


def test_unavailable_encryption_fails_closed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    vault = SecretVault(db)
    vault.store(owner, "hevy", api_key="synthetic-key-1234")

    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    assert vault.availability().available is False
    with pytest.raises(VaultUnavailableError):
        vault.store(owner, "claude", api_key="synthetic-claude-key")
    with pytest.raises(CredentialUnavailableError) as unavailable:
        vault.resolve(owner, "hevy", purpose="hevy_sync")
    assert unavailable.value.reason is UnavailableReason.VAULT_UNAVAILABLE


def test_wrong_master_key_reports_decryption_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    SecretVault(db).store(owner, "hevy", api_key="synthetic-key-1234")

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(CredentialUnavailableError) as caught:
        SecretVault(db).resolve(owner, "hevy", purpose="hevy_sync")
    assert caught.value.reason is UnavailableReason.DECRYPTION_FAILED


def test_migrate_legacy_plaintext_and_fernet_rows(tmp_path: Any) -> None:
    import os

    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    key = os.environ["ENCRYPTION_KEY"]
    fernet_token = Fernet(key.encode()).encrypt(b"synthetic-fernet-key").decode()
    now = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO user_api_keys "
            "(user_id, provider, api_key, created_at, updated_at) "
            "VALUES (?, 'hevy', 'synthetic-plaintext-key', ?, ?)",
            (owner, now, now),
        )
        conn.execute(
            "INSERT INTO user_api_keys "
            "(user_id, provider, api_key, created_at, updated_at) "
            "VALUES (?, 'claude', ?, ?, ?)",
            (owner, fernet_token, now, now),
        )

    report = SecretVault(db).migrate_legacy()

    assert report.upgraded == 2
    assert report.pending == 0
    hevy = _row(db, owner, "hevy")
    claude = _row(db, owner, "claude")
    assert hevy[0].startswith("wv2.")
    assert claude[0].startswith("wv2.")
    vault = SecretVault(db)
    assert (
        vault.resolve(owner, "hevy", purpose="hevy_sync").api_key.reveal()
        == "synthetic-plaintext-key"
    )
    assert (
        vault.resolve(owner, "claude", purpose="ai_generate").api_key.reveal()
        == "synthetic-fernet-key"
    )


def test_migrate_legacy_google_health_meta_token(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    set_meta(
        "google_health_refresh_token",
        "synthetic-google-refresh-token",
        db,
        user_id=owner,
    )

    report = SecretVault(db).migrate_legacy()

    assert report.upgraded == 1
    credential = SecretVault(db).resolve(
        owner,
        "google_health",
        purpose="health_sync",
    )
    assert credential.refresh_token is not None
    assert credential.refresh_token.reveal() == "synthetic-google-refresh-token"
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT value FROM hevy_meta WHERE user_id = ? AND key = ?",
                (owner, "google_health_refresh_token"),
            ).fetchone()[0]
            == ""
        )


def test_credential_owner_and_audit_rows_are_immutable(tmp_path: Any) -> None:
    db = _db(tmp_path)
    owner = _user(db, "owner@example.test")
    other = _user(db, "other@example.test")
    SecretVault(db).store(owner, "hevy", api_key="synthetic-key-1234")

    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE user_api_keys SET user_id = ? WHERE user_id = ?",
                (other, owner),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE credential_audit_events SET outcome = 'tampered' "
                "WHERE user_id = ?",
                (owner,),
            )


def test_has_usable_credential_answers_yes_or_no_and_never_the_secret(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db(tmp_path)
    alice = _user(db, "alice@example.test")
    bob = _user(db, "bob@example.test")
    vault = SecretVault(db)
    vault.store(alice, "hevy", api_key="synthetic-alice-key-1234")

    # Only the owner's own credential counts; another account's never does.
    result = vault.has_usable_credential(alice, "hevy")
    assert result is True
    assert vault.has_usable_credential(bob, "hevy") is False
    assert vault.has_usable_credential(alice, "claude") is False

    # A readiness check is not a use of the credential: nothing is audited.
    assert [e.action for e in vault.recent_events(alice)] == ["save"]

    # Revoked, undecryptable and "cannot decrypt at all" are all simply False.
    vault.store(bob, "hevy", api_key="synthetic-bob-key-5678")
    vault.revoke(bob, "hevy")
    assert vault.has_usable_credential(bob, "hevy") is False

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert vault.has_usable_credential(alice, "hevy") is False
    monkeypatch.delenv("ENCRYPTION_KEY")
    assert vault.has_usable_credential(alice, "hevy") is False


def test_has_usable_credential_still_requires_a_real_owner_and_provider(
    tmp_path: Any,
) -> None:
    from secret_vault import UnknownProviderError, UnknownUserError

    vault = SecretVault(_db(tmp_path))
    with pytest.raises(UnknownUserError):
        vault.has_usable_credential("   ", "hevy")
    with pytest.raises(UnknownProviderError):
        vault.has_usable_credential("someone", "not-a-provider")
