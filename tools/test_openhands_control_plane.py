"""Deterministic tests for the autonomous control-plane policy."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_openhands_control_plane import (
    CANONICAL_DOCUMENTS,
    RETIRED_AUTONOMOUS_WORKFLOWS,
    validate_control_plane,
)


class OpenHandsControlPlanePolicyTests(unittest.TestCase):
    def _valid_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)

        for relative_path, phrases in CANONICAL_DOCUMENTS.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(phrases), encoding="utf-8")

        for relative_path in RETIRED_AUTONOMOUS_WORKFLOWS:
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(
                    (
                        "name: Retired Synthetic Agent",
                        "# This autonomous GitHub-hosted workflow is retired.",
                        "on:",
                        "  workflow_dispatch:",
                        "permissions:",
                        "  contents: read",
                        "jobs:",
                        "  retired:",
                        "    runs-on: ubuntu-latest",
                        "    steps:",
                        '      - run: echo "retired"',
                    )
                )
                + "\n",
                encoding="utf-8",
            )

        return temporary, root

    def test_valid_control_plane_contract_passes(self) -> None:
        temporary, root = self._valid_repo()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(validate_control_plane(root), [])

    def test_missing_canonical_ownership_phrase_fails(self) -> None:
        temporary, root = self._valid_repo()
        self.addCleanup(temporary.cleanup)
        (root / "AGENTS.md").write_text("OpenHands Agent Canvas\nVPS\n", encoding="utf-8")
        errors = validate_control_plane(root)
        self.assertTrue(any("AGENTS.md" in error and "missing" in error for error in errors))

    def test_retired_workflow_cannot_regain_schedule(self) -> None:
        temporary, root = self._valid_repo()
        self.addCleanup(temporary.cleanup)
        path = root / RETIRED_AUTONOMOUS_WORKFLOWS[0]
        content = path.read_text(encoding="utf-8").replace(
            "  workflow_dispatch:\n", "  workflow_dispatch:\n  schedule:\n"
        )
        path.write_text(content, encoding="utf-8")
        errors = validate_control_plane(root)
        self.assertTrue(any("regained active trigger" in error for error in errors))

    def test_retired_workflow_cannot_regain_write_permission(self) -> None:
        temporary, root = self._valid_repo()
        self.addCleanup(temporary.cleanup)
        path = root / RETIRED_AUTONOMOUS_WORKFLOWS[0]
        content = path.read_text(encoding="utf-8").replace(
            "  contents: read\n", "  contents: write\n"
        )
        path.write_text(content, encoding="utf-8")
        errors = validate_control_plane(root)
        self.assertTrue(any("write permission" in error for error in errors))
        self.assertTrue(any("contents: read" in error for error in errors))

    def test_retired_workflow_must_remain_manual_only(self) -> None:
        temporary, root = self._valid_repo()
        self.addCleanup(temporary.cleanup)
        path = root / RETIRED_AUTONOMOUS_WORKFLOWS[0]
        content = path.read_text(encoding="utf-8").replace("  workflow_dispatch:\n", "")
        path.write_text(content, encoding="utf-8")
        errors = validate_control_plane(root)
        self.assertTrue(any("manual-only" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
