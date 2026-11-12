"""Authenticated, per-user secret encryption primitives for the credential vault.

Every stored credential is sealed with AES-256-GCM under a key that is derived
for the *owning user* from the operator's master key (``ENCRYPTION_KEY``), and the
ciphertext is cryptographically bound to the exact record it belongs to:

* the per-user key is ``HKDF-SHA256(master, info="user:<user_id>")``, so one
  tenant's key can never open another tenant's ciphertext;
* the AEAD associated data is ``(user_id, provider, field)``, so a ciphertext
  copied into another user's row, another provider's row or another column fails
  authentication instead of decrypting;
* the stored token embeds a short, non-secret key identifier so a changed or
  wrong ``ENCRYPTION_KEY`` is reported precisely rather than as corruption.

This module is deliberately **fail closed**. There is no plaintext fallback:
when ``ENCRYPTION_KEY`` is missing or invalid, sealing and opening raise
:class:`EncryptionUnavailableError`, and a ciphertext that does not authenticate
raises :class:`DecryptionError`. Callers must never substitute the stored value
for a credential. Only the credential vault (:mod:`secret_vault`) is meant to
call this module; feature code goes through that narrow service interface.

Generate a master key once with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Then set ``ENCRYPTION_KEY=<that value>`` for every process that shares the
database (web and agent). Changing the key makes existing sealed values
unreadable (they are reported as ``key_mismatch``); there is intentionally no
silent re-keying.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    _HAS_CRYPTOGRAPHY = True
except ImportError:  # pragma: no cover - exercised by patching the flag in tests
    _HAS_CRYPTOGRAPHY = False

ENCRYPTION_KEY_ENV = "ENCRYPTION_KEY"

#: Prefix that identifies a value sealed by this module (scheme version 2).
SEALED_PREFIX = "wv2"

_SCHEME_LABEL = b"workout-agent/secret-vault/v2"
_KEY_ID_LABEL = b"workout-agent/secret-vault/key-id"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MASTER_KEY_BYTES = 32
_DERIVED_KEY_BYTES = 32
# Tokens produced by ``cryptography.fernet`` always start with the version byte
# 0x80 and a timestamp whose base64 form begins with ``gAAAA``. Used only to
# recognise legacy rows written before the vault existed.
_LEGACY_FERNET_PREFIX = "gAAAA"


class EncryptionUnavailableError(RuntimeError):
    """The server cannot encrypt or decrypt credentials (fail closed).

    ``reason`` is one of ``not_configured``, ``invalid_key`` or
    ``cryptography_missing``. The message never contains key material.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"credential encryption is unavailable: {reason}")
        self.reason = reason


class DecryptionError(Exception):
    """A stored value could not be authenticated or decrypted.

    ``reason`` is one of ``malformed``, ``key_mismatch``,
    ``authentication_failed`` or ``legacy_undecryptable``. The message never
    contains ciphertext or plaintext.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"stored credential could not be decrypted: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True, repr=False)
class MasterKey:
    """Validated master key. The raw material is never exposed via ``repr``."""

    key_id: str
    _material: bytes = field(repr=False)
    _fernet_key: bytes = field(repr=False)

    def __repr__(self) -> str:
        return f"MasterKey(key_id={self.key_id!r})"


def load_master_key(environ: Mapping[str, str] | None = None) -> MasterKey:
    """Load and validate ``ENCRYPTION_KEY``; raise if it cannot be used.

    The key must be a canonical URL-safe base64 encoding of exactly 32 bytes
    (the format produced by ``Fernet.generate_key()``), so existing deployments
    keep their key.
    """
    if not _HAS_CRYPTOGRAPHY:
        raise EncryptionUnavailableError("cryptography_missing")

    source = os.environ if environ is None else environ
    raw = source.get(ENCRYPTION_KEY_ENV, "").strip()
    if not raw:
        raise EncryptionUnavailableError("not_configured")

    try:
        encoded = raw.encode("ascii")
        material = base64.b64decode(
            encoded.replace(b"-", b"+").replace(b"_", b"/"),
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise EncryptionUnavailableError("invalid_key") from None
    if (
        len(material) != _MASTER_KEY_BYTES
        or base64.urlsafe_b64encode(material) != encoded
    ):
        raise EncryptionUnavailableError("invalid_key")

    key_id = hmac.new(material, _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:8]
    return MasterKey(key_id=key_id, _material=material, _fernet_key=encoded)


def is_sealed(value: str | None) -> bool:
    """Return whether *value* was produced by :func:`seal` (any key)."""
    return isinstance(value, str) and value.startswith(f"{SEALED_PREFIX}.")


def _binding(user_id: str, provider: str, field_name: str) -> bytes:
    parts = (user_id, provider, field_name)
    if any((not part) or "\x00" in part for part in parts):
        raise ValueError("credential binding components must be non-empty text")
    return b"\x00".join((_SCHEME_LABEL, *(part.encode("utf-8") for part in parts)))


def _user_key(master: MasterKey, user_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_BYTES,
        salt=_SCHEME_LABEL,
        info=b"user:" + user_id.encode("utf-8"),
    ).derive(master._material)


def seal(
    master: MasterKey,
    *,
    user_id: str,
    provider: str,
    field_name: str,
    plaintext: str,
) -> str:
    """Encrypt *plaintext* for one user's credential field.

    The result is ``wv2.<key id>.<base64url(nonce || ciphertext || tag)>``.
    """
    aad = _binding(user_id, provider, field_name)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_user_key(master, user_id)).encrypt(
        nonce,
        plaintext.encode("utf-8"),
        aad,
    )
    body = base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")
    return f"{SEALED_PREFIX}.{master.key_id}.{body}"


def open_sealed(
    master: MasterKey,
    *,
    user_id: str,
    provider: str,
    field_name: str,
    token: str,
) -> str:
    """Decrypt a value produced by :func:`seal` for the same binding.

    Raises :class:`DecryptionError` when the token is malformed, was sealed under
    a different master key, belongs to a different user/provider/field, or was
    tampered with.
    """
    aad = _binding(user_id, provider, field_name)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != SEALED_PREFIX:
        raise DecryptionError("malformed")
    if parts[1] != master.key_id:
        raise DecryptionError("key_mismatch")
    try:
        padded = parts[2] + "=" * (-len(parts[2]) % 4)
        blob = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise DecryptionError("malformed") from None
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        raise DecryptionError("malformed")

    try:
        plaintext = AESGCM(_user_key(master, user_id)).decrypt(
            blob[:_NONCE_BYTES],
            blob[_NONCE_BYTES:],
            aad,
        )
        return plaintext.decode("utf-8")
    except InvalidTag:
        raise DecryptionError("authentication_failed") from None
    except UnicodeDecodeError:
        raise DecryptionError("malformed") from None


def read_legacy_value(master: MasterKey, value: str) -> str:
    """Read a credential stored before the vault existed (migration only).

    Legacy rows are either Fernet tokens produced under the master key or, when
    encryption was not configured at the time, the plaintext itself. A value
    that looks like a Fernet token but cannot be decrypted is *not* treated as
    plaintext: it was encrypted under a different key and must fail closed.
    """
    if not value.startswith(_LEGACY_FERNET_PREFIX):
        return value
    try:
        return Fernet(master._fernet_key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise DecryptionError("legacy_undecryptable") from None
