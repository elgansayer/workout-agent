"""Server-side account access policy: allowlist, invitation, status, revocation.

These tests exercise the policy and its persistence directly (no HTTP). The
route-level behaviour, including uniform denials and request-time session
revocation, is covered in ``test_auth_policy_routes.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from account_policy import (
    SESSION_EPOCH_KEY,
    AccessPolicy,
    AccessPolicyConfig,
    AccessPolicyConfigurationError,
    DenialReason,
    canonical_email,
    log_access_denied,
    parse_allowed_emails,
)
from data_classification import pseudonymise_identifier
from database import (
    ACCOUNT_STATUS_ACTIVE,
    ACCOUNT_STATUS_DELETED,
    ACCOUNT_STATUS_SUSPENDED,
    LEGACY_TENANT_EMAIL,
    AccountTransitionError,
    create_account_invitation,
    find_account_state,
    get_account_state,
    get_legacy_user_id,
    init_db,
    list_account_invitations,
    revoke_account_invitations,
    revoke_account_sessions,
    set_account_status,
)
from hypothesis import given, settings
from hypothesis import strategies as st

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "policy.db")
    init_db(path)
    return path


def _userinfo(email: str, *, verified: Any = True, name: str | None = "Test Person") -> dict[str, Any]:
    info: dict[str, Any] = {"email": email, "email_verified": verified}
    if name is not None:
        info["name"] = name
    return info


def _policy(
    db_path: str,
    *,
    allowed: tuple[str, ...] | None = None,
    invite_only: bool = False,
) -> AccessPolicy:
    config = AccessPolicyConfig(
        allowed_emails=frozenset(allowed) if allowed is not None else None,
        invite_only=invite_only,
    )
    return AccessPolicy(config, db_path)


def _user_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def _invite(db_path: str, email: str, *, days: int = 7) -> str:
    return create_account_invitation(
        email, expires_at=NOW + timedelta(days=days), db_path=db_path, now=NOW
    )


# --------------------------------------------------------------------------
# Email canonicalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice@example.com", "alice@example.com"),
        ("  Alice@Example.COM  ", "alice@example.com"),
        ("a.b+tag@sub.example.co.uk", "a.b+tag@sub.example.co.uk"),
        ("o'brien@example.com", "o'brien@example.com"),
        ("first_last-1@example-site.com", "first_last-1@example-site.com"),
    ],
)
def test_canonical_email_accepts_and_normalises(raw: str, expected: str) -> None:
    assert canonical_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        b"alice@example.com",
        "",
        "   ",
        "alice",
        "alice@",
        "@example.com",
        "alice@@example.com",
        "alice@example.com@example.org",
        "legacy@local",  # single-label domain: keeps the legacy tenant unreachable
        "alice@localhost",
        "alice@-example.com",
        "alice@example-.com",
        "alice@example..com",
        "alice@.example.com",
        ".alice@example.com",
        "alice.@example.com",
        "al..ice@example.com",
        "ali ce@example.com",
        "alice@exa mple.com",
        "alice@example.com\n",  # trailing newline is stripped...
        "ali\nce@example.com",  # ...but an embedded one is not allowed
        "alice\x00@example.com",
        "alice@example.com\r\nBcc: victim@example.org",
        "ａlice@example.com",  # fullwidth 'a': no Unicode compatibility folding
        "alicé@example.com",
        "alice@exämple.com",
        "İ@example.com",
        "a" * 65 + "@example.com",
        "a@" + "b" * 250 + ".com",
    ],
)
def test_canonical_email_rejects_unacceptable_input(raw: object) -> None:
    result = canonical_email(raw)
    if raw == "alice@example.com\n":
        assert result == "alice@example.com"
    else:
        assert result is None


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=80))
def test_canonical_email_is_idempotent_and_safe_for_any_input(raw: str) -> None:
    result = canonical_email(raw)
    if result is None:
        return
    assert canonical_email(result) == result
    assert result == result.lower()
    assert result.isascii()
    assert result.count("@") == 1
    assert not any(ch.isspace() or ord(ch) < 32 for ch in result)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_blank_allowlist_means_no_allowlist() -> None:
    assert parse_allowed_emails(None) is None
    assert parse_allowed_emails("") is None
    assert parse_allowed_emails("   ") is None


def test_allowlist_is_normalised_deduplicated_and_tolerates_stray_commas() -> None:
    parsed = parse_allowed_emails(" Alice@Example.com, bob@example.com ,,alice@example.com,")
    assert parsed == frozenset({"alice@example.com", "bob@example.com"})


@pytest.mark.parametrize(
    "raw",
    [
        ",",
        ",,  ,",
        "not-an-email",
        "alice@example.com bob@example.com",  # forgot the comma
        "alice@example.com; bob@example.com",
        "alice@example.com,bob",
        "*@example.com",
    ],
)
def test_malformed_allowlist_refuses_to_start_instead_of_opening_access(raw: str) -> None:
    """A typo must never turn a restricted deployment into an open one."""
    with pytest.raises(AccessPolicyConfigurationError) as excinfo:
        parse_allowed_emails(raw)
    # The message points at the problem without echoing configured values.
    assert "example.com" not in str(excinfo.value)
    assert "bob" not in str(excinfo.value)


def test_invalid_entry_error_reports_position_only() -> None:
    with pytest.raises(AccessPolicyConfigurationError, match=r"position\(s\) 2, 4"):
        parse_allowed_emails("a@example.com,bad,c@example.com,worse")


@pytest.mark.parametrize(("raw", "expected"), [("", False), ("0", False), ("off", False), ("1", True), ("TRUE", True), (" yes ", True)])
def test_invite_only_flag_parsing(raw: str, expected: bool) -> None:
    assert AccessPolicyConfig.from_environ({"INVITE_ONLY": raw}).invite_only is expected


def test_invite_only_flag_rejects_unrecognised_values() -> None:
    with pytest.raises(AccessPolicyConfigurationError, match="INVITE_ONLY"):
        AccessPolicyConfig.from_environ({"INVITE_ONLY": "maybe"})


def test_default_configuration_is_unrestricted_and_open() -> None:
    config = AccessPolicyConfig.from_environ({})
    assert config.allowed_emails is None
    assert config.invite_only is False
    assert config.allows("anyone@example.com")


def test_describe_never_lists_addresses() -> None:
    config = AccessPolicyConfig.from_environ(
        {"ALLOWED_EMAILS": "alice@example.com,bob@example.com", "INVITE_ONLY": "1"}
    )
    summary = config.describe()
    assert "allowlist=on" in summary and "allowlist_entries=2" in summary
    assert "invite_only=on" in summary
    assert "alice" not in summary and "example.com" not in summary


# --------------------------------------------------------------------------
# Login completion
# --------------------------------------------------------------------------


def test_open_deployment_creates_account_on_first_verified_login(db: str) -> None:
    policy = _policy(db)
    first = policy.admit_login(_userinfo("Alice@Example.com"), now=NOW)
    assert first.allowed and first.created and first.reason is None
    assert first.email == "alice@example.com"
    assert first.session_epoch == 0

    second = policy.admit_login(_userinfo("alice@example.com"), now=NOW)
    assert second.allowed and not second.created
    assert second.user_id == first.user_id
    assert _user_count(db) == 2  # alice + the legacy tenant created by init_db


@pytest.mark.parametrize("verified", [None, False, "false", "", 0, 1, "1", "yes", [], {}, "TRUEISH"])
def test_unverified_email_claim_is_refused_and_creates_nothing(db: str, verified: Any) -> None:
    info = {"email": "alice@example.com", "email_verified": verified}
    decision = _policy(db).admit_login(info, now=NOW)
    assert not decision.allowed and decision.reason is DenialReason.IDENTITY_UNVERIFIED
    assert find_account_state("alice@example.com", db) is None


def test_missing_email_verified_claim_is_refused(db: str) -> None:
    decision = _policy(db).admit_login({"email": "alice@example.com"}, now=NOW)
    assert decision.reason is DenialReason.IDENTITY_UNVERIFIED


@pytest.mark.parametrize("verified", [True, "true", "TRUE", " True "])
def test_only_an_affirmative_email_verified_claim_is_accepted(db: str, verified: Any) -> None:
    info = {"email": "alice@example.com", "email_verified": verified}
    assert _policy(db).admit_login(info, now=NOW).allowed


@pytest.mark.parametrize("userinfo", [None, "alice@example.com", [], 7, {}])
def test_missing_or_malformed_identity_is_refused(db: str, userinfo: Any) -> None:
    decision = _policy(db).admit_login(userinfo, now=NOW)
    assert not decision.allowed and decision.reason is DenialReason.IDENTITY_UNVERIFIED


def test_legacy_tenant_cannot_be_logged_into(db: str) -> None:
    decision = _policy(db).admit_login(_userinfo("legacy@local"), now=NOW)
    assert not decision.allowed


def test_allowlist_is_enforced_server_side_case_insensitively(db: str) -> None:
    policy = _policy(db, allowed=("alice@example.com",))
    assert policy.admit_login(_userinfo("ALICE@example.com"), now=NOW).allowed

    outsider = policy.admit_login(_userinfo("mallory@example.com"), now=NOW)
    assert outsider.reason is DenialReason.NOT_ALLOWLISTED
    assert find_account_state("mallory@example.com", db) is None


def test_allowlist_also_governs_accounts_that_already_exist(db: str) -> None:
    _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    restricted = _policy(db, allowed=("bob@example.com",))
    decision = restricted.admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.reason is DenialReason.NOT_ALLOWLISTED


def test_invite_only_refuses_an_uninvited_newcomer_and_creates_nothing(db: str) -> None:
    decision = _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.reason is DenialReason.NOT_INVITED
    assert find_account_state("alice@example.com", db) is None


def test_invitation_is_consumed_exactly_once_by_the_login_it_admits(db: str) -> None:
    _invite(db, "alice@example.com")
    policy = _policy(db, invite_only=True)

    first = policy.admit_login(_userinfo("alice@example.com"), now=NOW)
    assert first.allowed and first.created
    assert [i["status"] for i in list_account_invitations("alice@example.com", db)] == ["accepted"]

    # A retried/replayed callback is idempotent: same account, no second one.
    retry = policy.admit_login(_userinfo("alice@example.com"), now=NOW)
    assert retry.allowed and not retry.created and retry.user_id == first.user_id
    assert _user_count(db) == 2


def test_invitation_is_bound_to_its_address(db: str) -> None:
    _invite(db, "alice@example.com")
    policy = _policy(db, invite_only=True)
    assert policy.admit_login(_userinfo("bob@example.com"), now=NOW).reason is DenialReason.NOT_INVITED
    # ... and bob's attempt did not spend alice's invitation.
    assert policy.admit_login(_userinfo("alice@example.com"), now=NOW).allowed


def test_expired_invitation_is_refused(db: str) -> None:
    _invite(db, "alice@example.com", days=1)
    later = NOW + timedelta(days=1, seconds=1)
    decision = _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=later)
    assert decision.reason is DenialReason.NOT_INVITED
    assert find_account_state("alice@example.com", db) is None


def test_revoked_invitation_is_refused(db: str) -> None:
    _invite(db, "alice@example.com")
    assert revoke_account_invitations("alice@example.com", db_path=db, now=NOW) == 1
    decision = _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.reason is DenialReason.NOT_INVITED


def test_existing_accounts_do_not_need_an_invitation_when_invite_only_is_enabled(db: str) -> None:
    _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)  # onboarded earlier
    assert _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=NOW).allowed


def test_reinviting_supersedes_the_earlier_pending_invitation(db: str) -> None:
    first = _invite(db, "alice@example.com", days=1)
    second = _invite(db, "alice@example.com", days=30)
    assert first != second
    statuses = sorted(i["status"] for i in list_account_invitations("alice@example.com", db))
    assert statuses == ["pending", "revoked"]
    # The surviving invitation is the long one.
    later = NOW + timedelta(days=2)
    assert _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=later).allowed


def test_only_one_pending_invitation_per_address_is_possible(db: str) -> None:
    _invite(db, "alice@example.com")
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO account_invitations (id, email, status, created_at, expires_at) "
            "VALUES ('dup', 'alice@example.com', 'pending', 'x', 'y')"
        )


def test_suspended_account_cannot_log_in_even_if_allowlisted_and_invited(db: str) -> None:
    created = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert created.user_id
    set_account_status(created.user_id, ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    _invite(db, "alice@example.com")

    decision = _policy(db, allowed=("alice@example.com",), invite_only=True).admit_login(
        _userinfo("alice@example.com"), now=NOW
    )
    assert decision.reason is DenialReason.ACCOUNT_SUSPENDED
    # The refused attempt did not consume the pending invitation.
    assert [i["status"] for i in list_account_invitations("alice@example.com", db)] == ["pending"]


def test_deleted_account_is_a_permanent_tombstone_for_login(db: str) -> None:
    created = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert created.user_id
    set_account_status(created.user_id, ACCOUNT_STATUS_DELETED, db_path=db, now=NOW)
    _invite(db, "alice@example.com")

    decision = _policy(db, invite_only=True).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.reason is DenialReason.ACCOUNT_DELETED
    assert _user_count(db) == 2  # no fresh account was created for the address


def test_unknown_stored_status_fails_closed(db: str) -> None:
    created = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    with sqlite3.connect(db) as conn:
        # Simulate a row written by an older release that predated the CHECK.
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE users SET status = 'banana' WHERE id = ?", (created.user_id,))
    decision = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.reason is DenialReason.ACCOUNT_STATE_INVALID


def test_differently_cased_legacy_row_resolves_to_the_same_account(db: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES ('legacy-row', 'Alice@Example.com', 'x')"
        )
    decision = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert decision.allowed and not decision.created and decision.user_id == "legacy-row"
    assert _user_count(db) == 2


def test_display_name_is_sanitised(db: str) -> None:
    policy = _policy(db)
    decision = policy.admit_login(_userinfo("alice@example.com", name="Al\x00ice\n" + "x" * 500), now=NOW)
    assert decision.allowed
    with sqlite3.connect(db) as conn:
        name = conn.execute(
            "SELECT display_name FROM users WHERE id = ?", (decision.user_id,)
        ).fetchone()[0]
    assert "\x00" not in name and "\n" not in name and len(name) <= 200


# --------------------------------------------------------------------------
# Session re-validation and revocation
# --------------------------------------------------------------------------


def _login_session(policy: AccessPolicy, email: str = "alice@example.com") -> dict[str, Any]:
    decision = policy.admit_login(_userinfo(email), now=NOW)
    assert decision.allowed
    return {"user": decision.email, "user_id": decision.user_id, SESSION_EPOCH_KEY: decision.session_epoch}


def test_fresh_session_is_valid(db: str) -> None:
    policy = _policy(db)
    assert policy.check_session(_login_session(policy)).allowed


def test_session_issued_before_this_feature_stays_valid_until_first_revocation(db: str) -> None:
    policy = _policy(db)
    session = _login_session(policy)
    del session[SESSION_EPOCH_KEY]  # cookie minted by the previous release
    assert policy.check_session(session).allowed
    revoke_account_sessions(session["user_id"], db)
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED


def test_revoking_sessions_ends_old_cookies_but_not_new_logins(db: str) -> None:
    policy = _policy(db)
    old = _login_session(policy)
    other_device = _login_session(policy)
    state = revoke_account_sessions(old["user_id"], db)
    assert state is not None and state["session_epoch"] == 1

    assert policy.check_session(old).reason is DenialReason.SESSION_REVOKED
    assert policy.check_session(other_device).reason is DenialReason.SESSION_REVOKED
    assert policy.check_session(_login_session(policy)).allowed


def test_suspension_ends_sessions_immediately_and_reactivation_does_not_resurrect_them(db: str) -> None:
    policy = _policy(db)
    session = _login_session(policy)
    set_account_status(session["user_id"], ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    assert policy.check_session(session).reason is DenialReason.ACCOUNT_SUSPENDED

    set_account_status(session["user_id"], ACCOUNT_STATUS_ACTIVE, db_path=db, now=NOW)
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED
    assert policy.check_session(_login_session(policy)).allowed


def test_deletion_ends_sessions_and_is_terminal(db: str) -> None:
    policy = _policy(db)
    session = _login_session(policy)
    set_account_status(session["user_id"], ACCOUNT_STATUS_DELETED, db_path=db, now=NOW)
    assert policy.check_session(session).reason is DenialReason.ACCOUNT_DELETED

    for status in (ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_SUSPENDED):
        with pytest.raises(AccountTransitionError):
            set_account_status(session["user_id"], status, db_path=db, now=NOW)
    assert policy.check_session(session).reason is DenialReason.ACCOUNT_DELETED
    # Re-asserting the terminal state is allowed and still revokes.
    assert set_account_status(session["user_id"], ACCOUNT_STATUS_DELETED, db_path=db, now=NOW)


def test_reasserting_a_status_still_advances_the_epoch(db: str) -> None:
    """A status written outside the app must still be revocable via the CLI."""
    policy = _policy(db)
    session = _login_session(policy)
    with sqlite3.connect(db) as conn:  # operator edits the status by hand
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (session["user_id"],))
    set_account_status(session["user_id"], ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    set_account_status(session["user_id"], ACCOUNT_STATUS_ACTIVE, db_path=db, now=NOW)
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED


def test_allowlist_removal_revokes_active_sessions(db: str) -> None:
    open_policy = _policy(db)
    session = _login_session(open_policy)
    restricted = _policy(db, allowed=("bob@example.com",))
    assert restricted.check_session(session).reason is DenialReason.NOT_ALLOWLISTED


def test_allowlist_removal_is_durable_across_a_later_re_add(db: str) -> None:
    """Re-adding an address must not resurrect a cookie issued before removal."""
    initial = _policy(db, allowed=("alice@example.com", "bob@example.com"))
    session = _login_session(initial)
    bob = _login_session(initial, "bob@example.com")

    removed = _policy(db, allowed=("bob@example.com",))
    assert removed.reconcile() == 1  # only alice is outside the allowlist
    assert removed.check_session(bob).allowed  # bob's sessions are untouched

    re_added = _policy(db, allowed=("alice@example.com", "bob@example.com"))
    assert re_added.reconcile() == 0
    assert re_added.check_session(session).reason is DenialReason.SESSION_REVOKED
    assert re_added.check_session(_login_session(re_added)).allowed


def test_reconcile_skips_the_pre_tenant_legacy_owner(db: str) -> None:
    """The legacy tenant is seeded by init_db, is not a person, and is never counted."""
    assert LEGACY_TENANT_EMAIL == "legacy@local"
    legacy_id = get_legacy_user_id(db)
    assert get_account_state(legacy_id, db)["email"] == LEGACY_TENANT_EMAIL  # type: ignore[index]

    policy = _policy(db, allowed=("alice@example.com",))
    assert policy.reconcile() == 0
    state = get_account_state(legacy_id, db)
    assert state is not None and state["session_epoch"] == 0


def test_reconcile_is_a_no_op_without_an_allowlist(db: str) -> None:
    policy = _policy(db)
    session = _login_session(policy)
    assert policy.reconcile() == 0
    assert policy.check_session(session).allowed


def test_session_for_an_unknown_or_malformed_account_is_refused(db: str) -> None:
    policy = _policy(db)
    assert policy.check_session({"user": "x", "user_id": "no-such-id"}).reason is DenialReason.ACCOUNT_UNKNOWN
    for session in ({}, {"user_id": ""}, {"user_id": None}, {"user_id": 7}, {"user_id": ["a"]}):
        assert policy.check_session(session).reason is DenialReason.ACCOUNT_UNKNOWN


@pytest.mark.parametrize("epoch", [True, False, "0", 0.0, None, [0], {"v": 0}, 99, -1])
def test_epoch_must_be_exactly_the_stored_integer(db: str, epoch: Any) -> None:
    """Includes a future epoch: a restored older backup must invalidate sessions."""
    policy = _policy(db)
    session = _login_session(policy)
    session[SESSION_EPOCH_KEY] = epoch
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED


def test_session_check_surfaces_storage_errors_instead_of_allowing(tmp_path: Path) -> None:
    broken = AccessPolicy(AccessPolicyConfig(), str(tmp_path / "never-initialised.db"))
    with pytest.raises(sqlite3.Error):
        broken.check_session({"user": "x", "user_id": "some-id"})


# --------------------------------------------------------------------------
# Persistence primitives
# --------------------------------------------------------------------------


def test_set_account_status_validates_input(db: str) -> None:
    created = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert created.user_id
    with pytest.raises(ValueError, match="Unsupported account status"):
        set_account_status(created.user_id, "banned", db_path=db)
    assert set_account_status("no-such-user", ACCOUNT_STATUS_SUSPENDED, db_path=db) is None
    assert revoke_account_sessions("no-such-user", db) is None


def test_set_account_status_records_the_change_time_and_bumps_the_epoch(db: str) -> None:
    created = _policy(db).admit_login(_userinfo("alice@example.com"), now=NOW)
    assert created.user_id
    state = set_account_status(created.user_id, ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    assert state is not None
    assert (state["status"], state["session_epoch"]) == (ACCOUNT_STATUS_SUSPENDED, 1)
    with sqlite3.connect(db) as conn:
        changed = conn.execute(
            "SELECT status_changed_at FROM users WHERE id = ?", (created.user_id,)
        ).fetchone()[0]
    assert changed == "2026-11-05T12:00:00+00:00"
    assert get_account_state(created.user_id, db) == state


def test_invitation_validation(db: str) -> None:
    with pytest.raises(ValueError, match="expire in the future"):
        create_account_invitation(
            "alice@example.com", expires_at=NOW - timedelta(seconds=1), db_path=db, now=NOW
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        create_account_invitation(
            # A naive datetime is exactly what must be rejected here.
            "alice@example.com",
            expires_at=datetime(2030, 1, 1),  # noqa: DTZ001
            db_path=db,
            now=NOW,
        )
    assert list_account_invitations("alice@example.com", db) == []


def test_revoking_invitations_reports_how_many_were_pending(db: str) -> None:
    assert revoke_account_invitations("alice@example.com", db_path=db, now=NOW) == 0
    _invite(db, "alice@example.com")
    assert revoke_account_invitations("alice@example.com", db_path=db, now=NOW) == 1
    assert revoke_account_invitations("alice@example.com", db_path=db, now=NOW) == 0


def test_account_state_does_not_leak_between_tenants(db: str) -> None:
    policy = _policy(db)
    alice = _login_session(policy, "alice@example.com")
    bob = _login_session(policy, "bob@example.com")
    set_account_status(alice["user_id"], ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    assert not policy.check_session(alice).allowed
    assert policy.check_session(bob).allowed
    assert get_account_state(bob["user_id"], db) == {
        "id": bob["user_id"],
        "email": "bob@example.com",
        "status": ACCOUNT_STATUS_ACTIVE,
        "session_epoch": 0,
    }


# --------------------------------------------------------------------------
# Denial logging
# --------------------------------------------------------------------------


def test_every_denial_is_logged_with_reason_and_pseudonymous_identity(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="account_policy")
    policy = _policy(db, allowed=("alice@example.com", "carol@example.com"), invite_only=True)
    policy.admit_login(_userinfo("mallory@example.com"), now=NOW)
    policy.admit_login(_userinfo("alice@example.com", verified=False), now=NOW)
    policy.admit_login(_userinfo("carol@example.com"), now=NOW)

    reasons = [r.getMessage() for r in caplog.records]
    assert any("surface=login reason=not_allowlisted" in m for m in reasons)
    assert any("reason=identity_unverified" in m for m in reasons)
    assert any("reason=not_invited" in m for m in reasons)
    assert any(f"identity={pseudonymise_identifier('mallory@example.com')}" in m for m in reasons)


def test_logs_never_contain_addresses_or_secrets(db: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secret_looking = "ya29.a0-super-secret-access-token"
    policy = _policy(db, allowed=("alice@example.com",), invite_only=True)
    policy.admit_login(
        {
            "email": "mallory@example.com",
            "email_verified": True,
            "name": "Mallory",
            "access_token": secret_looking,
            "id_token": secret_looking,
        },
        now=NOW,
    )
    session = _login_session(_policy(db))
    set_account_status(session["user_id"], ACCOUNT_STATUS_SUSPENDED, db_path=db, now=NOW)
    _policy(db).check_session(session)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=" in text
    for forbidden in ("mallory", "alice", "example.com", "Mallory", secret_looking, "access_token"):
        assert forbidden not in text


def test_log_fields_cannot_be_used_to_inject_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="account_policy")
    log_access_denied(
        "login",
        DenialReason.NOT_ALLOWLISTED,
        user_id="abc\nFORGED access_granted",
        email="x@example.com\nFORGED access_granted",
    )
    (record,) = caplog.records
    message = record.getMessage()
    assert "\n" not in message and "FORGED" not in message
    assert "account=-" in message


def test_opaque_account_ids_are_logged_for_correlation(db: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="account_policy")
    policy = _policy(db)
    session = _login_session(policy)
    revoke_account_sessions(session["user_id"], db)
    policy.check_session(session)
    assert f"account={session['user_id']}" in caplog.records[-1].getMessage()
    assert "reason=session_revoked" in caplog.records[-1].getMessage()
