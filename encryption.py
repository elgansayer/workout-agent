"""Symmetric encryption for API keys stored in the database.

Uses Fernet (AES-128-CBC with HMAC) from the `cryptography` library. A single
master key, read from the ``ENCRYPTION_KEY`` environment variable, encrypts every
secret before it hits SQLite and decrypts it when it comes back out.

If ``ENCRYPTION_KEY`` is not set the functions fall back to a no-op (plaintext),
so existing deployments keep working until the operator provisions a key.

Generate a fresh key once with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Then set ``ENCRYPTION_KEY=<that value>`` in ``.env``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False
    InvalidToken = Exception  # type: ignore[misc,assignment]


def _fernet() -> Fernet | None:
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    if not _HAS_CRYPTOGRAPHY:
        logger.warning(
            "ENCRYPTION_KEY is set but the `cryptography` package is not installed. "
            "API keys will be stored in PLAINTEXT. Run: pip install cryptography",
        )
        return None
    try:
        return Fernet(key.encode())
    except Exception as exc:  # noqa: BLE001
        logger.error("ENCRYPTION_KEY is invalid (%s). Keys will be stored in plaintext.", exc)
        return None


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns the Fernet token as a UTF-8 string.

    Falls back to returning the plaintext unchanged when encryption is not
    available.
    """
    f = _fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token back to the original string.

    Falls back to returning the input unchanged when encryption is not
    available or when the input does not look like a Fernet token (i.e. it
    was stored before encryption was enabled).
    """
    f = _fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):  # noqa: BLE001
        # The value was probably stored before encryption was enabled.
        return ciphertext
