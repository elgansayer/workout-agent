from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.ops_drill import (
    REQUIRED_RUNBOOK_FIELDS,
    SCENARIO_HEADINGS,
    build_evidence,
    privacy_redaction_drill,
    sqlite_restore_drill,
    token_rotation_drill,
    validate_runbook,
    write_follow_ups,
)


def _valid_runbook() -> str:
    scenarios = []
    for heading in SCENARIO_HEADINGS:
        body = [heading, "", "**Owner:** Primary on-call", "", "**Prerequisites:** Access to logs"]
        for field in REQUIRED_RUNBOOK_FIELDS[2:]:
            body.extend(("", field, "", "Safe, reversible procedure."))
        scenarios.append("\n".join(body))
    return (
        "# Workout Agent operational incident runbooks\n\n"
        "## Incident command and evidence rules\n\n"
        "Evidence is redacted and immutable.\n\n"
        + "\n\n".join(scenarios)
        + "\n"
    )


class OpsDrillTests(unittest.TestCase):
    def test_valid_runbook_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runbook.md"
            path.write_text(_valid_runbook(), encoding="utf-8")
            checks = validate_runbook(path)
        self.assertTrue(checks)
        self.assertTrue(all(check.status == "pass" for check in checks), checks)

    def test_missing_runbook_field_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runbook.md"
            path.write_text(_valid_runbook().replace("### Rollback", "### Recovery", 1), encoding="utf-8")
            checks = validate_runbook(path)
        failures = [check for check in checks if check.status == "fail"]
        self.assertEqual(len(failures), 1)
        self.assertIn("Rollback", failures[0].detail)
        self.assertIsNotNone(failures[0].follow_up)

    def test_forbidden_destructive_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runbook.md"
            path.write_text(_valid_runbook() + "\nrm -rf /\n", encoding="utf-8")
            checks = validate_runbook(path)
        self.assertTrue(any(check.name == "runbook-command-safety" and check.status == "fail" for check in checks))

    def test_sqlite_backup_restore_drill_is_tenant_preserving(self) -> None:
        result = sqlite_restore_drill()
        self.assertEqual(result.status, "pass")
        self.assertIn("3 tenant-scoped rows", result.detail)
        self.assertIn("sha256=", result.detail)

    def test_synthetic_rotation_and_redaction_pass(self) -> None:
        self.assertEqual(token_rotation_drill().status, "pass")
        self.assertEqual(privacy_redaction_drill().status, "pass")

    def test_evidence_is_deterministic_with_explicit_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runbook.md"
            path.write_text(_valid_runbook(), encoding="utf-8")
            first = build_evidence(path, "2026-08-20T15:00:00Z")
            second = build_evidence(path, "2026-08-20T15:00:00+00:00")
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(first["occurred_at"], "2026-08-20T15:00:00Z")
        self.assertEqual(first["status"], "pass")
        self.assertEqual(first["failure_count"], 0)

    def test_failed_evidence_creates_checkbox_follow_up(self) -> None:
        evidence = {
            "evidence_id": "ops-drill-test",
            "occurred_at": "2026-08-20T15:00:00Z",
            "checks": [
                {
                    "name": "example",
                    "status": "fail",
                    "detail": "broken",
                    "follow_up": "Repair the example.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "followups.md"
            write_follow_ups(evidence, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("- [ ] **example**: Repair the example.", text)
        self.assertIn("ops-drill-test", text)

    def test_evidence_never_serializes_synthetic_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runbook.md"
            path.write_text(_valid_runbook(), encoding="utf-8")
            evidence = build_evidence(path, "2026-08-20T15:00:00Z")
        serialised = json.dumps(evidence, sort_keys=True)
        self.assertNotIn("synthetic-secret-never-export", serialised)


if __name__ == "__main__":
    unittest.main()
