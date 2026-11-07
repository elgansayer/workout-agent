"""Cross-tenant IDOR matrix for every user-scoped surface.

Application-level ``WHERE user_id = ?`` filters are only trustworthy if every
operations class is exercised against a *different* tenant.  This matrix puts
two authenticated owners (``owner_a`` and ``owner_b``) side by side and proves
that ``owner_b`` cannot reach ``owner_a``'s data through any of:

* **read** - listing/reloading records owned by the other tenant,
* **write** - upserting over the other tenant's records,
* **export** - exporting a record to another tenant's provider,
* **delete** - removing the other tenant's rows by primary key,
* **background jobs** - minting an owner for an arbitrary identifier,
* **caches** - reviving a cached owner/session for another request,
* **indirect object references** - guessing a global id (notification,
  exercise, context id) instead of an owner-scoped key.

Each test states the attacker (``owner_b``) and the victim (``owner_a``) so a
regression pinpoints which operations class lost its tenant boundary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from database import (
    delete_notification,
    delete_user_api_key,
    get_active_programme,
    get_body_metrics,
    get_chat_messages,
    get_daily_logs,
    get_deep_correlation,
    get_meta,
    get_notifications,
    get_personal_records,
    get_programme_start_date,
    get_reasoning_log,
    get_recent_hevy_logs,
    get_user_api_key,
    init_db,
    mark_notification_read,
    save_body_metrics,
    save_chat_message,
    save_daily_log,
    save_deep_correlation,
    save_notification,
    save_reasoning_log,
    save_user_api_key,
    save_workout,
    set_active_programme,
    set_meta,
)
from ownership import (
    AuthenticatedOwner,
    InvalidOwnerContextError,
    UnknownOwnerError,
    owner_for_user,
    owner_id,
)
from tenant_fixtures import create_owner


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "idor.db")
    init_db(db)
    return db


@pytest.fixture()
def tenants(tmp_path: Path) -> tuple[str, AuthenticatedOwner, AuthenticatedOwner]:
    db = _db(tmp_path)
    owner_a = create_owner(db, "victim@example.test")
    owner_b = create_owner(db, "attacker@example.test")
    return db, owner_a, owner_b


# ---------------------------------------------------------------------------
# READ: cross-tenant reads return nothing belonging to the other owner
# ---------------------------------------------------------------------------


def test_idor_read_isolates_every_listing_surface(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants
    save_workout({"secret": "a-workout"}, db, owner=owner_a)
    save_chat_message("user", "a-secret", db, owner=owner_a)
    save_daily_log("2026-01-01", 1, "A", "A", "a-plan", "a-life", db, owner=owner_a)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, owner=owner_a)
    save_reasoning_log("ctx-a", "Squat", "a-reasoning", db, owner=owner_a)
    save_deep_correlation("a-narrative", db, owner=owner_a)
    set_meta("programme_start_date", "2026-01-01", db, owner=owner_a)

    # The attacker reads every surface and must see an empty result.
    assert get_recent_hevy_logs(limit=10, db_path=db, owner=owner_b) == []
    assert get_chat_messages(db_path=db, owner=owner_b) == []
    assert get_daily_logs(db_path=db, owner=owner_b) == []
    assert get_body_metrics(db_path=db, owner=owner_b) == []
    assert get_reasoning_log("ctx-a", db, owner=owner_b) is None
    assert get_deep_correlation(db, owner=owner_b) is None
    assert get_meta("programme_start_date", db, owner=owner_b) is None

    # The victim still sees their own records.
    assert len(get_recent_hevy_logs(limit=10, db_path=db, owner=owner_a)) == 1
    assert get_meta("programme_start_date", db, owner=owner_a) == "2026-01-01"
    assert get_programme_start_date(db, owner=owner_a).isoformat() == "2026-01-01"


# ---------------------------------------------------------------------------
# WRITE: a tenant cannot overwrite another tenant's row through an upsert
# ---------------------------------------------------------------------------


def test_idor_write_cannot_upsert_over_another_tenant(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants
    set_meta("programme_start_date", "2026-01-01", db, owner=owner_a)
    save_deep_correlation("a-narrative", db, owner=owner_a)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, owner=owner_a)

    # Same logical keys as the victim, written by the attacker.
    set_meta("programme_start_date", "2026-09-09", db, owner=owner_b)
    save_deep_correlation("b-narrative", db, owner=owner_b)
    save_body_metrics({"weight_kg": 99.0}, "2026-01-01", db, owner=owner_b)

    # Victim's rows are untouched; the attacker got their own rows.
    assert get_meta("programme_start_date", db, owner=owner_a) == "2026-01-01"
    assert get_deep_correlation(db, owner=owner_a) == "a-narrative"
    assert get_body_metrics(db_path=db, owner=owner_a)[0]["weight_kg"] == 80.0
    assert get_meta("programme_start_date", db, owner=owner_b) == "2026-09-09"
    assert get_deep_correlation(db, owner=owner_b) == "b-narrative"


# ---------------------------------------------------------------------------
# DELETE: cross-tenant deletes are no-ops, including by guessed primary key
# ---------------------------------------------------------------------------


def test_idor_delete_refuses_rows_owned_by_another_tenant(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants
    notification_id = save_notification(owner_a, "Secret", "victim-only", db_path=db)

    # Indirect object reference: the attacker guesses the victim's global id.
    assert delete_notification(notification_id, owner_b, db_path=db) is False
    assert mark_notification_read(notification_id, owner_b, db_path=db) is False
    assert len(get_notifications(owner_a, db_path=db)) == 1
    assert get_notifications(owner_a, db_path=db)[0]["is_read"] == 0

    # The owner of the row can still delete it.
    assert delete_notification(notification_id, owner_a, db_path=db) is True
    assert get_notifications(owner_a, db_path=db) == []


def test_idor_delete_cannot_remove_another_tenants_api_key(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, owner_a, owner_b = tenants
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    save_user_api_key(owner_a, "openai", "sk-victim", db_path=db)

    delete_user_api_key(owner_b, "openai", db_path=db)

    # Attacker deletion targeted their own (empty) key store, not the victim's.
    assert get_user_api_key(owner_a, "openai", db_path=db) is not None
    assert get_user_api_key(owner_b, "openai", db_path=db) is None


# ---------------------------------------------------------------------------
# EXPORT: a record can only be exported for its authenticated owner
# ---------------------------------------------------------------------------


def test_idor_export_refuses_another_tenants_provider() -> None:
    from training_export import ExportedWorkout

    workout = ExportedWorkout(
        user_id=1,
        provider="hevy",
        name="Push A",
        source_programme_id="prog-1",
        explicit_user_approval=True,
    )
    with pytest.raises(ValueError, match="another user's provider"):
        workout.validate(authenticated_user_id=2)
    # The real owner can export.
    workout.validate(authenticated_user_id=1)


# ---------------------------------------------------------------------------
# BACKGROUND JOBS: an owner can only be minted for a stored tenant
# ---------------------------------------------------------------------------


def test_idor_background_job_cannot_mint_an_arbitrary_owner(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants

    # A job resolving the attacker's identifier gets the attacker, never a
    # victim context.
    attacker = owner_for_user(db, owner_id(owner_b))
    assert attacker == owner_b
    assert attacker != owner_a

    # An identifier that is not a stored tenant fails closed, so a job cannot
    # be pointed at a record for a phantom or another owner.
    with pytest.raises(UnknownOwnerError):
        owner_for_user(db, "not-a-stored-user")


# ---------------------------------------------------------------------------
# CACHES: a minted owner cannot be revived for another request/tenant
# ---------------------------------------------------------------------------


def test_idor_cached_owner_cannot_be_revived_for_another_tenant(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    import copy
    import pickle

    _db_path, owner_a, owner_b = tenants

    # A cached/forged context cannot be copied onto a different identity, and a
    # duck-typed stand-in is rejected at the SQL boundary.
    forged = MagicMock()
    forged.user_id = owner_b.user_id
    for candidate in (
        forged,
        owner_b.user_id,  # a bare string from a cache/session
        None,
        object(),
    ):
        with pytest.raises(InvalidOwnerContextError):
            owner_id(candidate)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        pickle.loads(pickle.dumps(owner_a))
    # Copying is allowed only as an identity-preserving no-op, so a copy can
    # never be retargeted onto another tenant.
    assert copy.deepcopy(owner_a, {}) is owner_a
    assert copy.copy(owner_a) is owner_a

    # Only the authenticated owner value carries the right tenant.
    assert owner_id(owner_a) != owner_id(owner_b)


# ---------------------------------------------------------------------------
# INDIRECT OBJECT REFERENCES: composite-key lookups stay tenant scoped
# ---------------------------------------------------------------------------


def test_idor_indirect_reference_does_not_cross_tenants(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants
    save_workout({"exercise": "Squat"}, db, owner=owner_a)
    save_notification(owner_a, "Title", "message", db_path=db)

    # Personal records are derived per owner, not from a shared row id.
    assert get_personal_records(db, owner=owner_b) == []
    # Notifications are keyed by owner even though rows share a global counter.
    ids_a = {row["id"] for row in get_notifications(owner_a, db_path=db)}
    ids_b = {row["id"] for row in get_notifications(owner_b, db_path=db)}
    assert ids_a and not ids_b


# ---------------------------------------------------------------------------
# PROGRAMMES: the active programme is a per-tenant singleton
# ---------------------------------------------------------------------------


def test_idor_active_programme_is_per_tenant(
    tenants: tuple[str, AuthenticatedOwner, AuthenticatedOwner],
) -> None:
    db, owner_a, owner_b = tenants
    set_active_programme(
        owner_a,
        source="hevy",
        template_key="hevy-prog-a",
        definition={"name": "A"},
        db_path=db,
    )

    assert get_active_programme(owner_b, db_path=db) is None
    active_a = get_active_programme(owner_a, db_path=db)
    assert active_a is not None and active_a["template_key"] == "hevy-prog-a"
