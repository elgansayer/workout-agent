"""Tests for encryption.py: Fernet-based symmetric encryption for API keys."""

from __future__ import annotations

from unittest import mock

import pytest

from encryption import decrypt, encrypt


class TestEncryptDecryptRoundTrip:
    """Encrypt then decrypt should return the original plaintext."""

    def test_round_trip_with_valid_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("ENCRYPTION_KEY", key)
        plaintext = "sk-abc123-def456"
        ciphertext = encrypt(plaintext)
        assert ciphertext != plaintext
        assert decrypt(ciphertext) == plaintext

    def test_round_trip_without_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        plaintext = "sk-abc123-def456"
        assert encrypt(plaintext) == plaintext
        assert decrypt(plaintext) == plaintext

    def test_round_trip_with_empty_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ENCRYPTION_KEY", "")
        plaintext = "sk-abc123-def456"
        assert encrypt(plaintext) == plaintext
        assert decrypt(plaintext) == plaintext

    def test_decrypt_plaintext_unchanged_when_no_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
        stored = "legacy-plaintext-key"
        assert decrypt(stored) == stored

    def test_decrypt_invalid_token_returns_original(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("ENCRYPTION_KEY", key)
        not_a_token = "this-is-just-plaintext"
        assert decrypt(not_a_token) == not_a_token

    def test_encrypt_different_keys_produce_different_ciphertext(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cryptography.fernet import Fernet

        key1 = Fernet.generate_key().decode()
        plaintext = "sk-sensitive-data"
        monkeypatch.setenv("ENCRYPTION_KEY", key1)
        ct1 = encrypt(plaintext)

        key2 = Fernet.generate_key().decode()
        monkeypatch.setenv("ENCRYPTION_KEY", key2)
        ct2 = encrypt(plaintext)

        assert ct1 != ct2

    def test_encrypt_same_key_produces_different_ciphertext_each_time(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("ENCRYPTION_KEY", key)
        plaintext = "sk-data"
        ct1 = encrypt(plaintext)
        ct2 = encrypt(plaintext)
        # Fernet uses random IV, so same plaintext yields different tokens
        assert ct1 != ct2
        assert decrypt(ct1) == plaintext
        assert decrypt(ct2) == plaintext


class TestEncryptDecryptWithInvalidKey:
    """Graceful fallback when ENCRYPTION_KEY is malformed."""

    def test_invalid_key_falls_back_to_plaintext(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ENCRYPTION_KEY", "not-a-valid-fernet-key")
        plaintext = "sk-secret"
        assert encrypt(plaintext) == plaintext
        assert decrypt(plaintext) == plaintext


class TestEncryptDecryptWithoutCryptography:
    """Graceful fallback when cryptography package is not importable."""

    def test_generate_falls_back_when_fernet_is_none(self) -> None:
        with mock.patch("encryption._fernet", return_value=None):
            plaintext = "sk-fallback"
            assert encrypt(plaintext) == plaintext
            assert decrypt(plaintext) == plaintext
