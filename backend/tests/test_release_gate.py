from __future__ import annotations

from release_gate import validate_release_evidence


def _valid_payload() -> dict[str, object]:
    deployed_sha = "a" * 40
    reviewed_sha = "c" * 40
    digest = "sha256:" + ("b" * 64)
    return {
        "schema_version": 1,
        "head_sha": deployed_sha,
        "reviewed_head_sha": reviewed_sha,
        "head_on_main": True,
        "pr_merged": True,
        "change_author": "author-user",
        "approved_by": "reviewer-user",
        "operator": "release-operator",
        "config_version": "prod-2026-08-20",
        "migration_plan": "No schema migration required for this release.",
        "database_migration": "none",
        "backup_status": "verified",
        "rollback_command": "docker compose up -d --no-deps web agent",
        "security_review": "No unresolved release-blocking security findings.",
        "smoke_test_plan": "Run authenticated health, readiness, login and dashboard smoke tests.",
        "smoke_test_status": "passed",
        "smoke_test_evidence": "staging-smoke-run-1842",
        "verification_results": "All required deterministic checks passed for the reviewed release.",
        "image_digests": {"web": digest, "agent": digest},
        "checks": [
            {"name": "Python Source Integrity", "conclusion": "success"},
            {"name": "Canonical Health Platform", "conclusion": "success"},
        ],
    }


def test_valid_release_evidence_passes() -> None:
    assert validate_release_evidence(_valid_payload()) == []


def test_reviewed_sha_must_be_well_formed() -> None:
    payload = _valid_payload()
    payload["reviewed_head_sha"] = "not-a-sha"

    findings = validate_release_evidence(payload)

    assert any(finding.field == "reviewed_head_sha" for finding in findings)


def test_merge_commit_and_reviewed_head_may_differ() -> None:
    payload = _valid_payload()

    assert payload["head_sha"] != payload["reviewed_head_sha"]
    assert validate_release_evidence(payload) == []


def test_author_cannot_self_approve_or_self_promote() -> None:
    payload = _valid_payload()
    payload["approved_by"] = "AUTHOR-user"
    payload["operator"] = "author-user"

    findings = validate_release_evidence(payload)

    assert {finding.field for finding in findings} >= {"approved_by", "operator"}


def test_non_success_checks_block_release() -> None:
    payload = _valid_payload()
    payload["checks"] = [
        {"name": "tests", "conclusion": "failure"},
        {"name": "lint", "conclusion": "skipped"},
        {"name": "security", "conclusion": "neutral"},
    ]

    findings = validate_release_evidence(payload)

    assert sum(finding.field.endswith(".conclusion") for finding in findings) == 3


def test_duplicate_check_names_are_rejected() -> None:
    payload = _valid_payload()
    payload["checks"] = [
        {"name": "tests", "conclusion": "success"},
        {"name": "TESTS", "conclusion": "success"},
    ]

    findings = validate_release_evidence(payload)

    assert any(finding.field == "checks[1].name" for finding in findings)


def test_backup_and_rollback_are_mandatory() -> None:
    payload = _valid_payload()
    payload["backup_status"] = "unknown"
    payload["rollback_command"] = ""

    findings = validate_release_evidence(payload)

    assert {finding.field for finding in findings} >= {"backup_status", "rollback_command"}


def test_smoke_test_must_have_passed_with_evidence() -> None:
    payload = _valid_payload()
    payload["smoke_test_status"] = "pending"
    payload["smoke_test_evidence"] = ""

    findings = validate_release_evidence(payload)

    assert {finding.field for finding in findings} >= {
        "smoke_test_status",
        "smoke_test_evidence",
    }


def test_verification_results_are_mandatory() -> None:
    payload = _valid_payload()
    payload["verification_results"] = ""

    findings = validate_release_evidence(payload)

    assert any(finding.field == "verification_results" for finding in findings)


def test_image_references_must_be_immutable_digests() -> None:
    payload = _valid_payload()
    payload["image_digests"] = {
        "web": "ghcr.io/example/workout-agent-web:latest",
        "agent": "sha256:not-a-real-digest",
    }

    findings = validate_release_evidence(payload)

    assert {finding.field for finding in findings} >= {
        "image_digests.web",
        "image_digests.agent",
    }


def test_unmerged_or_non_main_commit_is_rejected() -> None:
    payload = _valid_payload()
    payload["head_on_main"] = False
    payload["pr_merged"] = False

    findings = validate_release_evidence(payload)

    assert {finding.field for finding in findings} >= {"head_on_main", "pr_merged"}
