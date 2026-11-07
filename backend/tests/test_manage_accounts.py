"""Operator CLI for account access (invitations, suspension, deletion, sign-out)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from account_policy import AccessPolicy, AccessPolicyConfig, DenialReason
from data_classification import pseudonymise_identifier
from database import find_account_state, init_db, list_account_invitations
from scripts import manage_accounts

ADDRESS = "operator.target@example.com"
BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    return str(tmp_path / "cli.db")


def run(db: str, *argv: str) -> int:
    return manage_accounts.main(["--db", db, *argv])


def _login(db: str, *, invite_only: bool = False) -> dict[str, object]:
    init_db(db)  # the web service does this in its lifespan; the CLI does it itself
    policy = AccessPolicy(AccessPolicyConfig(invite_only=invite_only), db)
    decision = policy.admit_login({"email": ADDRESS, "email_verified": True})
    assert decision.allowed
    return {"user": decision.email, "user_id": decision.user_id, "session_epoch": decision.session_epoch}


def test_invite_lets_exactly_that_person_in_and_is_single_use(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "invite", ADDRESS, "--days", "3") == 0
    assert "valid for 3 day(s)" in capsys.readouterr().out
    assert [i["status"] for i in list_account_invitations(ADDRESS, db)] == ["pending"]

    policy = AccessPolicy(AccessPolicyConfig(invite_only=True), db)
    assert policy.admit_login({"email": ADDRESS, "email_verified": True}).created
    assert [i["status"] for i in list_account_invitations(ADDRESS, db)] == ["accepted"]
    other = policy.admit_login({"email": "someone.else@example.com", "email_verified": True})
    assert other.reason is DenialReason.NOT_INVITED


def test_revoke_invite_stops_a_pending_invitation(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(db, "invite", ADDRESS) == 0
    assert run(db, "revoke-invite", ADDRESS) == 0
    assert "Revoked 1 pending" in capsys.readouterr().out
    policy = AccessPolicy(AccessPolicyConfig(invite_only=True), db)
    assert policy.admit_login({"email": ADDRESS, "email_verified": True}).reason is DenialReason.NOT_INVITED


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
    login_again = policy.admit_login({"email": ADDRESS, "email_verified": True})
    assert login_again.reason is DenialReason.ACCOUNT_DELETED


@pytest.mark.parametrize("command", [["suspend"], ["reactivate"], ["revoke-sessions"], ["delete", "--yes"]])
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
