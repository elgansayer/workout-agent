"""Tests for the encryption module (Fernet-based API key encryption)."""

from __future__ import annotations

import os
from unittest import mock

from cryptography.fernet import Fernet

from encryption import decrypt, encrypt


class TestEncryptDecryptWithValidKey:
    """When ENCRYPTION_KEY is a valid Fernet key, encrypt/decrypt round-trip."""

    @staticmethod
    def _set_key(key: str) -> None:
        os.environ["ENCRYPTION_KEY"] = key

    @staticmethod
    def _clear_key() -> None:
        os.environ.pop("ENCRYPTION_KEY", None)

    def test_round_trip_ascii(self):
        key = Fernet.generate_key().decode()
        self._set_key(key)
        try:
            plaintext = "hevy_api_key_abc123"
            encrypted = encrypt(plaintext)
            assert encrypted != plaintext
            assert encrypted.startswith("gAAAAA")
            assert decrypt(encrypted) == plaintext
        finally:
            self._clear_key()

    def test_round_trip_unicode(self):
        key = Fernet.generate_key().decode()
        self._set_key(key)
        try:
            plaintext = "secret–with–unicode–dashes and emoji 🔑"
            encrypted = encrypt(plaintext)
            assert decrypt(encrypted) == plaintext
        finally:
            self._clear_key()

    def test_round_trip_empty_string(self):
        key = Fernet.generate_key().decode()
        self._set_key(key)
        try:
            plaintext = ""
            encrypted = encrypt(plaintext)
            assert decrypt(encrypted) == ""
        finally:
            self._clear_key()


class TestEncryptDecryptWithoutKey:
    """When ENCRYPTION_KEY is unset, encrypt/decrypt pass through."""

    @staticmethod
    def setup_method() -> None:
        os.environ.pop("ENCRYPTION_KEY", None)

    def test_encrypt_passes_through(self):
        plaintext = "plaintext_key_no_env"
        assert encrypt(plaintext) == plaintext

    def test_decrypt_passes_through(self):
        ciphertext = "something_stored_in_plaintext"
        assert decrypt(ciphertext) == ciphertext

    def test_decrypt_of_fernet_token_without_key_returns_ciphertext(self):
        """If a Fernet token was stored (maybe env changed), decrypt without
        key should return the token as-is (safe fallback)."""
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
        try:
            ciphertext = encrypt("secret")
        finally:
            os.environ.pop("ENCRYPTION_KEY", None)
        # Now the key is gone — decrypt should pass through
        assert decrypt(ciphertext) == ciphertext


class TestEncryptDecryptEdgeCases:
    """Edge cases: invalid key, missing cryptography, etc."""

    @staticmethod
    def _clear_key() -> None:
        os.environ.pop("ENCRYPTION_KEY", None)

    def test_invalid_key_falls_back_to_plaintext(self):
        os.environ["ENCRYPTION_KEY"] = "this-is-not-a-valid-fernet-key!!!"
        try:
            plaintext = "test_api_key"
            encrypted = encrypt(plaintext)
            assert encrypted == plaintext
            decrypted = decrypt(encrypted)
            assert decrypted == plaintext
        finally:
            self._clear_key()

    def test_decrypt_of_corrupt_data(self):
        """Decrypting non-token data with a valid key should fall back."""
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
        try:
            # This is not a valid Fernet token
            result = decrypt("not-a-valid-token-!@#")
            assert result == "not-a-valid-token-!@#"
        finally:
            self._clear_key()

    @mock.patch("encryption._HAS_CRYPTOGRAPHY", False)
    def test_no_cryptography_package_falls_back(self):
        """When cryptography is not installed, encrypt should pass through."""
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
        try:
            plaintext = "test_key_no_crypto"
            encrypted = encrypt(plaintext)
            assert encrypted == plaintext
        finally:
            self._clear_key()
