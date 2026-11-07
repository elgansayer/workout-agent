"""Tests for the per-user authenticated credential encryption primitives."""

from __future__ import annotations

from unittest import mock

import encryption
import pytest
from cryptography.fernet import Fernet
from encryption import (
    DecryptionError,
    EncryptionUnavailableError,
    is_sealed,
    load_master_key,
    open_sealed,
    read_legacy_value,
    seal,
)


def _configure(monkeypatch: pytest.MonkeyPatch) -> encryption.MasterKey:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    return load_master_key()


def _seal(master: encryption.MasterKey, plaintext: str = "sk-synthetic-secret") -> str:
    return seal(
        master,
        user_id="user-a",
        provider="openai",
        field_name="api_key",
        plaintext=plaintext,
    )


def test_round_trip_is_bound_to_user_provider_and_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = _configure(monkeypatch)
    token = _seal(master)

    assert is_sealed(token)
    assert token.startswith("wv2.")
    assert "sk-synthetic-secret" not in token
    assert (
        open_sealed(
            master,
            user_id="user-a",
            provider="openai",
            field_name="api_key",
            token=token,
        )
        == "sk-synthetic-secret"
    )


def test_ciphertext_differs_between_users_for_the_same_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = _configure(monkeypatch)
    first = _seal(master)
    second = seal(
        master,
        user_id="user-b",
        provider="openai",
        field_name="api_key",
        plaintext="sk-synthetic-secret",
    )

    assert first != second


@pytest.mark.parametrize(
    ("user_id", "provider", "field_name"),
    [
        ("user-b", "openai", "api_key"),
        ("user-a", "claude", "api_key"),
        ("user-a", "openai", "client_secret"),
    ],
)
def test_binding_mismatch_fails_authentication(
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    provider: str,
    field_name: str,
) -> None:
    master = _configure(monkeypatch)
    token = _seal(master)

    with pytest.raises(DecryptionError) as caught:
        open_sealed(
            master,
            user_id=user_id,
            provider=provider,
            field_name=field_name,
            token=token,
        )

    assert caught.value.reason == "authentication_failed"


def test_wrong_master_key_is_reported_as_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _seal(_configure(monkeypatch))
    other = load_master_key(
        {"ENCRYPTION_KEY": Fernet.generate_key().decode()},
    )

    with pytest.raises(DecryptionError) as caught:
        open_sealed(
            other,
            user_id="user-a",
            provider="openai",
            field_name="api_key",
            token=token,
        )
    assert caught.value.reason == "key_mismatch"


def test_tampered_ciphertext_never_decrypts(monkeypatch: pytest.MonkeyPatch) -> None:
    master = _configure(monkeypatch)
    token = _seal(master)
    version, key_id, body = token.split(".")
    tampered = f"{version}.{key_id}.{body[:-2]}{'AA' if body[-2:] != 'AA' else 'BB'}"

    with pytest.raises(DecryptionError):
        open_sealed(
            master,
            user_id="user-a",
            provider="openai",
            field_name="api_key",
            token=tampered,
        )


def test_missing_or_invalid_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(EncryptionUnavailableError) as missing:
        load_master_key()
    assert missing.value.reason == "not_configured"

    monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(EncryptionUnavailableError) as invalid:
        load_master_key()
    assert invalid.value.reason == "invalid_key"


def test_missing_cryptography_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    with (
        mock.patch("encryption._HAS_CRYPTOGRAPHY", False),
        pytest.raises(EncryptionUnavailableError) as caught,
    ):
        load_master_key()
    assert caught.value.reason == "cryptography_missing"


def test_legacy_fernet_token_is_read_only_for_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_key = Fernet.generate_key()
    monkeypatch.setenv("ENCRYPTION_KEY", raw_key.decode())
    master = load_master_key()
    legacy = Fernet(raw_key).encrypt(b"legacy-secret").decode()

    assert read_legacy_value(master, legacy) == "legacy-secret"
    assert read_legacy_value(master, "legacy-plaintext") == "legacy-plaintext"


def test_legacy_fernet_token_under_another_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = _configure(monkeypatch)
    foreign = Fernet(Fernet.generate_key()).encrypt(b"legacy-secret").decode()

    with pytest.raises(DecryptionError) as caught:
        read_legacy_value(master, foreign)
    assert caught.value.reason == "legacy_undecryptable"


def test_master_key_repr_never_contains_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = _configure(monkeypatch)
    inline = repr(master)
    standalone = f"{master}"

    assert "MasterKey" in inline
    assert master.key_id in inline
    assert master.key_id in standalone
    assert master._material.hex() not in inline
