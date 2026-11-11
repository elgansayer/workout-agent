"""Behaviour of the per-account quota engine and its storage schema."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from database import get_or_create_user, init_db
from quotas import (
    CODE_DISABLED,
    CODE_EXCEEDED,
    DEFAULT_POLICY,
    LOGIN_ADDRESS_RULES,
    Entitlement,
    QuotaConfigError,
    QuotaExceededError,
    QuotaKind,
    QuotaRule,
    QuotaUnavailableError,
    consume,
    consume_login_attempt,
    describe_denial,
    rules_for,
    seconds_until_allowed,
    usage_snapshot,
    validate_policy_environment,
)

T0 = 1_800_000_000.0


def _overrides(entitlements: dict[str, Any] | None = None, **extra: Any) -> dict[str, str]:
    payload: dict[str, Any] = dict(extra)
    if entitlements is not None:
        payload["entitlements"] = entitlements
    return {"QUOTA_POLICY_OVERRIDES": json.dumps(payload)}


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "quota.db")
    init_db(path)
    return path


def _account(db: str, name: str) -> str:
    return get_or_create_user(f"{name}@example.com", name, db_path=db)["id"]


def _rows(db: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def _exhaust(db: str, user: str, kind: QuotaKind, count: int, *, at: float = T0) -> None:
    for offset in range(count):
        consume(user, kind, db, now=at + offset)


def _denied(db: str, user: str, kind: QuotaKind, *, at: float) -> Any:
    with pytest.raises(QuotaExceededError) as caught:
        consume(user, kind, db, now=at)
    return caught.value.denial


# --- policy and configuration -------------------------------------------------


def test_policy_covers_every_quota_for_every_entitlement() -> None:
    assert {kind.value for kind in QuotaKind} == {
        "login",
        "ai_request",
        "import",
        "export",
        "sync",
        "notification",
        "report",
    }
    for entitlement in Entitlement:
        for kind in QuotaKind:
            rules = DEFAULT_POLICY[entitlement][kind]
            assert rules, (entitlement, kind)
            assert all(rule.limit > 0 and rule.window_seconds > 0 for rule in rules)


def test_limits_grow_with_the_entitlement_rule_for_rule() -> None:
    for kind in QuotaKind:
        restricted = DEFAULT_POLICY[Entitlement.RESTRICTED][kind]
        standard = DEFAULT_POLICY[Entitlement.STANDARD][kind]
        extended = DEFAULT_POLICY[Entitlement.EXTENDED][kind]
        assert [r.window_seconds for r in restricted] == [r.window_seconds for r in standard]
        assert [r.window_seconds for r in standard] == [r.window_seconds for r in extended]
        for low, mid, high in zip(restricted, standard, extended, strict=True):
            assert low.limit < mid.limit < high.limit, kind


def test_policy_overrides_replace_only_the_named_rules() -> None:
    environ = _overrides({"standard": {"ai_request": [[30, 60], [500, 86400]]}})

    rules = rules_for(Entitlement.STANDARD, QuotaKind.AI_REQUEST, environ=environ)

    assert [(r.limit, r.window_seconds) for r in rules] == [(30, 60), (500, 86400)]
    assert rules_for(Entitlement.STANDARD, QuotaKind.REPORT, environ=environ) == (
        DEFAULT_POLICY[Entitlement.STANDARD][QuotaKind.REPORT]
    )
    assert rules_for(Entitlement.EXTENDED, QuotaKind.AI_REQUEST, environ=environ) == (
        DEFAULT_POLICY[Entitlement.EXTENDED][QuotaKind.AI_REQUEST]
    )


def test_blank_policy_override_means_defaults() -> None:
    validate_policy_environment({"QUOTA_POLICY_OVERRIDES": "  "})
    validate_policy_environment({})


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"surprise": 1}',
        '{"entitlements": []}',
        '{"entitlements": {"platinum": {"ai_request": [[1, 60]]}}}',
        '{"entitlements": {"standard": {"teleport": [[1, 60]]}}}',
        '{"entitlements": {"standard": []}}',
        '{"entitlements": {"standard": {"ai_request": []}}}',
        '{"entitlements": {"standard": {"ai_request": [[1]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[1, 60, 3]]}}}',
        '{"entitlements": {"standard": {"ai_request": [["1", 60]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[true, 60]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[0, 60]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[1, 0]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[1000001, 60]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[1, 604801]]}}}',
        '{"entitlements": {"standard": {"ai_request": [[1, 60], [2, 60]]}}}',
        (
            '{"entitlements": {"standard": {"ai_request": '
            "[[1, 60], [2, 120], [3, 180], [4, 240], [5, 300]]}}}"
        ),
        '{"login_by_address": []}',
        '{"login_by_address": [[0, 60]]}',
    ],
)
def test_invalid_policy_overrides_are_rejected(payload: str) -> None:
    with pytest.raises(QuotaConfigError):
        validate_policy_environment({"QUOTA_POLICY_OVERRIDES": payload})


def test_policy_overrides_are_read_from_the_process_environment(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _account(db, "env")
    monkeypatch.setenv(
        "QUOTA_POLICY_OVERRIDES",
        json.dumps({"entitlements": {"standard": {"report": [[2, 60]]}}}),
    )

    _exhaust(db, user, QuotaKind.REPORT, 2)

    assert _denied(db, user, QuotaKind.REPORT, at=T0 + 2).limit == 2


# --- sliding windows and retry information -----------------------------------


def test_requests_are_allowed_up_to_the_limit_and_remaining_counts_down(db: str) -> None:
    user = _account(db, "athlete")

    remaining = [
        consume(user, QuotaKind.AI_REQUEST, db, now=T0 + offset).remaining
        for offset in range(15)
    ]

    assert remaining == list(range(14, -1, -1))


def test_denial_carries_exact_actionable_retry_information(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    denial = _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 15)

    # The first request leaves the 60 second window at T0 + 60.
    assert denial.code == CODE_EXCEEDED
    assert (denial.limit, denial.window_seconds) == (15, 60)
    assert denial.retry_after_seconds == 45
    message = describe_denial(denial)
    assert "AI requests are limited to 15 per minute" in message
    assert "Try again in 45 seconds" in message


def test_retry_time_is_when_enough_old_events_have_left_the_window() -> None:
    events = [100.0, 110.0, 130.0]  # ascending, all inside a 60 second window at 140

    # Limit 3 and full: the oldest event (100) must leave, i.e. at 160.
    assert seconds_until_allowed(events, limit=3, window_seconds=60, now=140.0) == 20
    # Limit 1 with three events: none may remain, so the newest (130) must leave, at 190.
    assert seconds_until_allowed(events, limit=1, window_seconds=60, now=140.0) == 50
    # Never less than one second, even at the exact moment of expiry.
    assert seconds_until_allowed(events, limit=3, window_seconds=60, now=160.0) == 1
    # A racing change that leaves too few events yields the full window, not an error.
    assert seconds_until_allowed([], limit=2, window_seconds=60, now=140.0) == 60
    assert seconds_until_allowed([100.0], limit=2, window_seconds=60, now=140.0) == 60


def test_the_window_slides_so_allowance_returns_one_request_at_a_time(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    assert _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 59).retry_after_seconds == 1
    # The request at T0 has aged out; the one at T0 + 1 is next to leave.
    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0 + 60.5).remaining == 0
    assert _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 60.7).retry_after_seconds == 1
    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0 + 61.5).remaining == 0


def test_the_longest_denying_window_decides_the_retry_time(db: str) -> None:
    user = _account(db, "athlete")
    environ = _overrides({"standard": {"ai_request": [[2, 60], [3, 3600]]}})
    for offset in (0, 61, 122):  # never more than two within any 60 seconds
        consume(user, QuotaKind.AI_REQUEST, db, now=T0 + offset, environ=environ)

    with pytest.raises(QuotaExceededError) as caught:
        consume(user, QuotaKind.AI_REQUEST, db, now=T0 + 123, environ=environ)

    denial = caught.value.denial
    assert (denial.limit, denial.window_seconds) == (3, 3600)
    assert denial.retry_after_seconds == 3600 - 123


def test_quotas_are_scoped_to_the_account(db: str) -> None:
    first = _account(db, "first")
    second = _account(db, "second")
    _exhaust(db, first, QuotaKind.AI_REQUEST, 15)
    _denied(db, first, QuotaKind.AI_REQUEST, at=T0 + 15)

    assert consume(second, QuotaKind.AI_REQUEST, db, now=T0 + 15).remaining == 14
    assert [row[0] for row in _rows(db, "SELECT DISTINCT user_id FROM quota_denials")] == [first]


def test_each_quota_kind_is_counted_separately(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    assert consume(user, QuotaKind.REPORT, db, now=T0 + 15).remaining == 29
    assert consume(user, "notification", db, now=T0 + 15).remaining == 9


def test_every_quota_kind_is_enforced_with_its_own_limit(db: str) -> None:
    user = _account(db, "athlete")
    for kind in QuotaKind:
        first_rule = DEFAULT_POLICY[Entitlement.STANDARD][kind][0]
        _exhaust(db, user, kind, first_rule.limit)
        denial = _denied(db, user, kind, at=T0 + first_rule.limit)
        assert denial.kind is kind
        assert denial.limit == first_rule.limit
        assert denial.retry_after_seconds and denial.retry_after_seconds >= 1


def test_unknown_quota_kinds_and_missing_identity_are_programming_errors(db: str) -> None:
    user = _account(db, "athlete")

    with pytest.raises(ValueError):
        consume(user, "teleport", db, now=T0)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            consume(bad, QuotaKind.REPORT, db, now=T0)  # type: ignore[arg-type]


def test_refused_requests_are_not_recorded_as_usage_but_are_tallied_hourly(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    for second in range(15, 60):  # still inside the first request's window
        _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + second)

    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 15
    tallies = _rows(db, "SELECT kind, denials FROM quota_denials WHERE user_id = ?", user)
    assert tallies == [("ai_request", 45)]


def _hold_write_lock(db: str) -> sqlite3.Connection:
    blocker = sqlite3.connect(db, isolation_level=None, timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    return blocker


def test_refusing_an_exhausted_account_never_waits_for_the_write_lock(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    blocker = _hold_write_lock(db)
    try:
        started = time.monotonic()
        denial = _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 20)
        elapsed = time.monotonic() - started
        # The refusal is still correct and the skipped tally is best effort.
        assert denial.retry_after_seconds == 40
        assert _rows(db, "SELECT COUNT(*) FROM quota_denials")[0][0] == 0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert elapsed < 2.0  # the busy timeout is 10 seconds
    _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 21)
    assert _rows(db, "SELECT SUM(denials) FROM quota_denials")[0][0] == 1


def test_refusing_an_exhausted_address_never_waits_for_the_write_lock(db: str) -> None:
    environ = _overrides(login_by_address=[[1, 3600]])
    consume_login_attempt("198.51.100.4", db, now=T0, environ=environ)

    blocker = _hold_write_lock(db)
    try:
        started = time.monotonic()
        with pytest.raises(QuotaExceededError):
            consume_login_attempt("198.51.100.4", db, now=T0 + 1, environ=environ)
        elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert elapsed < 2.0


def test_refusal_tallies_are_sampled_so_a_flood_cannot_amplify_writes(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)

    for offset in (0.0, 0.2, 0.4, 0.99):  # all within one second of the first
        _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 15 + offset)
    assert _rows(db, "SELECT SUM(denials) FROM quota_denials")[0][0] == 1

    _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + 16)
    assert _rows(db, "SELECT SUM(denials) FROM quota_denials")[0][0] == 2


def test_a_flood_of_refusals_cannot_flood_the_logs(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 15)
    environ = _overrides(login_by_address=[[1, 3600]])
    consume_login_attempt("198.51.100.4", db, now=T0, environ=environ)

    with caplog.at_level("WARNING", logger="quotas"):
        for second in range(15, 55):
            _denied(db, user, QuotaKind.AI_REQUEST, at=T0 + second)
        for _ in range(40):
            with pytest.raises(QuotaExceededError):
                consume_login_attempt("198.51.100.4", db, now=T0 + 1, environ=environ)

    refusals = [r for r in caplog.records if r.getMessage().startswith("Quota denied")]
    assert 1 <= len(refusals) <= 2 + 2  # one per account and one per address, per interval
    rendered = " ".join(record.getMessage() for record in refusals)
    assert user not in rendered and "198.51.100.4" not in rendered


def test_old_usage_is_pruned_so_the_ledger_stays_bounded(db: str) -> None:
    user = _account(db, "athlete")
    _exhaust(db, user, QuotaKind.REPORT, 5)

    consume(user, QuotaKind.REPORT, db, now=T0 + 3 * 86400)

    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 1


def test_concurrent_requests_can_never_exceed_the_limit(db: str) -> None:
    user = _account(db, "athlete")
    environ = _overrides({"standard": {"ai_request": [[5, 3600]]}})

    def attempt(_: int) -> bool:
        try:
            consume(user, QuotaKind.AI_REQUEST, db, now=T0, environ=environ)
        except QuotaExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(48)))

    assert outcomes.count(True) == 5
    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 5


# --- entitlement and administrator overrides ---------------------------------


def _set_entitlement(db: str, user: str, value: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO account_quota_profiles (user_id, entitlement, updated_at) "
            "VALUES (?, ?, ?)",
            (user, value, "2026-08-09T00:00:00+00:00"),
        )


def test_the_entitlement_selects_the_limits(db: str) -> None:
    restricted = _account(db, "restricted")
    extended = _account(db, "extended")
    _set_entitlement(db, restricted, "restricted")
    _set_entitlement(db, extended, "extended")

    _exhaust(db, restricted, QuotaKind.AI_REQUEST, 3)
    assert _denied(db, restricted, QuotaKind.AI_REQUEST, at=T0 + 3).limit == 3

    _exhaust(db, extended, QuotaKind.AI_REQUEST, 30)
    assert _denied(db, extended, QuotaKind.AI_REQUEST, at=T0 + 30).limit == 30


def test_an_unrecognised_stored_entitlement_fails_closed_to_restricted(db: str) -> None:
    user = _account(db, "athlete")
    _set_entitlement(db, user, "platinum")

    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0).limit == 3


def _set_override(
    db: str, user: str, kind: str, percent: int, expires_at: float | None
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO quota_overrides "
            "(user_id, kind, limit_percent, expires_at, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user, kind, percent, expires_at, user, "2026-08-09T00:00:00+00:00"),
        )


def test_an_override_scales_the_limit_until_it_expires(db: str) -> None:
    user = _account(db, "athlete")
    _set_override(db, user, "ai_request", 200, T0 + 100)

    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0).limit == 30
    # Expired: the standard limit applies again.
    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0 + 101).limit == 15


def test_an_override_only_affects_its_own_quota_and_account(db: str) -> None:
    user = _account(db, "athlete")
    other = _account(db, "other")
    _set_override(db, user, "ai_request", 10, None)

    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0).limit == 1
    assert consume(user, QuotaKind.REPORT, db, now=T0).limit == 30
    assert consume(other, QuotaKind.AI_REQUEST, db, now=T0).limit == 15


def test_a_tiny_override_never_rounds_the_limit_down_to_zero(db: str) -> None:
    user = _account(db, "athlete")
    _set_override(db, user, "ai_request", 1, None)

    assert consume(user, QuotaKind.AI_REQUEST, db, now=T0).limit == 1


def test_a_zero_override_switches_the_quota_off_with_retry_when_time_boxed(db: str) -> None:
    user = _account(db, "athlete")
    _set_override(db, user, "export", 0, T0 + 600)

    denial = _denied(db, user, QuotaKind.EXPORT, at=T0 + 100)

    assert denial.code == CODE_DISABLED
    assert denial.retry_after_seconds == 500
    assert "turned off" in describe_denial(denial)
    assert "Try again in 9 minutes" in describe_denial(denial)
    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 0


def test_an_indefinite_zero_override_has_no_retry_time(db: str) -> None:
    user = _account(db, "athlete")
    _set_override(db, user, "export", 0, None)

    denial = _denied(db, user, QuotaKind.EXPORT, at=T0)

    assert denial.code == CODE_DISABLED
    assert denial.retry_after_seconds is None
    assert "Contact the administrator" in describe_denial(denial)


# --- failure handling ---------------------------------------------------------


def test_storage_failures_fail_closed(tmp_path: Path) -> None:
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()

    with pytest.raises(QuotaUnavailableError):
        consume("user-1", QuotaKind.REPORT, empty, now=T0)
    with pytest.raises(QuotaUnavailableError):
        consume_login_attempt("203.0.113.9", empty, now=T0)
    with pytest.raises(QuotaUnavailableError):
        usage_snapshot("user-1", empty, now=T0)


def test_unknown_accounts_cannot_accrue_quota_state(db: str) -> None:
    with pytest.raises(QuotaUnavailableError):
        consume("ghost-account", QuotaKind.REPORT, db, now=T0)

    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 0


def test_an_unusable_runtime_policy_fails_closed_without_recording_usage(db: str) -> None:
    user = _account(db, "athlete")
    broken = {"QUOTA_POLICY_OVERRIDES": "{broken"}

    with pytest.raises(QuotaUnavailableError):
        consume(user, QuotaKind.REPORT, db, now=T0, environ=broken)
    with pytest.raises(QuotaUnavailableError):
        consume_login_attempt("203.0.113.9", db, now=T0, environ=broken)

    assert _rows(db, "SELECT COUNT(*) FROM quota_usage")[0][0] == 0
    assert _rows(db, "SELECT COUNT(*) FROM login_attempts")[0][0] == 0


# --- sign-in attempts before an account exists --------------------------------


def test_sign_in_attempts_are_throttled_per_address_with_retry_information(db: str) -> None:
    for second in range(60):
        consume_login_attempt("203.0.113.9", db, now=T0 + second)

    with pytest.raises(QuotaExceededError) as caught:
        consume_login_attempt("203.0.113.9", db, now=T0 + 61)

    denial = caught.value.denial
    assert denial.kind is QuotaKind.LOGIN
    assert (denial.limit, denial.window_seconds) == (60, 300)
    assert denial.retry_after_seconds == 300 - 61
    assert "Sign-in attempts are limited to 60 per 5 minutes" in describe_denial(denial)
    consume_login_attempt("203.0.113.10", db, now=T0 + 61)  # another address is unaffected


def test_refused_sign_in_attempts_do_not_extend_their_own_lockout(db: str) -> None:
    environ = _overrides(login_by_address=[[2, 60]])
    consume_login_attempt("198.51.100.4", db, now=T0, environ=environ)
    consume_login_attempt("198.51.100.4", db, now=T0 + 1, environ=environ)

    for second in range(2, 30):
        with pytest.raises(QuotaExceededError):
            consume_login_attempt("198.51.100.4", db, now=T0 + second, environ=environ)

    assert _rows(db, "SELECT COUNT(*) FROM login_attempts")[0][0] == 2
    consume_login_attempt("198.51.100.4", db, now=T0 + 60.5, environ=environ)


def test_sign_in_attempts_store_only_a_keyed_hash_of_the_address(db: str) -> None:
    consume_login_attempt("203.0.113.9", db, now=T0, environ={"WEB_AUTH_SECRET": "a" * 32})
    consume_login_attempt("203.0.113.9", db, now=T0, environ={"WEB_AUTH_SECRET": "b" * 32})

    hashes = [row[0] for row in _rows(db, "SELECT subject_hash FROM login_attempts")]

    assert len(set(hashes)) == 2
    assert all("203.0.113.9" not in value and len(value) == 32 for value in hashes)
    columns = [row[1] for row in _rows(db, "PRAGMA table_info(login_attempts)")]
    assert "user_id" not in columns


def test_addresses_are_normalised_so_rotation_buys_no_new_allowance(db: str) -> None:
    environ = _overrides(login_by_address=[[1, 3600]])
    consume_login_attempt("2001:db8:1:2::1", db, now=T0, environ=environ)
    for same_network in ("2001:db8:1:2:ffff::9", "2001:0db8:0001:0002::beef"):
        with pytest.raises(QuotaExceededError):
            consume_login_attempt(same_network, db, now=T0 + 1, environ=environ)
    consume_login_attempt("2001:db8:1:3::1", db, now=T0 + 1, environ=environ)

    consume_login_attempt("192.0.2.7", db, now=T0, environ=environ)
    with pytest.raises(QuotaExceededError):
        consume_login_attempt("::ffff:192.0.2.7", db, now=T0 + 1, environ=environ)

    consume_login_attempt("not-an-address", db, now=T0, environ=environ)
    with pytest.raises(QuotaExceededError):
        consume_login_attempt("also-not-an-address", db, now=T0 + 1, environ=environ)


def test_old_sign_in_attempts_are_pruned(db: str) -> None:
    for second in range(10):
        consume_login_attempt(f"203.0.113.{second}", db, now=T0 + second)

    consume_login_attempt("198.51.100.1", db, now=T0 + 7200)

    assert _rows(db, "SELECT COUNT(*) FROM login_attempts")[0][0] == 1


# --- usage snapshot -----------------------------------------------------------


def test_usage_snapshot_reports_every_quota_for_one_account(db: str) -> None:
    user = _account(db, "athlete")
    other = _account(db, "other")
    _exhaust(db, user, QuotaKind.AI_REQUEST, 4)
    _exhaust(db, other, QuotaKind.AI_REQUEST, 9)
    _set_override(db, user, "report", 50, T0 + 1000)

    snapshot = usage_snapshot(user, db, now=T0 + 10)

    assert snapshot["entitlement"] == "standard"
    assert set(snapshot["quotas"]) == {kind.value for kind in QuotaKind}
    burst, daily = snapshot["quotas"]["ai_request"]["rules"]
    assert burst == {
        "limit": 15,
        "window_seconds": 60,
        "used": 4,
        "remaining": 11,
        "resets_in_seconds": 50,
    }
    assert daily["used"] == 4 and daily["remaining"] == 296
    assert snapshot["quotas"]["ai_request"]["override"] is None
    report = snapshot["quotas"]["report"]
    assert report["rules"][0]["limit"] == 15
    assert report["override"]["limit_percent"] == 50
    assert report["override"]["expires_at"].endswith("+00:00")
    assert snapshot["quotas"]["login"]["rules"][0]["resets_in_seconds"] == 0
    json.dumps(snapshot)  # serialisable as-is for the API


# --- schema -------------------------------------------------------------------

_OWNED_TABLES = (
    "account_quota_profiles",
    "quota_overrides",
    "quota_usage",
    "quota_denials",
    "quota_audit_log",
)


def _user_id_is_indexed_first(conn: sqlite3.Connection, table: str) -> bool:
    for (index_name,) in conn.execute("SELECT name FROM pragma_index_list(?)", (table,)):
        columns = conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,)
        ).fetchall()
        if columns and columns[0][0] == "user_id":
            return True
    return False


def test_quota_tables_are_tenant_scoped_indexed_and_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "schema.db")
    init_db(path)
    init_db(path)

    with sqlite3.connect(path) as conn:
        for table in _OWNED_TABLES:
            foreign_keys = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert any(
                row[2] == "users" and row[3] == "user_id" and row[4] == "id"
                for row in foreign_keys
            ), table
            assert _user_id_is_indexed_first(conn, table), table
        login_columns = {row[1] for row in conn.execute("PRAGMA table_info(login_attempts)")}
        assert login_columns == {"subject_hash", "attempted_at"}


def test_the_owner_of_a_quota_record_is_immutable(db: str) -> None:
    first = _account(db, "first")
    second = _account(db, "second")
    consume(first, QuotaKind.AI_REQUEST, db, now=T0)
    _exhaust(db, first, QuotaKind.AI_REQUEST, 14, at=T0 + 1)
    _denied(db, first, QuotaKind.AI_REQUEST, at=T0 + 20)
    _set_entitlement(db, first, "extended")
    _set_override(db, first, "report", 50, None)

    with sqlite3.connect(db) as conn:
        for table in ("account_quota_profiles", "quota_overrides", "quota_usage", "quota_denials"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(f"UPDATE {table} SET user_id = ?", (second,))


def test_the_audit_log_rejects_updates(db: str) -> None:
    actor = _account(db, "admin")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO quota_audit_log "
            "(user_id, actor_user_id, action, detail_json, reason, created_at) "
            "VALUES (?, ?, 'entitlement_set', '{}', 'test', '2026-08-09T00:00:00+00:00')",
            (actor, actor),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE quota_audit_log SET reason = 'rewritten'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE quota_audit_log SET user_id = ?", (actor,))


# --- documentation stays in step with the code --------------------------------

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "ACCOUNT_QUOTAS.md"
_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _compact(rule: QuotaRule) -> str:
    unit = {60: "min", 3600: "hour", 86400: "day"}.get(rule.window_seconds)
    return f"{rule.limit}/{unit}" if unit else f"{rule.limit}/{rule.window_seconds // 60} min"


def test_documented_default_limits_match_the_policy() -> None:
    table: dict[str, list[str]] = {}
    for line in _DOCS.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].startswith("`"):
            table[cells[0].strip("`")] = cells[1:]

    assert set(table) == {kind.value for kind in QuotaKind}
    for kind in QuotaKind:
        for cell, entitlement in zip(
            table[kind.value],
            (Entitlement.RESTRICTED, Entitlement.STANDARD, Entitlement.EXTENDED),
            strict=True,
        ):
            expected = ", ".join(_compact(rule) for rule in DEFAULT_POLICY[entitlement][kind])
            assert cell == expected, (kind.value, entitlement.value)


def test_documented_sign_in_address_allowance_matches_the_policy() -> None:
    docs = _DOCS.read_text(encoding="utf-8")
    labels = {300: "5 minutes", 3600: "hour"}

    for rule in LOGIN_ADDRESS_RULES:
        assert f"{rule.limit} per {labels[rule.window_seconds]}" in docs


def test_every_quota_setting_is_documented() -> None:
    docs = _DOCS.read_text(encoding="utf-8")
    example = _ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in ("QUOTA_ADMIN_EMAILS", "QUOTA_POLICY_OVERRIDES"):
        assert f"\n{name}=" in example, name
        assert name in docs, name


def test_quota_settings_appear_in_the_repository_inventory_as_documented() -> None:
    """The generated security/onboarding inventory must see the new settings."""
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "repository_inventory_under_test",
        root / "backend" / "scripts" / "repository_inventory.py",
    )
    assert spec is not None and spec.loader is not None
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)

    variables = {
        item["name"]: item
        for item in inventory.discover_environment(root, inventory._read_test_corpus(root))
    }

    for name in ("QUOTA_ADMIN_EMAILS", "QUOTA_POLICY_OVERRIDES"):
        assert variables[name]["documented_in_env_example"] is True, name
        assert variables[name]["sensitivity"] == "configuration", name
