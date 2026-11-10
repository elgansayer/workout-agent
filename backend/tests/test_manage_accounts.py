"""Operator CLI for accounts and the explicit assignment of legacy data.

The CLI is the only supported production path for giving pre-account data to a
verified owner, so these tests drive it end to end against a synthetic database
upgraded from the single-user schema. All identities and emails are synthetic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from account_policy import AccessPolicy, AccessPolicyConfig, DenialReason
from accounts import resolve_oauth_user, set_account_status
from data_classification import pseudonymise_identifier
from database import (
    USER_OWNED_TABLES,
    find_account_state,
    get_all_users,
    get_or_create_user,
    init_db,
    list_account_invitations,
)
from legacy_ownership import OWNED_TABLES, plan_legacy_ownership
from scripts import manage_accounts
from tenancy import LEGACY_TENANT_EMAIL
from test_database import _create_pre_multi_tenant_schema

_OWNER = "owner@example.com"
_SUBJECT = "synthetic-google-subject-owner"


@pytest.fixture()
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.delenv("DATABASE_PATH", raising=False)


@pytest.fixture()
def legacy_db(tmp_path: Path, production: None) -> str:
    """A production database upgraded from the single-user era."""
    path = str(tmp_path / "cli.db")
    _create_pre_multi_tenant_schema(path)
    init_db(path)
    return path


def _sign_in_owner(db: str) -> str:
    return resolve_oauth_user(
        provider="google",
        subject=_SUBJECT,
        email=_OWNER,
        email_verified=True,
        db_path=db,
    )["id"]


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = manage_accounts.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _snapshot(db: str) -> dict[str, Any]:
    """Every owned row, account row and audit row, for before/after comparison."""
    with sqlite3.connect(db) as conn:
        state: dict[str, Any] = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in USER_OWNED_TABLES
        }
        state["users"] = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        state["audit"] = conn.execute(
            "SELECT * FROM legacy_ownership_migrations"
        ).fetchall()
    return state


def _digest(db: str) -> str:
    return plan_legacy_ownership(db, owner_email=_OWNER).digest


def _audit_rows(db: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT owner_email, applied_by, to_user_id "
            "FROM legacy_ownership_migrations ORDER BY id"
        ).fetchall()


def _statuses(db: str) -> dict[str, str]:
    return {
        user["email"]: user["status"]
        for user in get_all_users(db, include_inactive=True)
    }


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_reports_status_and_verification_without_identity_subjects(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    owner_id = _sign_in_owner(legacy_db)

    code, out, _err = _run(capsys, "--database", legacy_db, "list")

    assert code == 0
    rows = {line.split()[3]: line.split() for line in out.splitlines()[1:]}
    assert rows[LEGACY_TENANT_EMAIL][:2] == ["unclaimed", "no"]
    assert rows[_OWNER][:2] == ["active", "yes"]
    assert rows[_OWNER][4] == owner_id
    # The provider subject is an identifier that must never be echoed.
    assert _SUBJECT not in out


# ---------------------------------------------------------------------------
# plan-legacy-assignment
# ---------------------------------------------------------------------------


def test_plan_is_read_only_and_prints_the_confirmation_digest(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _sign_in_owner(legacy_db)
    before = _snapshot(legacy_db)

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "plan-legacy-assignment",
        "--owner-email",
        _OWNER,
    )

    assert code == 0
    assert f"Plan digest: {_digest(legacy_db)}" in out
    assert "Blocked" not in out
    assert _snapshot(legacy_db) == before


def test_plan_json_is_machine_readable_and_matches_the_library_plan(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _sign_in_owner(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER)

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "plan-legacy-assignment",
        "--owner-email",
        _OWNER,
        "--json",
    )

    assert code == 0
    document = json.loads(out)
    assert document["digest"] == plan.digest
    assert document["blockers"] == []
    assert document["movable_rows"] == plan.movable_rows > 0
    assert {entry["table"] for entry in document["tables"]} == {
        table.name for table in OWNED_TABLES
    }


@pytest.mark.parametrize(
    ("prepare", "blocker", "explanation"),
    [
        pytest.param(
            lambda db: None,
            "owner_not_found",
            "must sign in once first",
            id="owner-never-signed-in",
        ),
        pytest.param(
            lambda db: get_or_create_user(_OWNER, db_path=db),
            "owner_has_no_verified_identity",
            "has not signed in through a verified identity",
            id="owner-without-verified-identity",
        ),
        pytest.param(
            lambda db: set_account_status(_sign_in_owner(db), "suspended", db),
            "owner_not_active",
            "not active",
            id="owner-suspended",
        ),
    ],
)
def test_plan_exits_nonzero_and_explains_each_blocker(
    legacy_db: str,
    capsys: pytest.CaptureFixture[str],
    prepare: Any,
    blocker: str,
    explanation: str,
) -> None:
    prepare(legacy_db)

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "plan-legacy-assignment",
        "--owner-email",
        _OWNER,
    )

    assert code == 1
    assert "Blocked:" in out
    assert blocker in out
    assert explanation in out
    # A blocked plan never offers a digest an operator could confirm.
    assert "Plan digest" not in out


def test_the_placeholder_cannot_be_named_as_the_owner(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "plan-legacy-assignment",
        "--owner-email",
        LEGACY_TENANT_EMAIL,
    )

    assert code == 1
    assert "owner_is_legacy_placeholder" in out


# ---------------------------------------------------------------------------
# assign-legacy-data
# ---------------------------------------------------------------------------


def test_assign_is_refused_without_a_backup_attestation(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _sign_in_owner(legacy_db)
    before = _snapshot(legacy_db)

    code, out, err = _run(
        capsys,
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        _digest(legacy_db),
    )

    assert code == 1
    assert out == ""
    assert "verified backup" in err
    assert _snapshot(legacy_db) == before


@pytest.mark.parametrize("stale", [False, True], ids=["mistyped", "stale"])
def test_assign_is_refused_for_a_mistyped_or_stale_digest(
    legacy_db: str, capsys: pytest.CaptureFixture[str], stale: bool
) -> None:
    _sign_in_owner(legacy_db)
    confirmed = _digest(legacy_db)
    if stale:
        # The data changed after the operator reviewed the plan.
        with sqlite3.connect(legacy_db) as conn:
            legacy_id = conn.execute(
                "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO workout_history (date, hevy_payload, user_id) "
                "VALUES ('2026-01-02', '{}', ?)",
                (legacy_id,),
            )
    else:
        confirmed = "not-the-digest"
    before = _snapshot(legacy_db)

    code, _out, err = _run(
        capsys,
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        confirmed,
        "--backup-verified",
    )

    assert code == 1
    assert "no longer matches" in err
    assert _snapshot(legacy_db) == before
    assert _audit_rows(legacy_db) == []


def test_assign_to_an_unverified_owner_is_refused_and_changes_nothing(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_or_create_user(_OWNER, db_path=legacy_db)
    before = _snapshot(legacy_db)

    code, _out, err = _run(
        capsys,
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        "anything",
        "--backup-verified",
    )

    assert code == 1
    assert "owner_has_no_verified_identity" in err
    assert _snapshot(legacy_db) == before


def test_assign_moves_the_data_and_records_the_named_operator(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    owner_id = _sign_in_owner(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER)

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        plan.digest,
        "--backup-verified",
        "--operator",
        "ops-user",
    )

    assert code == 0
    assert f"Assigned {plan.movable_rows} rows to the verified owner." in out
    assert _statuses(legacy_db) == {LEGACY_TENANT_EMAIL: "retired", _OWNER: "active"}
    assert _audit_rows(legacy_db) == [(_OWNER, "ops-user", owner_id)]
    with sqlite3.connect(legacy_db) as conn:
        for table in plan.tables:
            owned = conn.execute(
                f"SELECT count(*) FROM {table.table} WHERE user_id = ?", (owner_id,)
            ).fetchone()[0]
            assert owned == table.movable, table.table


def test_assign_defaults_the_operator_to_the_os_user(
    legacy_db: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sign_in_owner(legacy_db)
    monkeypatch.setattr("getpass.getuser", lambda: "os-operator")

    code, _out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        _digest(legacy_db),
        "--backup-verified",
    )

    assert code == 0
    assert [applied_by for _owner, applied_by, _to in _audit_rows(legacy_db)] == [
        "os-operator"
    ]


def test_assign_twice_is_a_no_op(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _sign_in_owner(legacy_db)
    arguments = (
        "--database",
        legacy_db,
        "assign-legacy-data",
        "--owner-email",
        _OWNER,
        "--confirm-plan",
        _digest(legacy_db),
        "--backup-verified",
    )
    assert _run(capsys, *arguments)[0] == 0
    after_first = _snapshot(legacy_db)

    code, out, _err = _run(capsys, *arguments)

    assert code == 0
    assert "already assigned to this owner" in out
    assert _snapshot(legacy_db) == after_first
    assert len(_audit_rows(legacy_db)) == 1


# ---------------------------------------------------------------------------
# set-status
# ---------------------------------------------------------------------------


def test_set_status_suspends_and_reactivates_an_account_case_insensitively(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    owner_id = _sign_in_owner(legacy_db)

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "set-status",
        "--email",
        "Owner@Example.COM",
        "--status",
        "suspended",
    )
    assert code == 0
    assert "Account is now suspended." in out
    # Background jobs only ever see active accounts.
    assert owner_id not in {user["id"] for user in get_all_users(legacy_db)}

    code, out, _err = _run(
        capsys,
        "--database",
        legacy_db,
        "set-status",
        "--email",
        _OWNER,
        "--status",
        "active",
    )
    assert code == 0
    assert "Account is now active." in out
    assert owner_id in {user["id"] for user in get_all_users(legacy_db)}


def test_set_status_refuses_unknown_accounts_and_the_placeholder(
    legacy_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(
        capsys,
        "--database",
        legacy_db,
        "set-status",
        "--email",
        "nobody@example.com",
        "--status",
        "suspended",
    )
    assert code == 1
    assert "No account has that email." in err

    before = _snapshot(legacy_db)
    code, _out, err = _run(
        capsys,
        "--database",
        legacy_db,
        "set-status",
        "--email",
        LEGACY_TENANT_EMAIL,
        "--status",
        "active",
    )
    assert code == 1
    assert "Refused:" in err
    assert "ownership migration" in err
    assert _snapshot(legacy_db) == before
    assert _statuses(legacy_db)[LEGACY_TENANT_EMAIL] == "unclaimed"


def test_set_status_only_offers_the_operator_controlled_statuses(
    legacy_db: str,
) -> None:
    _sign_in_owner(legacy_db)
    before = _snapshot(legacy_db)

    for status in ("retired", "deleted", "unclaimed"):
        with pytest.raises(SystemExit) as raised:
            manage_accounts.main(
                [
                    "--database",
                    legacy_db,
                    "set-status",
                    "--email",
                    _OWNER,
                    "--status",
                    status,
                ]
            )
        assert raised.value.code == 2

    assert _snapshot(legacy_db) == before


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_the_database_flag_overrides_the_environment(
    legacy_db: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unused = tmp_path / "from-environment.db"
    monkeypatch.setenv("DATABASE_PATH", str(unused))

    code, out, _err = _run(capsys, "--database", legacy_db, "list")

    assert code == 0
    assert LEGACY_TENANT_EMAIL in out
    assert not unused.exists()


def test_the_database_defaults_to_the_environment(
    legacy_db: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", f"  {legacy_db}  ")

    code, out, _err = _run(capsys, "list")

    assert code == 0
    assert LEGACY_TENANT_EMAIL in out


def test_the_cli_runs_as_a_script_from_any_directory(
    legacy_db: str, tmp_path: Path
) -> None:
    script = Path(manage_accounts.__file__).resolve()
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    environment.update({"APP_ENV": "production", "ALLOW_ANONYMOUS_WEB": "0"})

    result = subprocess.run(
        [sys.executable, str(script), "--database", legacy_db, "list"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert LEGACY_TENANT_EMAIL in result.stdout


# ---------------------------------------------------------------------------
# Account access policy CLI (invitations, suspension, deletion, sign-out)
# ---------------------------------------------------------------------------

ADDRESS = "operator.target@example.com"
BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    return str(tmp_path / "cli.db")


def run(db: str, *argv: str) -> int:
    return manage_accounts.main(["--db", db, *argv])


def _claims(email: str = ADDRESS) -> dict[str, object]:
    """Synthetic Google claims: one address, one stable subject."""
    return {"sub": f"google-{email}", "email": email, "email_verified": True}


def _login(db: str, *, invite_only: bool = False) -> dict[str, object]:
    init_db(db)  # the web service does this in its lifespan; the CLI does it itself
    policy = AccessPolicy(AccessPolicyConfig(invite_only=invite_only), db)
    decision = policy.admit_login(_claims())
    assert decision.allowed
    return {"user": decision.email, "user_id": decision.user_id, "session_epoch": decision.session_epoch}


def test_invite_lets_exactly_that_person_in_and_is_single_use(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "invite", ADDRESS, "--days", "3") == 0
    assert "valid for 3 day(s)" in capsys.readouterr().out
    assert [i["status"] for i in list_account_invitations(ADDRESS, db)] == ["pending"]

    policy = AccessPolicy(AccessPolicyConfig(invite_only=True), db)
    assert policy.admit_login(_claims()).created
    assert [i["status"] for i in list_account_invitations(ADDRESS, db)] == ["accepted"]
    other = policy.admit_login(_claims("someone.else@example.com"))
    assert other.reason is DenialReason.NOT_INVITED


def test_revoke_invite_stops_a_pending_invitation(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "invite", ADDRESS) == 0
    assert run(db, "revoke-invite", ADDRESS) == 0
    assert "Revoked 1 pending" in capsys.readouterr().out
    policy = AccessPolicy(AccessPolicyConfig(invite_only=True), db)
    assert policy.admit_login(_claims()).reason is DenialReason.NOT_INVITED


def test_suspend_and_reactivate_end_sessions_and_never_resurrect_them(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    session = _login(db)
    policy = AccessPolicy(AccessPolicyConfig(), db)
    assert policy.check_session(session).allowed

    assert run(db, "suspend", ADDRESS) == 0
    assert "status=suspended" in capsys.readouterr().out
    assert policy.check_session(session).reason is DenialReason.ACCOUNT_SUSPENDED

    assert run(db, "reactivate", ADDRESS) == 0
    assert "status=active" in capsys.readouterr().out
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED
    assert policy.check_session(_login(db)).allowed


def test_revoke_sessions_signs_the_account_out_everywhere(db: str) -> None:
    session = _login(db)
    assert run(db, "revoke-sessions", ADDRESS) == 0
    policy = AccessPolicy(AccessPolicyConfig(), db)
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED
    state = find_account_state(ADDRESS, db)
    assert state is not None and state["status"] == "active" and state["session_epoch"] == 1


def test_delete_needs_confirmation_and_is_terminal(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    session = _login(db)
    assert run(db, "delete", ADDRESS) == manage_accounts.EXIT_USAGE
    assert "--yes" in capsys.readouterr().err
    state = find_account_state(ADDRESS, db)
    assert state is not None and state["status"] == "active"  # untouched

    assert run(db, "delete", ADDRESS, "--yes") == 0
    policy = AccessPolicy(AccessPolicyConfig(), db)
    assert policy.check_session(session).reason is DenialReason.ACCOUNT_DELETED

    assert run(db, "reactivate", ADDRESS) == manage_accounts.EXIT_FAILED
    assert "cannot be restored" in capsys.readouterr().err
    assert run(db, "suspend", ADDRESS) == manage_accounts.EXIT_FAILED
    login_again = policy.admit_login(_claims())
    assert login_again.reason is DenialReason.ACCOUNT_DELETED


def _claims_for(subject: str) -> dict[str, object]:
    return {"sub": subject, "email": ADDRESS, "email_verified": True}


def test_status_says_whether_google_sign_in_is_linked_without_printing_it(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    init_db(db)
    get_or_create_user(ADDRESS, "Provisioned", db)
    assert run(db, "status", ADDRESS) == 0
    assert "google_sign_in=not_linked" in capsys.readouterr().out

    AccessPolicy(AccessPolicyConfig(), db).admit_login(_claims_for("subject-that-must-not-print"))
    assert run(db, "status", ADDRESS) == 0
    out = capsys.readouterr().out
    assert "google_sign_in=linked" in out
    assert "subject-that-must-not-print" not in out and ADDRESS not in out


def test_relink_needs_confirmation_and_changes_nothing_without_it(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _login(db)

    assert run(db, "relink", ADDRESS) == manage_accounts.EXIT_USAGE

    assert "--yes" in capsys.readouterr().err
    assert AccessPolicy(AccessPolicyConfig(), db).check_session(session).allowed
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM user_identities").fetchone() == (1,)


def test_relink_moves_an_account_to_a_recreated_google_account(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _login(db)  # bound to the subject derived from the address
    policy = AccessPolicy(AccessPolicyConfig(), db)
    recreated = _claims_for("the-recreated-google-account")
    # Sign-in never moves an account by address alone.
    assert policy.admit_login(recreated).reason is DenialReason.IDENTITY_CONFLICT

    assert run(db, "relink", ADDRESS, "--yes") == 0

    out = capsys.readouterr().out
    assert "unbound" in out and "session_epoch=1" in out
    assert ADDRESS not in out and "the-recreated-google-account" not in out
    assert policy.check_session(session).reason is DenialReason.SESSION_REVOKED
    state = find_account_state(ADDRESS, db)
    assert state is not None
    relinked = policy.admit_login(recreated)
    assert relinked.allowed and not relinked.created and relinked.user_id == state["id"]
    # The previous Google account no longer matches the bound one.
    assert policy.admit_login(_claims_for(f"google-{ADDRESS}")).reason is (
        DenialReason.IDENTITY_CONFLICT
    )


def test_relink_with_no_google_identity_is_a_harmless_no_op(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    init_db(db)
    get_or_create_user(ADDRESS, "Provisioned", db)

    assert run(db, "relink", ADDRESS, "--yes") == 0

    assert "nothing to relink" in capsys.readouterr().out
    state = find_account_state(ADDRESS, db)
    assert state is not None and state["session_epoch"] == 0


def test_relink_on_an_unknown_account_fails_without_creating_it(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(db, "relink", ADDRESS, "--yes") == manage_accounts.EXIT_FAILED
    assert "No account exists" in capsys.readouterr().err
    assert find_account_state(ADDRESS, db) is None


def test_a_deleted_account_cannot_be_relinked(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _login(db)
    assert run(db, "delete", ADDRESS, "--yes") == 0
    capsys.readouterr()

    assert run(db, "relink", ADDRESS, "--yes") == manage_accounts.EXIT_FAILED

    assert "cannot be re-bound" in capsys.readouterr().err
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM user_identities").fetchone() == (1,)


@pytest.mark.parametrize(
    "command", [["suspend"], ["reactivate"], ["revoke-sessions"], ["relink", "--yes"], ["delete", "--yes"]]
)
def test_commands_on_an_unknown_account_fail_without_creating_it(
    db: str, command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(db, command[0], ADDRESS, *command[1:]) == manage_accounts.EXIT_FAILED
    assert "No account exists" in capsys.readouterr().err
    assert find_account_state(ADDRESS, db) is None


@pytest.mark.parametrize("address", ["", "not an email", "a@b", "legacy@local", "x@example.com\nBcc: y@example.com"])
def test_invalid_addresses_are_rejected_before_the_database_is_touched(db: str, address: str) -> None:
    assert run(db, "invite", address) == manage_accounts.EXIT_USAGE
    assert not Path(db).exists()


@pytest.mark.parametrize("days", ["0", "-1", "91", "1000"])
def test_invitation_lifetime_is_bounded(db: str, days: str) -> None:
    assert run(db, "invite", ADDRESS, "--days", days) == manage_accounts.EXIT_USAGE
    assert list_account_invitations(ADDRESS, db) == []


def test_status_reports_state_without_printing_the_address(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _login(db)
    assert run(db, "invite", ADDRESS) == 0
    capsys.readouterr()
    assert run(db, "status", ADDRESS) == 0
    out = capsys.readouterr().out
    assert f"identity={pseudonymise_identifier(ADDRESS)}" in out
    assert "status=active" in out and "invitation status=pending" in out


def test_no_command_ever_prints_the_address(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _login(db)
    for argv in (
        ("status", ADDRESS), ("invite", ADDRESS), ("revoke-invite", ADDRESS), ("suspend", ADDRESS),
        ("reactivate", ADDRESS), ("revoke-sessions", ADDRESS), ("delete", ADDRESS), ("delete", ADDRESS, "--yes"),
        ("reactivate", ADDRESS),
    ):
        run(db, *argv)
    captured = capsys.readouterr()
    assert "operator.target" not in captured.out + captured.err
    assert "example.com" not in captured.out + captured.err


def test_unreadable_database_is_reported_without_a_traceback_or_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert manage_accounts.main(["--db", str(tmp_path), "status", ADDRESS]) == manage_accounts.EXIT_FAILED
    err = capsys.readouterr().err
    assert "Database error" in err and str(tmp_path) not in err and "Traceback" not in err


def test_script_runs_as_documented_from_the_application_root(tmp_path: Path) -> None:
    """``python scripts/manage_accounts.py`` must not be shadowed by sibling modules."""
    result = subprocess.run(
        [sys.executable, "-I", "scripts/manage_accounts.py", "--db", str(tmp_path / "doc.db"), "status", ADDRESS],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "account=none" in result.stdout


def test_help_documents_every_command() -> None:
    parser = manage_accounts.build_parser()
    help_text = parser.format_help()
    for command in ("status", "invite", "revoke-invite", "suspend", "reactivate", "revoke-sessions", "delete"):
        assert command in help_text
