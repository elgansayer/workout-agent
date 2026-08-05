"""Tests for encryption.py – encrypt/decrypt round-trips and fallbacks."""

from __future__ import annotations

import os
from unittest.mock import patch

from cryptography.fernet import Fernet

from encryption import decrypt, encrypt


def test_encrypt_decrypt_roundtrip_ascii() -> None:
    """Round-trip encrypt/decrypt with ASCII plaintext."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}):
        plain = "test-api-key-12345"
        cipher = encrypt(plain)
        assert cipher != plain
        assert decrypt(cipher) == plain


def test_encrypt_decrypt_roundtrip_unicode() -> None:
    """Round-trip encrypt/decrypt with Unicode plaintext."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}):
        plain = "clé-secrète-üñîçødé"
        cipher = encrypt(plain)
        assert cipher != plain
        assert decrypt(cipher) == plain


def test_encrypt_decrypt_roundtrip_empty() -> None:
    """Round-trip encrypt/decrypt of an empty string."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}):
        cipher = encrypt("")
        assert cipher != ""
        assert decrypt(cipher) == ""


def test_passthrough_when_key_unset() -> None:
    """encrypt/decrypt return plaintext when ENCRYPTION_KEY is not set."""
    with patch.dict(os.environ, clear=True):
        plain = "secret-value"
        assert encrypt(plain) == plain
        assert decrypt(plain) == plain


def test_passthrough_when_key_empty() -> None:
    """encrypt/decrypt return plaintext when ENCRYPTION_KEY is empty string."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": ""}):
        plain = "secret-value"
        assert encrypt(plain) == plain
        assert decrypt(plain) == plain


def test_passthrough_on_invalid_key() -> None:
    """encrypt/decrypt fall back to plaintext with a garbage key."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": "not-a-valid-fernet-key!!"}):
        plain = "secret"
        assert encrypt(plain) == plain
        assert decrypt(plain) == plain


def test_decrypt_corrupt_ciphertext() -> None:
    """decrypt falls back to returning garbled ciphertext as-is."""
    with patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}):
        corrupt = "not-a-valid-fernet-token"
        assert decrypt(corrupt) == corrupt


def test_decrypt_different_key_ciphertext() -> None:
    """decrypt falls back when ciphertext was encrypted with a different key."""
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key_a}):
        cipher = encrypt("value")
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key_b}):
        # decrypt should fall back and return the ciphertext as-is
        assert decrypt(cipher) == cipher


def test_missing_cryptography_package_fallback() -> None:
    """encrypt returns plaintext when cryptography is not installed."""
    with (
        patch.dict(os.environ, {"ENCRYPTION_KEY": Fernet.generate_key().decode()}),
        patch("encryption._HAS_CRYPTOGRAPHY", False),
    ):
        plain = "falling-back"
        assert encrypt(plain) == plain
        assert decrypt(plain) == plain