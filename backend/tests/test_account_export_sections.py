"""Disclosure-boundary tests for the reviewed account export collections."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from account_export_sections import (
    COLLECTIONS,
    DERIVED_ORIGINS,
    EXCLUSIONS,
    NOT_EXPORTED_TABLES,
    ORIGIN_AI_GENERATED,
    ORIGIN_USER_ENTERED,
    SECTIONS,
    SYNC_STATE_KEYS,
    Collection,
    Provenance,
    exported_tables,
    find_credential_keys,
    iter_records,
    scrub_credentials,
)
from account_export_support import secret_values, seed_tenant
from data_classification import DataClass, classify_field
from database import init_db

REQUIRED_SECTIONS = {
    "profile",
    "programmes",
    "workouts",
    "metrics",
    "check_ins",
    "connectors",
    "notifications",
    "coaching",
    "audit",
}


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    db_path = str(tmp_path / "sections.db")
    init_db(db_path)
    alpha = seed_tenant(
        db_path, email="alpha@example.com", display_name="Alpha Example", tag="alpha"
    )
    bravo = seed_tenant(
        db_path, email="bravo@example.com", display_name="Bravo Example", tag="bravo"
    )
    return {"db_path": db_path, "alpha": alpha, "bravo": bravo}


def _records(db_path: str, collection: Collection, owner_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        return list(iter_records(conn, collection, owner_id))


def _by_key(key: str) -> Collection:
    return next(collection for collection in COLLECTIONS if collection.key == key)


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_registry_covers_every_required_section() -> None:
    assert {key for key, _title in SECTIONS} == REQUIRED_SECTIONS
    assert {collection.section for collection in COLLECTIONS} == REQUIRED_SECTIONS


def test_collection_keys_and_paths_are_unique() -> None:
    keys = [collection.key for collection in COLLECTIONS]
    paths = [collection.path for collection in COLLECTIONS]
    assert len(set(keys)) == len(keys)
    assert len(set(paths)) == len(paths)


def test_every_collection_is_owner_scoped_and_versioned() -> None:
    for collection in COLLECTIONS:
        assert f"{collection.owner_column} = ?" in collection.sql, collection.key
        assert collection.sql.count("?") == 1 + len(collection.params_after_owner)
        assert collection.schema_version >= 1
        assert collection.description.strip()


def test_collection_without_owner_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="not owner-scoped"):
        Collection(
            section="profile",
            name="unscoped",
            description="x",
            schema_version=1,
            provenance=Provenance(ORIGIN_USER_ENTERED, "x"),
            sql="SELECT email FROM users",
            to_record=lambda row: {"email": row[0]},
        )
    with pytest.raises(ValueError, match="parameter count"):
        Collection(
            section="profile",
            name="mismatch",
            description="x",
            schema_version=1,
            provenance=Provenance(ORIGIN_USER_ENTERED, "x"),
            sql="SELECT email FROM users WHERE id = ? AND email != ?",
            owner_column="id",
            to_record=lambda row: {"email": row[0]},
        )


def test_derived_collections_declare_generator_and_inputs() -> None:
    derived = [c for c in COLLECTIONS if c.provenance.origin in DERIVED_ORIGINS]
    assert {c.key for c in derived} >= {
        "programmes.programmes",
        "workouts.exercise_progress",
        "check_ins.check_ins",
        "coaching.reasoning_logs",
        "coaching.dashboard_insight",
        "coaching.correlation_report",
        "coaching.daily_plans",
    }
    for collection in derived:
        described = collection.provenance.as_dict()
        assert described["generator"], collection.key
        assert described["derived_from"], collection.key


def test_derived_provenance_without_generator_is_rejected() -> None:
    with pytest.raises(ValueError, match="generator and inputs"):
        Provenance(ORIGIN_AI_GENERATED, "an AI job")
    with pytest.raises(ValueError, match="unknown provenance origin"):
        Provenance("made_up", "somewhere")


def test_internal_derived_from_references_resolve_to_real_collections() -> None:
    known = {collection.path.removesuffix(".json") for collection in COLLECTIONS}
    for collection in COLLECTIONS:
        for source in collection.provenance.derived_from:
            token = source.split(" ", 1)[0]
            if re.fullmatch(r"[a-z_]+/[a-z_]+", token):
                assert token in known, f"{collection.key} derives from unknown {token}"


def test_exclusions_state_the_three_boundaries() -> None:
    assert {item["category"] for item in EXCLUSIONS} == {
        "credentials",
        "internal_security_data",
        "other_accounts",
    }


def test_sql_never_selects_credential_columns() -> None:
    forbidden = re.compile(
        r"\b(client_secret|refresh_token|p256dh|auth|client_id|encrypted_\w+)\b"
    )
    for collection in COLLECTIONS:
        assert not forbidden.search(collection.sql), collection.key
        # The only mention of a key column is the presence test, evaluated in
        # SQLite so the stored ciphertext never reaches Python.
        stripped = collection.sql.replace("length(api_key) > 0", "")
        assert not re.search(r"\bapi_key\b", stripped), collection.key
    google = _by_key("connectors.google_health").sql
    assert google.startswith("SELECT 1 FROM hevy_meta")
    assert "SELECT value" not in google


def test_sync_state_allowlist_excludes_secret_keys() -> None:
    assert not {key for key in SYNC_STATE_KEYS if "token" in key or "state" in key}
    assert "google_health_refresh_token" not in SYNC_STATE_KEYS
    assert "google_health_oauth_state" not in SYNC_STATE_KEYS


# ---------------------------------------------------------------------------
# Credential scrubbing
# ---------------------------------------------------------------------------


def test_scrub_credentials_removes_credential_keys_at_any_depth() -> None:
    document = {
        "title": "Session",
        "api_key": "removed",
        "nested": [{"refresh_token": "removed", "reps": 5}, {"keep": {"Authorization": "x"}}],
        "preview_token": "removed",
    }
    scrubbed = scrub_credentials(document)
    assert scrubbed == {"title": "Session", "nested": [{"reps": 5}, {"keep": {}}]}
    assert document["api_key"] == "removed"  # the source is not mutated
    assert find_credential_keys(scrubbed) == []


def test_find_credential_keys_reports_paths() -> None:
    found = find_credential_keys({"a": [{"secret_value": 1}], "ok": 1})
    assert found == ["$.a[0].secret_value"]


def test_scrub_credentials_rejects_pathological_nesting() -> None:
    deep: Any = "leaf"
    for _ in range(80):
        deep = {"level": deep}
    with pytest.raises(ValueError, match="nested too deeply"):
        scrub_credentials(deep)
    assert find_credential_keys(deep)  # reported, never silently accepted


# ---------------------------------------------------------------------------
# Tenant isolation and secret exclusion
# ---------------------------------------------------------------------------


def test_each_collection_returns_only_the_owners_records(world: dict[str, Any]) -> None:
    for collection in COLLECTIONS:
        alpha_text = json.dumps(_records(world["db_path"], collection, world["alpha"]))
        bravo_text = json.dumps(_records(world["db_path"], collection, world["bravo"]))
        assert "bravo" not in alpha_text, collection.key
        assert "alpha" not in bravo_text, collection.key


def test_every_collection_returns_data_for_a_fully_populated_account(
    world: dict[str, Any],
) -> None:
    counts = {
        collection.key: len(_records(world["db_path"], collection, world["alpha"]))
        for collection in COLLECTIONS
    }
    assert counts["audit.events"] == 0  # no audited action has happened yet
    empty = {key for key, count in counts.items() if count == 0}
    assert empty == {"audit.events"}


def test_an_unknown_owner_gets_nothing(world: dict[str, Any]) -> None:
    for collection in COLLECTIONS:
        assert _records(world["db_path"], collection, "no-such-account") == []


def test_blank_owner_is_refused(world: dict[str, Any]) -> None:
    with sqlite3.connect(world["db_path"]) as conn, pytest.raises(ValueError):
        list(iter_records(conn, COLLECTIONS[0], ""))


def test_no_credential_value_reaches_any_record(world: dict[str, Any]) -> None:
    rendered = json.dumps(
        {
            collection.key: _records(world["db_path"], collection, world["alpha"])
            for collection in COLLECTIONS
        }
    )
    for secret in secret_values("alpha"):
        assert secret not in rendered
    assert "alpha-client-id" not in rendered  # client ids are internal data


def test_no_record_carries_a_credential_class_key(world: dict[str, Any]) -> None:
    for collection in COLLECTIONS:
        for record in _records(world["db_path"], collection, world["alpha"]):
            assert find_credential_keys(record) == [], collection.key
            assert all(
                classify_field(key) is not DataClass.CREDENTIAL for key in record
            ), collection.key


def test_connections_report_presence_and_model_only(world: dict[str, Any]) -> None:
    (record,) = _records(world["db_path"], _by_key("connectors.connections"), world["alpha"])
    assert set(record) == {
        "provider",
        "credential_configured",
        "model",
        "created_at",
        "updated_at",
    }
    assert record["provider"] == "hevy"
    assert record["credential_configured"] is True
    assert record["model"] == "alpha-connector-model"


def test_connection_without_a_key_is_reported_unconfigured(tmp_path: Path) -> None:
    db_path = str(tmp_path / "unconfigured.db")
    init_db(db_path)
    uid = seed_tenant(
        db_path, email="gamma@example.com", display_name="Gamma Example", tag="gamma"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE user_api_keys SET api_key = '' WHERE user_id = ?", (uid,))
    (record,) = _records(db_path, _by_key("connectors.connections"), uid)
    assert record["credential_configured"] is False


def test_google_health_link_state_never_includes_the_token(world: dict[str, Any]) -> None:
    (record,) = _records(world["db_path"], _by_key("connectors.google_health"), world["alpha"])
    assert record == {"provider": "google_health", "connected": True}
    with sqlite3.connect(world["db_path"]) as conn:
        conn.execute(
            "UPDATE hevy_meta SET value = '' WHERE user_id = ? "
            "AND key = 'google_health_refresh_token'",
            (world["alpha"],),
        )
    assert _records(world["db_path"], _by_key("connectors.google_health"), world["alpha"]) == []


def test_sync_state_exports_only_allow_listed_keys(world: dict[str, Any]) -> None:
    records = _records(world["db_path"], _by_key("connectors.sync_state"), world["alpha"])
    assert {record["key"] for record in records} == {
        "programme_start_date",
        "routine_folder_id",
    }


def test_push_subscription_is_reduced_to_the_service_host(world: dict[str, Any]) -> None:
    (record,) = _records(
        world["db_path"], _by_key("notifications.push_subscriptions"), world["alpha"]
    )
    assert record["endpoint_origin"] == "https://push.example.com"
    assert set(record) == {"record_id", "endpoint_origin", "created_at"}


def test_workout_payload_and_programme_definition_are_scrubbed(
    world: dict[str, Any],
) -> None:
    (workout,) = _records(world["db_path"], _by_key("workouts.hevy_workouts"), world["alpha"])
    assert workout["payload"]["title"] == "alpha-workout"
    assert "access_token" not in json.dumps(workout)
    (programme,) = _records(world["db_path"], _by_key("programmes.programmes"), world["alpha"])
    assert programme["engine_version"] == "hevy-programme-v1"
    assert programme["definition"]["name"] == "alpha programme"
    assert "preview_token" not in programme["definition"]
    assert programme["active"] is True


def test_unreadable_json_is_reported_without_exporting_the_raw_text(
    world: dict[str, Any],
) -> None:
    with sqlite3.connect(world["db_path"]) as conn:
        conn.execute(
            "UPDATE workout_history SET hevy_payload = ? WHERE user_id = ?",
            ("{not json refresh_token=alpha-secret-raw", world["alpha"]),
        )
        conn.execute(
            "UPDATE dashboard_insights SET insight_json = ? WHERE user_id = ?",
            ("also not json", world["alpha"]),
        )
    (workout,) = _records(world["db_path"], _by_key("workouts.hevy_workouts"), world["alpha"])
    assert workout["payload"] is None
    assert workout["payload_error"] == "unreadable_json"
    assert "alpha-secret-raw" not in json.dumps(workout)
    (insight,) = _records(world["db_path"], _by_key("coaching.dashboard_insight"), world["alpha"])
    assert insight["insight"] is None
    assert insight["insight_error"] == "unreadable_json"


def test_preferences_decode_list_columns(world: dict[str, Any]) -> None:
    (record,) = _records(world["db_path"], _by_key("profile.preferences"), world["alpha"])
    assert record["goals"] == ["alpha-goal"]
    assert record["constraints"] == ["alpha-constraint"]
    assert record["custom_rules"] == ["alpha-rule"]
    assert record["ai_model"] == "alpha-model"


def test_account_record_names_only_the_owner(world: dict[str, Any]) -> None:
    (record,) = _records(world["db_path"], _by_key("profile.account"), world["alpha"])
    assert record["user_id"] == world["alpha"]
    assert record["email"] == "alpha@example.com"


def test_dashboard_insight_is_decoded_with_its_generation_date(
    world: dict[str, Any],
) -> None:
    (record,) = _records(world["db_path"], _by_key("coaching.dashboard_insight"), world["alpha"])
    assert record["insight"]["advice"] == "alpha-advice"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["generated_on"])


def test_streaming_reads_more_rows_than_one_batch(tmp_path: Path) -> None:
    db_path = str(tmp_path / "batch.db")
    init_db(db_path)
    uid = seed_tenant(
        db_path, email="delta@example.com", display_name="Delta Example", tag="delta"
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO notifications (user_id, type, title, message, is_read, created_at) "
            "VALUES (?, 'system', ?, 'm', 0, '2026-10-01T00:00:00+00:00')",
            [(uid, f"delta-bulk-{index}") for index in range(1200)],
        )
    records = _records(db_path, _by_key("notifications.notifications"), uid)
    assert len(records) == 1201
    assert [record["record_id"] for record in records] == sorted(
        record["record_id"] for record in records
    )


# ---------------------------------------------------------------------------
# Completeness: no table is missed by accident
# ---------------------------------------------------------------------------


def _schema_tables(db_path: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }


def test_every_table_is_exported_or_explicitly_excluded(tmp_path: Path) -> None:
    """A new table must be added as a collection or excluded with a reason."""

    db_path = str(tmp_path / "completeness.db")
    init_db(db_path)
    tables = _schema_tables(db_path)

    undecided = tables - exported_tables() - set(NOT_EXPORTED_TABLES)
    assert not undecided, (
        f"decide whether {sorted(undecided)} belongs in the account export: add a "
        "collection in account_export_sections.py or list it in NOT_EXPORTED_TABLES "
        "with the reason"
    )
    assert exported_tables() <= tables, "a collection reads a table that does not exist"
    assert all(reason.strip() for reason in NOT_EXPORTED_TABLES.values())
    assert not set(NOT_EXPORTED_TABLES) & exported_tables()


def test_internal_security_tables_are_not_exported() -> None:
    assert {"csrf_tokens", "rate_limits"} <= set(NOT_EXPORTED_TABLES)
    assert not {"csrf_tokens", "rate_limits"} & exported_tables()


def test_every_user_owned_table_is_tenant_scoped_before_it_is_exported(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "ownership.db")
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for table in exported_tables():
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            owner = "id" if table == "users" else "user_id"
            assert owner in columns, f"{table} has no owner column to scope the export by"
