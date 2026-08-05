"""Tests for encryption.py — Fernet-based API key encryption."""

from __future__ import annotations

import os
from unittest import mock

import pytest
from cryptography.fernet import Fernet

from encryption import _fernet, decrypt, encrypt


@pytest.fixture(autouse=True)
def _clear_env() -> None:
    """Ensure tests don't leak ENCRYPTION_KEY between each other."""
    os.environ.pop("ENCRYPTION_KEY", None)


# ── encrypt / decrypt round-trip ────────────────────────────────────────


def test_encrypt_decrypt_round_trip() -> None:
    """A round-trip through encrypt → decrypt preserves the original string."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    plaintext = "sk-1234-abcd-5678"
    token = encrypt(plaintext)
    assert token != plaintext
    assert decrypt(token) == plaintext


def test_encrypt_decrypt_unicode() -> None:
    """Encrypt/decrypt handles unicode strings correctly."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    plaintext = "🔑 key with emoji and ünicode"
    assert decrypt(encrypt(plaintext)) == plaintext


def test_encrypt_decrypt_empty_string() -> None:
    """Encrypt/decrypt handles an empty string."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    assert decrypt(encrypt("")) == ""


@pytest.mark.parametrize(
    "plaintext",
    [
        "short",
        "a" * 4096,
        '{"key": "value", "nested": {"a": 1}}',
    ],
)
def test_encrypt_decrypt_various_lengths(plaintext: str) -> None:
    """Encrypt/decrypt handles strings of various lengths."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    assert decrypt(encrypt(plaintext)) == plaintext


# ── pass-through when ENCRYPTION_KEY is missing ─────────────────────────


def test_encrypt_pass_through_when_key_unset() -> None:
    """encrypt returns plaintext unchanged when ENCRYPTION_KEY is not set."""
    assert encrypt("secret-api-key") == "secret-api-key"


def test_decrypt_pass_through_when_key_unset() -> None:
    """decrypt returns ciphertext unchanged when ENCRYPTION_KEY is not set."""
    assert decrypt("some-old-plaintext-key") == "some-old-plaintext-key"


def test_encrypt_pass_through_when_key_empty() -> None:
    """encrypt returns plaintext unchanged when ENCRYPTION_KEY is whitespace."""
    os.environ["ENCRYPTION_KEY"] = "   "
    assert encrypt("my-key") == "my-key"


def test_decrypt_pass_through_when_key_empty() -> None:
    """decrypt returns ciphertext unchanged when ENCRYPTION_KEY is whitespace."""
    os.environ["ENCRYPTION_KEY"] = "   "
    assert decrypt("my-key") == "my-key"


# ── invalid key handling ────────────────────────────────────────────────


def test_encrypt_falls_back_when_key_is_invalid() -> None:
    """encrypt returns plaintext when ENCRYPTION_KEY is not valid base64."""
    os.environ["ENCRYPTION_KEY"] = "this-is-not-a-valid-fernet-key!!!"
    assert encrypt("my-secret") == "my-secret"


def test_decrypt_falls_back_when_key_is_invalid() -> None:
    """decrypt returns ciphertext when ENCRYPTION_KEY is not valid base64."""
    os.environ["ENCRYPTION_KEY"] = "this-is-not-a-valid-fernet-key!!!"
    assert decrypt("my-secret") == "my-secret"


# ── Fernet token tampering / wrong key ──────────────────────────────────


def test_decrypt_returns_input_on_garbage_input() -> None:
    """decrypt returns the input unchanged when it's not a valid Fernet token."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    assert decrypt("not-a-valid-fernet-token-at-all") == "not-a-valid-fernet-token-at-all"


def test_decrypt_returns_input_on_wrong_key() -> None:
    """decrypt returns the input when encrypted with a different key."""
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    os.environ["ENCRYPTION_KEY"] = key_a
    token = encrypt("my-token")

    # Now switch to a different key — decrypt should fall back gracefully
    os.environ["ENCRYPTION_KEY"] = key_b
    assert decrypt(token) == token  # returns ciphertext unchanged


# ── multiple encryptions produce different tokens (salt) ────────────────


def test_encrypt_is_nondeterministic() -> None:
    """Two encryptions of the same plaintext produce different tokens (Fernet salts)."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    token1 = encrypt("hello")
    token2 = encrypt("hello")
    assert token1 != token2
    # Both must decrypt to the same value
    assert decrypt(token1) == "hello"
    assert decrypt(token2) == "hello"


# ── cryptography not installed ──────────────────────────────────────────


def test_encrypt_falls_back_when_crypto_unavailable() -> None:
    """encrypt returns plaintext when cryptography is not importable."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    with mock.patch("encryption._HAS_CRYPTOGRAPHY", False):
        result = encrypt("api-key-12345")
    assert result == "api-key-12345"


def test_decrypt_falls_back_when_crypto_unavailable() -> None:
    """decrypt returns ciphertext unchanged when cryptography is not importable."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    with mock.patch("encryption._HAS_CRYPTOGRAPHY", False):
        result = decrypt("some-data")
    assert result == "some-data"


# ── _fernet() helper ────────────────────────────────────────────────────


def test_fernet_returns_fernet_for_valid_key() -> None:
    """_fernet() returns a Fernet instance for a valid key."""
    key = Fernet.generate_key().decode()
    os.environ["ENCRYPTION_KEY"] = key
    f = _fernet()
    assert f is not None
    assert isinstance(f, Fernet)
    # Quick smoke: round-trip through the Fernet directly
    token = f.encrypt(b"test").decode()
    assert f.decrypt(token.encode()).decode() == "test"


def test_fernet_returns_none_for_unset_key() -> None:
    """_fernet() returns None when ENCRYPTION_KEY is not set."""
    assert _fernet() is None


def test_fernet_returns_none_for_empty_key() -> None:
    """_fernet() returns None when ENCRYPTION_KEY is whitespace-only."""
    os.environ["ENCRYPTION_KEY"] = "   "
    assert _fernet() is None