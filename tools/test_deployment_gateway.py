from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Self

import pytest
from cryptography.fernet import Fernet

from tools.deployment_gateway import (
    DeploymentError,
    GatewayConfig,
    attach_evidence,
    deploy,
)

SHA, DIGEST = "a" * 40, "b" * 64


class FakeResponse:
    status, headers = 202, {"Content-Type": "application/json"}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return b'{"deployment_id":"arcane-42"}'


def _payload() -> dict[str, Any]:
    return {
        "commit_sha": SHA,
        "images": {
            "agent": f"ghcr.io/example/workout-agent@sha256:{DIGEST}",
            "web": f"ghcr.io/example/workout-agent-web@sha256:{DIGEST}",
        },
    }


def test_deploy_verifies_backup_before_arcane_and_persists_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "workout.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE workouts (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO workouts(note) VALUES (?)", ("synthetic",))
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEYS", f"production:{Fernet.generate_key().decode()}"
    )
    calls: list[str] = []

    def opener(request: Any, *, timeout: float) -> FakeResponse:
        state = json.loads(next((tmp_path / "records").glob("*.json")).read_text())
        assert state["status"] == "backup_verified"
        assert state["backup"]["integrity_check"] == "ok"
        assert list((tmp_path / "backups").glob("*.wab"))
        calls.append(request.full_url)
        return FakeResponse()

    evidence = deploy(
        GatewayConfig(
            database,
            tmp_path / "backups",
            tmp_path / "records",
            "http://127.0.0.1:3552/api/stacks/workout/deploy",
        ),
        _payload(),
        opener=opener,
    )
    assert calls == ["http://127.0.0.1:3552/api/stacks/workout/deploy"]
    assert evidence["backup"]["integrity_check"] == "ok"
    assert evidence["arcane"]["deployment_reference"] == "arcane-42"
    record_path = next((tmp_path / "records").glob("*.json"))
    record = json.loads(record_path.read_text())
    assert record["arcane"]["response_body"] == '{"deployment_id":"arcane-42"}'
    assert record_path.stat().st_mode & 0o777 == 0o600


def test_backup_failure_never_calls_arcane(tmp_path: Path) -> None:
    called = False

    def opener(request: Any, *, timeout: float) -> FakeResponse:
        nonlocal called
        called = True
        return FakeResponse()

    with pytest.raises(DeploymentError):
        deploy(
            GatewayConfig(
                tmp_path / "missing.db",
                tmp_path / "backups",
                tmp_path / "records",
                "https://arcane.example.invalid/deploy",
            ),
            _payload(),
            opener=opener,
        )
    assert called is False
    record = json.loads(next((tmp_path / "records").glob("*.json")).read_text())
    assert record["status"] == "failed"


def test_attach_evidence_rejects_opaque_acceptance_and_records_valid_proof(
    tmp_path: Path,
) -> None:
    release, response = tmp_path / "release.json", tmp_path / "response.json"
    release.write_text(json.dumps({"sha": SHA, "decision": "eligible"}))
    response.write_text(json.dumps({"status": "accepted"}))
    with pytest.raises(DeploymentError):
        attach_evidence(response, release, SHA)
    response.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_id": "deploy-1",
                "commit_sha": SHA,
                "status": "arcane_accepted",
                "backup": {
                    "integrity_check": "ok",
                    "foreign_key_violations": [],
                    "database_sha256": "c" * 64,
                },
                "arcane": {"http_status": 202, "response_sha256": "d" * 64},
                "record_id": "deploy-1.json",
            }
        )
    )
    attach_evidence(response, release, SHA)
    assert json.loads(release.read_text())["deployment"]["deployment_id"] == "deploy-1"
