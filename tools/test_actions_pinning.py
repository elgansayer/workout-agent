from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_actions_pinning import scan_repository, validate_uses_line


SHA = "d23441a48e516b6c34aea4fa41551a30e30af803"


class UsesLinePolicyTests(unittest.TestCase):
    def test_accepts_full_sha_with_version_comment(self) -> None:
        self.assertIsNone(
            validate_uses_line(f"      - uses: actions/checkout@{SHA} # v6")
        )

    def test_accepts_pinned_reusable_workflow(self) -> None:
        self.assertIsNone(
            validate_uses_line(
                f"    uses: owner/repo/.github/workflows/reusable.yml@{SHA} # v2.1.0"
            )
        )

    def test_accepts_explicit_local_action(self) -> None:
        self.assertIsNone(validate_uses_line("      - uses: ./.github/actions/build"))

    def test_rejects_version_tag(self) -> None:
        result = validate_uses_line("      - uses: actions/checkout@v6")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("40-character", result[1])

    def test_rejects_branch_ref(self) -> None:
        result = validate_uses_line("      - uses: owner/action@main")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("40-character", result[1])

    def test_rejects_short_sha(self) -> None:
        result = validate_uses_line("      - uses: owner/action@d23441a # v6")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("40-character", result[1])

    def test_rejects_missing_version_comment(self) -> None:
        result = validate_uses_line(f"      - uses: owner/action@{SHA}")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("adjacent", result[1])

    def test_rejects_non_version_comment(self) -> None:
        result = validate_uses_line(f"      - uses: owner/action@{SHA} # reviewed")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("version", result[1])

    def test_rejects_external_docker_action(self) -> None:
        result = validate_uses_line("      - uses: docker://alpine:latest")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("Docker", result[1])

    def test_ignores_non_uses_lines(self) -> None:
        self.assertIsNone(validate_uses_line("      run: echo '@v6 is documentation'"))


class RepositoryScanTests(unittest.TestCase):
    def test_scans_workflows_and_composite_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflows = root / ".github" / "workflows"
            actions = root / ".github" / "actions" / "example"
            workflows.mkdir(parents=True)
            actions.mkdir(parents=True)
            (workflows / "ci.yml").write_text(
                f"jobs:\n  test:\n    steps:\n      - uses: actions/checkout@{SHA} # v6\n",
                encoding="utf-8",
            )
            (actions / "action.yml").write_text(
                "runs:\n  using: composite\n  steps:\n    - uses: owner/action@main\n",
                encoding="utf-8",
            )

            violations = scan_repository(root)

            self.assertEqual(1, len(violations))
            self.assertEqual(".github/actions/example/action.yml", str(violations[0].path.relative_to(root)))

    def test_current_repository_satisfies_policy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        violations = scan_repository(root)
        self.assertEqual([], violations, "\n".join(v.render(root) for v in violations))


if __name__ == "__main__":
    unittest.main()
