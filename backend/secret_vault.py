"""Per-user secret vault: the only supported interface to stored credentials.

Every AI-provider key, connector API key and OAuth token belongs to exactly one
user and lives in the ``user_api_keys`` table, sealed with a key derived for the
owning user (see :mod:`encryption`). Feature code never reads or writes that
table, and never receives a stored secret except through :meth:`SecretVault.resolve`.

Narrow interface
----------------
* Owner lifecycle (each one is written to the append-only
  ``credential_audit_events`` table in the same transaction as the change):
  :meth:`~SecretVault.store`, :meth:`~SecretVault.rotate`,
  :meth:`~SecretVault.verify_stored` / :meth:`~SecretVault.verify_candidate`,
  :meth:`~SecretVault.revoke`, :meth:`~SecretVault.delete`.
* Display: :meth:`~SecretVault.describe` and :meth:`~SecretVault.list_credentials`
  return :class:`CredentialSummary` objects holding only metadata and a masked
  hint. They never decrypt anything, so Settings keeps working even when the
  server cannot currently decrypt.
* Use: :meth:`~SecretVault.resolve` returns a :class:`ResolvedCredential` whose
  secrets are opaque :class:`SecretValue` objects, for in-process consumers such as
  the AI provider factory and the Hevy client. Background jobs call the same
  method with the job's own ``user_id`` and handle
  :class:`CredentialUnavailableError`; there is no fallback to another user's or
  a shared credential.

Safety properties
-----------------
* Fail closed: without a valid ``ENCRYPTION_KEY`` nothing is stored or read.
* Plaintext never appears in return values for the browser, audit rows, logs or
  exception messages. ``SecretValue`` redacts itself in ``repr``/``str``/format
  and refuses to be pickled or copied into a dict.
* Nothing is cached; each ``resolve`` decrypts from storage.
* Revocation destroys the secret material and keeps only non-secret metadata.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import encryption
from database import DEFAULT_DB_PATH, _connect

logger = logging.getLogger(__name__)

#: Columns of ``user_api_keys`` that hold sealed secret material.
SECRET_FIELDS = ("api_key", "client_secret", "refresh_token")

#: Legacy location of the Google Health refresh token (plaintext, per user).
LEGACY_GOOGLE_HEALTH_META_KEY = "google_health_refresh_token"

MASK_BULLETS = "••••••••"
_MIN_HINT_SECRET_LENGTH = 16
_HINT_LENGTH = 4
_MAX_SECRET_LENGTH = 4096
_MAX_MODEL_LENGTH = 200
_MAX_AUDIT_PAGE = 200

_SECRET_PATTERN = re.compile(r"^[\x21-\x7e]+$")
_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,47}$")
_DETAIL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

ACTORS = frozenset({"owner", "system", "operator"})
SOURCES = frozenset(
    {"web", "oauth_callback", "oauth_refresh", "scheduler", "migration", "cli"}
)


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    NOT_CONFIGURED = "not_configured"


class CredentialAction(StrEnum):
    SAVE = "save"
    ROTATE = "rotate"
    TEST = "test"
    REVOKE = "revoke"
    DELETE = "delete"
    MIGRATE = "migrate"


class UnavailableReason(StrEnum):
    NOT_CONFIGURED = "not_configured"
    REVOKED = "revoked"
    VAULT_UNAVAILABLE = "vault_unavailable"
    DECRYPTION_FAILED = "decryption_failed"


# ---------------------------------------------------------------------------
# Errors. Messages are user-safe and never contain secret material.
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """Base class for every error raised by the vault."""


class UnknownProviderError(VaultError, ValueError):
    """The provider is not part of the credential catalogue."""


class InvalidSecretError(VaultError, ValueError):
    """A submitted secret or model failed validation (the value is not echoed)."""


class UnknownUserError(VaultError, LookupError):
    """The owning user does not exist."""


class CredentialNotFoundError(VaultError, LookupError):
    """No active credential exists for this user and provider."""


class CredentialExistsError(VaultError):
    """An active credential already exists; rotate it instead."""


class CredentialConflictError(VaultError):
    """The credential changed concurrently; retry the operation."""


class VaultUnavailableError(VaultError):
    """The server cannot encrypt or decrypt credentials right now."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Credential storage is unavailable ({reason}).")
        self.reason = reason


class CredentialUnavailableError(VaultError, ValueError):
    """A credential cannot be used. Raised by :meth:`SecretVault.resolve`.

    Subclasses :class:`ValueError` so pre-vault callers that already treat a
    missing key as a configuration error keep failing safely.
    """

    def __init__(
        self,
        provider: str,
        reason: UnavailableReason,
        message: str | None = None,
    ) -> None:
        super().__init__(message or _unavailable_message(provider, reason))
        self.provider = provider
        self.reason = reason


class VerificationUnsupportedError(VaultError, ValueError):
    """No connectivity test exists for this credential type."""


class VerificationFailedError(VaultError):
    """A candidate credential was rejected, so nothing was changed."""

    def __init__(self, result: VerificationResult) -> None:
        super().__init__("The credential could not be verified; nothing was changed.")
        self.result = result


def _unavailable_message(provider: str, reason: UnavailableReason) -> str:
    if reason is UnavailableReason.NOT_CONFIGURED:
        return f"No {provider} key configured. Add one in Settings."
    if reason is UnavailableReason.REVOKED:
        return f"Your {provider} key was revoked. Add a new key in Settings."
    if reason is UnavailableReason.DECRYPTION_FAILED:
        return (
            f"Your stored {provider} key could not be decrypted. "
            "Add the key again in Settings."
        )
    return "Credential storage is not available on this server."


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class SecretValue:
    """Opaque holder for a decrypted secret.

    The value is only reachable through :meth:`reveal`, so an accidental
    ``logger.info("%s", credential)``, f-string, ``repr`` or ``json.dumps`` can
    never expose it, and it refuses to be pickled or deep-copied (which also
    blocks ``dataclasses.asdict``).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "<redacted>"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return hmac.compare_digest(
            self._value.encode("utf-8"),
            other._value.encode("utf-8"),
        )

    def __reduce__(self) -> Any:
        raise TypeError("SecretValue cannot be serialised or copied")


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """A decrypted credential for in-process use. Never serialise it."""

    provider: str
    version: int
    api_key: SecretValue | None = None
    client_id: str | None = None
    client_secret: SecretValue | None = None
    refresh_token: SecretValue | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialSummary:
    """Non-secret view of a credential, safe to return to the browser."""

    provider: str
    kind: str
    status: CredentialStatus
    configured: bool
    masked: str | None = None
    model: str | None = None
    version: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    rotated_at: str | None = None
    revoked_at: str | None = None
    last_tested_at: str | None = None
    last_test_status: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status.value,
            "has_key": self.configured,
            "masked": self.masked,
            "model": self.model,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "rotated_at": self.rotated_at,
            "revoked_at": self.revoked_at,
            "last_tested_at": self.last_tested_at,
            "last_test_status": self.last_test_status,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a connectivity test. ``code`` is a short, safe identifier."""

    ok: bool
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


Verifier = Callable[[ResolvedCredential], VerificationResult]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: int
    provider: str
    action: str
    outcome: str
    detail: str | None
    actor: str
    source: str
    credential_version: int | None
    created_at: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "action": self.action,
            "outcome": self.outcome,
            "detail": self.detail,
            "actor": self.actor,
            "source": self.source,
            "version": self.credential_version,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class VaultAvailability:
    available: bool
    reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {"available": self.available, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Result of upgrading legacy credentials to per-user encryption."""

    upgraded: int = 0
    failed: int = 0
    pending: int = 0
    unavailable: bool = False


# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider: str
    kind: str  # "ai" or "connector"
    fields: tuple[str, ...]
    #: Whether a connectivity test exists for this credential type.
    testable: bool = True


_CONNECTOR_SPECS: Mapping[str, ProviderSpec] = {
    "hevy": ProviderSpec("hevy", "connector", ("api_key",)),
    "google_health": ProviderSpec(
        "google_health",
        "connector",
        ("refresh_token",),
        testable=False,
    ),
}


def provider_spec(provider: str) -> ProviderSpec:
    """Return the catalogue entry for *provider* or raise ``UnknownProviderError``."""
    key = provider.strip().lower() if isinstance(provider, str) else ""
    if not _PROVIDER_PATTERN.fullmatch(key):
        raise UnknownProviderError("Unknown credential provider.")
    connector = _CONNECTOR_SPECS.get(key)
    if connector is not None:
        return connector
    from ai_provider import PROVIDERS

    if key in PROVIDERS:
        return ProviderSpec(key, "ai", ("api_key",))
    raise UnknownProviderError("Unknown credential provider.")


def known_providers() -> tuple[str, ...]:
    """Every provider id the vault can hold, connectors first."""
    from ai_provider import PROVIDERS

    return (*_CONNECTOR_SPECS, *PROVIDERS)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _clean_secret(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidSecretError("The credential must be text.")
    text = value.strip()
    if not text:
        raise InvalidSecretError("The credential must not be empty.")
    if len(text) > _MAX_SECRET_LENGTH:
        raise InvalidSecretError("The credential is too long.")
    if not _SECRET_PATTERN.fullmatch(text):
        raise InvalidSecretError(
            "The credential must be printable ASCII without spaces."
        )
    return text


def _clean_model(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSecretError("The model must be text.")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_MODEL_LENGTH:
        raise InvalidSecretError("The model name is too long.")
    if _CONTROL_CHARACTERS.search(text):
        raise InvalidSecretError("The model name contains invalid characters.")
    return text


def _clean_identity(user_id: object) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise UnknownUserError("An authenticated user is required.")
    text = user_id.strip()
    if len(text) > 128 or "\x00" in text:
        raise UnknownUserError("An authenticated user is required.")
    return text


def _clean_origin(actor: str, source: str) -> tuple[str, str]:
    if actor not in ACTORS or source not in SOURCES:
        raise ValueError("Unrecognised credential audit actor or source.")
    return actor, source


def _clean_purpose(purpose: str) -> str:
    if not isinstance(purpose, str) or not _PURPOSE_PATTERN.fullmatch(purpose):
        raise ValueError("A short lowercase purpose is required to resolve a credential.")
    return purpose


def _safe_detail(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _DETAIL_PATTERN.fullmatch(value) else "unspecified"


def _hint_for(secret: str) -> str:
    if len(secret) < _MIN_HINT_SECRET_LENGTH:
        return ""
    return secret[-_HINT_LENGTH:]


def _user_ref(user_id: str) -> str:
    """Short pseudonym for log lines; raw identifiers are not logged."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Storage rows
# ---------------------------------------------------------------------------

_SELECT_ROW = (
    "SELECT id, user_id, provider, api_key, client_id, client_secret, "
    "refresh_token, extra_json, status, key_hint, key_version, created_at, "
    "updated_at, rotated_at, revoked_at, last_tested_at, last_test_status "
    "FROM user_api_keys"
)


@dataclass(frozen=True, slots=True)
class _Row:
    id: int
    user_id: str
    provider: str
    api_key: str
    client_id: str | None
    client_secret: str | None
    refresh_token: str | None
    extra_json: str | None
    status: str
    key_hint: str | None
    key_version: int
    created_at: str
    updated_at: str
    rotated_at: str | None
    revoked_at: str | None
    last_tested_at: str | None
    last_test_status: str | None

    @property
    def has_secret(self) -> bool:
        return bool(self.api_key or self.client_secret or self.refresh_token)

    @property
    def configured(self) -> bool:
        return self.status == CredentialStatus.ACTIVE.value and self.has_secret

    @property
    def model(self) -> str | None:
        if not self.extra_json:
            return None
        try:
            extra = json.loads(self.extra_json)
        except ValueError:
            return None
        value = extra.get("model") if isinstance(extra, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    def secret_columns(self) -> dict[str, str]:
        return {
            name: value
            for name in SECRET_FIELDS
            if (value := getattr(self, name))
        }


def _row_from(values: tuple[Any, ...]) -> _Row:
    return _Row(*values)


def _summary_for(spec: ProviderSpec, row: _Row | None) -> CredentialSummary:
    if row is None:
        return CredentialSummary(
            provider=spec.provider,
            kind=spec.kind,
            status=CredentialStatus.NOT_CONFIGURED,
            configured=False,
        )
    if row.status == CredentialStatus.REVOKED.value:
        status = CredentialStatus.REVOKED
    elif row.has_secret:
        status = CredentialStatus.ACTIVE
    else:
        status = CredentialStatus.NOT_CONFIGURED
    configured = row.configured
    return CredentialSummary(
        provider=spec.provider,
        kind=spec.kind,
        status=status,
        configured=configured,
        masked=(MASK_BULLETS + (row.key_hint or "")) if configured else None,
        model=row.model if configured else None,
        version=row.key_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        rotated_at=row.rotated_at,
        revoked_at=row.revoked_at,
        last_tested_at=row.last_tested_at if configured else None,
        last_test_status=row.last_test_status if configured else None,
    )


def _failure_detail(exc: VaultError) -> str:
    if isinstance(exc, VaultUnavailableError):
        return {
            "not_configured": "encryption_not_configured",
            "invalid_key": "encryption_key_invalid",
            "cryptography_missing": "encryption_unavailable",
        }.get(exc.reason, "vault_unavailable")
    if isinstance(exc, CredentialUnavailableError):
        return exc.reason.value
    if isinstance(exc, VerificationFailedError):
        return f"verification_failed:{exc.result.code}"
    if isinstance(exc, CredentialNotFoundError):
        return "not_found"
    if isinstance(exc, CredentialExistsError):
        return "already_exists"
    if isinstance(exc, CredentialConflictError):
        return "conflict"
    if isinstance(exc, UnknownUserError):
        return "unknown_user"
    return "error"


def _run_verifier(verifier: Verifier, credential: ResolvedCredential) -> VerificationResult:
    """Run a connectivity test; it can fail but can never leak or crash the caller."""
    try:
        result = verifier(credential)
    except Exception as exc:  # noqa: BLE001 - provider SDKs raise arbitrary types
        logger.warning(
            "Credential verifier for %s raised %s.",
            credential.provider,
            type(exc).__name__,
        )
        return VerificationResult(
            ok=False,
            code="error",
            message="The connection test failed unexpectedly.",
        )
    code = _safe_detail(result.code) or "unspecified"
    return VerificationResult(
        ok=bool(result.ok),
        code=code,
        message=result.message,
        details=dict(result.details),
    )


# ---------------------------------------------------------------------------
# The vault
# ---------------------------------------------------------------------------


class SecretVault:
    """Tenant-scoped credential service. Construct one per use; it is stateless."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._environ = environ
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))

    # -- infrastructure ----------------------------------------------------

    def _now(self) -> str:
        return self._clock().isoformat()

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        try:
            with _connect(self._db_path) as conn:
                yield conn
        except sqlite3.Error as exc:
            logger.warning("Credential storage error (%s).", type(exc).__name__)
            raise VaultUnavailableError("storage_error") from None

    def _master(self) -> encryption.MasterKey:
        try:
            return encryption.load_master_key(self._environ)
        except encryption.EncryptionUnavailableError as exc:
            raise VaultUnavailableError(exc.reason) from None

    def availability(self) -> VaultAvailability:
        """Report whether credentials can be encrypted here (no secrets, no I/O)."""
        try:
            encryption.load_master_key(self._environ)
        except encryption.EncryptionUnavailableError as exc:
            return VaultAvailability(False, exc.reason)
        return VaultAvailability(True, None)

    @staticmethod
    def _fetch(conn: sqlite3.Connection, user_id: str, provider: str) -> _Row | None:
        found = conn.execute(
            _SELECT_ROW + " WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()
        return _row_from(found) if found else None

    @staticmethod
    def _require_user(conn: sqlite3.Connection, user_id: str) -> None:
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise UnknownUserError("The account no longer exists.")

    @staticmethod
    def _insert_audit(
        conn: sqlite3.Connection,
        *,
        user_id: str,
        provider: str,
        action: CredentialAction,
        outcome: str,
        detail: str | None,
        actor: str,
        source: str,
        version: int | None,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO credential_audit_events
                (user_id, provider, action, outcome, detail, actor, source,
                 credential_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                provider,
                action.value,
                outcome,
                _safe_detail(detail),
                actor,
                source,
                version,
                now,
            ),
        )

    def _audit_failure(
        self,
        user_id: str,
        provider: str,
        action: CredentialAction,
        exc: VaultError,
        actor: str,
        source: str,
    ) -> None:
        """Best-effort failure record; it can never mask the original error."""
        try:
            with self._db() as conn:
                self._insert_audit(
                    conn,
                    user_id=user_id,
                    provider=provider,
                    action=action,
                    outcome="failure",
                    detail=_failure_detail(exc),
                    actor=actor,
                    source=source,
                    version=None,
                    now=self._now(),
                )
        except VaultError:
            logger.warning("Could not record a failed credential %s.", action.value)

    # -- display (metadata only, never decrypts) ---------------------------

    def describe(self, user_id: str, provider: str) -> CredentialSummary:
        """Return the masked summary for one credential (placeholder if unset)."""
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        with self._db() as conn:
            row = self._fetch(conn, uid, spec.provider)
        return _summary_for(spec, row)

    def list_credentials(self, user_id: str) -> dict[str, CredentialSummary]:
        """Return a summary for every known provider, keyed by provider id."""
        uid = _clean_identity(user_id)
        with self._db() as conn:
            rows = {
                row.provider: row
                for row in (
                    _row_from(found)
                    for found in conn.execute(
                        _SELECT_ROW + " WHERE user_id = ?", (uid,)
                    ).fetchall()
                )
            }
        summaries: dict[str, CredentialSummary] = {}
        for provider in known_providers():
            spec = provider_spec(provider)
            summaries[provider] = _summary_for(spec, rows.get(provider))
        return summaries

    def recent_events(
        self,
        user_id: str,
        *,
        provider: str | None = None,
        limit: int = 50,
    ) -> list[AuditEvent]:
        """Return the owner's most recent audit events, newest first."""
        uid = _clean_identity(user_id)
        page = max(1, min(int(limit), _MAX_AUDIT_PAGE))
        sql = (
            "SELECT id, provider, action, outcome, detail, actor, source, "
            "credential_version, created_at FROM credential_audit_events "
            "WHERE user_id = ?"
        )
        params: list[Any] = [uid]
        if provider is not None:
            sql += " AND provider = ?"
            params.append(provider_spec(provider).provider)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(page)
        with self._db() as conn:
            found = conn.execute(sql, params).fetchall()
        return [AuditEvent(*values) for values in found]

    # -- owner lifecycle ---------------------------------------------------

    def store(
        self,
        user_id: str,
        provider: str,
        *,
        api_key: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        model: str | None = None,
        actor: str = "owner",
        source: str = "web",
        verifier: Verifier | None = None,
    ) -> CredentialSummary:
        """Save a new credential for the owner.

        Raises :class:`CredentialExistsError` when an active credential already
        exists (use :meth:`rotate`). A previously revoked credential can be
        replaced. When *verifier* is given the candidate is tested first and
        rejected without being stored if the test fails.
        """
        return self._write(
            CredentialAction.SAVE,
            user_id,
            provider,
            secrets={
                "api_key": api_key,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            client_id=client_id,
            model=model,
            actor=actor,
            source=source,
            verifier=verifier,
        )

    def rotate(
        self,
        user_id: str,
        provider: str,
        *,
        api_key: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        model: str | None = None,
        actor: str = "owner",
        source: str = "web",
        verifier: Verifier | None = None,
    ) -> CredentialSummary:
        """Replace the secret material of an existing active credential.

        The previous secret is overwritten, never retained. With *verifier* the
        new secret must pass its test first, otherwise the existing credential is
        left untouched.
        """
        return self._write(
            CredentialAction.ROTATE,
            user_id,
            provider,
            secrets={
                "api_key": api_key,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            client_id=None,
            model=model,
            actor=actor,
            source=source,
            verifier=verifier,
        )

    def _write(
        self,
        action: CredentialAction,
        user_id: str,
        provider: str,
        *,
        secrets: Mapping[str, object],
        client_id: str | None,
        model: object,
        actor: str,
        source: str,
        verifier: Verifier | None,
    ) -> CredentialSummary:
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        actor, source = _clean_origin(actor, source)
        supplied = self._validated_secrets(spec, secrets)
        clean_model = _clean_model(model)
        if client_id is not None and not isinstance(client_id, str):
            raise InvalidSecretError("The client id must be text.")
        clean_client_id = client_id.strip() or None if client_id else None
        try:
            return self._write_checked(
                action,
                uid,
                spec,
                supplied,
                clean_client_id,
                clean_model,
                actor,
                source,
                verifier,
            )
        except VaultError as exc:
            self._audit_failure(uid, spec.provider, action, exc, actor, source)
            raise

    @staticmethod
    def _validated_secrets(
        spec: ProviderSpec,
        secrets: Mapping[str, object],
    ) -> dict[str, str]:
        supplied: dict[str, str] = {}
        for name in SECRET_FIELDS:
            value = secrets.get(name)
            if value is None:
                continue
            if name not in spec.fields:
                raise InvalidSecretError("This credential type does not use that field.")
            supplied[name] = _clean_secret(value)
        if not supplied:
            raise InvalidSecretError("A credential value is required.")
        return supplied

    def _write_checked(
        self,
        action: CredentialAction,
        uid: str,
        spec: ProviderSpec,
        supplied: Mapping[str, str],
        client_id: str | None,
        model: str | None,
        actor: str,
        source: str,
        verifier: Verifier | None,
    ) -> CredentialSummary:
        # Fail before doing any work when the server cannot encrypt.
        master = self._master()

        with self._db() as conn:
            self._require_user(conn, uid)
            current = self._fetch(conn, uid, spec.provider)
        if action is CredentialAction.ROTATE and (current is None or not current.configured):
            raise CredentialNotFoundError("No active credential exists to rotate.")
        if (
            action in (CredentialAction.SAVE, CredentialAction.MIGRATE)
            and current is not None
            and current.configured
        ):
            raise CredentialExistsError("A credential already exists; rotate it instead.")

        verified = False
        if verifier is not None:
            candidate = self._candidate(spec, supplied, client_id, model, current)
            result = _run_verifier(verifier, candidate)
            if not result.ok:
                raise VerificationFailedError(result)
            verified = True

        sealed = {
            name: encryption.seal(
                master,
                user_id=uid,
                provider=spec.provider,
                field_name=name,
                plaintext=value,
            )
            for name, value in supplied.items()
        }
        primary = self._primary_field(spec)
        now = self._now()

        with self._db() as conn:
            fresh = self._fetch(conn, uid, spec.provider)
            if (fresh is None) != (current is None) or (
                fresh is not None
                and current is not None
                and fresh.key_version != current.key_version
            ):
                raise CredentialConflictError("The credential changed; try again.")

            if fresh is None:
                version = 1
                self._insert_row(
                    conn,
                    uid,
                    spec.provider,
                    sealed,
                    client_id,
                    model,
                    _hint_for(supplied[primary]) if primary in supplied else "",
                    verified,
                    now,
                )
            elif action is CredentialAction.ROTATE:
                version = fresh.key_version + 1
                columns = {
                    name: sealed.get(name, getattr(fresh, name) or None)
                    for name in SECRET_FIELDS
                }
                hint = (
                    _hint_for(supplied[primary])
                    if primary in supplied
                    else (fresh.key_hint or "")
                )
                self._update_row(
                    conn,
                    fresh,
                    columns,
                    client_id=fresh.client_id,
                    extra_json=self._extra_json(model, fresh),
                    hint=hint,
                    version=version,
                    status=CredentialStatus.ACTIVE,
                    created_at=fresh.created_at,
                    rotated_at=now,
                    revoked_at=None,
                    verified=verified,
                    now=now,
                )
            else:
                # Re-saving after a revoke (or an empty placeholder row).
                version = fresh.key_version + 1
                columns = {name: sealed.get(name) for name in SECRET_FIELDS}
                self._update_row(
                    conn,
                    fresh,
                    columns,
                    client_id=client_id,
                    extra_json=self._extra_json(model, None),
                    hint=_hint_for(supplied[primary]) if primary in supplied else "",
                    version=version,
                    status=CredentialStatus.ACTIVE,
                    created_at=now,
                    rotated_at=None,
                    revoked_at=None,
                    verified=verified,
                    now=now,
                )

            self._insert_audit(
                conn,
                user_id=uid,
                provider=spec.provider,
                action=action,
                outcome="success",
                detail="verified" if verified else None,
                actor=actor,
                source=source,
                version=version,
                now=now,
            )
            stored = self._fetch(conn, uid, spec.provider)
        return _summary_for(spec, stored)

    @staticmethod
    def _primary_field(spec: ProviderSpec) -> str:
        return "api_key" if "api_key" in spec.fields else spec.fields[0]

    @staticmethod
    def _extra_json(model: str | None, current: _Row | None) -> str | None:
        if model is not None:
            return json.dumps({"model": model})
        return current.extra_json if current is not None else None

    @staticmethod
    def _candidate(
        spec: ProviderSpec,
        supplied: Mapping[str, str],
        client_id: str | None,
        model: str | None,
        current: _Row | None,
    ) -> ResolvedCredential:
        return ResolvedCredential(
            provider=spec.provider,
            version=0,
            api_key=SecretValue(supplied["api_key"]) if "api_key" in supplied else None,
            client_id=client_id,
            client_secret=(
                SecretValue(supplied["client_secret"])
                if "client_secret" in supplied
                else None
            ),
            refresh_token=(
                SecretValue(supplied["refresh_token"])
                if "refresh_token" in supplied
                else None
            ),
            model=model or (current.model if current is not None else None),
        )

    def _insert_row(
        self,
        conn: sqlite3.Connection,
        uid: str,
        provider: str,
        sealed: Mapping[str, str],
        client_id: str | None,
        model: str | None,
        hint: str,
        verified: bool,
        now: str,
    ) -> None:
        try:
            conn.execute(
                """
                INSERT INTO user_api_keys
                    (user_id, provider, api_key, client_id, client_secret,
                     refresh_token, extra_json, status, key_hint, key_version,
                     created_at, updated_at, last_tested_at, last_test_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 1, ?, ?, ?, ?)
                """,
                (
                    uid,
                    provider,
                    sealed.get("api_key", ""),
                    client_id,
                    sealed.get("client_secret"),
                    sealed.get("refresh_token"),
                    self._extra_json(model, None),
                    hint or None,
                    now,
                    now,
                    now if verified else None,
                    "ok" if verified else None,
                ),
            )
        except sqlite3.IntegrityError:
            raise CredentialConflictError("The credential changed; try again.") from None

    @staticmethod
    def _update_row(
        conn: sqlite3.Connection,
        current: _Row,
        columns: Mapping[str, str | None],
        *,
        client_id: str | None,
        extra_json: str | None,
        hint: str,
        version: int,
        status: CredentialStatus,
        created_at: str,
        rotated_at: str | None,
        revoked_at: str | None,
        verified: bool,
        now: str,
    ) -> None:
        updated = conn.execute(
            """
            UPDATE user_api_keys
            SET api_key = ?, client_id = ?, client_secret = ?, refresh_token = ?,
                extra_json = ?, status = ?, key_hint = ?, key_version = ?,
                created_at = ?, updated_at = ?, rotated_at = ?, revoked_at = ?,
                last_tested_at = ?, last_test_status = ?
            WHERE id = ? AND user_id = ? AND key_version = ?
            """,
            (
                columns.get("api_key") or "",
                client_id,
                columns.get("client_secret"),
                columns.get("refresh_token"),
                extra_json,
                status.value,
                hint or None,
                version,
                created_at,
                now,
                rotated_at,
                revoked_at,
                now if verified else None,
                "ok" if verified else None,
                current.id,
                current.user_id,
                current.key_version,
            ),
        )
        if updated.rowcount != 1:
            raise CredentialConflictError("The credential changed; try again.")

    def revoke(
        self,
        user_id: str,
        provider: str,
        *,
        actor: str = "owner",
        source: str = "web",
    ) -> CredentialSummary:
        """Invalidate a credential and destroy its secret material.

        Only non-secret metadata is kept, so the owner can see that the
        credential was revoked and background jobs fail with a precise reason.
        Revoking an already revoked credential is a no-op.
        """
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        actor, source = _clean_origin(actor, source)
        try:
            with self._db() as conn:
                current = self._fetch(conn, uid, spec.provider)
                if current is None:
                    raise CredentialNotFoundError("No credential exists to revoke.")
                if current.status == CredentialStatus.REVOKED.value:
                    return _summary_for(spec, current)
                now = self._now()
                version = current.key_version + 1
                self._update_row(
                    conn,
                    current,
                    {},
                    client_id=current.client_id,
                    extra_json=current.extra_json,
                    hint="",
                    version=version,
                    status=CredentialStatus.REVOKED,
                    created_at=current.created_at,
                    rotated_at=current.rotated_at,
                    revoked_at=now,
                    verified=False,
                    now=now,
                )
                self._insert_audit(
                    conn,
                    user_id=uid,
                    provider=spec.provider,
                    action=CredentialAction.REVOKE,
                    outcome="success",
                    detail=None,
                    actor=actor,
                    source=source,
                    version=version,
                    now=now,
                )
                return _summary_for(spec, self._fetch(conn, uid, spec.provider))
        except VaultError as exc:
            self._audit_failure(uid, spec.provider, CredentialAction.REVOKE, exc, actor, source)
            raise

    def delete(
        self,
        user_id: str,
        provider: str,
        *,
        actor: str = "owner",
        source: str = "web",
    ) -> bool:
        """Remove a credential record entirely. Returns whether one existed.

        The audit trail keeps the history of the credential.
        """
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        actor, source = _clean_origin(actor, source)
        with self._db() as conn:
            current = self._fetch(conn, uid, spec.provider)
            if current is None:
                return False
            deleted = conn.execute(
                "DELETE FROM user_api_keys WHERE id = ? AND user_id = ?",
                (current.id, uid),
            )
            if deleted.rowcount != 1:
                return False
            self._insert_audit(
                conn,
                user_id=uid,
                provider=spec.provider,
                action=CredentialAction.DELETE,
                outcome="success",
                detail=None,
                actor=actor,
                source=source,
                version=current.key_version,
                now=self._now(),
            )
        return True

    # -- connectivity tests ------------------------------------------------

    def verify_stored(
        self,
        user_id: str,
        provider: str,
        *,
        verifier: Verifier,
        actor: str = "owner",
        source: str = "web",
    ) -> VerificationResult:
        """Test the owner's stored credential without ever returning it."""
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        actor, source = _clean_origin(actor, source)
        if not spec.testable:
            raise VerificationUnsupportedError("This credential cannot be tested.")
        try:
            credential = self.resolve(uid, spec.provider, purpose="credential_test")
        except CredentialUnavailableError as exc:
            self._audit_failure(uid, spec.provider, CredentialAction.TEST, exc, actor, source)
            raise
        result = _run_verifier(verifier, credential)
        now = self._now()
        with self._db() as conn:
            current = self._fetch(conn, uid, spec.provider)
            if current is not None and current.key_version == credential.version:
                conn.execute(
                    "UPDATE user_api_keys SET last_tested_at = ?, last_test_status = ? "
                    "WHERE id = ? AND user_id = ?",
                    (now, result.code if not result.ok else "ok", current.id, uid),
                )
            self._insert_audit(
                conn,
                user_id=uid,
                provider=spec.provider,
                action=CredentialAction.TEST,
                outcome="success" if result.ok else "failure",
                detail=result.code,
                actor=actor,
                source=source,
                version=credential.version,
                now=now,
            )
        return result

    def verify_candidate(
        self,
        user_id: str,
        provider: str,
        *,
        verifier: Verifier,
        api_key: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        model: str | None = None,
        actor: str = "owner",
        source: str = "web",
    ) -> VerificationResult:
        """Test a not-yet-stored credential. Nothing is persisted except the audit row."""
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        actor, source = _clean_origin(actor, source)
        if not spec.testable:
            raise VerificationUnsupportedError("This credential cannot be tested.")
        supplied = self._validated_secrets(
            spec,
            {
                "api_key": api_key,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        clean_model = _clean_model(model)
        result = _run_verifier(
            verifier,
            self._candidate(spec, supplied, None, clean_model, None),
        )
        try:
            with self._db() as conn:
                self._require_user(conn, uid)
                self._insert_audit(
                    conn,
                    user_id=uid,
                    provider=spec.provider,
                    action=CredentialAction.TEST,
                    outcome="success" if result.ok else "failure",
                    detail=f"candidate:{result.code}",
                    actor=actor,
                    source=source,
                    version=None,
                    now=self._now(),
                )
        except VaultError as exc:
            self._audit_failure(uid, spec.provider, CredentialAction.TEST, exc, actor, source)
            raise
        return result

    # -- use ---------------------------------------------------------------

    def resolve(
        self,
        user_id: str,
        provider: str,
        *,
        purpose: str,
    ) -> ResolvedCredential:
        """Decrypt the owner's credential for in-process use.

        This is the only place plaintext leaves the vault. It always selects by
        the caller-supplied ``user_id`` (a background job passes the job's own
        owner), never caches, and raises :class:`CredentialUnavailableError`
        with a precise reason instead of falling back to any other credential.
        """
        _clean_purpose(purpose)
        uid = _clean_identity(user_id)
        spec = provider_spec(provider)
        try:
            with self._db() as conn:
                row = self._fetch(conn, uid, spec.provider)
        except VaultUnavailableError:
            raise CredentialUnavailableError(
                spec.provider, UnavailableReason.VAULT_UNAVAILABLE
            ) from None
        if row is None or (
            row.status == CredentialStatus.ACTIVE.value and not row.has_secret
        ):
            raise CredentialUnavailableError(spec.provider, UnavailableReason.NOT_CONFIGURED)
        if row.status == CredentialStatus.REVOKED.value:
            raise CredentialUnavailableError(spec.provider, UnavailableReason.REVOKED)

        try:
            master = self._master()
        except VaultUnavailableError:
            raise CredentialUnavailableError(
                spec.provider, UnavailableReason.VAULT_UNAVAILABLE
            ) from None

        plaintext: dict[str, str] = {}
        legacy_fields: set[str] = set()
        for name, stored in row.secret_columns().items():
            try:
                if encryption.is_sealed(stored):
                    plaintext[name] = encryption.open_sealed(
                        master,
                        user_id=uid,
                        provider=spec.provider,
                        field_name=name,
                        token=stored,
                    )
                else:
                    plaintext[name] = encryption.read_legacy_value(master, stored)
                    legacy_fields.add(name)
            except encryption.DecryptionError as exc:
                logger.warning(
                    "A stored %s credential for user %s could not be decrypted (%s).",
                    spec.provider,
                    _user_ref(uid),
                    exc.reason,
                )
                raise CredentialUnavailableError(
                    spec.provider, UnavailableReason.DECRYPTION_FAILED
                ) from None

        if legacy_fields:
            self._upgrade_in_place(master, row, plaintext)

        def secret(name: str) -> SecretValue | None:
            value = plaintext.get(name)
            return SecretValue(value) if value else None

        return ResolvedCredential(
            provider=spec.provider,
            version=row.key_version,
            api_key=secret("api_key"),
            client_id=row.client_id,
            client_secret=secret("client_secret"),
            refresh_token=secret("refresh_token"),
            model=row.model,
        )

    def has_usable_credential(self, user_id: str, provider: str) -> bool:
        """Return whether the owner's *provider* credential can be used right now.

        For readiness and capability checks that need a yes/no and nothing
        else: the secret is decrypted inside the vault and discarded, so the
        caller never holds it. An absent, revoked or undecryptable credential,
        or a server that cannot decrypt at all, is simply ``False``.
        """
        try:
            self.resolve(user_id, provider, purpose="readiness_check")
        except CredentialUnavailableError:
            return False
        return True

    # -- legacy migration --------------------------------------------------

    def _upgrade_in_place(
        self,
        master: encryption.MasterKey,
        row: _Row,
        plaintext: Mapping[str, str],
    ) -> bool:
        """Re-seal a legacy row under per-user encryption (compare-and-swap)."""
        sealed = {
            name: encryption.seal(
                master,
                user_id=row.user_id,
                provider=row.provider,
                field_name=name,
                plaintext=value,
            )
            for name, value in plaintext.items()
        }
        primary = "api_key" if plaintext.get("api_key") else "refresh_token"
        hint = _hint_for(plaintext.get(primary, "")) if primary in plaintext else ""
        try:
            with self._db() as conn:
                updated = conn.execute(
                    """
                    UPDATE user_api_keys
                    SET api_key = ?, client_secret = ?, refresh_token = ?,
                        key_hint = COALESCE(key_hint, ?)
                    WHERE id = ? AND user_id = ?
                      AND api_key = ? AND client_secret IS ? AND refresh_token IS ?
                    """,
                    (
                        sealed.get("api_key", ""),
                        sealed.get("client_secret"),
                        sealed.get("refresh_token"),
                        hint or None,
                        row.id,
                        row.user_id,
                        row.api_key,
                        row.client_secret,
                        row.refresh_token,
                    ),
                )
                if updated.rowcount != 1:
                    return False  # another process upgraded it first
                self._insert_audit(
                    conn,
                    user_id=row.user_id,
                    provider=row.provider,
                    action=CredentialAction.MIGRATE,
                    outcome="success",
                    detail="legacy_format",
                    actor="system",
                    source="migration",
                    version=row.key_version,
                    now=self._now(),
                )
        except VaultError:
            return False
        return True

    def _count_legacy_fields(self) -> int:
        with self._db() as conn:
            found = conn.execute(
                """
                SELECT
                    COALESCE(SUM(api_key <> '' AND api_key NOT LIKE 'wv2.%'), 0)
                  + COALESCE(SUM(COALESCE(client_secret, '') <> ''
                                 AND client_secret NOT LIKE 'wv2.%'), 0)
                  + COALESCE(SUM(COALESCE(refresh_token, '') <> ''
                                 AND refresh_token NOT LIKE 'wv2.%'), 0)
                FROM user_api_keys
                """
            ).fetchone()
        return int(found[0]) if found else 0

    def migrate_legacy(self) -> MigrationReport:
        """Upgrade pre-vault credentials to per-user encryption (idempotent).

        Re-seals ``user_api_keys`` rows that hold Fernet tokens or plaintext, and
        moves each user's Google Health refresh token out of the generic
        ``hevy_meta`` table. When the server cannot encrypt, nothing is touched
        and the report says so. Values that cannot be decrypted are left as they
        are and counted as failed; they are never treated as usable plaintext.
        """
        try:
            master = self._master()
        except VaultUnavailableError as exc:
            try:
                pending = self._count_legacy_fields()
            except VaultError:
                pending = 0
            if pending:
                logger.warning(
                    "Credential encryption is unavailable (%s); %d stored credential "
                    "value(s) remain in the legacy format.",
                    exc.reason,
                    pending,
                )
            return MigrationReport(pending=pending, unavailable=True)

        upgraded = 0
        failed = 0
        with self._db() as conn:
            rows = [
                _row_from(found)
                for found in conn.execute(_SELECT_ROW).fetchall()
            ]
        for row in rows:
            legacy = {
                name: value
                for name, value in row.secret_columns().items()
                if not encryption.is_sealed(value)
            }
            if not legacy:
                continue
            try:
                plaintext = {
                    name: (
                        encryption.read_legacy_value(master, value)
                        if name in legacy
                        else encryption.open_sealed(
                            master,
                            user_id=row.user_id,
                            provider=row.provider,
                            field_name=name,
                            token=value,
                        )
                    )
                    for name, value in row.secret_columns().items()
                }
            except encryption.DecryptionError as exc:
                failed += 1
                logger.warning(
                    "A legacy %s credential could not be upgraded (%s).",
                    row.provider,
                    exc.reason,
                )
                continue
            if self._upgrade_in_place(master, row, plaintext):
                upgraded += 1

        moved, move_failed = self._migrate_google_health_tokens()
        upgraded += moved
        failed += move_failed
        try:
            pending = self._count_legacy_fields()
        except VaultError:
            pending = 0
        return MigrationReport(upgraded=upgraded, failed=failed, pending=pending)

    def _migrate_google_health_tokens(self) -> tuple[int, int]:
        with self._db() as conn:
            found = conn.execute(
                "SELECT user_id, value FROM hevy_meta WHERE key = ? AND value <> ''",
                (LEGACY_GOOGLE_HEALTH_META_KEY,),
            ).fetchall()
        moved = 0
        failed = 0
        for user_id, value in found:
            try:
                existing = self.describe(user_id, "google_health")
                if not existing.configured:
                    self._write_checked(
                        CredentialAction.MIGRATE,
                        user_id,
                        provider_spec("google_health"),
                        {"refresh_token": _clean_secret(value)},
                        None,
                        None,
                        "system",
                        "migration",
                        None,
                    )
                    moved += 1
                with self._db() as conn:
                    conn.execute(
                        "UPDATE hevy_meta SET value = '' "
                        "WHERE user_id = ? AND key = ? AND value = ?",
                        (user_id, LEGACY_GOOGLE_HEALTH_META_KEY, value),
                    )
            except (VaultError, ValueError):
                failed += 1
                logger.warning("A legacy Google Health token could not be moved.")
        return moved, failed


def migrate_legacy_credentials(db_path: str = DEFAULT_DB_PATH) -> MigrationReport:
    """Upgrade legacy credentials in *db_path*; safe to call at every startup."""
    return SecretVault(db_path).migrate_legacy()


def configured_provider_ids(conn: sqlite3.Connection, user_id: str) -> frozenset[str]:
    """Return providers where *user_id* has an active credential (metadata only).

    Takes the caller's connection so read-only diagnostics can use their own
    ``mode=ro`` handle. Secret columns are only tested for presence, never read.
    """
    found = conn.execute(
        """
        SELECT provider FROM user_api_keys
        WHERE user_id = ? AND status = 'active'
          AND (api_key <> ''
               OR COALESCE(client_secret, '') <> ''
               OR COALESCE(refresh_token, '') <> '')
        """,
        (user_id,),
    ).fetchall()
    return frozenset(str(row[0]).strip().lower() for row in found if row[0])
