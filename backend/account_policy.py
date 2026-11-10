"""Server-side account access policy: allowlist, invitation, status, revocation.

Restricting an invite-only or allowlisted deployment in the UI is not a control.
This module is the single decision point the web tier asks, twice:

* **Login completion** (``AccessPolicy.admit_login``): after Google has
  authenticated the person, decide whether *this deployment* will admit them.
  It checks that the identity is verified and carries Google's stable subject,
  that the address passes the address and domain allowlists (when configured),
  that an existing account is neither suspended nor deleted, that the subject
  and address do not point at different accounts, and that a new account is
  backed by a pending invitation (when the deployment is invite-only).
  Resolving the identity, registering a new account and spending its
  invitation are one atomic database transaction, so a refused sign-in leaves
  no account, identity or spent invitation behind.
* **Every authenticated request** (``AccessPolicy.check_session``): a signed
  session cookie proves who logged in, not that they may still be here. The
  account's current state is re-read from the database, so suspension,
  deletion, allowlist removal and explicit revocation take effect on the next
  request instead of when the cookie expires.

Denials are deliberately uniform to the outside world. ``DenialReason`` values
exist only for the server log; they are never placed in a response body, a
redirect target or a header, so a response cannot be used to learn whether an
address is allowlisted, invited, suspended or deleted.

Logs never contain an email address, token, cookie or secret. Identity appears
only as a pseudonymous tag (``data_classification.pseudonymise_identifier``)
plus the opaque account id when one exists.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from accounts import (
    AccountError,
    AccountInactiveError,
    AccountNotFoundError,
    AccountNotInvitedError,
    IdentityConflictError,
    IdentityVerificationError,
    admit_oauth_identity,
)
from data_classification import pseudonymise_identifier
from database import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_SUSPENDED,
    AccountState,
    get_account_state,
    revoke_sessions_outside_allowlist,
)

logger = logging.getLogger(__name__)

SESSION_EPOCH_KEY = "session_epoch"

_EMAIL_MAX_LENGTH = 254
_LOCAL_PART_MAX_LENGTH = 64
_DISPLAY_NAME_MAX_LENGTH = 200
# Conservative on purpose: letters, digits and the punctuation real mailboxes
# use. Anything else is denied rather than guessed at.
_LOCAL_PART_RE = re.compile(r"[a-z0-9._%+'-]+")
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_DOMAIN_MAX_LENGTH = 253
_SUBJECT_MAX_LENGTH = 255
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})

#: The identity provider this deployment signs people in with.
GOOGLE_PROVIDER = "google"

#: How a newcomer is admitted; see ``AccessPolicyConfig.registration_mode``.
RegistrationMode = Literal["open", "restricted", "invite_only"]


class AccessPolicyConfigurationError(RuntimeError):
    """Raised at startup when the access policy configuration is unsafe."""


class DenialReason(StrEnum):
    """Why access was refused. Log-only: never exposed to the client."""

    IDENTITY_UNVERIFIED = "identity_unverified"
    NOT_ALLOWLISTED = "not_allowlisted"
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    NOT_INVITED = "not_invited"
    IDENTITY_CONFLICT = "identity_conflict"
    ACCOUNT_SUSPENDED = "account_suspended"
    ACCOUNT_DELETED = "account_deleted"
    ACCOUNT_STATE_INVALID = "account_state_invalid"
    ACCOUNT_UNKNOWN = "account_unknown"
    SESSION_REVOKED = "session_revoked"


def canonical_email(value: object) -> str | None:
    """Return the canonical form of an email address, or None if unacceptable.

    Canonical means stripped and lower-cased. The check is intentionally
    strict: ASCII only (no Unicode-compatibility folding that could make two
    different identities compare equal), one ``@``, a dotted domain, and a
    conservative local-part alphabet. Provider-specific aliasing (dots, plus
    tags) is *not* collapsed, because that would equate identities the
    identity provider treats as distinct.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _EMAIL_MAX_LENGTH or not text.isascii():
        return None
    text = text.lower()
    local, separator, domain = text.partition("@")
    if not separator or "@" in domain:
        return None
    if (
        not local
        or len(local) > _LOCAL_PART_MAX_LENGTH
        or not _LOCAL_PART_RE.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return None
    if not _is_valid_domain(domain):
        return None
    return text


def _is_valid_domain(domain: str) -> bool:
    """Whether ``domain`` is a dotted, lower-case ASCII host name."""
    labels = domain.split(".")
    return (
        len(domain) <= _DOMAIN_MAX_LENGTH
        and len(labels) >= 2
        and all(_DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
    )


def canonical_domain(value: object) -> str | None:
    """Return the canonical form of a bare domain, or None if unacceptable.

    A domain is trimmed and lower-cased and must be a plain, dotted ASCII host
    name (``example.com``). Wildcards, a leading ``@`` or ``.``, and anything
    else that is not a host name are refused rather than interpreted, so an
    entry can never mean more than it says.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or not text.isascii() or not _is_valid_domain(text):
        return None
    return text


def parse_allowed_emails(raw: str | None) -> frozenset[str] | None:
    """Parse ``ALLOWED_EMAILS``; ``None`` means no allowlist is configured.

    Whether an allowlist exists is decided by the *raw* setting, never by how
    many entries parsed. A typo must not turn a restricted deployment into an
    open one, so a non-blank value with an invalid entry, or with no usable
    entry at all, stops startup instead of being silently ignored.
    """
    text = (raw or "").strip()
    if not text:
        return None
    addresses: set[str] = set()
    invalid_positions: list[int] = []
    for position, entry in enumerate(text.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        address = canonical_email(entry)
        if address is None:
            invalid_positions.append(position)
        else:
            addresses.add(address)
    if invalid_positions:
        raise AccessPolicyConfigurationError(
            "ALLOWED_EMAILS has invalid entries at position(s) "
            + ", ".join(str(position) for position in invalid_positions)
            + ". Use a comma-separated list of plain ASCII email addresses."
        )
    if not addresses:
        raise AccessPolicyConfigurationError(
            "ALLOWED_EMAILS is set but contains no email addresses. Leave it "
            "unset to disable the allowlist."
        )
    return frozenset(addresses)


def parse_allowed_domains(raw: str | None) -> frozenset[str] | None:
    """Parse ``ALLOWED_EMAIL_DOMAINS``; ``None`` means no domain rule is set.

    Like ``ALLOWED_EMAILS``, whether the rule exists is decided by the *raw*
    setting, so a typo stops startup instead of silently admitting every
    domain. Entries are bare domains (``example.com``) and match that exact
    domain only; subdomains are different domains and must be listed.
    """
    text = (raw or "").strip()
    if not text:
        return None
    domains: set[str] = set()
    invalid_positions: list[int] = []
    for position, entry in enumerate(text.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        domain = canonical_domain(entry)
        if domain is None:
            invalid_positions.append(position)
        else:
            domains.add(domain)
    if invalid_positions:
        raise AccessPolicyConfigurationError(
            "ALLOWED_EMAIL_DOMAINS has invalid entries at position(s) "
            + ", ".join(str(position) for position in invalid_positions)
            + ". Use a comma-separated list of bare domains such as example.com."
        )
    if not domains:
        raise AccessPolicyConfigurationError(
            "ALLOWED_EMAIL_DOMAINS is set but contains no domains. Leave it "
            "unset to disable the domain rule."
        )
    return frozenset(domains)


def _parse_flag(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise AccessPolicyConfigurationError(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


@dataclass(frozen=True, slots=True)
class AccessPolicyConfig:
    """Validated, non-secret deployment access policy."""

    allowed_emails: frozenset[str] | None = None
    invite_only: bool = False
    allowed_domains: frozenset[str] | None = None

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str] | None = None
    ) -> AccessPolicyConfig:
        source = os.environ if environ is None else environ
        return cls(
            allowed_emails=parse_allowed_emails(source.get("ALLOWED_EMAILS")),
            invite_only=_parse_flag(source, "INVITE_ONLY"),
            allowed_domains=parse_allowed_domains(source.get("ALLOWED_EMAIL_DOMAINS")),
        )

    def rejection(self, email: str) -> DenialReason | None:
        """Why a canonical address fails the address or domain rules, if it does.

        Each configured rule must pass: an address allowlist and a domain
        allowlist together admit only the listed addresses at the listed
        domains. The domain is the part after the last ``@`` and matches
        exactly (no subdomains).
        """
        if self.allowed_emails is not None and email not in self.allowed_emails:
            return DenialReason.NOT_ALLOWLISTED
        if (
            self.allowed_domains is not None
            and email.rpartition("@")[2] not in self.allowed_domains
        ):
            return DenialReason.DOMAIN_NOT_ALLOWED
        return None

    def allows(self, email: str) -> bool:
        """Whether a canonical address passes every configured address rule."""
        return self.rejection(email) is None

    @property
    def registration_mode(self) -> RegistrationMode:
        """How a newcomer is admitted: ``invite_only``, ``restricted`` or ``open``.

        ``restricted`` means an address or domain allowlist limits who may
        sign in. The value describes the deployment, never an individual, so it
        is safe to show anonymously on the sign-in page.
        """
        if self.invite_only:
            return "invite_only"
        if self.allowed_emails is not None or self.allowed_domains is not None:
            return "restricted"
        return "open"

    def describe(self) -> str:
        """Non-secret summary for the startup log (never lists addresses)."""
        entries = 0 if self.allowed_emails is None else len(self.allowed_emails)
        domains = 0 if self.allowed_domains is None else len(self.allowed_domains)
        return (
            f"allowlist={'on' if self.allowed_emails is not None else 'off'} "
            f"allowlist_entries={entries} "
            f"domain_allowlist={'on' if self.allowed_domains is not None else 'off'} "
            f"domain_entries={domains} "
            f"invite_only={'on' if self.invite_only else 'off'}"
        )


@dataclass(frozen=True, slots=True)
class LoginDecision:
    """Outcome of login completion. ``reason`` is set exactly when denied."""

    allowed: bool
    reason: DenialReason | None = None
    user_id: str | None = None
    email: str | None = None
    session_epoch: int = 0
    created: bool = False


@dataclass(frozen=True, slots=True)
class SessionDecision:
    """Outcome of re-validating an existing session."""

    allowed: bool
    reason: DenialReason | None = None
    user_id: str | None = None


def log_access_denied(
    surface: str,
    reason: DenialReason,
    *,
    user_id: str | None = None,
    email: str | None = None,
) -> None:
    """Record a denial without placing any secret or address in the log.

    Every field is drawn from a closed vocabulary or sanitised here, so a
    hostile identity cannot inject log lines or smuggle content into the log:
    the reason and surface are constants, the account id must look like an
    opaque id, and the address is reduced to a pseudonymous tag.
    """
    account = user_id if user_id and _SAFE_ID_RE.fullmatch(user_id) else "-"
    identity = pseudonymise_identifier(email) if email else "-"
    logger.warning(
        "access_denied surface=%s reason=%s account=%s identity=%s",
        surface,
        reason.value,
        account,
        identity,
    )


def _email_is_verified(userinfo: Mapping[str, Any]) -> bool:
    """Accept only an explicit, affirmative ``email_verified`` claim."""
    claim = userinfo.get("email_verified")
    if claim is True:
        return True
    return isinstance(claim, str) and claim.strip().lower() == "true"


def _clean_subject(value: object) -> str | None:
    """Return the provider's stable subject, or None if it is unusable.

    The subject selects the account, so anything that is not a short, printable,
    non-blank string (including a number or other non-string claim) is refused
    instead of being coerced into something that could match.
    """
    if not isinstance(value, str):
        return None
    subject = value.strip()
    if (
        not subject
        or len(subject) > _SUBJECT_MAX_LENGTH
        or not subject.isprintable()
    ):
        return None
    return subject


def _clean_display_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:_DISPLAY_NAME_MAX_LENGTH] or None


_STATUS_REASONS = {
    ACCOUNT_STATUS_SUSPENDED: DenialReason.ACCOUNT_SUSPENDED,
    ACCOUNT_STATUS_DELETED: DenialReason.ACCOUNT_DELETED,
}


class AccessPolicy:
    """Evaluate the deployment's access policy against server-side state."""

    def __init__(self, config: AccessPolicyConfig, db_path: str) -> None:
        self.config = config
        self.db_path = db_path

    # ------------------------------------------------------------------ login

    def admit_login(
        self,
        userinfo: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> LoginDecision:
        """Decide whether a freshly authenticated Google identity may sign in.

        The identity must carry a verified address and Google's stable subject;
        the subject, not the address, selects the account. On success the
        account exists (it is registered here for a first sign-in the policy
        permits, spending its invitation in the same transaction) and the
        returned ``session_epoch`` must be stored in the new session. On denial
        nothing is created, bound or spent.
        """
        if not isinstance(userinfo, Mapping):
            return self._deny_login(DenialReason.IDENTITY_UNVERIFIED)
        email = canonical_email(userinfo.get("email"))
        subject = _clean_subject(userinfo.get("sub"))
        if email is None or subject is None or not _email_is_verified(userinfo):
            return self._deny_login(DenialReason.IDENTITY_UNVERIFIED, email=email)
        rejected = self.config.rejection(email)
        if rejected is not None:
            return self._deny_login(rejected, email=email)

        try:
            admission = admit_oauth_identity(
                provider=GOOGLE_PROVIDER,
                subject=subject,
                email=email,
                email_verified=True,
                display_name=_clean_display_name(userinfo.get("name")),
                require_invitation=self.config.invite_only,
                db_path=self.db_path,
                now=now,
            )
        except AccountNotInvitedError:
            return self._deny_login(DenialReason.NOT_INVITED, email=email)
        except AccountInactiveError as exc:
            reason = _STATUS_REASONS.get(exc.status, DenialReason.ACCOUNT_STATE_INVALID)
            return self._deny_login(reason, email=email, user_id=exc.user_id)
        except IdentityConflictError:
            return self._deny_login(DenialReason.IDENTITY_CONFLICT, email=email)
        except IdentityVerificationError:
            return self._deny_login(DenialReason.IDENTITY_UNVERIFIED, email=email)
        except AccountNotFoundError:
            # An identity whose account row is gone: nobody to admit.
            return self._deny_login(DenialReason.ACCOUNT_UNKNOWN, email=email)
        except AccountError:
            # Any other account-model refusal fails closed rather than surfacing
            # as a server error that could differ from every other denial.
            return self._deny_login(DenialReason.ACCOUNT_STATE_INVALID, email=email)

        user = admission.user
        # The subject may name an account whose recorded address differs from
        # the one Google reports now. Sessions are re-checked against the
        # recorded address on every request, so refuse here instead of issuing
        # a session the very next request would end.
        recorded_rejection = self.config.rejection(user["email"].strip().lower())
        if recorded_rejection is not None:
            return self._deny_login(
                recorded_rejection, email=email, user_id=user["id"]
            )
        return LoginDecision(
            allowed=True,
            user_id=user["id"],
            email=user["email"],
            session_epoch=admission.session_epoch,
            created=admission.created,
        )

    @staticmethod
    def _deny_login(
        reason: DenialReason,
        *,
        email: str | None = None,
        user_id: str | None = None,
    ) -> LoginDecision:
        log_access_denied("login", reason, user_id=user_id, email=email)
        return LoginDecision(allowed=False, reason=reason)

    # ---------------------------------------------------------------- session

    def check_session(self, session: Mapping[str, Any]) -> SessionDecision:
        """Re-validate a decoded session against current server-side state.

        Raises ``sqlite3.Error`` if the state cannot be read; the caller must
        treat that as "unavailable" (and keep the session), never as "allowed".
        """
        user_id = session.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return self._deny_session(DenialReason.ACCOUNT_UNKNOWN)
        state = get_account_state(user_id, self.db_path)
        if state is None:
            return self._deny_session(DenialReason.ACCOUNT_UNKNOWN, user_id=user_id)

        reason = self._account_denial(state, session)
        if reason is not None:
            return self._deny_session(
                reason, user_id=state["id"], email=state["email"]
            )
        return SessionDecision(allowed=True, user_id=state["id"])

    def _account_denial(
        self, state: AccountState, session: Mapping[str, Any]
    ) -> DenialReason | None:
        status = state["status"]
        if status != ACCOUNT_STATUS_ACTIVE:
            return _STATUS_REASONS.get(status, DenialReason.ACCOUNT_STATE_INVALID)
        rejected = self.config.rejection(state["email"].strip().lower())
        if rejected is not None:
            return rejected
        # Sessions issued before this feature carry no epoch; they are valid
        # exactly until the first revocation advances the stored epoch.
        issued = session.get(SESSION_EPOCH_KEY, 0)
        if isinstance(issued, bool) or not isinstance(issued, int):
            return DenialReason.SESSION_REVOKED
        if issued != state["session_epoch"]:
            return DenialReason.SESSION_REVOKED
        return None

    @staticmethod
    def _deny_session(
        reason: DenialReason,
        *,
        user_id: str | None = None,
        email: str | None = None,
    ) -> SessionDecision:
        log_access_denied("session", reason, user_id=user_id, email=email)
        return SessionDecision(allowed=False, reason=reason)

    # -------------------------------------------------------------- lifecycle

    def reconcile(self) -> int:
        """Make an allowlist change a durable revocation. Run at startup.

        An address removed from ``ALLOWED_EMAILS`` (or a domain removed from
        ``ALLOWED_EMAIL_DOMAINS``) is already refused on its next request, but
        adding it back later would otherwise make a cookie issued before the
        removal valid again. Advancing the epoch of every account the
        allowlists do not admit ends those sessions for good.
        """
        if self.config.allowed_emails is None and self.config.allowed_domains is None:
            return 0
        return revoke_sessions_outside_allowlist(
            self.config.allowed_emails,
            self.db_path,
            allowed_domains=self.config.allowed_domains,
        )
