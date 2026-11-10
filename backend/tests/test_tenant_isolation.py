"""Owner identity, scoped keys, per-owner locks and re-authorizing job execution."""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import insight_cron
import main
import pytest
from config import ConfigError
from cryptography.fernet import Fernet
from database import get_or_create_user, init_db, save_user_api_key
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tenant_isolation import (
    JobStatus,
    OwnedContext,
    OwnedJob,
    OwnerIdentity,
    OwnerLocks,
    OwnershipError,
    authorize_owner,
    owner_scoped_key,
    run_owned_job,
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "tenant.db")
    init_db(path)
    return path


def _user(db: str, name: str) -> str:
    return get_or_create_user(f"{name}@example.com", name.title(), db)["id"]


# ---------------------------------------------------------------------------
# OwnerIdentity
# ---------------------------------------------------------------------------


def test_owner_identity_is_immutable() -> None:
    owner = OwnerIdentity("user-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        owner.user_id = "user-2"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    ["", " leading", "trailing ", "line\nbreak", "tab\tchar", "x" * 129, None, 7],
)
def test_owner_identity_rejects_blank_or_malformed_ids(value: Any) -> None:
    with pytest.raises(OwnershipError):
        OwnerIdentity(value)


def test_owner_identity_compares_by_value() -> None:
    assert OwnerIdentity("user-1") == OwnerIdentity("user-1")
    assert OwnerIdentity("user-1") != OwnerIdentity("user-2")


# ---------------------------------------------------------------------------
# owner_scoped_key
# ---------------------------------------------------------------------------


def test_scoped_key_binds_the_owner_as_the_second_segment() -> None:
    assert owner_scoped_key(OwnerIdentity("u1"), "cache", "plan") == "cache:u1:plan"


def test_scoped_key_differs_between_owners_for_identical_parts() -> None:
    first = owner_scoped_key(OwnerIdentity("u1"), "cursor", "hevy", "conn")
    second = owner_scoped_key(OwnerIdentity("u2"), "cursor", "hevy", "conn")
    assert first != second


def test_scoped_key_delimiters_cannot_shift_ownership() -> None:
    """('a', 'b:c') and ('a:b', 'c') must not collapse to one key."""
    one = owner_scoped_key(OwnerIdentity("a"), "cache", "b:c")
    two = owner_scoped_key(OwnerIdentity("a:b"), "cache", "c")
    assert one != two


def test_scoped_key_requires_a_real_owner_identity() -> None:
    with pytest.raises(OwnershipError):
        owner_scoped_key("user-1", "cache", "plan")  # type: ignore[arg-type]


@pytest.mark.parametrize("namespace", ["", "Cache", "a:b", "-x", "has space"])
def test_scoped_key_rejects_unsafe_namespaces(namespace: str) -> None:
    with pytest.raises(ValueError):
        owner_scoped_key(OwnerIdentity("u1"), namespace, "plan")


def test_scoped_key_rejects_empty_parts() -> None:
    with pytest.raises(ValueError):
        owner_scoped_key(OwnerIdentity("u1"), "cache", "")


# Letters, numbers, punctuation and symbols are always printable, so every
# generated id is one OwnerIdentity accepts (':' and '%' are punctuation).
_ids = st.text(
    alphabet=st.characters(categories=("L", "N", "P", "S")),
    min_size=1,
    max_size=20,
)
_parts = st.lists(st.text(min_size=1, max_size=12), max_size=3)


@settings(max_examples=120, deadline=None)
@given(_ids, _parts, _ids, _parts)
@example("a", ["b:c"], "a:b", ["c"])
@example("a", ["b", "c"], "a", ["b%3Ac"])
@example("a b", ["x y"], "a b", ["x y"])
def test_scoped_keys_are_injective_per_owner_and_parts(
    owner_a: str, parts_a: list[str], owner_b: str, parts_b: list[str]
) -> None:
    key_a = owner_scoped_key(OwnerIdentity(owner_a), "cache", *parts_a)
    key_b = owner_scoped_key(OwnerIdentity(owner_b), "cache", *parts_b)
    assert (key_a == key_b) == ((owner_a, parts_a) == (owner_b, parts_b))


# ---------------------------------------------------------------------------
# authorize_owner
# ---------------------------------------------------------------------------


def test_authorize_owner_returns_the_stored_identity(db: str) -> None:
    user_id = _user(db, "alpha")
    assert authorize_owner(db, user_id) == OwnerIdentity(user_id)


@pytest.mark.parametrize("claimed", [None, ""])
def test_authorize_owner_requires_an_explicit_owner(db: str, claimed: Any) -> None:
    with pytest.raises(OwnershipError, match="explicit owner"):
        authorize_owner(db, claimed)


def test_authorize_owner_rejects_unknown_users(db: str) -> None:
    with pytest.raises(OwnershipError, match="does not exist"):
        authorize_owner(db, "forged-owner")


def test_authorize_owner_rejects_malformed_claims(db: str) -> None:
    with pytest.raises(OwnershipError):
        authorize_owner(db, "bad\nid")


@pytest.mark.parametrize(
    "outage",
    [
        sqlite3.OperationalError("synthetic outage"),
        PermissionError("synthetic unusable database directory"),
    ],
)
def test_authorize_owner_fails_closed_when_the_store_is_unreachable(
    db: str, monkeypatch: pytest.MonkeyPatch, outage: Exception
) -> None:
    def _boom(user_id: str, db_path: str) -> None:
        raise outage

    monkeypatch.setattr("tenant_isolation.get_user_by_id", _boom)
    with pytest.raises(OwnershipError, match="could not be verified"):
        authorize_owner(db, "any-user")


# ---------------------------------------------------------------------------
# OwnerLocks
# ---------------------------------------------------------------------------


def test_lock_is_exclusive_per_key_and_released_afterwards() -> None:
    locks = OwnerLocks()
    with locks.try_acquire("k") as first:
        assert first is True
        with locks.try_acquire("k") as second:
            assert second is False
    with locks.try_acquire("k") as again:
        assert again is True


def test_lock_does_not_span_different_keys() -> None:
    locks = OwnerLocks()
    with locks.try_acquire("owner-a") as a, locks.try_acquire("owner-b") as b:
        assert (a, b) == (True, True)


def test_lock_is_released_when_the_critical_section_raises() -> None:
    locks = OwnerLocks()
    with pytest.raises(RuntimeError), locks.try_acquire("k"):
        raise RuntimeError("synthetic")
    with locks.try_acquire("k") as acquired:
        assert acquired is True


def test_job_lock_keys_are_owner_scoped() -> None:
    first = OwnedJob("coaching", OwnerIdentity("u1")).key
    second = OwnedJob("coaching", OwnerIdentity("u2")).key
    assert first != second
    assert first.startswith("job:u1:")


# ---------------------------------------------------------------------------
# run_owned_job
# ---------------------------------------------------------------------------


def test_run_owned_job_gives_work_the_reauthorized_identity(db: str) -> None:
    user_id = _user(db, "alpha")
    job = OwnedJob("coaching", OwnerIdentity(user_id))
    seen: list[OwnerIdentity] = []

    outcome = run_owned_job(job, db, lambda owner: seen.append(owner) or 7)

    assert outcome.status is JobStatus.COMPLETED
    assert outcome.result == 7
    assert seen == [OwnerIdentity(user_id)]


def test_run_owned_job_denies_an_unknown_owner_without_running(db: str) -> None:
    work = MagicMock()
    outcome = run_owned_job(OwnedJob("coaching", OwnerIdentity("forged")), db, work)

    assert outcome.status is JobStatus.DENIED
    work.assert_not_called()


def test_run_owned_job_denies_an_owner_removed_after_queueing(db: str) -> None:
    user_id = _user(db, "alpha")
    job = OwnedJob("coaching", OwnerIdentity(user_id))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    work = MagicMock()

    outcome = run_owned_job(job, db, work)

    assert outcome.status is JobStatus.DENIED
    work.assert_not_called()


def test_run_owned_job_contains_failures_without_leaking_their_message(
    db: str,
) -> None:
    job = OwnedJob("coaching", OwnerIdentity(_user(db, "alpha")))

    def _fail(owner: OwnerIdentity) -> None:
        raise RuntimeError("secret-token-123")

    outcome = run_owned_job(job, db, _fail)

    assert outcome.status is JobStatus.FAILED
    assert outcome.error == "RuntimeError"
    assert "secret-token-123" not in repr(outcome)


def test_run_owned_job_skips_an_overlapping_duplicate_for_the_same_owner(
    db: str,
) -> None:
    job = OwnedJob("coaching", OwnerIdentity(_user(db, "alpha")))
    locks = OwnerLocks()
    work = MagicMock()

    with locks.try_acquire(job.key):
        outcome = run_owned_job(job, db, work, locks=locks)

    assert outcome.status is JobStatus.SKIPPED
    work.assert_not_called()


def test_run_owned_job_never_blocks_one_owner_on_another(db: str) -> None:
    job_a = OwnedJob("coaching", OwnerIdentity(_user(db, "alpha")))
    job_b = OwnedJob("coaching", OwnerIdentity(_user(db, "bravo")))
    locks = OwnerLocks()

    with locks.try_acquire(job_a.key):
        outcome = run_owned_job(job_b, db, lambda owner: owner.user_id, locks=locks)

    assert outcome.status is JobStatus.COMPLETED
    assert outcome.result == job_b.owner.user_id


def test_owned_job_and_context_are_immutable(db: str) -> None:
    owner = OwnerIdentity(_user(db, "alpha"))
    job = OwnedJob("coaching", owner)
    context = OwnedContext(owner, "daily-insight", {"metrics": []})

    with pytest.raises(dataclasses.FrozenInstanceError):
        job.owner = OwnerIdentity("someone-else")  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.owner = OwnerIdentity("someone-else")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Worker entrypoints refuse an unproven owner before touching any tenant data
# ---------------------------------------------------------------------------


def _config(db: str) -> Any:
    from config import Config

    return Config(
        hevy_api_key="synthetic-server-hevy-key",
        gemini_api_key="synthetic-server-gemini-key",
        gemini_model="gemini-3.8-flash",
        health_connect_file=None,
        database_path=db,
        hevy_sync_routines=True,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id="synthetic-client-id",
        google_health_client_secret="synthetic-client-secret",
        google_health_refresh_token="synthetic-env-token",
        self_review_enabled=False,
        self_review_weekday=6,
    )


def _spy_on_every_main_side_effect(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    spies = {
        name: MagicMock(name=name)
        for name in (
            "_scope_config_to_user",
            "_resolve_provider",
            "sync_routines",
            "fetch_latest_workout",
            "save_daily_log",
            "save_notification",
            "save_workout",
            "generate_next_workout",
        )
    }
    for name, spy in spies.items():
        monkeypatch.setattr(f"main.{name}", spy)
    spies["sync_body_metrics"] = MagicMock(name="sync_body_metrics")
    monkeypatch.setattr(
        "main.google_health_client.sync_body_metrics", spies["sync_body_metrics"]
    )
    return spies


def test_run_refuses_a_forged_user_id_before_any_side_effect(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    spies = _spy_on_every_main_side_effect(monkeypatch)

    assert main.run(preview=False, user_id="forged-owner") == 1

    for name, spy in spies.items():
        assert not spy.called, f"{name} ran for an unauthorized owner"


def test_run_authorizes_an_owner_taken_from_the_environment(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    monkeypatch.setenv("SCHEDULER_USER_ID", "forged-env-owner")
    spies = _spy_on_every_main_side_effect(monkeypatch)

    assert main.run(preview=False) == 1
    assert not spies["_scope_config_to_user"].called
    assert not spies["sync_body_metrics"].called


def test_run_fails_closed_when_the_user_store_is_unreachable(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    spies = _spy_on_every_main_side_effect(monkeypatch)

    def _boom(user_id: str, db_path: str) -> None:
        raise sqlite3.OperationalError("synthetic outage")

    monkeypatch.setattr("tenant_isolation.get_user_by_id", _boom)

    assert main.run(preview=False, user_id=_user(db, "alpha")) == 1
    assert not spies["_scope_config_to_user"].called


def test_insight_jobs_refuse_a_forged_owner_before_reading_or_generating(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(db)
    provider_factory = MagicMock(name="resolve_provider")
    reads = [MagicMock(name=n) for n in ("metrics", "logs", "history")]
    writes = [MagicMock(name=n) for n in ("insight", "correlation", "notification")]
    monkeypatch.setattr("insight_cron.resolve_provider", provider_factory)
    for target, spy in zip(
        ("get_body_metrics", "get_daily_logs", "get_progress_history"), reads
    ):
        monkeypatch.setattr(f"insight_cron.{target}", spy)
    for target, spy in zip(
        ("save_dashboard_insight", "save_deep_correlation", "save_notification"),
        writes,
    ):
        monkeypatch.setattr(f"insight_cron.{target}", spy)

    with pytest.raises(OwnershipError):
        insight_cron.generate_daily_header(config, user_id="forged-owner")
    with pytest.raises(OwnershipError):
        insight_cron.generate_weekly_correlations(config, user_id="forged-owner")

    provider_factory.assert_not_called()
    for spy in (*reads, *writes):
        spy.assert_not_called()


def test_insight_jobs_never_fall_back_to_unscoped_reads(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No owner and no resolvable legacy tenant must stop the job, not widen it."""
    config = _config(db)
    unscoped_read = MagicMock(name="get_body_metrics")
    monkeypatch.setattr("insight_cron._resolve_insight_user", lambda path: None)
    monkeypatch.setattr("insight_cron.get_body_metrics", unscoped_read)

    with pytest.raises(OwnershipError):
        insight_cron.generate_daily_header(config)

    unscoped_read.assert_not_called()


def test_insight_cli_exits_non_zero_for_a_forged_owner(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_factory = MagicMock(name="resolve_provider")
    monkeypatch.setattr("insight_cron.Config.load", lambda: _config(db))
    monkeypatch.setattr("insight_cron.resolve_provider", provider_factory)
    monkeypatch.setattr(
        sys, "argv", ["insight_cron.py", "--daily", "--user-id", "nope"]
    )

    with pytest.raises(SystemExit) as exit_info:
        insight_cron.main()

    assert exit_info.value.code == 1
    provider_factory.assert_not_called()


# ---------------------------------------------------------------------------
# --sync-history authorizes its owner before any credential is read
# ---------------------------------------------------------------------------


def _record_history_rebuilds(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | None = None
) -> list[tuple[str, str | None, str | None]]:
    rebuilds: list[tuple[str, str | None, str | None]] = []

    def _sync_all(
        api_key: str, db_path: str | None = None, *, user_id: str | None = None
    ) -> dict[str, Any]:
        rebuilds.append((api_key, db_path, user_id))
        return result or {"workouts_found": 1, "processed": 1}

    monkeypatch.setattr("scripts.sync_history.sync_all", _sync_all)
    return rebuilds


def test_sync_history_cli_refuses_a_forged_owner_before_reading_credentials(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    rebuilds = _record_history_rebuilds(monkeypatch)
    scope = MagicMock(name="_scope_config_to_user")
    monkeypatch.setattr("main._scope_config_to_user", scope)

    assert main._sync_history("forged-owner") == 1

    scope.assert_not_called()
    assert rebuilds == []


def test_sync_history_cli_authorizes_an_owner_taken_from_the_environment(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    monkeypatch.setenv("SCHEDULER_USER_ID", "forged-env-owner")
    rebuilds = _record_history_rebuilds(monkeypatch)
    scope = MagicMock(name="_scope_config_to_user")
    monkeypatch.setattr("main._scope_config_to_user", scope)

    assert main._sync_history() == 1

    scope.assert_not_called()
    assert rebuilds == []


def test_sync_history_cli_fails_closed_when_the_user_store_is_unreachable(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user(db, "alpha")
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    rebuilds = _record_history_rebuilds(monkeypatch)

    def _boom(user_id: str, db_path: str) -> None:
        raise sqlite3.OperationalError("synthetic outage")

    monkeypatch.setattr("tenant_isolation.get_user_by_id", _boom)

    assert main._sync_history(owner) == 1
    assert rebuilds == []


def test_sync_history_cli_rebuilds_with_only_the_authorized_owners_key(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    alpha, beta = _user(db, "alpha"), _user(db, "beta")
    save_user_api_key(alpha, "hevy", "synthetic-alpha-hevy-key", db_path=db)
    save_user_api_key(beta, "hevy", "synthetic-beta-hevy-key", db_path=db)
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    rebuilds = _record_history_rebuilds(monkeypatch)

    assert main._sync_history(alpha) == 0

    assert rebuilds == [("synthetic-alpha-hevy-key", db, alpha)]


def test_sync_history_cli_never_lends_the_server_key_to_a_non_legacy_owner(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    rebuilds = _record_history_rebuilds(monkeypatch)

    assert main._sync_history(_user(db, "keyless")) == 1

    assert rebuilds == []


def test_sync_history_cli_exits_non_zero_when_the_rebuild_reports_an_error(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    owner = _user(db, "alpha")
    save_user_api_key(owner, "hevy", "synthetic-alpha-hevy-key", db_path=db)
    monkeypatch.setattr("main.Config.load", lambda: _config(db))
    _record_history_rebuilds(monkeypatch, {"error": "synthetic Hevy outage"})

    assert main._sync_history(owner) == 1


def test_sync_history_cli_reports_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _bad_config() -> None:
        raise ConfigError("synthetic configuration error")

    monkeypatch.setattr("main.Config.load", _bad_config)

    assert main._sync_history("anyone") == 1
