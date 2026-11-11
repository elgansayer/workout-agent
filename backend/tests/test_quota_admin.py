"""Audited administrator actions: overrides, entitlements and abuse review."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import quota_admin
from database import get_or_create_user, init_db
from quota_admin import (
    AccountNotFoundError,
    AdminAuthorizationError,
    AdminValidationError,
    abuse_overview,
    clear_override,
    list_audit,
    require_admin,
    review_abuse,
    set_entitlement,
    set_override,
)
from quotas import (
    ABUSE_DENIAL_THRESHOLD,
    CODE_DISABLED,
    QuotaExceededError,
    QuotaKind,
    consume,
)

T0 = 1_800_000_000.0
ADMIN_ENV = {"QUOTA_ADMIN_EMAILS": " Admin@Example.com ,ops@example.com"}
# One report per hour whatever the account's entitlement, so a flood is easy to
# produce regardless of the tier a test starts from.
ONE_REPORT_PER_HOUR = {
    "QUOTA_POLICY_OVERRIDES": json.dumps(
        {
            "entitlements": {
                tier: {"report": [[1, 3600]]}
                for tier in ("restricted", "standard", "extended")
            }
        }
    )
}


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "admin.db")
    init_db(path)
    return path


def _account(db: str, name: str) -> str:
    return get_or_create_user(f"{name}@example.com", name, db_path=db)["id"]


@pytest.fixture()
def admin(db: str) -> str:
    return _account(db, "admin")


@pytest.fixture()
def member(db: str) -> str:
    return _account(db, "member")


def _rows(db: str, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


def _audit_count(db: str) -> int:
    return int(_rows(db, "SELECT COUNT(*) FROM quota_audit_log")[0][0])


def _flood(db: str, user: str, *, denials: int, at: float = T0) -> None:
    """Spend the single allowed report, then be refused ``denials`` times."""
    consume(user, QuotaKind.REPORT, db, now=at, environ=ONE_REPORT_PER_HOUR)
    for offset in range(denials):
        with pytest.raises(QuotaExceededError):
            consume(
                user, QuotaKind.REPORT, db, now=at + 1 + offset, environ=ONE_REPORT_PER_HOUR
            )


# --- who may administer -------------------------------------------------------


def test_nobody_is_an_administrator_without_configuration(
    db: str, admin: str, member: str
) -> None:
    for environ in ({}, {"QUOTA_ADMIN_EMAILS": ""}, {"QUOTA_ADMIN_EMAILS": " , "}):
        with pytest.raises(AdminAuthorizationError):
            require_admin(admin, db, environ=environ)
        with pytest.raises(AdminAuthorizationError):
            set_entitlement(admin, member, "extended", "because", db, environ=environ)
        with pytest.raises(AdminAuthorizationError):
            abuse_overview(admin, db, environ=environ)
        with pytest.raises(AdminAuthorizationError):
            list_audit(admin, db, environ=environ)

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0


def test_only_listed_accounts_are_administrators_case_insensitively(
    db: str, admin: str, member: str
) -> None:
    require_admin(admin, db, environ=ADMIN_ENV)  # configured as "Admin@Example.com"

    for impostor in (member, "no-such-account", "", "   "):
        with pytest.raises(AdminAuthorizationError):
            require_admin(impostor, db, environ=ADMIN_ENV)
    with pytest.raises(AdminAuthorizationError):
        require_admin(None, db, environ=ADMIN_ENV)  # type: ignore[arg-type]


def test_a_non_administrator_cannot_change_anything(db: str, member: str) -> None:
    other = _account(db, "other")

    with pytest.raises(AdminAuthorizationError):
        set_entitlement(member, other, "extended", "self service", db, environ=ADMIN_ENV)
    with pytest.raises(AdminAuthorizationError):
        set_override(member, member, "report", 1000, None, "self service", db, environ=ADMIN_ENV)
    with pytest.raises(AdminAuthorizationError):
        review_abuse(member, other, "restrict", "retaliation", db, environ=ADMIN_ENV)

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM quota_overrides")[0][0] == 0
    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0


# --- entitlement --------------------------------------------------------------


def test_changing_an_entitlement_changes_limits_and_is_audited(
    db: str, admin: str, member: str
) -> None:
    result = set_entitlement(
        admin, member, "extended", "Paid beta tester", db, now=T0, environ=ADMIN_ENV
    )

    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0).limit == 30
    entries = list_audit(admin, db, environ=ADMIN_ENV)
    assert entries == [
        {
            "id": result["audit_id"],
            "account_id": member,
            "actor_account_id": admin,
            "action": "entitlement_set",
            "reason": "Paid beta tester",
            "detail": {"previous": "standard", "current": "extended"},
            "created_at": "2027-01-15T08:00:00+00:00",
        }
    ]
    assert result["entitlement"] == "extended" and result["previous"] == "standard"


def test_a_change_and_its_audit_record_commit_together(
    db: str, admin: str, member: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_audit(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(quota_admin, "_audit", failing_audit)

    with pytest.raises(Exception):  # noqa: B017 - any failure must roll back
        set_entitlement(admin, member, "restricted", "contain abuse", db, environ=ADMIN_ENV)

    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0
    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0).limit == 15


@pytest.mark.parametrize(
    "reason", ["", "  ", "no", "x" * 501, "bad\x00reason", None, 7]
)
def test_a_meaningful_reason_is_required(
    db: str, admin: str, member: str, reason: Any
) -> None:
    with pytest.raises(AdminValidationError):
        set_entitlement(admin, member, "extended", reason, db, environ=ADMIN_ENV)

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0


def test_reasons_are_normalised_to_a_single_line(db: str, admin: str, member: str) -> None:
    set_entitlement(
        admin, member, "extended", "line one\n\tline   two\r\nforged: entry", db,
        environ=ADMIN_ENV,
    )

    assert list_audit(admin, db, environ=ADMIN_ENV)[0]["reason"] == (
        "line one line two forged: entry"
    )


def test_invalid_entitlements_and_unknown_accounts_have_no_side_effects(
    db: str, admin: str, member: str
) -> None:
    with pytest.raises(AdminValidationError, match="restricted, standard, extended"):
        set_entitlement(admin, member, "platinum", "valid reason", db, environ=ADMIN_ENV)
    for target in ("no-such-account", "", "  ", None):
        with pytest.raises(AccountNotFoundError):
            set_entitlement(
                admin, target, "extended", "valid reason", db, environ=ADMIN_ENV  # type: ignore[arg-type]
            )

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0


# --- overrides ----------------------------------------------------------------


def test_an_override_scales_one_quota_for_one_account_and_is_audited(
    db: str, admin: str, member: str
) -> None:
    other = _account(db, "other")

    result = set_override(
        admin, member, "ai_request", 200, 3600, "Support ticket 42", db,
        now=T0, environ=ADMIN_ENV,
    )

    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0 + 1).limit == 30
    assert consume(other, QuotaKind.AI_REQUEST, db, now=T0 + 1).limit == 15
    assert consume(member, QuotaKind.REPORT, db, now=T0 + 1).limit == 30
    assert result["override"] == {
        "limit_percent": 200,
        "expires_at": "2027-01-15T09:00:00+00:00",
    }
    entry = list_audit(admin, db, account_id=member, environ=ADMIN_ENV)[0]
    assert entry["action"] == "override_set"
    assert entry["reason"] == "Support ticket 42"
    assert entry["detail"] == {
        "kind": "ai_request",
        "previous": None,
        "current": result["override"],
    }
    # After the override lapses the standard limit applies again.
    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0 + 3601).limit == 15


def test_a_new_override_replaces_the_old_one_and_audits_both_states(
    db: str, admin: str, member: str
) -> None:
    set_override(admin, member, "sync", 300, None, "first adjustment", db, environ=ADMIN_ENV)
    set_override(admin, member, "sync", 50, None, "second adjustment", db, environ=ADMIN_ENV)

    newest = list_audit(admin, db, environ=ADMIN_ENV)[0]
    assert newest["detail"]["previous"]["limit_percent"] == 300
    assert newest["detail"]["current"]["limit_percent"] == 50
    assert _rows(db, "SELECT COUNT(*) FROM quota_overrides")[0][0] == 1


def test_clearing_an_override_restores_the_policy_and_is_audited(
    db: str, admin: str, member: str
) -> None:
    set_override(admin, member, "sync", 500, None, "temporary boost", db, environ=ADMIN_ENV)

    result = clear_override(admin, member, "sync", "boost finished", db, environ=ADMIN_ENV)

    assert result["override"] is None
    assert consume(member, QuotaKind.SYNC, db, now=T0).limit == 2
    actions = [e["action"] for e in list_audit(admin, db, environ=ADMIN_ENV)]
    assert actions == ["override_clear", "override_set"]
    with pytest.raises(AdminValidationError, match="No override"):
        clear_override(admin, member, "sync", "again", db, environ=ADMIN_ENV)
    assert _audit_count(db) == 2


def test_a_zero_percent_override_switches_the_quota_off_until_it_expires(
    db: str, admin: str, member: str
) -> None:
    set_override(
        admin, member, "export", 0, 600, "Suspected scraping", db, now=T0, environ=ADMIN_ENV
    )

    with pytest.raises(QuotaExceededError) as caught:
        consume(member, QuotaKind.EXPORT, db, now=T0 + 100)

    assert caught.value.denial.code == CODE_DISABLED
    assert caught.value.denial.retry_after_seconds == 500
    assert consume(member, QuotaKind.EXPORT, db, now=T0 + 601).limit == 3


@pytest.mark.parametrize(
    ("kind", "percent", "lifetime"),
    [
        ("teleport", 100, None),
        ("report", -1, None),
        ("report", 1001, None),
        ("report", True, None),
        ("report", "100", None),
        ("report", 100.5, None),
        ("report", 100, 59),
        ("report", 100, 90 * 24 * 60 * 60 + 1),
        ("report", 100, True),
        ("report", 100, "3600"),
    ],
)
def test_invalid_overrides_are_rejected_without_side_effects(
    db: str, admin: str, member: str, kind: str, percent: Any, lifetime: Any
) -> None:
    with pytest.raises(AdminValidationError):
        set_override(
            admin, member, kind, percent, lifetime, "valid reason", db, environ=ADMIN_ENV
        )

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM quota_overrides")[0][0] == 0


def test_overrides_for_unknown_accounts_are_refused(db: str, admin: str) -> None:
    with pytest.raises(AccountNotFoundError):
        set_override(admin, "ghost", "report", 100, None, "valid reason", db, environ=ADMIN_ENV)
    with pytest.raises(AccountNotFoundError):
        clear_override(admin, "ghost", "report", "valid reason", db, environ=ADMIN_ENV)

    assert _audit_count(db) == 0


# --- abuse review -------------------------------------------------------------


def test_the_overview_flags_only_accounts_over_the_denial_threshold(
    db: str, admin: str, member: str
) -> None:
    noisy = _account(db, "noisy")
    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD)
    _flood(db, noisy, denials=ABUSE_DENIAL_THRESHOLD - 1)

    overview = abuse_overview(admin, db, now=T0 + 100, environ=ADMIN_ENV)

    assert [entry["account_id"] for entry in overview["queue"]] == [member]
    flagged = overview["queue"][0]
    assert flagged["email"] == "member@example.com"
    assert flagged["entitlement"] == "standard"
    assert flagged["denials"] == ABUSE_DENIAL_THRESHOLD
    assert flagged["denials_by_kind"] == {"report": ABUSE_DENIAL_THRESHOLD}
    assert overview["review_threshold"] == ABUSE_DENIAL_THRESHOLD
    assert overview["usage"]["report"] == {
        "allowed": 2,
        "denied": 2 * ABUSE_DENIAL_THRESHOLD - 1,
    }
    assert overview["usage"]["ai_request"] == {"allowed": 0, "denied": 0}


def test_old_denials_age_out_of_the_review_queue(db: str, admin: str, member: str) -> None:
    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD)

    assert abuse_overview(admin, db, now=T0 + 2 * 86400, environ=ADMIN_ENV)["queue"] == []


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("restrict", "restricted"), ("restore", "standard"), ("dismiss", "extended")],
)
def test_an_abuse_decision_is_applied_audited_and_clears_the_queue_entry(
    db: str, admin: str, member: str, decision: str, expected: str
) -> None:
    set_entitlement(admin, member, "extended", "initial grant", db, now=T0, environ=ADMIN_ENV)
    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD)

    result = review_abuse(
        admin, member, decision, "Reviewed the denial history", db,
        now=T0 + 100, environ=ADMIN_ENV,
    )

    assert result["entitlement"] == expected and result["previous"] == "extended"
    entry = list_audit(admin, db, account_id=member, environ=ADMIN_ENV)[0]
    assert entry["action"] == "abuse_review"
    assert entry["actor_account_id"] == admin
    assert entry["detail"] == {
        "decision": decision,
        "previous": "extended",
        "current": expected,
        "evidence": {
            "window_seconds": 86400,
            "denials_by_kind": {"report": ABUSE_DENIAL_THRESHOLD},
        },
    }
    assert abuse_overview(admin, db, now=T0 + 101, environ=ADMIN_ENV)["queue"] == []
    assert _rows(db, "SELECT COUNT(*) FROM quota_denials WHERE user_id = ?", member)[0][0] == 0


def test_restricting_an_account_tightens_its_limits(db: str, admin: str, member: str) -> None:
    review_abuse(admin, member, "restrict", "Confirmed abuse", db, now=T0, environ=ADMIN_ENV)

    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0 + 1).limit == 3
    review_abuse(admin, member, "restore", "Appeal upheld", db, now=T0 + 2, environ=ADMIN_ENV)
    assert consume(member, QuotaKind.AI_REQUEST, db, now=T0 + 3).limit == 15


def test_a_reviewed_account_returns_to_the_queue_only_with_fresh_evidence(
    db: str, admin: str, member: str
) -> None:
    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD)
    review_abuse(admin, member, "dismiss", "Looked benign", db, now=T0 + 50, environ=ADMIN_ENV)

    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD - 1, at=T0 + 4000)
    assert abuse_overview(admin, db, now=T0 + 5000, environ=ADMIN_ENV)["queue"] == []

    with pytest.raises(QuotaExceededError):
        consume(member, QuotaKind.REPORT, db, now=T0 + 4100, environ=ONE_REPORT_PER_HOUR)
    assert len(abuse_overview(admin, db, now=T0 + 5000, environ=ADMIN_ENV)["queue"]) == 1


def test_invalid_review_requests_have_no_side_effects(
    db: str, admin: str, member: str
) -> None:
    _flood(db, member, denials=ABUSE_DENIAL_THRESHOLD)

    with pytest.raises(AdminValidationError, match="dismiss, restrict, restore"):
        review_abuse(admin, member, "ban", "valid reason", db, environ=ADMIN_ENV)
    with pytest.raises(AdminValidationError):
        review_abuse(admin, member, "restrict", "", db, environ=ADMIN_ENV)
    with pytest.raises(AccountNotFoundError):
        review_abuse(admin, "ghost", "restrict", "valid reason", db, environ=ADMIN_ENV)

    assert _audit_count(db) == 0
    assert _rows(db, "SELECT COUNT(*) FROM account_quota_profiles")[0][0] == 0
    assert _rows(db, "SELECT SUM(denials) FROM quota_denials")[0][0] == ABUSE_DENIAL_THRESHOLD


# --- audit trail --------------------------------------------------------------


def test_the_audit_trail_is_newest_first_filterable_and_bounded(
    db: str, admin: str, member: str
) -> None:
    other = _account(db, "other")
    set_entitlement(admin, member, "extended", "first change", db, environ=ADMIN_ENV)
    set_entitlement(admin, other, "restricted", "second change", db, environ=ADMIN_ENV)
    set_override(admin, member, "report", 150, None, "third change", db, environ=ADMIN_ENV)

    everything = list_audit(admin, db, environ=ADMIN_ENV)
    assert [e["reason"] for e in everything] == [
        "third change",
        "second change",
        "first change",
    ]
    assert [e["reason"] for e in list_audit(admin, db, account_id=member, environ=ADMIN_ENV)] == [
        "third change",
        "first change",
    ]
    assert len(list_audit(admin, db, limit=1, environ=ADMIN_ENV)) == 1
    assert len(list_audit(admin, db, limit=0, environ=ADMIN_ENV)) == 1  # clamped up
    assert list_audit(admin, db, account_id="no-such-account", environ=ADMIN_ENV) == []
    with pytest.raises(AdminAuthorizationError):
        list_audit(member, db, environ=ADMIN_ENV)


def test_audit_records_hold_no_contact_details_or_credentials(
    db: str, admin: str, member: str
) -> None:
    set_entitlement(admin, member, "extended", "grant", db, environ=ADMIN_ENV)
    set_override(admin, member, "report", 150, 3600, "boost", db, environ=ADMIN_ENV)
    review_abuse(admin, member, "dismiss", "fine", db, environ=ADMIN_ENV)

    stored = json.dumps(_rows(db, "SELECT * FROM quota_audit_log"))

    assert "example.com" not in stored
    assert "secret" not in stored.lower() and "token" not in stored.lower()
