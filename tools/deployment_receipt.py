"""Validate the deployed website-ops backup receipt and preserve release evidence."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

EXPECTED_SERVICE_REVISION = "80030f73057e72c607a86a07e939ceebe2ee19a6"
_RECEIPT_FIELDS = {
    "schema_version",
    "service",
    "service_revision",
    "deployment_id",
    "head_sha",
    "status",
    "backup",
    "backup_id",
    "backup_sha256",
    "backup_bytes",
    "sqlite_integrity_check",
    "prepared_at",
    "accepted_at",
    "arcane_status",
}


def attach_evidence(response_path: Path, release_path: Path, expected_sha: str) -> None:
    response = json.loads(response_path.read_text(encoding="utf-8"))
    if not isinstance(response, dict):
        raise TypeError("Deployment receipt must be an object")
    if set(response) != _RECEIPT_FIELDS:
        raise ValueError("Deployment receipt fields do not match schema version 1")
    expected = {
        "schema_version": 1, "service": "website-ops",
        "service_revision": EXPECTED_SERVICE_REVISION, "head_sha": expected_sha,
        "status": "accepted", "backup": "verified", "sqlite_integrity_check": "ok",
        "arcane_status": 202,
    }
    if any(type(response.get(key)) is not type(value) or response[key] != value for key, value in expected.items()):
        raise ValueError("Receipt does not prove backup verification and Arcane acceptance")
    patterns = {
        "deployment_id": r"workout-[0-9]+-[0-9a-f]{12}",
        "backup_id": r"pre-deploy-[0-9]+\.sqlite3",
        "backup_sha256": r"[0-9a-f]{64}",
    }
    if any(not isinstance(response.get(key), str) or not re.fullmatch(pattern, response[key]) for key, pattern in patterns.items()):
        raise ValueError("Receipt backup or implementation identity is malformed")
    if type(response.get("backup_bytes")) is not int or response["backup_bytes"] <= 0:
        raise ValueError("Receipt backup must contain bytes")
    prepared = datetime.fromisoformat(response["prepared_at"])
    accepted = datetime.fromisoformat(response["accepted_at"])
    if (
        prepared.tzinfo is None
        or accepted.tzinfo is None
        or prepared.utcoffset().total_seconds() != 0
        or accepted.utcoffset().total_seconds() != 0
        or accepted < prepared
    ):
        raise ValueError("Receipt timestamps must record ordered UTC events")
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("sha") != expected_sha or release.get("decision") != "eligible":
        raise ValueError("Release evidence does not match the eligible commit")
    images = {}
    for line in Path("promoted-images.env").read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        images[key] = value
    for key, image in [("AGENT_IMAGE", "workout-agent"), ("WEB_IMAGE", "workout-agent-web")]:
        if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/" + image + r"@sha256:[0-9a-f]{64}", images.get(key, "")):
            raise ValueError("Promoted immutable image reference is missing")
    release.update(deployment=response, promoted_images=images)
    release_path.write_text(json.dumps(release, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--release-evidence", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    attach_evidence(args.response, args.release_evidence, args.expected_sha)


if __name__ == "__main__":
    main()
