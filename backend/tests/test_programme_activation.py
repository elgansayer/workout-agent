"""Transactional activation through the programme domain service (#975)."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any

import database
import pytest
from dynamic_programme import ProgrammeCadence
from programme_domain import ProgrammeService
from programme_domain.weeks import plan_week
from programme_fixtures import (
    START,
    activate,
    build_preview,
    make_active,
    new_user,
)
from programme_service import (
    ProgrammeConflictError,
    ProgrammeValidationError,
    activate_programme,
)


def _rows(db: str, sql: str, *params: Any) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def test_activation_persists_revision_week_state_event_and_notification(tmp_path: Any) -> None:
    db, user_id, programme_id, preview = make_active(tmp_path)
    service = ProgrammeService(db)
    programme = service.get_programme(user_id, programme_id)

    assert programme is not None
    assert programme.status == "active"
    revision = programme.current_revision
    assert revision is not None and revision.revision_number == 1
    assert revision.reason == "activation"
    assert revision.engine_version == preview["engine_version"]
    audit = revision.source_summary["audit"]
    assert audit["preview_token"] == preview["preview_token"]
    assert audit["activated_by"] == user_id
    assert {item["routine_id"] for item in audit["source_hashes"]} == {"routine-a", "routine-b"}
    assert [routine.source_routine_id for routine in revision.routines] == ["routine-a", "routine-b"]
    assert revision.prescriptions and revision.blocks

    week = service.get_programme_week(user_id, programme_id, 1)
    assert week is not None and week.state is not None
    assert week.state.review_state == "planned"
    assert week.state.planned["week_number"] == 1
    assert week.state.actual["next_exposure"]["routine_position"] == 1

    events = service.list_adaptation_decisions(user_id, programme_id)
    assert [item.decision_type for item in events] == ["activation"]
    assert events[0].confirmed is True
    assert events[0].output["first_week"] == week.state.planned

    notifications = database.get_notifications(user_id, db_path=db)
    assert [item["title"] for item in notifications] == ["Hevy programme activated"]

    legacy = database.get_active_programme(user_id, db_path=db)
    assert legacy is not None and legacy["definition"]["preview_token"] == preview["preview_token"]
    assert database.get_programme_start_date(db, user_id=user_id) == START


def test_activation_archives_the_previous_programme_and_keeps_one_active(tmp_path: Any) -> None:
    db, user_id, first_id, _ = make_active(tmp_path)
    second_preview = build_preview(duration=6)
    second_id = activate(db, user_id, second_preview)

    service = ProgrammeService(db)
    first = service.get_programme(user_id, first_id)
    second = service.get_programme(user_id, second_id)
    assert first is not None and second is not None
    assert first.status == "archived" and first.archived_at
    assert second.status == "active"
    assert second.predecessor_programme_id == first_id
    # History is preserved: the archived programme keeps its revision and decisions.
    assert [rev.revision_number for rev in service.list_revision_history(user_id, first_id)] == [1]
    assert service.list_adaptation_decisions(user_id, first_id)
    actives = _rows(db, "SELECT id FROM programmes WHERE user_id = ? AND status = 'active'", user_id)
    assert [row["id"] for row in actives] == [second_id]
    assert database.get_active_programme(user_id, db_path=db)["id"] == second_id


def test_stale_token_hash_or_engine_version_is_refused_without_writing(tmp_path: Any) -> None:
    db = str(tmp_path / "stale.db")
    database.init_db(db)
    user_id = new_user(db)
    preview = build_preview()

    with pytest.raises(ProgrammeConflictError) as stale_token:
        activate_programme(user_id, preview, db_path=db, presented_token="0" * 64)
    assert "changed" in stale_token.value.message.lower()

    stale_hashes = [
        {"routine_id": item["id"], "source_hash": "stale"} for item in preview["source_snapshots"]
    ]
    with pytest.raises(ProgrammeConflictError) as stale_hash:
        activate_programme(
            user_id,
            preview,
            db_path=db,
            presented_token=preview["preview_token"],
            presented_source_hashes=stale_hashes,
        )
    changed = stale_hash.value.details["conflict"]["changed_routines"]
    assert {item["routine_id"] for item in changed} == {"routine-a", "routine-b"}

    old_engine = {**preview, "engine_version": "hevy-programme-v0"}
    with pytest.raises(ProgrammeConflictError) as stale_engine:
        activate_programme(
            user_id, old_engine, db_path=db, presented_token=preview["preview_token"]
        )
    assert stale_engine.value.details["conflict"]["engine_changed"] is True

    assert _rows(db, "SELECT id FROM programmes") == []
    assert database.get_notifications(user_id, db_path=db) == []


def test_blocking_violations_are_refused(tmp_path: Any) -> None:
    db = str(tmp_path / "blocked.db")
    database.init_db(db)
    user_id = new_user(db)
    preview = build_preview()
    preview["blocking_violations"] = [{"code": "synthetic", "message": "blocked"}]
    with pytest.raises(ProgrammeValidationError) as error:
        activate_programme(user_id, preview, db_path=db, presented_token=preview["preview_token"])
    assert error.value.details["violations"]
    assert _rows(db, "SELECT id FROM programmes") == []


def test_shared_validator_reruns_against_the_tenants_current_rules(tmp_path: Any) -> None:
    """Rules saved after the preview was built still gate activation."""

    db = str(tmp_path / "rules.db")
    database.init_db(db)
    user_id = new_user(db)
    other_id = new_user(db, "athlete-b@example.test")
    preview = build_preview()
    database.save_training_rules(user_id, {"volume": {"max_sets_per_exercise": 1}}, db)

    with pytest.raises(ProgrammeValidationError):
        activate_programme(user_id, preview, db_path=db, presented_token=preview["preview_token"])
    assert _rows(db, "SELECT id FROM programmes WHERE user_id = ?", user_id) == []
    # Another tenant's rules are never applied to this tenant, and vice versa.
    activate_programme(other_id, preview, db_path=db, presented_token=preview["preview_token"])
    assert ProgrammeService(db).get_active_programme(other_id) is not None


def test_failure_inside_the_transaction_rolls_everything_back(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, user_id, first_id, _ = make_active(tmp_path)
    before = {
        table: len(_rows(db, f"SELECT id FROM {table}"))
        for table in (
            "programmes",
            "programme_revisions",
            "programme_week_states",
            "adaptation_decisions",
            "notifications",
        )
    }
    from programme_domain import repository as repo_module

    original = repo_module.ProgrammeRepository._save_week_state_in

    def exploding(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(repo_module.ProgrammeRepository, "_save_week_state_in", exploding)
    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        activate(db, user_id, build_preview(duration=6))
    monkeypatch.setattr(repo_module.ProgrammeRepository, "_save_week_state_in", original)

    after = {table: len(_rows(db, f"SELECT id FROM {table}")) for table in before}
    assert after == before
    service = ProgrammeService(db)
    active = service.get_active_programme(user_id)
    assert active is not None and active.id == first_id and active.status == "active"


def test_concurrent_activations_leave_exactly_one_active_programme(tmp_path: Any) -> None:
    db = str(tmp_path / "race.db")
    database.init_db(db)
    user_id = new_user(db)
    previews = [build_preview(duration=weeks) for weeks in (4, 5, 6, 7)]

    def run(preview: dict[str, Any]) -> str:
        try:
            activate_programme(
                user_id, preview, db_path=db, presented_token=preview["preview_token"]
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(run, previews))

    assert outcomes.count("ok") == 4, outcomes
    rows = _rows(db, "SELECT id, status FROM programmes WHERE user_id = ?", user_id)
    assert len(rows) == 4
    assert [row["status"] for row in rows].count("active") == 1
    assert [row["status"] for row in rows].count("archived") == 3
    active = ProgrammeService(db).get_active_programme(user_id)
    assert active is not None
    assert database.get_active_programme(user_id, db_path=db)["id"] == active.id


def test_activation_is_tenant_scoped(tmp_path: Any) -> None:
    db = str(tmp_path / "tenants.db")
    database.init_db(db)
    first = new_user(db)
    second = new_user(db, "athlete-b@example.test")
    first_id = activate(db, first)
    second_id = activate(db, second, build_preview(duration=6))

    service = ProgrammeService(db)
    assert service.get_programme(first, second_id) is None
    assert service.get_programme(second, first_id) is None
    assert service.get_active_programme(first).id == first_id  # type: ignore[union-attr]
    assert service.get_active_programme(second).id == second_id  # type: ignore[union-attr]
    assert database.get_notifications(first, db_path=db)
    assert len(database.get_notifications(second, db_path=db)) == 1


def test_first_week_state_is_reproducible_from_the_stored_revision(tmp_path: Any) -> None:
    db, user_id, programme_id, _ = make_active(tmp_path)
    service = ProgrammeService(db)
    programme = service.get_programme(user_id, programme_id)
    assert programme is not None and programme.current_revision is not None
    stored = service.get_programme_week(user_id, programme_id, 1)
    assert stored is not None and stored.state is not None

    from programme_domain.activation import definition_from_revision

    definition = definition_from_revision(programme, programme.current_revision)
    replay = plan_week(
        definition,
        start_date=programme.start_date,  # type: ignore[arg-type]
        duration_weeks=programme.duration_weeks or 0,
        week_number=1,
        timezone_name="UTC",
        rotation_pointer=0,
    ).to_dict()
    replay["rotation_pointer"] = 0
    assert replay == stored.state.planned


@pytest.mark.parametrize("mode,weekdays", [("rolling", []), ("weekly", [1, 3])])
def test_first_week_follows_the_configured_cadence(
    tmp_path: Any, mode: str, weekdays: list[int]
) -> None:
    cadence = ProgrammeCadence(mode=mode, weekdays=weekdays)  # type: ignore[arg-type]
    db, user_id, programme_id, _ = make_active(tmp_path, cadence=cadence)
    week = ProgrammeService(db).get_programme_week(user_id, programme_id, 1)
    assert week is not None and week.state is not None
    planned = week.state.planned
    assert planned["cadence"] == ("rolling" if mode == "rolling" else "calendar")
    if mode == "weekly":
        assert [item["scheduled_date"] for item in planned["exposures"]] == [
            "2026-09-01",
            "2026-09-03",
        ]
    assert planned["next_exposure"]["routine_position"] == 1


def test_activation_uses_the_tenant_timezone_for_dates(tmp_path: Any) -> None:
    db = str(tmp_path / "tz.db")
    database.init_db(db)
    user_id = new_user(db, timezone_name="Pacific/Auckland")
    preview = build_preview(
        cadence=ProgrammeCadence(mode="weekly", weekdays=[1, 3]),
    )
    activate_programme(
        user_id,
        preview,
        db_path=db,
        presented_token=preview["preview_token"],
        now=datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc),
    )
    programme = ProgrammeService(db).get_active_programme(user_id)
    assert programme is not None and programme.start_date == date(2026, 9, 1)
    event = ProgrammeService(db).list_adaptation_decisions(user_id, programme.id)[0]
    assert event.inputs["timezone"] == "Pacific/Auckland"
    assert event.inputs["activated_at"].startswith("2026-09-01T23:30")
