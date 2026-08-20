from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tools.supply_chain import build_provenance, validate_trivy_ignore


class TrivyExceptionPolicyTests(unittest.TestCase):
    def _validate(self, contents: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".trivyignore"
            path.write_text(contents, encoding="utf-8")
            return validate_trivy_ignore(path, today=date(2026, 8, 20))

    def test_empty_policy_is_valid(self) -> None:
        self.assertEqual(self._validate("# No exceptions.\n"), [])

    def test_documented_time_bounded_exception_is_valid(self) -> None:
        errors = self._validate(
            "# owner=@security; tracking=#891; reason=Temporary upstream remediation window\n"
            "CVE-2099-0001 exp:2026-09-01\n"
        )
        self.assertEqual(errors, [])

    def test_permanent_exception_is_rejected(self) -> None:
        errors = self._validate(
            "# owner=@security; tracking=#891; reason=Temporary upstream remediation window\n"
            "CVE-2099-0001\n"
        )
        self.assertTrue(any("permanent suppressions are forbidden" in error for error in errors))

    def test_expired_exception_is_rejected(self) -> None:
        errors = self._validate(
            "# owner=@security; tracking=#891; reason=Temporary upstream remediation window\n"
            "CVE-2099-0001 exp:2026-08-19\n"
        )
        self.assertTrue(any("expired" in error for error in errors))

    def test_exception_longer_than_ninety_days_is_rejected(self) -> None:
        errors = self._validate(
            "# owner=@security; tracking=#891; reason=Temporary upstream remediation window\n"
            "CVE-2099-0001 exp:2026-12-31\n"
        )
        self.assertTrue(any("maximum is 90" in error for error in errors))

    def test_exception_requires_owner_tracking_and_reason(self) -> None:
        errors = self._validate("CVE-2099-0001 exp:2026-09-01\n")
        self.assertTrue(any("needs a preceding metadata comment" in error for error in errors))


class ProvenanceTests(unittest.TestCase):
    def test_manifest_hashes_dependency_and_build_inputs_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend").mkdir()
            (root / "frontend").mkdir()
            (root / ".github/workflows").mkdir(parents=True)
            (root / "backend/requirements.txt").write_text("fastapi==1.0\n", encoding="utf-8")
            (root / "frontend/package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
            (root / "frontend/package.json").write_text('{"name": "frontend"}\n', encoding="utf-8")
            (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            (root / ".github/workflows/supply-chain-security.yml").write_text("name: scan\n", encoding="utf-8")

            manifest = build_provenance(
                root,
                commit_sha="a" * 40,
                repository="elgansayer/workout-agent",
                source_ref="refs/heads/main",
                generated_at="2026-08-20T14:00:00+00:00",
            )

            self.assertEqual(manifest["commit_sha"], "a" * 40)
            materials = manifest["materials"]
            self.assertEqual([item["path"] for item in materials], sorted(item["path"] for item in materials))
            lock = next(item for item in materials if item["path"] == "frontend/package-lock.json")
            expected = hashlib.sha256(b'{"lockfileVersion": 3}\n').hexdigest()
            self.assertEqual(lock["sha256"], expected)

            encoded = json.dumps(manifest, sort_keys=True)
            self.assertIn("frontend/package-lock.json", encoded)

    def test_manifest_requires_commit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                build_provenance(
                    Path(directory),
                    commit_sha="",
                    repository="elgansayer/workout-agent",
                    source_ref="refs/heads/main",
                    generated_at="2026-08-20T14:00:00+00:00",
                )


class SupplyChainWorkflowTests(unittest.TestCase):
    def test_external_actions_are_pinned_to_full_commit_shas(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/supply-chain-security.yml"
        ).read_text(encoding="utf-8")
        uses_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]
        self.assertTrue(uses_lines)
        for line in uses_lines:
            ref = line.split("@", 1)[1].split()[0]
            self.assertRegex(ref, r"^[0-9a-f]{40}$", msg=line)

    def test_workflow_covers_required_supply_chain_boundaries(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/supply-chain-security.yml"
        ).read_text(encoding="utf-8")
        required_fragments = (
            "format: cyclonedx",
            "scanners: vuln,misconfig,license",
            "gitleaks/gitleaks-action@",
            "scan-type: image",
            "scanners: vuln,secret,license",
            "supply-chain-source-${{ github.sha }}",
            "supply-chain-image-${{ matrix.name }}-${{ github.sha }}",
        )
        for fragment in required_fragments:
            self.assertIn(fragment, workflow)


if __name__ == "__main__":
    unittest.main()
