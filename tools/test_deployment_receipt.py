from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.deployment_receipt import EXPECTED_SERVICE_REVISION, attach_evidence


class ReceiptChecks(unittest.TestCase):
    def test_only_correlated_verified_receipt_is_preserved_with_immutable_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            old = Path.cwd()
            os.chdir(temporary)
            try:
                release, response = Path("release.json"), Path("response.json")
                eligible = {"sha": "a" * 40, "decision": "eligible"}
                Path("promoted-images.env").write_text("\n".join(
                    f"{key}=ghcr.io/example/{image}@sha256:{'b' * 64}"
                    for key, image in [("AGENT_IMAGE", "workout-agent"), ("WEB_IMAGE", "workout-agent-web")]
                ))
                receipt = {
                    "schema_version": 1, "service": "website-ops",
                    "service_revision": EXPECTED_SERVICE_REVISION,
                    "deployment_id": "workout-123-aabbccddeeff", "head_sha": "a" * 40,
                    "status": "accepted", "backup": "verified", "backup_id": "pre-deploy-123.sqlite3",
                    "backup_sha256": "d" * 64, "backup_bytes": 8192, "sqlite_integrity_check": "ok",
                    "prepared_at": "2026-11-01T00:00:00+00:00", "accepted_at": "2026-11-01T00:00:01+00:00",
                    "arcane_status": 202,
                }
                for key, bad in [("schema_version", True), ("head_sha", "e" * 40), ("status", "prepared"),
                                 ("backup", "pending"), ("backup_id", "../data.db"), ("backup_sha256", "invalid"),
                                 ("backup_bytes", 0), ("sqlite_integrity_check", "failed"), ("arcane_status", 200),
                                 ("service_revision", "c" * 40),
                                 ("accepted_at", "2025-01-01T00:00:00+00:00")]:
                    release.write_text(json.dumps(eligible))
                    response.write_text(json.dumps(dict(receipt, **{key: bad})))
                    with self.subTest(field=key), self.assertRaises(ValueError):
                        attach_evidence(response, release, "a" * 40)
                    self.assertEqual(json.loads(release.read_text()), eligible)
                response.write_text(json.dumps({"status": "accepted"}))
                with self.assertRaises(ValueError):
                    attach_evidence(response, release, "a" * 40)
                response.write_text(json.dumps(receipt))
                attach_evidence(response, release, "a" * 40)
                saved = json.loads(release.read_text())
                self.assertEqual(saved["deployment"], receipt)
                self.assertEqual(len(saved["promoted_images"]), 2)
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
