"""Explicit, verified assignment of pre-account data to its owner."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import legacy_ownership
import pytest
from accounts import resolve_oauth_user, set_account_status
from cryptography.fernet import Fernet
from database import (
    USER_OWNED_TABLES,
    advance_day,
    get_active_programme,
    get_all_users,
    get_current_day,
    get_meta,
    get_or_create_user,
    get_recent_hevy_logs,
    get_user_api_key,
    init_db,
    save_dashboard_insight,
    save_user_api_key,
    save_workout,
    set_active_programme,
    set_meta,
)
from legacy_ownership import (
    OwnershipBlockedError,
    OwnershipError,
    OwnershipPlanMismatchError,
    apply_legacy_ownership,
    get_deployment_owner_id,
    is_deployment_owner,
    plan_legacy_ownership,
)
from tenancy import LEGACY_TENANT_EMAIL
from test_database import _create_pre_multi_tenant_schema

_OWNER_EMAIL = "owner@example.com"

# Tables the synthetic single-user schema populates, one row each.
_PRE_ACCOUNT_TABLES = (
    "workout_history",
    "exercise_progress",
    "body_metrics",
    "daily_log",
    "check_ins",
    "chat_messages",
    "programme_state",
    "hevy_meta",
    "hevy_routines",
    "reasoning_logs",
    "dashboard_insights",
    "deep_correlations",
)


@pytest.fixture()
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "0")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def legacy_db(tmp_path: Path, production: None) -> str:
    """A production database upgraded from the single-user era."""
    path = str(tmp_path / "legacy.db")
    _create_pre_multi_tenant_schema(path)
    init_db(path)
    return path


def _sign_in(db: str, email: str = _OWNER_EMAIL, subject: str | None = None) -> str:
    user = resolve_oauth_user(
        provider="google",
        subject=subject or f"subject-{email}",
        email=email,
        email_verified=True,
        db_path=db,
    )
    return user["id"]


def _placeholder_id(db: str) -> str:
    users = get_all_users(db, include_inactive=True)
    return next(u["id"] for u in users if u["email"] == LEGACY_TENANT_EMAIL)


def _status(db: str, user_id: str) -> str:
    with sqlite3.connect(db) as conn:
        return str(
            conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        )


def _snapshot(db: str) -> dict[str, Any]:
    """Every owned row plus account state and audit rows, for before/after checks."""
    with sqlite3.connect(db) as conn:
        state: dict[str, Any] = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in USER_OWNED_TABLES
        }
        state["users"] = conn.execute(
            "SELECT id, status, status_changed_at, updated_at FROM users ORDER BY id"
        ).fetchall()
        state["audit"] = conn.execute(
            "SELECT * FROM legacy_ownership_migrations"
        ).fetchall()
    return state


def _owned_by(db: str, table: str, user_id: str) -> int:
    with sqlite3.connect(db) as conn:
        return int(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_the_plan_reports_every_table_and_changes_nothing(legacy_db: str) -> None:
    owner = _sign_in(legacy_db)
    before = _snapshot(legacy_db)

    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)

    assert plan.blockers == ()
    assert plan.can_apply
    assert plan.owner_user_id == owner
    assert plan.legacy_user_id == _placeholder_id(legacy_db)
    assert {t.table for t in plan.tables} == set(USER_OWNED_TABLES)
    movable = {t.table: t.movable for t in plan.tables}
    assert all(movable[table] == 1 for table in _PRE_ACCOUNT_TABLES)
    assert plan.movable_rows == len(_PRE_ACCOUNT_TABLES)
    assert plan.left_behind_rows == 0
    assert len(plan.digest) == 64
    assert plan.digest == plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL).digest
    as_dict = json.loads(json.dumps(plan.as_dict()))
    assert as_dict["digest"] == plan.digest
    assert as_dict["blockers"] == []
    assert _snapshot(legacy_db) == before  # planning is read-only


def test_the_plan_is_case_insensitive_about_the_owner_email(legacy_db: str) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email="  OWNER@Example.COM ")
    assert plan.blockers == ()
    assert plan.owner_email == _OWNER_EMAIL


def test_the_digest_changes_when_the_data_changes(legacy_db: str) -> None:
    _sign_in(legacy_db)
    first = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL).digest
    save_workout({"late": True}, legacy_db, when="2031-02-02", user_id=_placeholder_id(legacy_db))
    second = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL).digest
    assert first != second


def test_blockers_are_listed_for_every_ineligible_owner(legacy_db: str) -> None:
    _sign_in(legacy_db, "suspended@example.com")
    suspended = next(
        u for u in get_all_users(legacy_db) if u["email"] == "suspended@example.com"
    )
    set_account_status(suspended["id"], "suspended", legacy_db)
    get_or_create_user("unverified@example.com", None, legacy_db)

    def blockers(email: str) -> tuple[str, ...]:
        return plan_legacy_ownership(legacy_db, owner_email=email).blockers

    assert blockers("nobody@example.com") == ("owner_not_found",)
    assert blockers("unverified@example.com") == ("owner_has_no_verified_identity",)
    assert blockers("suspended@example.com") == ("owner_not_active",)
    assert blockers(LEGACY_TENANT_EMAIL) == ("owner_is_legacy_placeholder",)


def test_a_table_the_plan_cannot_move_blocks_the_assignment(legacy_db: str) -> None:
    _sign_in(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE future_feature (user_id TEXT, payload TEXT)")
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    assert plan.blockers == ("unmapped_user_table:future_feature",)
    assert not plan.can_apply


@pytest.mark.parametrize(
    "owner_email",
    ["nobody@example.com", LEGACY_TENANT_EMAIL, "unverified@example.com"],
)
def test_a_blocked_assignment_changes_nothing(legacy_db: str, owner_email: str) -> None:
    _sign_in(legacy_db)
    get_or_create_user("unverified@example.com", None, legacy_db)
    before = _snapshot(legacy_db)

    with pytest.raises(OwnershipBlockedError) as blocked:
        apply_legacy_ownership(
            legacy_db,
            owner_email=owner_email,
            confirm_digest="0" * 64,
            applied_by="operator",
            backup_verified=True,
        )

    assert blocked.value.blockers
    assert _snapshot(legacy_db) == before


# ---------------------------------------------------------------------------
# Apply: attestations and confirmation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backup_verified", [False, None, 1, "yes"])
def test_a_verified_backup_must_be_attested(legacy_db: str, backup_verified: Any) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    before = _snapshot(legacy_db)
    with pytest.raises(OwnershipError, match="backup"):
        apply_legacy_ownership(
            legacy_db,
            owner_email=_OWNER_EMAIL,
            confirm_digest=plan.digest,
            applied_by="operator",
            backup_verified=backup_verified,
        )
    assert _snapshot(legacy_db) == before


def test_the_operator_must_be_named(legacy_db: str) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    with pytest.raises(OwnershipError, match="operator"):
        apply_legacy_ownership(
            legacy_db,
            owner_email=_OWNER_EMAIL,
            confirm_digest=plan.digest,
            applied_by="   ",
            backup_verified=True,
        )


@pytest.mark.parametrize("digest", ["", "0" * 64, "é" * 64, "plan-digest"])
def test_a_wrong_digest_is_refused_without_touching_data(
    legacy_db: str, digest: str
) -> None:
    _sign_in(legacy_db)
    before = _snapshot(legacy_db)
    with pytest.raises(OwnershipPlanMismatchError):
        apply_legacy_ownership(
            legacy_db,
            owner_email=_OWNER_EMAIL,
            confirm_digest=digest,
            applied_by="operator",
            backup_verified=True,
        )
    assert _snapshot(legacy_db) == before
    assert _status(legacy_db, _placeholder_id(legacy_db)) == "unclaimed"


def test_a_stale_digest_is_refused_after_the_data_changed(legacy_db: str) -> None:
    _sign_in(legacy_db)
    stale = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL).digest
    save_workout({"late": True}, legacy_db, when="2031-02-02", user_id=_placeholder_id(legacy_db))
    with pytest.raises(OwnershipPlanMismatchError, match="fresh plan"):
        apply_legacy_ownership(
            legacy_db,
            owner_email=_OWNER_EMAIL,
            confirm_digest=stale,
            applied_by="operator",
            backup_verified=True,
        )


def test_the_digest_is_accepted_however_it_was_copied(legacy_db: str) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    result = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=f"  {plan.digest.upper()}\n",
        applied_by="operator",
        backup_verified=True,
    )
    assert result.status == "assigned"


# ---------------------------------------------------------------------------
# Apply: the assignment itself
# ---------------------------------------------------------------------------


def test_the_assignment_moves_all_legacy_data_to_the_verified_owner(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    bystander = _sign_in(legacy_db, "bystander@example.com")
    save_workout({"theirs": True}, legacy_db, when="2031-03-03", user_id=bystander)
    placeholder = _placeholder_id(legacy_db)
    bystander_before = _owned_by(legacy_db, "workout_history", bystander)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)

    result = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by=" operator ",
        backup_verified=True,
    )

    assert result.status == "assigned"
    assert result.plan_digest == plan.digest
    assert result.moved == {table: 1 for table in _PRE_ACCOUNT_TABLES}
    assert result.left_behind == {}
    for table in _PRE_ACCOUNT_TABLES:
        assert _owned_by(legacy_db, table, placeholder) == 0, table
    assert _owned_by(legacy_db, "workout_history", owner) == 1
    assert _owned_by(legacy_db, "workout_history", bystander) == bystander_before
    assert _status(legacy_db, placeholder) == "retired"
    assert _status(legacy_db, owner) == "active"
    assert get_deployment_owner_id(legacy_db) == owner
    assert {u["id"] for u in get_all_users(legacy_db)} == {owner, bystander}

    # The migrated data is reachable through the ordinary tenant-scoped API.
    assert get_current_day(legacy_db, user_id=owner) == 3
    assert get_recent_hevy_logs(5, legacy_db, user_id=owner)
    assert get_recent_hevy_logs(5, legacy_db, user_id=bystander) != get_recent_hevy_logs(
        5, legacy_db, user_id=owner
    )

    with sqlite3.connect(legacy_db) as conn:
        audit = conn.execute(
            "SELECT plan_digest, from_user_id, to_user_id, owner_email, summary_json, "
            "applied_by, applied_at FROM legacy_ownership_migrations"
        ).fetchall()
    assert len(audit) == 1
    digest, from_id, to_id, email, summary, applied_by, applied_at = audit[0]
    assert (digest, from_id, to_id, email) == (plan.digest, placeholder, owner, _OWNER_EMAIL)
    assert applied_by == "operator"
    assert applied_at
    assert json.loads(summary) == {
        "plan_version": 1,
        "moved": {table: 1 for table in _PRE_ACCOUNT_TABLES},
        "left_behind": {},
        "backup_verified": True,
    }


def test_assignment_is_idempotent_and_cannot_be_redirected_afterwards(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    other = _sign_in(legacy_db, "other@example.com")
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    settled = _snapshot(legacy_db)

    again = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    assert again.status == "already_assigned"
    assert again.moved == {}
    assert _snapshot(legacy_db) == settled

    after = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    assert after.already_assigned
    assert not after.can_apply

    redirected = plan_legacy_ownership(legacy_db, owner_email="other@example.com")
    assert "already_assigned_to_different_owner" in redirected.blockers
    with pytest.raises(OwnershipBlockedError):
        apply_legacy_ownership(
            legacy_db,
            owner_email="other@example.com",
            confirm_digest=redirected.digest,
            applied_by="operator",
            backup_verified=True,
        )
    assert _snapshot(legacy_db) == settled
    assert get_deployment_owner_id(legacy_db) == owner
    assert other != owner


def test_a_failure_part_way_rolls_the_whole_assignment_back(
    legacy_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    before = _snapshot(legacy_db)

    def explode() -> str:
        raise RuntimeError("simulated crash after the rows moved")

    monkeypatch.setattr(legacy_ownership, "_now_iso", explode)
    with pytest.raises(RuntimeError, match="simulated crash"):
        apply_legacy_ownership(
            legacy_db,
            owner_email=_OWNER_EMAIL,
            confirm_digest=plan.digest,
            applied_by="operator",
            backup_verified=True,
        )
    assert _snapshot(legacy_db) == before

    monkeypatch.undo()
    retried = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    assert retried.status == "assigned"


def test_the_owners_existing_rows_win_and_conflicting_legacy_rows_stay_behind(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    placeholder = _placeholder_id(legacy_db)

    # The owner started using the app before the assignment was run.
    set_meta("programme_start_date", "2031-01-01", legacy_db, user_id=owner)
    advance_day(legacy_db, user_id=owner)
    set_active_programme(owner, "inferred", "mine", {"days": ["owner"]}, legacy_db)
    save_dashboard_insight('{"owner": true}', legacy_db, user_id=owner)
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            "INSERT INTO hevy_routines (user_id, routine_key, routine_id, content_hash) "
            "VALUES (?, 'synthetic-routine', 'owners-routine', 'owners-hash')",
            (owner,),
        )
        # Legacy programmes: one collides on its key, one on "single active",
        # one has neither conflict.
        for key, active in (("mine", 1), ("legacy-active", 1), ("legacy-spare", 0)):
            conn.execute(
                "INSERT INTO programmes "
                "(user_id, source, template_key, definition, active, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, '{}', ?, 'now', 'now')",
                (placeholder, key, active),
            )

    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    by_table = {t.table: (t.movable, t.left_behind) for t in plan.tables}
    assert by_table["hevy_meta"] == (0, 1)
    assert by_table["programme_state"] == (0, 1)
    assert by_table["dashboard_insights"] == (0, 1)
    assert by_table["hevy_routines"] == (0, 1)
    assert by_table["programmes"] == (1, 2)
    assert by_table["workout_history"] == (1, 0)

    result = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )

    assert result.status == "assigned"
    assert result.left_behind == {
        "hevy_meta": 1,
        "programme_state": 1,
        "dashboard_insights": 1,
        "hevy_routines": 1,
        "programmes": 2,
    }
    assert result.moved["programmes"] == 1
    # Nothing of the owner's was overwritten.
    assert get_meta("programme_start_date", legacy_db, user_id=owner) == "2031-01-01"
    assert get_current_day(legacy_db, user_id=owner) == 2
    active = get_active_programme(owner, legacy_db)
    assert active is not None
    assert active["template_key"] == "mine"
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute(
            "SELECT routine_id FROM hevy_routines WHERE user_id = ?", (owner,)
        ).fetchall() == [("owners-routine",)]
        # Left-behind rows are still on the (now retired) placeholder, intact.
        assert conn.execute(
            "SELECT count(*) FROM programmes WHERE user_id = ?", (placeholder,)
        ).fetchone() == (2,)
        summary = json.loads(
            conn.execute("SELECT summary_json FROM legacy_ownership_migrations").fetchone()[0]
        )
    assert summary["left_behind"] == result.left_behind


def test_programme_revisions_move_without_losing_their_immutability_guard(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    placeholder = _placeholder_id(legacy_db)
    with sqlite3.connect(legacy_db) as conn:
        programme_id = conn.execute(
            "INSERT INTO programmes "
            "(user_id, source, template_key, definition, active, created_at, updated_at) "
            "VALUES (?, 'inferred', 'legacy-revision', '{}', 0, 'now', 'now')",
            (placeholder,),
        ).lastrowid
        conn.execute(
            "INSERT INTO programme_revisions "
            "(user_id, programme_id, revision_number, reason, engine_version, "
            "settings_json, source_summary_json, created_at) "
            "VALUES (?, ?, 1, 'legacy_migration', 'test', '{}', '{}', 'now')",
            (placeholder, programme_id),
        )

    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    result = apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )

    assert result.moved["programme_revisions"] == 1
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute(
            "SELECT user_id FROM programme_revisions"
        ).fetchone() == (owner,)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE programme_revisions SET reason = 'tampered'")


def test_stored_credentials_move_with_their_owner_unless_the_owner_has_one(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    placeholder = _placeholder_id(legacy_db)
    save_user_api_key(placeholder, "hevy", "legacy-hevy-key", db_path=legacy_db)
    save_user_api_key(placeholder, "gemini", "legacy-gemini-key", db_path=legacy_db)
    save_user_api_key(owner, "gemini", "owners-gemini-key", db_path=legacy_db)

    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )

    hevy = get_user_api_key(owner, "hevy", db_path=legacy_db)
    gemini = get_user_api_key(owner, "gemini", db_path=legacy_db)
    assert hevy is not None
    assert hevy["api_key"] == "legacy-hevy-key"
    assert gemini is not None
    assert gemini["api_key"] == "owners-gemini-key"


def test_nothing_is_assigned_when_there_is_no_legacy_data(production: None, tmp_path: Path) -> None:
    path = str(tmp_path / "clean.db")
    init_db(path)  # a clean production database has no placeholder at all
    _sign_in(path)

    plan = plan_legacy_ownership(path, owner_email=_OWNER_EMAIL)
    assert plan.legacy_user_id is None
    assert plan.movable_rows == 0
    assert plan.blockers == ()
    assert not plan.can_apply

    result = apply_legacy_ownership(
        path,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    assert result.status == "nothing_to_assign"
    assert get_deployment_owner_id(path) is None
    assert _snapshot(path)["audit"] == []


def test_the_audit_trail_is_append_only(legacy_db: str) -> None:
    _sign_in(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    with sqlite3.connect(legacy_db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE legacy_ownership_migrations SET applied_by = 'someone'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM legacy_ownership_migrations")
        assert conn.execute(
            "SELECT count(*) FROM legacy_ownership_migrations"
        ).fetchone() == (1,)


def test_the_retired_placeholder_can_never_be_revived(legacy_db: str) -> None:
    _sign_in(legacy_db)
    placeholder = _placeholder_id(legacy_db)
    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )

    init_db(legacy_db)  # later starts must not reactivate or recreate it
    assert _status(legacy_db, placeholder) == "retired"
    with sqlite3.connect(legacy_db) as conn, pytest.raises(
        sqlite3.IntegrityError, match="cannot change status"
    ):
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (placeholder,))
    assert [u["email"] for u in get_all_users(legacy_db, include_inactive=True)].count(
        LEGACY_TENANT_EMAIL
    ) == 1


# ---------------------------------------------------------------------------
# Deployment-wide connector settings follow the verified owner
# ---------------------------------------------------------------------------


def test_deployment_wide_settings_belong_to_nobody_until_the_assignment(
    legacy_db: str,
) -> None:
    owner = _sign_in(legacy_db)
    other = _sign_in(legacy_db, "other@example.com")
    placeholder = _placeholder_id(legacy_db)

    assert get_deployment_owner_id(legacy_db) is None
    for user_id in (owner, other, placeholder):
        assert not is_deployment_owner(user_id, legacy_db)

    plan = plan_legacy_ownership(legacy_db, owner_email=_OWNER_EMAIL)
    apply_legacy_ownership(
        legacy_db,
        owner_email=_OWNER_EMAIL,
        confirm_digest=plan.digest,
        applied_by="operator",
        backup_verified=True,
    )
    assert is_deployment_owner(owner, legacy_db)
    assert not is_deployment_owner(other, legacy_db)
    assert not is_deployment_owner(placeholder, legacy_db)


def test_in_local_mode_the_deployment_owner_is_the_local_tenant(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "local.db")
    init_db(path)  # conftest: APP_ENV=test with the explicit anonymous flag
    placeholder = _placeholder_id(path)
    stranger = get_or_create_user("stranger@example.com", None, path)["id"]

    assert is_deployment_owner(placeholder, path)
    assert not is_deployment_owner(stranger, path)
