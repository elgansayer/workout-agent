from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_scheduler_ownership import (
    CONTRACT_DOC,
    REQUIRED_CONTRACT_SECTIONS,
    executable_lines,
    instruction_fallback_violations,
    scan_repository,
    scan_workflow,
    validate_contract_document,
    workflow_hardening_violations,
    workflow_triggers,
)


def _workflow(body: str) -> str:
    return body


class TriggerParsingTests(unittest.TestCase):
    def test_reads_block_triggers(self) -> None:
        text = _workflow(
            "name: Example\n"
            "on:\n"
            "  pull_request:\n"
            "    paths:\n"
            "      - 'backend/**'\n"
            "  workflow_dispatch:\n"
        )

        self.assertEqual(workflow_triggers(text), ["pull_request", "workflow_dispatch"])

    def test_reads_inline_triggers(self) -> None:
        text = _workflow("name: Example\non: [push, workflow_dispatch]\n")

        self.assertEqual(workflow_triggers(text), ["push", "workflow_dispatch"])

    def test_ignores_nested_keys(self) -> None:
        text = _workflow(
            "name: Example\n"
            "on:\n"
            "  schedule:\n"
            "    - cron: '23 4 1 * *'\n"
            "  push:\n"
            "    branches: [main]\n"
        )

        self.assertEqual(workflow_triggers(text), ["push", "schedule"])

    def test_reads_flow_mapping_triggers(self) -> None:
        text = _workflow("name: Example\non: {pull_request: {}, workflow_dispatch: {}}\n")

        self.assertEqual(
            workflow_triggers(text), ["pull_request", "workflow_dispatch"]
        )

    def test_flow_mapping_triggers_are_not_confused_by_nested_values(self) -> None:
        text = _workflow("name: Example\non: {push: {paths: [x, issues]}}\n")

        self.assertEqual(workflow_triggers(text), ["push"])


class ExecutableLineTests(unittest.TestCase):
    def test_path_filters_are_not_executable(self) -> None:
        text = _workflow(
            "name: Example\n"
            "on:\n"
            "  pull_request:\n"
            "    paths:\n"
            "      - 'tools/agent_queue.py'\n"
            "      - 'docs/AUTONOMOUS_QUEUE_CONTRACT.md'\n"
            "jobs:\n"
            "  run:\n"
            "    steps:\n"
            "      - run: echo hello\n"
        )

        lines = [line for _, line in executable_lines(text)]

        self.assertTrue(any("echo hello" in line for line in lines))
        self.assertFalse(any("agent_queue.py" in line for line in lines))
        self.assertFalse(any("AUTONOMOUS_QUEUE_CONTRACT" in line for line in lines))


class WorkflowScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.workflows = self.root / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        self.addCleanup(self._tempdir.cleanup)

    def _write(self, name: str, body: str) -> Path:
        path = self.workflows / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_deterministic_verification_workflow_is_allowed(self) -> None:
        path = self._write(
            "ci.yml",
            "name: Required CI\n"
            "on:\n"
            "  pull_request:\n"
            "  push:\n"
            "    branches: [main]\n"
            "permissions:\n"
            "  contents: read\n"
            "concurrency:\n"
            "  group: required-ci-${{ github.ref }}\n"
            "  cancel-in-progress: true\n"
            "jobs:\n"
            "  backend:\n"
            "    steps:\n"
            "      - run: python -m pytest -q\n",
        )

        self.assertEqual(scan_workflow(path), [])

    def test_retired_agent_workflow_is_rejected(self) -> None:
        path = self._write(
            "auto-dispatcher.yml",
            "name: Retired dispatcher\non: workflow_dispatch\npermissions: {}\n"
            "concurrency:\n  group: retired\n  cancel-in-progress: true\n",
        )

        violations = scan_workflow(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("retired GitHub-hosted agent workflow", violations[0].reason)

    def test_every_retired_agent_role_workflow_name_is_rejected(self) -> None:
        for name in (
            "agent-daily.yml",
            "agent-hourly.yml",
            "agent-weekly.yml",
            "daily.yml",
            "failure-monitor.yml",
            "hourly.yml",
            "planner.yml",
            "resolver.yml",
            "reviewer.yml",
            "swarm.yml",
            "weekly.yml",
        ):
            path = self._write(
                name,
                "name: Retired\non: workflow_dispatch\npermissions: {}\n"
                "concurrency:\n  group: retired\n  cancel-in-progress: true\n",
            )

            violations = scan_workflow(path)

            self.assertEqual(len(violations), 1, name)
            self.assertIn("retired GitHub-hosted agent workflow", violations[0].reason)

    def test_retired_failure_monitor_is_rejected(self) -> None:
        path = self._write(
            "on-failure.yml",
            "name: Retired failure monitor\non: workflow_dispatch\npermissions: {}\n"
            "concurrency:\n  group: retired\n  cancel-in-progress: true\n",
        )

        violations = scan_workflow(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("on-failure.yml", violations[0].reason)

    def test_missing_top_level_permissions_is_rejected(self) -> None:
        path = self._write(
            "missing-permissions.yml",
            "name: Missing permissions\non: workflow_dispatch\nconcurrency:\n"
            "  group: test\n  cancel-in-progress: true\njobs: {}\n",
        )

        violations = workflow_hardening_violations(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("permissions", violations[0].reason)

    def test_missing_top_level_concurrency_is_rejected(self) -> None:
        path = self._write(
            "missing-concurrency.yml",
            "name: Missing concurrency\non: workflow_dispatch\npermissions: {}\njobs: {}\n",
        )

        violations = workflow_hardening_violations(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("concurrency", violations[0].reason)

    def test_issue_trigger_is_rejected(self) -> None:
        path = self._write(
            "issue-driven.yml",
            "name: Issue-driven\n"
            "on:\n"
            "  issues:\n"
            "    types: [labeled]\n"
            "permissions:\n"
            "  contents: write\n",
        )

        violations = scan_workflow(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("issue-driven selection", violations[0].reason)

    def test_issue_comment_trigger_is_rejected(self) -> None:
        path = self._write(
            "comment.yml",
            "name: Comment\non:\n  issue_comment:\n    types: [created]\n",
        )

        violations = scan_workflow(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("issue_comment", violations[0].reason)

    def test_flow_mapping_issue_trigger_is_rejected(self) -> None:
        path = self._write(
            "flow.yml",
            "name: Flow\non: {issues: {types: [labeled]}}\npermissions:\n  contents: write\n",
        )

        violations = scan_workflow(path)

        self.assertEqual(len(violations), 1)
        self.assertIn("issue-driven selection", violations[0].reason)

    def test_queue_label_mutation_is_rejected(self) -> None:
        path = self._write(
            "label.yml",
            "name: Label\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  label:\n"
            "    steps:\n"
            "      - run: gh issue edit 846 --add-label agent:queued\n",
        )

        violations = scan_workflow(path)

        self.assertTrue(
            any("implementation label" in violation.reason for violation in violations)
        )

    def test_reserved_branch_creation_is_rejected(self) -> None:
        path = self._write(
            "branch.yml",
            "name: Branch\n"
            "on:\n"
            "  push:\n"
            "jobs:\n"
            "  branch:\n"
            "    steps:\n"
            "      - run: git checkout -b agent/issue-846-queue-contract\n",
        )

        violations = scan_workflow(path)

        self.assertTrue(
            any("reserved branch namespace" in violation.reason for violation in violations)
        )

    def test_queue_selection_command_is_rejected(self) -> None:
        path = self._write(
            "claim.yml",
            "name: Claim\n"
            "on:\n"
            "  schedule:\n"
            "jobs:\n"
            "  claim:\n"
            "    steps:\n"
            "      - run: python tools/agent_queue.py claim --worker ci\n",
        )

        violations = scan_workflow(path)

        self.assertTrue(
            any("queue command" in violation.reason for violation in violations)
        )

    def test_module_form_invocation_is_rejected(self) -> None:
        path = self._write(
            "module.yml",
            "name: Module\n"
            "on:\n"
            "  schedule:\n"
            "jobs:\n"
            "  cancel:\n"
            "    steps:\n"
            "      - run: python -m tools.agent_queue cancel --issue 846\n",
        )

        violations = scan_workflow(path)

        self.assertTrue(
            any("queue command 'cancel'" in violation.reason for violation in violations)
        )

    def test_heartbeat_command_is_rejected(self) -> None:
        path = self._write(
            "heartbeat.yml",
            "name: Heartbeat\n"
            "on:\n"
            "  schedule:\n"
            "jobs:\n"
            "  heartbeat:\n"
            "    steps:\n"
            "      - run: python tools/agent_queue.py heartbeat --worker ci\n",
        )

        violations = scan_workflow(path)

        self.assertTrue(
            any("queue command 'heartbeat'" in violation.reason for violation in violations)
        )

    def test_read_only_status_command_is_allowed(self) -> None:
        path = self._write(
            "status.yml",
            "name: Status\n"
            "on:\n"
            "  schedule:\n"
            "jobs:\n"
            "  status:\n"
            "    steps:\n"
            "      - run: python tools/agent_queue.py status\n",
        )

        self.assertEqual(scan_workflow(path), [])

    def test_queue_test_invocations_are_allowed(self) -> None:
        path = self._write(
            "tests.yml",
            "name: Queue tests\n"
            "on:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  tests:\n"
            "    steps:\n"
            "      - run: python -m unittest tools.test_agent_queue tools.test_scheduler_ownership -v\n"
            "      - run: python -m pytest tools/test_agent_queue.py tools/test_scheduler_ownership.py\n",
        )

        self.assertEqual(scan_workflow(path), [])

    def test_repository_scan_reports_violations(self) -> None:
        self._write(
            "bad.yml",
            "name: Bad\non:\n  issues:\npermissions: {}\nconcurrency:\n"
            "  group: bad\n  cancel-in-progress: true\njobs:\n  a:\n    steps:\n"
            "      - run: echo hi\n",
        )

        violations = scan_repository(self.root)

        self.assertEqual(len(violations), 1)

    def test_current_repository_workflows_are_hardened(self) -> None:
        root = Path(__file__).resolve().parents[1]

        violations = scan_repository(root)

        self.assertEqual([], violations, "\n".join(v.render(root) for v in violations))


class InstructionFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        (self.root / ".agents" / "skills" / "gate").mkdir(parents=True)
        (self.root / ".agents" / "automations").mkdir(parents=True)
        self.addCleanup(self._tempdir.cleanup)

    def test_skill_referencing_the_retired_swarm_daemon_is_rejected(self) -> None:
        (self.root / ".agents" / "skills" / "gate" / "SKILL.md").write_text(
            "Run `swarmd.py` before committing.\n", encoding="utf-8"
        )

        violations = instruction_fallback_violations(self.root)

        self.assertEqual(len(violations), 1)
        self.assertIn("retired swarm daemon", violations[0].reason)

    def test_readme_referencing_the_retired_task_directory_is_rejected(self) -> None:
        (self.root / "README.md").write_text(
            "Stuck work lands in `.tasks/stuck/`.\n", encoding="utf-8"
        )

        violations = instruction_fallback_violations(self.root)

        self.assertEqual(len(violations), 1)
        self.assertIn(".tasks/stuck", violations[0].reason)

    def test_clean_instructions_declaring_the_vps_owner_are_allowed(self) -> None:
        (self.root / "README.md").write_text(
            "The OpenHands VPS scheduler owns claim, branch, pull request and repair.\n",
            encoding="utf-8",
        )
        (self.root / ".agents" / "automations" / "task.md").write_text(
            "Run the verification gate.\n", encoding="utf-8"
        )

        self.assertEqual(instruction_fallback_violations(self.root), [])

    def test_current_repository_instructions_have_no_swarm_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]

        violations = instruction_fallback_violations(root)

        self.assertEqual([], violations, "\n".join(v.render(root) for v in violations))


class ContractDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        (self.root / "docs").mkdir(parents=True)
        self.addCleanup(self._tempdir.cleanup)

    def _write_contract(self, body: str) -> None:
        (self.root / CONTRACT_DOC).write_text(body, encoding="utf-8")

    def test_missing_document_is_reported(self) -> None:
        problems = validate_contract_document(self.root)

        self.assertEqual(len(problems), 1)
        self.assertIn("missing queue contract document", problems[0])

    def test_incomplete_document_is_reported(self) -> None:
        self._write_contract("# Autonomous implementation queue contract\n")

        problems = validate_contract_document(self.root)

        self.assertTrue(
            any("missing required section" in problem for problem in problems)
        )

    def test_drift_between_the_document_and_the_contract_is_reported(self) -> None:
        source = (Path(__file__).resolve().parents[1] / CONTRACT_DOC).read_text(
            encoding="utf-8"
        )
        drifted = source.replace("**1800** seconds", "**1200** seconds")
        self.assertNotEqual(source, drifted)
        self._write_contract(drifted)

        problems = validate_contract_document(self.root)

        self.assertTrue(any("does not match the executable contract" in p for p in problems))

    def test_current_repository_contract_is_complete(self) -> None:
        root = Path(__file__).resolve().parents[1]

        problems = validate_contract_document(root)

        self.assertEqual(problems, [])

    def test_undocumented_queue_command_is_reported(self) -> None:
        source = (Path(__file__).resolve().parents[1] / CONTRACT_DOC).read_text(
            encoding="utf-8"
        )
        drifted = source.replace("| `heartbeat` |", "| `heart-beat` |")
        self.assertNotEqual(source, drifted)
        self._write_contract(drifted)

        problems = validate_contract_document(self.root)

        self.assertTrue(
            any("undocumented queue command 'heartbeat'" in p for p in problems)
        )

    def test_required_sections_are_documented_in_the_repository(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = (root / CONTRACT_DOC).read_text(encoding="utf-8")

        for section in REQUIRED_CONTRACT_SECTIONS:
            self.assertIn(section, text)


if __name__ == "__main__":
    unittest.main()
