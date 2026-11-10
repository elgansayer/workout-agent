"""Archive, job-service, maintenance and worker tests for the account export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import account_export as ax
import account_export_sections as sections
import account_export_store as store
import pytest
from account_export_support import (
    archive_json,
    archive_text,
    secret_values,
    seed_tenant,
)
from database import init_db

REQUIRED_SECTIONS = [
    "profile",
    "programmes",
    "workouts",
    "metrics",
    "check_ins",
    "connectors",
    "notifications",
    "coaching",
    "audit",
]


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.delenv("ACCOUNT_EXPORT_ENABLED", raising=False)
    monkeypatch.delenv("ACCOUNT_EXPORT_TTL_HOURS", raising=False)
    monkeypatch.delenv("ACCOUNT_EXPORT_DIR", raising=False)
    db_path = str(tmp_path / "export.db")
    init_db(db_path)
    alpha = seed_tenant(
        db_path, email="alpha@example.com", display_name="Alpha Example", tag="alpha"
    )
    bravo = seed_tenant(
        db_path, email="bravo@example.com", display_name="Bravo Example", tag="bravo"
    )
    return {
        "db_path": db_path,
        "export_dir": tmp_path / "exports",
        "alpha": alpha,
        "bravo": bravo,
    }


def _request(world: dict[str, Any], who: str = "alpha", **kwargs: Any) -> store.ExportJob:
    job, _created = ax.request_export(
        world[who], db_path=world["db_path"], export_dir=world["export_dir"], **kwargs
    )
    return job


def _build(world: dict[str, Any], who: str = "alpha") -> store.ExportJob:
    job = _request(world, who)
    done = ax.run_export(
        job.id, db_path=world["db_path"], export_dir=world["export_dir"]
    )
    assert done is not None
    return done


def _artifact(world: dict[str, Any], job: store.ExportJob) -> Path:
    return ax.artifact_path(world["export_dir"], job.id)


def _status(world: dict[str, Any], who: str, job_id: str) -> str:
    job = store.get_job(world[who], job_id, db_path=world["db_path"])
    assert job is not None
    return job.status


def _events(world: dict[str, Any], who: str) -> list[str]:
    with sqlite3.connect(world["db_path"]) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM account_audit_events WHERE user_id = ? ORDER BY id",
                (world[who],),
            )
        ]


# ---------------------------------------------------------------------------
# Archive contents
# ---------------------------------------------------------------------------


def test_archive_contains_every_required_section_with_versions_and_provenance(
    world: dict[str, Any],
) -> None:
    job = _build(world)
    files = archive_json(_artifact(world, job))
    manifest = files["manifest.json"]

    assert manifest["format"] == ax.EXPORT_FORMAT
    assert manifest["schema_version"] == ax.EXPORT_SCHEMA_VERSION == 1
    assert manifest["export_id"] == job.id
    assert manifest["account"] == {"user_id": world["alpha"]}
    assert [section["section"] for section in manifest["sections"]] == REQUIRED_SECTIONS
    assert {item["category"] for item in manifest["exclusions"]} == {
        "credentials",
        "internal_security_data",
        "other_accounts",
    }

    listed = [c for section in manifest["sections"] for c in section["collections"]]
    assert len(listed) == len(sections.COLLECTIONS)
    assert set(files) == {"manifest.json", *(entry["path"] for entry in listed)}
    for entry in listed:
        body = files[entry["path"]]
        assert body["schema_version"] == entry["schema_version"] >= 1
        assert body["provenance"] == entry["provenance"]
        assert body["record_count"] == entry["record_count"] == len(body["records"])
        assert body["collection"] == entry["name"]
        assert entry["provenance"]["origin"] in {
            "user_entered",
            "connector_import",
            "system_generated",
            "derived_computation",
            "ai_generated",
            "mixed",
        }
        if entry["provenance"]["origin"] in sections.DERIVED_ORIGINS:
            assert entry["provenance"]["generator"]
            assert entry["provenance"]["derived_from"]


def test_manifest_checksums_match_the_member_contents(world: dict[str, Any]) -> None:
    job = _build(world)
    text = archive_text(_artifact(world, job))
    manifest = json.loads(text["manifest.json"])
    for section in manifest["sections"]:
        for entry in section["collections"]:
            digest = hashlib.sha256(text[entry["path"]].encode("utf-8")).hexdigest()
            assert digest == entry["sha256"], entry["path"]


def test_archive_holds_the_owners_data_in_every_section(world: dict[str, Any]) -> None:
    job = _build(world)
    files = archive_json(_artifact(world, job))

    def records(path: str) -> list[dict[str, Any]]:
        return files[path]["records"]

    assert records("profile/account.json")[0]["email"] == "alpha@example.com"
    assert records("profile/preferences.json")[0]["goals"] == ["alpha-goal"]
    assert records("programmes/programmes.json")[0]["definition"]["name"] == "alpha programme"
    assert records("programmes/state.json")[0]["current_day"] == 2
    assert records("workouts/hevy_workouts.json")[0]["payload"]["title"] == "alpha-workout"
    assert records("workouts/exercise_progress.json")[0]["exercise_name"] == "alpha-squat"
    assert records("metrics/body_metrics.json")[0]["weight_kg"] == 80.5
    assert records("check_ins/check_ins.json")[0]["message"] == "alpha-checkin-message"
    assert records("connectors/connections.json")[0]["provider"] == "hevy"
    assert records("connectors/hevy_routines.json")[0]["routine_id"] == "alpha-routine-id"
    assert records("notifications/notifications.json")[0]["title"] == "alpha-notification-title"
    assert [m["role"] for m in records("coaching/chat_messages.json")] == ["user", "assistant"]
    assert records("coaching/reasoning_logs.json")[0]["reasoning"] == "alpha-reasoning"
    assert records("coaching/dashboard_insight.json")[0]["insight"]["fatigue"] == "alpha-fatigue"
    assert "alpha-correlation" in records("coaching/correlation_report.json")[0]["report_markdown"]
    assert records("coaching/daily_plans.json")[0]["plan"] == "alpha-plan"
    # The export's own request is audited and appears in the export that follows.
    audit = archive_json(_artifact(world, job))["audit/events.json"]["records"]
    assert [event["event_type"] for event in audit] == [store.EVENT_REQUESTED]
    assert audit[0]["resource_id"] == job.id


def test_archive_excludes_other_accounts_and_every_credential(world: dict[str, Any]) -> None:
    job = _build(world)
    text = "\n".join(archive_text(_artifact(world, job)).values())
    assert "bravo" not in text
    assert "bravo@example.com" not in text
    for owner in ("alpha", "bravo"):
        for secret in secret_values(owner):
            assert secret not in text, secret
    assert "alpha-client-id" not in text
    for forbidden_key in ("preview_token", "access_token", "p256dh", "refresh_token"):
        assert forbidden_key not in text.replace(
            "tokens are never exported", ""
        ).replace("refresh tokens", ""), forbidden_key


def test_internal_security_data_never_reaches_the_archive(world: dict[str, Any]) -> None:
    """CSRF nonces and rate-limit counters carry the account's id but are not its data."""

    pytest.importorskip("fastapi")
    from webapp.csrf_security import CSRFTokenStore

    nonce = CSRFTokenStore(world["db_path"]).issue(world["alpha"], "alpha-session-cookie")
    with sqlite3.connect(world["db_path"]) as conn:
        conn.execute("INSERT INTO rate_limits (ip, timestamp) VALUES ('203.0.113.77', 1.0)")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        stored = conn.execute(
            "SELECT token_hash, session_hash FROM csrf_tokens WHERE user_id = ?",
            (world["alpha"],),
        ).fetchall()
    assert {"csrf_tokens", "rate_limits"} <= tables and stored  # the rows really exist

    job = _build(world)
    text = "\n".join(archive_text(_artifact(world, job)).values())
    forbidden = [nonce, "alpha-session-cookie", "203.0.113.77", "token_hash", "session_hash"]
    forbidden += [value for row in stored for value in row]
    for value in forbidden:
        assert value not in text, value
    manifest = json.loads(archive_text(_artifact(world, job))["manifest.json"])
    exported_paths = {e["path"] for s in manifest["sections"] for e in s["collections"]}
    assert not any("csrf" in path or "rate" in path for path in exported_paths)


def test_each_account_gets_only_its_own_archive(world: dict[str, Any]) -> None:
    alpha_job = _build(world, "alpha")
    bravo_job = _build(world, "bravo")
    alpha_text = "\n".join(archive_text(_artifact(world, alpha_job)).values())
    bravo_text = "\n".join(archive_text(_artifact(world, bravo_job)).values())
    assert "alpha" in alpha_text and "bravo" not in alpha_text
    assert "bravo" in bravo_text and "alpha" not in bravo_text
    assert json.loads(archive_text(_artifact(world, bravo_job))["manifest.json"])[
        "account"
    ] == {"user_id": world["bravo"]}


def test_archive_is_owner_only_on_disk(world: dict[str, Any]) -> None:
    job = _build(world)
    path = _artifact(world, job)
    assert path.stat().st_mode & 0o777 == 0o600
    assert world["export_dir"].stat().st_mode & 0o777 == 0o700
    with zipfile.ZipFile(path) as archive:
        assert {info.external_attr >> 16 & 0o777 for info in archive.infolist()} == {0o600}
    assert not list(world["export_dir"].glob("*.partial"))


def test_completed_job_records_size_checksum_counts_and_expiry(world: dict[str, Any]) -> None:
    before = datetime.now(tz=timezone.utc).replace(microsecond=0)
    job = _build(world)
    path = _artifact(world, job)
    assert job.status == store.READY
    assert job.size_bytes == path.stat().st_size
    assert job.checksum_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert job.record_counts["notifications.notifications"] == 1
    assert job.record_counts["audit.events"] == 1
    assert set(job.record_counts) == {c.key for c in sections.COLLECTIONS}
    expires = datetime.fromisoformat(job.expires_at)
    assert timedelta(hours=23, minutes=59) <= expires - before <= timedelta(hours=24, minutes=1)
    assert _events(world, "alpha") == [store.EVENT_REQUESTED, store.EVENT_COMPLETED]
    assert _events(world, "bravo") == []


def test_archive_reads_one_consistent_snapshot(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_iter = ax.iter_records
    calls = {"count": 0}

    def writer_in_the_middle(conn: sqlite3.Connection, collection: Any, owner: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:  # the first collection has already been read
            with sqlite3.connect(world["db_path"]) as other:
                other.execute(
                    "INSERT INTO notifications (user_id, type, title, message, is_read, "
                    "created_at) VALUES (?, 'system', 'alpha-late-write', 'm', 0, 'x')",
                    (world["alpha"],),
                )
        return real_iter(conn, collection, owner)

    monkeypatch.setattr(ax, "iter_records", writer_in_the_middle)
    job = _build(world)
    notifications = archive_json(_artifact(world, job))["notifications/notifications.json"]
    assert all(r["title"] != "alpha-late-write" for r in notifications["records"])


def test_snapshot_connection_cannot_write(world: dict[str, Any]) -> None:
    with sqlite3.connect(world["db_path"]) as check:
        users_before = check.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with ax._snapshot(world["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == users_before
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM users")
    with sqlite3.connect(world["db_path"]) as check:
        assert check.execute("SELECT COUNT(*) FROM users").fetchone() == (users_before,)


def test_non_finite_numbers_are_written_as_null(world: dict[str, Any]) -> None:
    with sqlite3.connect(world["db_path"]) as conn:
        conn.execute(
            "UPDATE body_metrics SET hrv = ? WHERE user_id = ?",
            (float("inf"), world["alpha"]),
        )
    job = _build(world)
    (metric,) = archive_json(_artifact(world, job))["metrics/body_metrics.json"]["records"]
    assert metric["hrv"] is None
    assert metric["weight_kg"] == 80.5


def test_an_account_with_no_data_still_gets_a_valid_archive(
    world: dict[str, Any],
) -> None:
    with sqlite3.connect(world["db_path"]) as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) "
            "VALUES ('empty-account', 'empty@example.com', 'Empty', '2026-01-01T00:00:00+00:00')"
        )
    world["empty"] = "empty-account"
    job = _build(world, "empty")
    files = archive_json(_artifact(world, job))
    counts = {
        e["path"]: e["record_count"]
        for s in files["manifest.json"]["sections"]
        for e in s["collections"]
    }
    assert counts["profile/account.json"] == 1
    assert counts["workouts/hevy_workouts.json"] == 0
    assert files["workouts/hevy_workouts.json"]["records"] == []


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_unexpected_failure_is_recorded_without_leaking_details(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom alpha-secret-hevy-key /private/path")

    monkeypatch.setattr(ax, "build_archive", explode)
    job = _request(world)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None and result.status == store.FAILED
    assert result.error_code == "build_failed"
    assert "boom" not in json.dumps(ax.public_view(result))
    assert not list(world["export_dir"].glob("*"))
    assert _events(world, "alpha")[-1] == store.EVENT_FAILED


def test_owner_can_request_again_after_a_failure(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ax, "build_archive", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    failed = _request(world)
    ax.run_export(failed.id, db_path=world["db_path"], export_dir=world["export_dir"])
    monkeypatch.undo()
    retry = _request(world)
    assert retry.id != failed.id
    done = ax.run_export(retry.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert done is not None and done.status == store.READY


def test_oversized_export_fails_cleanly(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ax, "MAX_UNCOMPRESSED_BYTES", 500)
    job = _request(world)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None
    assert (result.status, result.error_code) == (store.FAILED, "export_too_large")
    assert not list(world["export_dir"].glob("*"))


def test_export_that_runs_too_long_fails_cleanly(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ax, "MAX_BUILD_SECONDS", -1)
    job = _request(world)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None
    assert (result.status, result.error_code) == (store.FAILED, "export_timeout")
    assert not list(world["export_dir"].glob("*"))


def test_credential_class_field_blocks_the_export(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    leaky = replace(
        sections.COLLECTIONS[0],
        to_record=lambda row: {"user_id": row[0], "api_key": "alpha-secret-leak"},
    )
    monkeypatch.setattr(ax, "COLLECTIONS", (leaky, *sections.COLLECTIONS[1:]))
    job = _request(world)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None
    assert (result.status, result.error_code) == (store.FAILED, "policy_violation")
    assert not list(world["export_dir"].glob("*"))


def test_nested_credential_key_in_a_record_blocks_the_export(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    leaky = replace(
        sections.COLLECTIONS[0],
        to_record=lambda row: {"user_id": row[0], "extra": [{"client_secret": "x"}]},
    )
    monkeypatch.setattr(ax, "COLLECTIONS", (leaky,))
    job = _request(world)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None and result.error_code == "policy_violation"


def test_deleting_a_running_export_cancels_the_build(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_iter = ax.iter_records
    job = _request(world)
    calls = {"count": 0}

    def cancel_after_first(conn: sqlite3.Connection, collection: Any, owner: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            ax.delete_export(
                world["alpha"],
                job.id,
                db_path=world["db_path"],
                export_dir=world["export_dir"],
            )
        return real_iter(conn, collection, owner)

    monkeypatch.setattr(ax, "iter_records", cancel_after_first)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is None
    assert _status(world, "alpha", job.id) == store.DELETED
    assert not list(world["export_dir"].glob("*"))
    assert store.EVENT_COMPLETED not in _events(world, "alpha")


def test_cancelling_during_a_large_collection_stops_the_build_promptly(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat runs every few records, so a long collection is cancellable."""

    with sqlite3.connect(world["db_path"]) as conn:
        conn.executemany(
            "INSERT INTO notifications (user_id, type, title, message, is_read, created_at) "
            "VALUES (?, 'system', ?, 'm', 0, '2026-10-01T00:00:00+00:00')",
            [(world["alpha"], f"alpha-bulk-{index}") for index in range(450)],
        )
    monkeypatch.setattr(ax, "HEARTBEAT_SECONDS", 0)
    job = _request(world)
    real_iter = ax.iter_records
    streamed = {"notifications": 0}

    def cancel_midway(conn: sqlite3.Connection, collection: Any, owner: str) -> Any:
        for record in real_iter(conn, collection, owner):
            if collection.key == "notifications.notifications":
                streamed["notifications"] += 1
                if streamed["notifications"] == 120:
                    ax.delete_export(
                        world["alpha"],
                        job.id,
                        db_path=world["db_path"],
                        export_dir=world["export_dir"],
                    )
            yield record

    monkeypatch.setattr(ax, "iter_records", cancel_midway)
    result = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is None
    # The next heartbeat (every 100 records) noticed the cancellation.
    assert 120 < streamed["notifications"] <= 200
    assert _status(world, "alpha", job.id) == store.DELETED
    assert not list(world["export_dir"].glob("*"))


def test_run_export_only_builds_queued_jobs(world: dict[str, Any]) -> None:
    job = _request(world)
    first = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert first is not None and first.status == store.READY
    again = ax.run_export(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert again is None
    assert ax.run_export(
        "f" * 32, db_path=world["db_path"], export_dir=world["export_dir"]
    ) is None
    assert ax.run_export(
        "../escape", db_path=world["db_path"], export_dir=world["export_dir"]
    ) is None
    assert len(list(world["export_dir"].glob("*.zip"))) == 1


def test_kill_switch_blocks_requests_and_fails_queued_builds(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = _request(world)
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", "0")
    assert not ax.exports_enabled()
    with pytest.raises(ax.ExportDisabled):
        _request(world, "bravo")
    result = ax.run_export(queued.id, db_path=world["db_path"], export_dir=world["export_dir"])
    assert result is not None
    assert (result.status, result.error_code) == (store.FAILED, "export_disabled")
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", "1")
    assert ax.exports_enabled()


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " no ", "off"])
def test_kill_switch_accepts_common_false_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", value)
    assert not ax.exports_enabled()


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "anything"])
def test_exports_are_enabled_unless_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", value)
    assert ax.exports_enabled()


# ---------------------------------------------------------------------------
# Requesting, listing and limits
# ---------------------------------------------------------------------------


def test_request_returns_the_in_flight_export_instead_of_starting_another(
    world: dict[str, Any],
) -> None:
    first, created_first = ax.request_export(
        world["alpha"], db_path=world["db_path"], export_dir=world["export_dir"]
    )
    second, created_second = ax.request_export(
        world["alpha"], db_path=world["db_path"], export_dir=world["export_dir"]
    )
    assert created_first and not created_second
    assert second.id == first.id
    other, created_other = ax.request_export(
        world["bravo"], db_path=world["db_path"], export_dir=world["export_dir"]
    )
    assert created_other and other.id != first.id


def test_request_rate_limit_is_per_account(world: dict[str, Any]) -> None:
    for _ in range(ax.MAX_REQUESTS_PER_WINDOW):
        _build(world)
    with pytest.raises(ax.ExportRateLimited) as excinfo:
        _request(world)
    assert excinfo.value.retry_after_seconds > 0
    assert _request(world, "bravo").status == store.QUEUED


def test_only_the_newest_ready_exports_are_retained(world: dict[str, Any]) -> None:
    jobs = [_build(world) for _ in range(ax.MAX_READY_EXPORTS)]
    assert all(_artifact(world, job).exists() for job in jobs)
    _request(world)  # the next request trims the oldest ready export
    assert not _artifact(world, jobs[0]).exists()
    assert _status(world, "alpha", jobs[0].id) == store.EXPIRED
    assert all(_artifact(world, job).exists() for job in jobs[1:])


def test_list_and_get_are_scoped_to_the_owner(world: dict[str, Any]) -> None:
    job = _build(world)
    kwargs = {"db_path": world["db_path"], "export_dir": world["export_dir"]}
    assert [j.id for j in ax.list_exports(world["alpha"], **kwargs)] == [job.id]
    assert ax.list_exports(world["bravo"], **kwargs) == []
    assert ax.get_export(world["alpha"], job.id, **kwargs).id == job.id
    for hostile in (job.id, "0" * 32, "../" + job.id, "", "x" * 500):
        with pytest.raises(ax.ExportNotFound):
            ax.get_export(world["bravo"], hostile, **kwargs)


def test_public_view_never_names_the_owner_and_links_only_ready_exports(
    world: dict[str, Any],
) -> None:
    queued = _request(world)
    view = ax.public_view(queued)
    assert view["status"] == store.QUEUED
    assert view["download_url"] is None
    assert world["alpha"] not in json.dumps(view)
    assert "user_id" not in view
    done = ax.run_export(queued.id, db_path=world["db_path"], export_dir=world["export_dir"])
    ready_view = ax.public_view(done)
    assert ready_view["download_url"] == f"/api/account/exports/{queued.id}/download"
    assert world["alpha"] not in json.dumps(ready_view)
    assert set(ready_view) == {
        "id",
        "status",
        "schema_version",
        "requested_at",
        "completed_at",
        "expires_at",
        "size_bytes",
        "checksum_sha256",
        "record_counts",
        "error_code",
        "download_url",
    }


# ---------------------------------------------------------------------------
# Downloading, expiry and deletion
# ---------------------------------------------------------------------------


def _open(world: dict[str, Any], who: str, job_id: str, **kwargs: Any) -> ax.ExportDownload:
    return ax.open_export(
        world[who],
        job_id,
        db_path=world["db_path"],
        export_dir=world["export_dir"],
        **kwargs,
    )


def test_owner_can_open_a_ready_export_and_the_download_is_audited(
    world: dict[str, Any],
) -> None:
    job = _build(world)
    download = _open(world, "alpha", job.id)
    try:
        body = download.file.read()
    finally:
        download.file.close()
    assert len(body) == download.size_bytes == job.size_bytes
    assert hashlib.sha256(body).hexdigest() == job.checksum_sha256
    assert re.fullmatch(r"workout-agent-export-\d{4}-\d{2}-\d{2}\.zip", download.filename)
    assert _events(world, "alpha")[-1] == store.EVENT_DOWNLOADED
    assert _events(world, "bravo") == []


def test_other_accounts_cannot_open_an_export(world: dict[str, Any]) -> None:
    job = _build(world)
    events_before = _events(world, "alpha")
    with pytest.raises(ax.ExportNotFound):
        _open(world, "bravo", job.id)
    assert _events(world, "alpha") == events_before
    assert _events(world, "bravo") == []
    assert _artifact(world, job).exists()


def test_export_that_is_not_ready_cannot_be_downloaded(world: dict[str, Any]) -> None:
    queued = _request(world)
    with pytest.raises(ax.ExportNotReady):
        _open(world, "alpha", queued.id)


def test_failed_and_deleted_exports_cannot_be_downloaded(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ax, "build_archive", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    failed = _request(world)
    ax.run_export(failed.id, db_path=world["db_path"], export_dir=world["export_dir"])
    with pytest.raises(ax.ExportGone):
        _open(world, "alpha", failed.id)
    monkeypatch.undo()
    ready = _build(world)
    ax.delete_export(
        world["alpha"], ready.id, db_path=world["db_path"], export_dir=world["export_dir"]
    )
    with pytest.raises(ax.ExportNotFound):
        _open(world, "alpha", ready.id)


def test_expired_export_is_refused_and_its_file_removed(world: dict[str, Any]) -> None:
    job = _build(world)
    later = datetime.fromisoformat(job.expires_at) + timedelta(seconds=1)
    with pytest.raises(ax.ExportGone):
        _open(world, "alpha", job.id, now=later)
    assert not _artifact(world, job).exists()
    assert _status(world, "alpha", job.id) == store.EXPIRED
    assert store.EVENT_EXPIRED in _events(world, "alpha")


def test_export_remains_available_until_it_expires(world: dict[str, Any]) -> None:
    job = _build(world)
    just_before = datetime.fromisoformat(job.expires_at) - timedelta(seconds=5)
    download = _open(world, "alpha", job.id, now=just_before)
    download.file.close()


def test_missing_artifact_retires_the_export(world: dict[str, Any]) -> None:
    job = _build(world)
    _artifact(world, job).unlink()
    with pytest.raises(ax.ExportGone):
        _open(world, "alpha", job.id)
    assert _status(world, "alpha", job.id) == store.EXPIRED


def test_truncated_artifact_is_never_served(world: dict[str, Any]) -> None:
    job = _build(world)
    path = _artifact(world, job)
    path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(ax.ExportGone):
        _open(world, "alpha", job.id)
    assert not path.exists()


def test_symlinked_artifact_is_refused(world: dict[str, Any], tmp_path: Path) -> None:
    job = _build(world)
    path = _artifact(world, job)
    decoy = tmp_path / "decoy.zip"
    decoy.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(decoy)
    with pytest.raises(ax.ExportGone):
        _open(world, "alpha", job.id)
    assert decoy.exists()  # the link is retired, the target is untouched


def test_delete_removes_the_artifact_and_only_for_the_owner(world: dict[str, Any]) -> None:
    job = _build(world)
    kwargs = {"db_path": world["db_path"], "export_dir": world["export_dir"]}
    with pytest.raises(ax.ExportNotFound):
        ax.delete_export(world["bravo"], job.id, **kwargs)
    assert _artifact(world, job).exists()
    ax.delete_export(world["alpha"], job.id, **kwargs)
    assert not _artifact(world, job).exists()
    assert store.EVENT_DELETED in _events(world, "alpha")
    with pytest.raises(ax.ExportNotFound):
        ax.delete_export(world["alpha"], job.id, **kwargs)


def test_open_is_blocked_by_the_kill_switch(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _build(world)
    monkeypatch.setenv("ACCOUNT_EXPORT_ENABLED", "0")
    with pytest.raises(ax.ExportDisabled):
        _open(world, "alpha", job.id)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "hours"),
    [("", 24), ("6", 6), ("0", 1), ("-5", 1), ("9999", 168), ("abc", 24), (" 48 ", 48)],
)
def test_retention_is_bounded(monkeypatch: pytest.MonkeyPatch, raw: str, hours: int) -> None:
    monkeypatch.setenv("ACCOUNT_EXPORT_TTL_HOURS", raw)
    assert ax.retention_period() == timedelta(hours=hours)


def test_export_directory_defaults_beside_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACCOUNT_EXPORT_DIR", raising=False)
    db = tmp_path / "data" / "app.db"
    assert ax.export_dir_for(str(db)) == (tmp_path / "data").resolve() / "account-exports"
    monkeypatch.setenv("ACCOUNT_EXPORT_DIR", str(tmp_path / "elsewhere"))
    assert ax.export_dir_for(str(db)) == tmp_path / "elsewhere"


@pytest.mark.parametrize("bad", ["", "abc", "../" + "a" * 29, "A" * 32, "a" * 31])
def test_artifact_paths_reject_non_generated_ids(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        ax.artifact_path(tmp_path, bad)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def test_maintenance_deletes_expired_artifacts_for_owners_who_never_return(
    world: dict[str, Any],
) -> None:
    alpha_job = _build(world, "alpha")
    bravo_job = _build(world, "bravo")
    later = datetime.fromisoformat(alpha_job.expires_at) + timedelta(minutes=1)
    report = ax.maintain_exports(
        db_path=world["db_path"], export_dir=world["export_dir"], now=later
    )
    assert report.expired == 2
    assert not _artifact(world, alpha_job).exists()
    assert not _artifact(world, bravo_job).exists()
    assert _status(world, "alpha", alpha_job.id) == store.EXPIRED
    assert ax.maintain_exports(
        db_path=world["db_path"], export_dir=world["export_dir"], now=later
    ) == ax.MaintenanceReport(0, 0, 0, 0)


def test_maintenance_keeps_unexpired_exports(world: dict[str, Any]) -> None:
    job = _build(world)
    report = ax.maintain_exports(db_path=world["db_path"], export_dir=world["export_dir"])
    assert report == ax.MaintenanceReport(0, 0, 0, 0)
    assert _artifact(world, job).exists()


def test_maintenance_recovers_a_job_whose_worker_died(world: dict[str, Any]) -> None:
    job = _request(world)
    assert store.claim_job(job.id, now=datetime.now(tz=timezone.utc), db_path=world["db_path"])
    partial = _artifact(world, job).with_name(_artifact(world, job).name + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"half written")
    later = datetime.now(tz=timezone.utc) + ax.RUNNING_LEASE + timedelta(seconds=30)
    report = ax.maintain_exports(
        db_path=world["db_path"], export_dir=world["export_dir"], now=later
    )
    assert report.interrupted == 1
    assert not partial.exists()
    stale = store.get_job(world["alpha"], job.id, db_path=world["db_path"])
    assert (stale.status, stale.error_code) == (store.FAILED, "interrupted")
    # The owner is unblocked and can request again.
    assert _request(world, now=later).status == store.QUEUED


def test_request_time_hygiene_unblocks_an_account_with_a_dead_job(
    world: dict[str, Any],
) -> None:
    job = _request(world)
    later = datetime.now(tz=timezone.utc) + ax.QUEUED_LEASE + timedelta(minutes=1)
    replacement, created = ax.request_export(
        world["alpha"], db_path=world["db_path"], export_dir=world["export_dir"], now=later
    )
    assert created and replacement.id != job.id
    assert _status(world, "alpha", job.id) == store.FAILED


def test_maintenance_removes_unowned_artifact_files_after_the_grace_period(
    world: dict[str, Any],
) -> None:
    live = _build(world)
    export_dir = world["export_dir"]
    orphan = export_dir / f"{'a' * 32}.zip"
    orphan_partial = export_dir / f"{'b' * 32}.zip.partial"
    fresh = export_dir / f"{'c' * 32}.zip"
    unrelated = export_dir / "notes.txt"
    for path in (orphan, orphan_partial, fresh, unrelated):
        path.write_bytes(b"x")
    old = time.time() - 3600
    for path in (orphan, orphan_partial, unrelated, _artifact(world, live)):
        os.utime(path, (old, old))

    report = ax.maintain_exports(db_path=world["db_path"], export_dir=export_dir)
    assert report.orphans_removed == 2
    assert not orphan.exists() and not orphan_partial.exists()
    assert fresh.exists()  # inside the grace period: could be mid-publish
    assert unrelated.exists()  # not an artifact name: never touched
    assert _artifact(world, live).exists()  # owned by a live job


def test_maintenance_never_follows_symlinks_out_of_the_export_directory(
    world: dict[str, Any], tmp_path: Path
) -> None:
    export_dir = world["export_dir"]
    export_dir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    link = export_dir / f"{'d' * 32}.zip"
    link.symlink_to(victim)
    old = time.time() - 3600
    os.utime(link, (old, old), follow_symlinks=False)
    ax.maintain_exports(db_path=world["db_path"], export_dir=export_dir)
    assert victim.read_text() == "keep me"
    assert link.is_symlink()


def test_maintenance_handles_a_missing_export_directory(world: dict[str, Any]) -> None:
    report = ax.maintain_exports(db_path=world["db_path"], export_dir=world["export_dir"])
    assert report == ax.MaintenanceReport(0, 0, 0, 0)


def test_maintenance_prunes_old_finished_rows(world: dict[str, Any]) -> None:
    job = _build(world)
    far_future = datetime.now(tz=timezone.utc) + ax.FINISHED_ROW_RETENTION + timedelta(days=2)
    ax.maintain_exports(db_path=world["db_path"], export_dir=world["export_dir"], now=far_future)
    assert store.get_job(world["alpha"], job.id, db_path=world["db_path"]) is None


# ---------------------------------------------------------------------------
# Asynchronous worker
# ---------------------------------------------------------------------------


def _wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached before the timeout")


def test_runner_builds_queued_exports_in_the_background(world: dict[str, Any]) -> None:
    runner = ax.ExportRunner(workers=1)
    try:
        job = _request(world)
        runner.submit(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
        _wait_for(lambda: _status(world, "alpha", job.id) == store.READY)
        assert _artifact(world, job).exists()
    finally:
        runner.shutdown()


def test_runner_bounds_concurrency_and_survives_worker_errors(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    active = {"now": 0, "max": 0, "done": 0}
    guard = threading.Lock()
    release = threading.Event()

    def slow_export(export_id: str, **_kwargs: Any) -> None:
        with guard:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            if export_id == "1" * 32:
                raise RuntimeError("worker error")
            release.wait(5)
        finally:
            with guard:
                active["now"] -= 1
                active["done"] += 1

    monkeypatch.setattr(ax, "run_export", slow_export)
    runner = ax.ExportRunner(workers=2)
    try:
        ids = ["1" * 32] + [str(n) * 32 for n in range(2, 7)]
        for export_id in ids:
            runner.submit(export_id, db_path=world["db_path"], export_dir=world["export_dir"])
        _wait_for(lambda: active["done"] >= 1)  # the failing job did not kill a worker
        time.sleep(0.2)
        with guard:
            assert active["max"] <= 2
        release.set()
        _wait_for(lambda: active["done"] == len(ids))
        assert active["max"] <= 2
    finally:
        release.set()
        runner.shutdown()


def test_shared_runner_is_created_once_and_can_be_shut_down() -> None:
    ax.shutdown_runner()
    first = ax.get_runner()
    assert ax.get_runner() is first
    ax.shutdown_runner()
    assert ax.get_runner() is not first
    ax.shutdown_runner()


def test_large_export_is_built_asynchronously_and_stays_bounded(
    world: dict[str, Any],
) -> None:
    """Thousands of rows are streamed in batches; the request path never waits."""

    with sqlite3.connect(world["db_path"]) as conn:
        conn.executemany(
            "INSERT INTO chat_messages (user_id, role, content, created_at) "
            "VALUES (?, 'user', ?, '2026-10-01T00:00:00+00:00')",
            [(world["alpha"], f"alpha-bulk-{index} " + "x" * 200) for index in range(4000)],
        )
        conn.executemany(
            "INSERT INTO chat_messages (user_id, role, content, created_at) "
            "VALUES (?, 'user', ?, '2026-10-01T00:00:00+00:00')",
            [(world["bravo"], f"bravo-bulk-{index}") for index in range(4000)],
        )
    runner = ax.ExportRunner(workers=1)
    try:
        started = time.monotonic()
        job = _request(world)
        runner.submit(job.id, db_path=world["db_path"], export_dir=world["export_dir"])
        assert time.monotonic() - started < 2.0  # queuing never builds inline
        _wait_for(lambda: _status(world, "alpha", job.id) == store.READY, timeout=30)
    finally:
        runner.shutdown()
    ready = store.get_job(world["alpha"], job.id, db_path=world["db_path"])
    assert ready.record_counts["coaching.chat_messages"] == 4002
    chat = archive_json(_artifact(world, ready))["coaching/chat_messages.json"]
    assert chat["record_count"] == len(chat["records"]) == 4002
    assert not any("bravo" in message["content"] for message in chat["records"])
