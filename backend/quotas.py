"""Per-account quotas and abuse controls.

Every account-scoped quota is enforced for the authenticated ``user_id`` taken
from the signed session (or, for the scheduled agent, from the user row it acts
for). A client-supplied owner identifier is never consulted.

Usage is persisted as a sliding-window ledger in the shared SQLite database, so
the web service and the scheduled agent see the same counters and a restart or a
second replica cannot reset them. Each check-and-record runs in one
``BEGIN IMMEDIATE`` transaction, so concurrent callers cannot jointly exceed a
limit. Any storage or configuration failure fails closed
(:class:`QuotaUnavailableError`) instead of silently disabling protection.

Limits are chosen by the account's entitlement (``account_quota_profiles``), may
be scaled or switched off for a bounded period by an audited administrator
override (``quota_overrides``), and can be tuned per deployment with
``QUOTA_POLICY_OVERRIDES``. Rejected requests are tallied per account, quota and
hour (``quota_denials``) so abuse review has evidence without letting a flood of
refusals grow the database.

Sign-in attempts happen before an account is known, so they are additionally
throttled per network address (``login_attempts``); only a keyed hash of the
address is stored. See ``docs/ACCOUNT_QUOTAS.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

POLICY_OVERRIDES_ENV = "QUOTA_POLICY_OVERRIDES"

# Allowed-usage rows are kept for at least a day so the administrator overview
# can report recent volume even for quotas whose longest window is shorter.
USAGE_RETENTION_SECONDS = _DAY
DENIAL_BUCKET_SECONDS = _HOUR
# Refusals are tallied at most this often per account and quota (see _tally_refusal),
# and logged at most this often per account/address and quota.
DENIAL_SAMPLE_SECONDS = 1.0
LOG_INTERVAL_SECONDS = 1.0
_MAX_LOG_KEYS = 10_000
DENIAL_RETENTION_SECONDS = 30 * _DAY
ABUSE_WINDOW_SECONDS = _DAY
ABUSE_DENIAL_THRESHOLD = 20

MAX_RULES_PER_KIND = 4
MAX_RULE_LIMIT = 1_000_000
MAX_RULE_WINDOW_SECONDS = 7 * _DAY

CODE_EXCEEDED = "quota_exceeded"
CODE_DISABLED = "quota_disabled"


class QuotaKind(StrEnum):
    LOGIN = "login"
    AI_REQUEST = "ai_request"
    IMPORT = "import"
    EXPORT = "export"
    SYNC = "sync"
    NOTIFICATION = "notification"
    REPORT = "report"


class Entitlement(StrEnum):
    RESTRICTED = "restricted"
    STANDARD = "standard"
    EXTENDED = "extended"


DEFAULT_ENTITLEMENT = Entitlement.STANDARD


@dataclass(frozen=True, slots=True)
class QuotaRule:
    """Allow at most ``limit`` units within any ``window_seconds`` period."""

    limit: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class QuotaGrant:
    """Result of an allowed request, reported for the tightest applicable rule."""

    kind: QuotaKind
    entitlement: Entitlement
    limit: int
    window_seconds: int
    remaining: int


@dataclass(frozen=True, slots=True)
class QuotaDenial:
    """Why a request was refused and when (if ever) retrying can succeed."""

    kind: QuotaKind
    code: str
    limit: int
    window_seconds: int
    retry_after_seconds: int | None


class QuotaError(Exception):
    """Base class for quota failures."""


class QuotaConfigError(QuotaError, ValueError):
    """The deployment's quota configuration is invalid."""


class QuotaUnavailableError(QuotaError):
    """Quota state could not be read or recorded, so the request fails closed."""


class QuotaExceededError(QuotaError):
    """The account has no remaining allowance for the requested quota."""

    def __init__(self, denial: QuotaDenial) -> None:
        super().__init__(f"{denial.kind.value} quota unavailable")
        self.denial = denial


def _rules(*pairs: tuple[int, int]) -> tuple[QuotaRule, ...]:
    return tuple(QuotaRule(limit, window) for limit, window in pairs)


# Built-in limits per entitlement. A deployment can replace the rules of any
# entitlement/quota pair with QUOTA_POLICY_OVERRIDES; an administrator can move
# one account between entitlements or scale a single quota for that account.
DEFAULT_POLICY: Mapping[Entitlement, Mapping[QuotaKind, tuple[QuotaRule, ...]]] = {
    Entitlement.RESTRICTED: {
        QuotaKind.LOGIN: _rules((5, _HOUR)),
        QuotaKind.AI_REQUEST: _rules((3, _MINUTE), (20, _DAY)),
        QuotaKind.IMPORT: _rules((2, _MINUTE), (10, _HOUR)),
        QuotaKind.EXPORT: _rules((1, _HOUR), (2, _DAY)),
        QuotaKind.SYNC: _rules((1, 5 * _MINUTE), (2, _DAY)),
        QuotaKind.NOTIFICATION: _rules((2, _HOUR), (5, _DAY)),
        QuotaKind.REPORT: _rules((6, _MINUTE), (100, _DAY)),
    },
    Entitlement.STANDARD: {
        QuotaKind.LOGIN: _rules((20, _HOUR)),
        QuotaKind.AI_REQUEST: _rules((15, _MINUTE), (300, _DAY)),
        QuotaKind.IMPORT: _rules((10, _MINUTE), (60, _HOUR)),
        QuotaKind.EXPORT: _rules((3, _HOUR), (10, _DAY)),
        QuotaKind.SYNC: _rules((2, 5 * _MINUTE), (10, _DAY)),
        QuotaKind.NOTIFICATION: _rules((10, _HOUR), (50, _DAY)),
        QuotaKind.REPORT: _rules((30, _MINUTE), (1000, _DAY)),
    },
    Entitlement.EXTENDED: {
        QuotaKind.LOGIN: _rules((60, _HOUR)),
        QuotaKind.AI_REQUEST: _rules((30, _MINUTE), (1000, _DAY)),
        QuotaKind.IMPORT: _rules((30, _MINUTE), (200, _HOUR)),
        QuotaKind.EXPORT: _rules((10, _HOUR), (30, _DAY)),
        QuotaKind.SYNC: _rules((6, 5 * _MINUTE), (30, _DAY)),
        QuotaKind.NOTIFICATION: _rules((30, _HOUR), (150, _DAY)),
        QuotaKind.REPORT: _rules((90, _MINUTE), (5000, _DAY)),
    },
}

# Sign-in endpoints run before an account is known; this per-address policy
# bounds callback floods that would otherwise each trigger a token exchange.
LOGIN_ADDRESS_RULES = _rules((60, 5 * _MINUTE), (300, _HOUR))


@dataclass(frozen=True, slots=True)
class _PolicyOverrides:
    entitlements: Mapping[Entitlement, Mapping[QuotaKind, tuple[QuotaRule, ...]]]
    login_by_address: tuple[QuotaRule, ...] | None


def _parse_rules(raw: object, where: str) -> tuple[QuotaRule, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RULES_PER_KIND:
        raise QuotaConfigError(
            f"{POLICY_OVERRIDES_ENV} {where}: expected 1-{MAX_RULES_PER_KIND} "
            "[limit, window_seconds] pairs"
        )
    rules: list[QuotaRule] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(type(value) is int for value in item)
        ):
            raise QuotaConfigError(
                f"{POLICY_OVERRIDES_ENV} {where}: each rule must be [limit, window_seconds]"
            )
        limit, window = item
        if not 1 <= limit <= MAX_RULE_LIMIT or not 1 <= window <= MAX_RULE_WINDOW_SECONDS:
            raise QuotaConfigError(
                f"{POLICY_OVERRIDES_ENV} {where}: limit must be 1-{MAX_RULE_LIMIT} and "
                f"window_seconds 1-{MAX_RULE_WINDOW_SECONDS}"
            )
        rules.append(QuotaRule(limit, window))
    if len({rule.window_seconds for rule in rules}) != len(rules):
        raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} {where}: duplicate window")
    return tuple(rules)


def _parse_policy_overrides(text: str) -> _PolicyOverrides:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} is not valid JSON") from exc
    if not isinstance(data, dict) or not set(data) <= {"entitlements", "login_by_address"}:
        raise QuotaConfigError(
            f"{POLICY_OVERRIDES_ENV} must be an object with only "
            "'entitlements' and 'login_by_address'"
        )

    entitlements: dict[Entitlement, dict[QuotaKind, tuple[QuotaRule, ...]]] = {}
    raw_entitlements = data.get("entitlements", {})
    if not isinstance(raw_entitlements, dict):
        raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} 'entitlements' must be an object")
    for name, per_kind in raw_entitlements.items():
        try:
            entitlement = Entitlement(name)
        except ValueError as exc:
            raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} unknown entitlement") from exc
        if not isinstance(per_kind, dict):
            raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} {name}: expected an object")
        parsed: dict[QuotaKind, tuple[QuotaRule, ...]] = {}
        for kind_name, raw_rules in per_kind.items():
            try:
                kind = QuotaKind(kind_name)
            except ValueError as exc:
                raise QuotaConfigError(f"{POLICY_OVERRIDES_ENV} unknown quota kind") from exc
            parsed[kind] = _parse_rules(raw_rules, f"{name}.{kind_name}")
        entitlements[entitlement] = parsed

    login = data.get("login_by_address")
    return _PolicyOverrides(
        entitlements=entitlements,
        login_by_address=_parse_rules(login, "login_by_address") if login is not None else None,
    )


def _policy_overrides(environ: Mapping[str, str] | None) -> _PolicyOverrides:
    text = (
        os.environ.get("QUOTA_POLICY_OVERRIDES", "")
        if environ is None
        else environ.get(POLICY_OVERRIDES_ENV, "")
    )
    if not text.strip():
        return _PolicyOverrides(entitlements={}, login_by_address=None)
    return _parse_policy_overrides(text)


def validate_policy_environment(environ: Mapping[str, str] | None = None) -> None:
    """Raise :class:`QuotaConfigError` for an invalid ``QUOTA_POLICY_OVERRIDES``."""

    _policy_overrides(environ)


def rules_for(
    entitlement: Entitlement,
    kind: QuotaKind,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[QuotaRule, ...]:
    """Return the configured rules for one entitlement and quota."""

    overridden = _policy_overrides(environ).entitlements.get(entitlement, {}).get(kind)
    return overridden or DEFAULT_POLICY[entitlement][kind]


def login_address_rules(
    environ: Mapping[str, str] | None = None,
) -> tuple[QuotaRule, ...]:
    return _policy_overrides(environ).login_by_address or LOGIN_ADDRESS_RULES


@dataclass(frozen=True, slots=True)
class _Override:
    limit_percent: int
    expires_at: float | None


def _scaled(
    rules: Sequence[QuotaRule], override: _Override | None
) -> tuple[QuotaRule, ...]:
    if override is None:
        return tuple(rules)
    if override.limit_percent == 0:
        return tuple(QuotaRule(0, rule.window_seconds) for rule in rules)
    return tuple(
        QuotaRule(max(1, rule.limit * override.limit_percent // 100), rule.window_seconds)
        for rule in rules
    )


@contextmanager
def connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open a short-lived autocommit connection with foreign keys enforced."""

    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(db_path: str) -> Iterator[sqlite3.Connection]:
    """Run the body in one ``BEGIN IMMEDIATE`` transaction (commit or roll back)."""

    with connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def entitlement_for(conn: sqlite3.Connection, user_id: str) -> Entitlement:
    """Return the account's entitlement; unknown stored values fail closed."""

    row = conn.execute(
        "SELECT entitlement FROM account_quota_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_ENTITLEMENT
    try:
        return Entitlement(row[0])
    except ValueError:
        logger.error("Unrecognised stored entitlement; applying the restricted policy.")
        return Entitlement.RESTRICTED


def _active_override(
    conn: sqlite3.Connection, user_id: str, kind: QuotaKind, now: float
) -> _Override | None:
    row = conn.execute(
        "SELECT limit_percent, expires_at FROM quota_overrides "
        "WHERE user_id = ? AND kind = ? AND (expires_at IS NULL OR expires_at > ?)",
        (user_id, kind.value, now),
    ).fetchone()
    return None if row is None else _Override(int(row[0]), row[1])


def _require_account(user_id: object) -> str:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 128:
        raise ValueError("an authenticated account identity is required")
    return user_id


def _account_ref(value: str) -> str:
    """Stable, non-reversible reference for correlating log lines."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def seconds_until_allowed(
    event_times: Sequence[float], *, limit: int, window_seconds: int, now: float
) -> int:
    """Seconds until one more unit fits, given the in-window events (ascending).

    Callers invoke this when ``len(event_times) >= limit >= 1``. The count and
    the rows are read by separate statements, so the function stays total if a
    concurrent change leaves fewer events than expected: it then reports the
    full window, a safe upper bound.
    """

    excess = len(event_times) - limit + 1
    if excess < 1:
        return max(1, window_seconds)
    expiry = event_times[excess - 1] + window_seconds
    return max(1, math.ceil(expiry - now))


def _stricter(current: QuotaDenial | None, candidate: QuotaDenial) -> QuotaDenial:
    if current is None:
        return candidate
    if current.retry_after_seconds is None:
        return current
    if candidate.retry_after_seconds is None:
        return candidate
    return candidate if candidate.retry_after_seconds > current.retry_after_seconds else current


def _window_stats(
    conn: sqlite3.Connection, user_id: str, kind: QuotaKind, window: int, now: float
) -> tuple[int, float | None]:
    """Return how many units are in the window and when the oldest was used."""

    count, oldest = conn.execute(
        "SELECT COUNT(*), MIN(occurred_at) FROM quota_usage "
        "WHERE user_id = ? AND kind = ? AND occurred_at > ?",
        (user_id, kind.value, now - window),
    ).fetchone()
    return int(count), oldest


def _in_window(
    conn: sqlite3.Connection, user_id: str, kind: QuotaKind, window: int, now: float
) -> list[float]:
    rows = conn.execute(
        "SELECT occurred_at FROM quota_usage "
        "WHERE user_id = ? AND kind = ? AND occurred_at > ? ORDER BY occurred_at, id",
        (user_id, kind.value, now - window),
    ).fetchall()
    return [float(row[0]) for row in rows]


def _record_denial(
    conn: sqlite3.Connection, user_id: str, kind: QuotaKind, now: float
) -> None:
    conn.execute(
        "INSERT INTO quota_denials (user_id, kind, bucket, denials, last_denied_at) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(user_id, kind, bucket) DO UPDATE SET "
        "denials = denials + 1, last_denied_at = excluded.last_denied_at",
        (user_id, kind.value, int(now // DENIAL_BUCKET_SECONDS), now),
    )
    conn.execute(
        "DELETE FROM quota_denials WHERE user_id = ? AND last_denied_at <= ?",
        (user_id, now - DENIAL_RETENTION_SECONDS),
    )


def _tally_refusal(db_path: str, user_id: str, kind: QuotaKind, now: float) -> None:
    """Best-effort tally of a refusal; never blocks, delays or fails the refusal.

    Tallying needs the write lock, so it is sampled (at most once per
    ``DENIAL_SAMPLE_SECONDS`` per account and quota) and skipped when the
    database is busy. A flood of refused requests must not become write
    contention for everyone else.
    """

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=0.05, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 50")
        conn.execute("PRAGMA foreign_keys = ON")
        latest = conn.execute(
            "SELECT MAX(last_denied_at) FROM quota_denials WHERE user_id = ? AND kind = ?",
            (user_id, kind.value),
        ).fetchone()[0]
        if latest is not None and now - latest < DENIAL_SAMPLE_SECONDS:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            _record_denial(conn, user_id, kind, now)
            conn.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        logger.debug("A quota refusal was not tallied: %s", type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()


def _unavailable(exc: Exception) -> QuotaUnavailableError:
    # Log only the failure class: messages can carry file paths.
    logger.error("Quota state is unavailable: %s", type(exc).__name__)
    return QuotaUnavailableError("usage limits are temporarily unavailable")


_log_lock = threading.Lock()
_last_logged: dict[str, float] = {}


def _log_due(key: str) -> bool:
    """Allow one log line per key per interval so a flood cannot flood the logs."""

    now = time.monotonic()
    with _log_lock:
        last = _last_logged.get(key)
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return False
        if len(_last_logged) >= _MAX_LOG_KEYS:
            _last_logged.clear()
        _last_logged[key] = now
    return True


def _log_denial(reference: str, denial: QuotaDenial) -> None:
    if not _log_due(f"{reference}:{denial.kind.value}"):
        return
    logger.warning(
        "Quota denied: kind=%s code=%s limit=%d window_seconds=%d "
        "retry_after_seconds=%s account=%s",
        denial.kind.value,
        denial.code,
        denial.limit,
        denial.window_seconds,
        denial.retry_after_seconds,
        reference,
    )


def _decide(
    conn: sqlite3.Connection,
    user_id: str,
    kind: QuotaKind,
    now: float,
    environ: Mapping[str, str] | None,
) -> tuple[QuotaDenial | QuotaGrant, tuple[QuotaRule, ...]]:
    """Evaluate one request against the account's limits without recording it."""

    entitlement = entitlement_for(conn, user_id)
    override = _active_override(conn, user_id, kind, now)
    rules = _scaled(rules_for(entitlement, kind, environ=environ), override)

    if any(rule.limit == 0 for rule in rules):
        expires_at = override.expires_at if override is not None else None
        disabled = QuotaDenial(
            kind,
            CODE_DISABLED,
            0,
            rules[0].window_seconds,
            None if expires_at is None else max(1, math.ceil(expires_at - now)),
        )
        return disabled, rules

    denial: QuotaDenial | None = None
    tightest: QuotaGrant | None = None
    for rule in rules:
        used, _oldest = _window_stats(conn, user_id, kind, rule.window_seconds, now)
        if used >= rule.limit:
            events = _in_window(conn, user_id, kind, rule.window_seconds, now)
            denial = _stricter(
                denial,
                QuotaDenial(
                    kind,
                    CODE_EXCEEDED,
                    rule.limit,
                    rule.window_seconds,
                    seconds_until_allowed(
                        events,
                        limit=rule.limit,
                        window_seconds=rule.window_seconds,
                        now=now,
                    ),
                ),
            )
            continue
        remaining = rule.limit - used - 1
        if tightest is None or remaining < tightest.remaining:
            tightest = QuotaGrant(kind, entitlement, rule.limit, rule.window_seconds, remaining)
    if denial is not None:
        return denial, rules
    if tightest is None:
        raise QuotaConfigError("quota policy has no rules")
    return tightest, rules


def consume(
    user_id: str,
    kind: QuotaKind | str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> QuotaGrant:
    """Atomically check and record one unit of *kind* for the account.

    Raises :class:`QuotaExceededError` (with retry information) when the account
    has no allowance left and :class:`QuotaUnavailableError` when quota state
    cannot be read or written, so callers fail closed.
    """

    account = _require_account(user_id)
    quota = QuotaKind(kind)
    moment = time.time() if now is None else now
    try:
        # Fast path: an exhausted account is refused from a read-only snapshot,
        # so refused requests never queue behind (or hold) the write lock. A
        # snapshot can only over-refuse by milliseconds; it can never allow.
        with connection(db_path) as conn:
            outcome, _ = _decide(conn, account, quota, moment, environ)
        if isinstance(outcome, QuotaDenial):
            _tally_refusal(db_path, account, quota, moment)
        else:
            with write_transaction(db_path) as conn:
                # Authoritative check-and-record: concurrent callers serialise here.
                outcome, rules = _decide(conn, account, quota, moment, environ)
                if isinstance(outcome, QuotaDenial):
                    _record_denial(conn, account, quota, moment)
                else:
                    horizon = max(
                        max(rule.window_seconds for rule in rules), USAGE_RETENTION_SECONDS
                    )
                    conn.execute(
                        "DELETE FROM quota_usage "
                        "WHERE user_id = ? AND kind = ? AND occurred_at <= ?",
                        (account, quota.value, moment - horizon),
                    )
                    conn.execute(
                        "INSERT INTO quota_usage (user_id, kind, occurred_at) "
                        "VALUES (?, ?, ?)",
                        (account, quota.value, moment),
                    )
    except QuotaConfigError as exc:
        raise _unavailable(exc) from exc
    except sqlite3.Error as exc:
        raise _unavailable(exc) from exc

    if isinstance(outcome, QuotaDenial):
        _log_denial(_account_ref(account), outcome)
        raise QuotaExceededError(outcome)
    return outcome


def _network_key(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        # A single subscriber is typically delegated a /64, so rotating within
        # it must not buy a fresh allowance.
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(ip)


def _subject_hash(address: str, environ: Mapping[str, str] | None) -> str:
    secret = (
        os.environ.get("WEB_AUTH_SECRET", "")
        if environ is None
        else environ.get("WEB_AUTH_SECRET", "")
    )
    key = (secret.strip() or "workout-agent-login-throttle").encode()
    digest = hmac.new(key, f"login:{_network_key(address)}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def _login_denial(
    conn: sqlite3.Connection,
    subject: str,
    rules: Sequence[QuotaRule],
    now: float,
) -> QuotaDenial | None:
    denial: QuotaDenial | None = None
    for rule in rules:
        rows = conn.execute(
            "SELECT attempted_at FROM login_attempts "
            "WHERE subject_hash = ? AND attempted_at > ? ORDER BY attempted_at",
            (subject, now - rule.window_seconds),
        ).fetchall()
        events = [float(row[0]) for row in rows]
        if len(events) >= rule.limit:
            denial = _stricter(
                denial,
                QuotaDenial(
                    QuotaKind.LOGIN,
                    CODE_EXCEEDED,
                    rule.limit,
                    rule.window_seconds,
                    seconds_until_allowed(
                        events,
                        limit=rule.limit,
                        window_seconds=rule.window_seconds,
                        now=now,
                    ),
                ),
            )
    return denial


def consume_login_attempt(
    address: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Throttle a pre-authentication sign-in request by validated client address.

    Refused attempts are neither recorded nor allowed to take the write lock, so
    a flood of them cannot extend its own lock-out, grow the table or contend
    with legitimate writers. Raises the same errors as :func:`consume`.
    """

    moment = time.time() if now is None else now
    denial: QuotaDenial | None = None
    subject = ""
    try:
        rules = login_address_rules(environ)
        subject = _subject_hash(address, environ)
        with connection(db_path) as conn:
            denial = _login_denial(conn, subject, rules, moment)
        if denial is None:
            with write_transaction(db_path) as conn:
                denial = _login_denial(conn, subject, rules, moment)
                if denial is None:
                    conn.execute(
                        "DELETE FROM login_attempts WHERE attempted_at <= ?",
                        (moment - max(rule.window_seconds for rule in rules),),
                    )
                    conn.execute(
                        "INSERT INTO login_attempts (subject_hash, attempted_at) "
                        "VALUES (?, ?)",
                        (subject, moment),
                    )
    except QuotaConfigError as exc:
        raise _unavailable(exc) from exc
    except sqlite3.Error as exc:
        raise _unavailable(exc) from exc

    if denial is not None:
        _log_denial(f"address:{subject[:12]}", denial)
        raise QuotaExceededError(denial)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def usage_snapshot(
    user_id: str,
    db_path: str,
    *,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return the account's entitlement and current usage for every quota."""

    account = _require_account(user_id)
    moment = time.time() if now is None else now
    try:
        with connection(db_path) as conn:
            entitlement = entitlement_for(conn, account)
            quotas: dict[str, Any] = {}
            for kind in QuotaKind:
                override = _active_override(conn, account, kind, moment)
                entries = []
                for rule in _scaled(
                    rules_for(entitlement, kind, environ=environ), override
                ):
                    used, oldest = _window_stats(
                        conn, account, kind, rule.window_seconds, moment
                    )
                    entries.append(
                        {
                            "limit": rule.limit,
                            "window_seconds": rule.window_seconds,
                            "used": used,
                            "remaining": max(0, rule.limit - used),
                            "resets_in_seconds": (
                                max(1, math.ceil(oldest + rule.window_seconds - moment))
                                if oldest is not None
                                else 0
                            ),
                        }
                    )
                quotas[kind.value] = {
                    "rules": entries,
                    "override": None
                    if override is None
                    else {
                        "limit_percent": override.limit_percent,
                        "expires_at": None
                        if override.expires_at is None
                        else _iso(override.expires_at),
                    },
                }
    except QuotaConfigError as exc:
        raise _unavailable(exc) from exc
    except sqlite3.Error as exc:
        raise _unavailable(exc) from exc
    return {"entitlement": entitlement.value, "quotas": quotas}


_LABELS: Mapping[QuotaKind, str] = {
    QuotaKind.LOGIN: "Sign-in attempts",
    QuotaKind.AI_REQUEST: "AI requests",
    QuotaKind.IMPORT: "Imports",
    QuotaKind.EXPORT: "Exports",
    QuotaKind.SYNC: "Syncs",
    QuotaKind.NOTIFICATION: "Notifications",
    QuotaKind.REPORT: "Reports",
}


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" + ("" if count == 1 else "s")


def _window_label(seconds: int) -> str:
    for size, unit in ((_DAY, "day"), (_HOUR, "hour"), (_MINUTE, "minute")):
        if seconds % size == 0:
            count = seconds // size
            return unit if count == 1 else _plural(count, unit)
    return _plural(seconds, "second")


def _duration(seconds: int) -> str:
    if seconds < 90:
        return _plural(seconds, "second")
    if seconds < 90 * _MINUTE:
        return _plural(math.ceil(seconds / _MINUTE), "minute")
    if seconds < 36 * _HOUR:
        return _plural(math.ceil(seconds / _HOUR), "hour")
    return _plural(math.ceil(seconds / _DAY), "day")


def describe_denial(denial: QuotaDenial) -> str:
    """Return a user-facing, actionable explanation of a refusal."""

    label = _LABELS[denial.kind]
    retry = denial.retry_after_seconds
    if denial.code == CODE_DISABLED:
        base = f"{label} are turned off for this account."
        if retry is None:
            return f"{base} Contact the administrator."
        return f"{base} Try again in {_duration(retry)}."
    window = _window_label(denial.window_seconds)
    return (
        f"{label} are limited to {denial.limit} per {window}. "
        f"Try again in {_duration(retry or 1)}."
    )
