#!/usr/bin/env python3
"""Fail closed when GitHub Actions could select autonomous implementation work.

The OpenHands Agent Canvas on the VPS is the single autonomous scheduler for this
repository. GitHub Actions stay deterministic verification: they lint, type-check,
test, build and enforce policy, but they must never pick an issue to implement.

This checker enforces that boundary mechanically and keeps the documented queue
contract in ``docs/AUTONOMOUS_QUEUE_CONTRACT.md`` in step with the executable
contract in ``tools/agent_queue.py``.

Run it locally with:

    python tools/check_scheduler_ownership.py --root .
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.agent_queue import (
    BRANCH_PREFIX,
    CONTRACT_LABELS,
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    QUEUE_ENVIRONMENT_VARIABLES,
    READ_ONLY_COMMANDS,
    STATE_CHANGING_COMMANDS,
)

CONTRACT_DOC = "docs/AUTONOMOUS_QUEUE_CONTRACT.md"

REQUIRED_CONTRACT_SECTIONS: tuple[str, ...] = (
    "# Autonomous implementation queue contract",
    "## Roles",
    "## Eligible labels",
    "## Claim lease and heartbeat",
    "## Cancellation",
    "## Branch naming",
    "## Pull request creation",
    "## Review handling",
    "## Scheduler ownership",
    "### Queue commands",
)

# Issue-driven event triggers are how a GitHub-hosted swarm selects work. The
# scheduler owns selection, so no workflow may listen for them.
FORBIDDEN_TRIGGERS: tuple[str, ...] = ("issues", "issue_comment")

# The queue CLI entry point, in file form (tools/agent_queue.py) or module form
# (python -m tools.agent_queue). The lookbehind keeps unrelated names such as
# tools/test_agent_queue.py out of the match.
QUEUE_ENTRY_POINT_RE = re.compile(
    r"(?<![\w-])(?:[\w.-]+[/\\])?agent_queue(?:\.py)?(?=[\s\"']|$)"
)

# The command that follows the entry point on a workflow line.
QUEUE_COMMAND_RE = re.compile(r"[\s\"']+([A-Za-z][\w-]*)")

_LIST_KEY_RE = re.compile(
    r"^(paths|paths-ignore|branches|branches-ignore|tags|tags-ignore)\s*:"
)
_TRIGGER_KEY_RE = re.compile(r"^(?:\"on\"|'on'|on)\s*:\s*(.*)$")
_INLINE_LIST_RE = re.compile(r"^\[(.*)\]$")


@dataclass(frozen=True)
class Violation:
    """A single scheduler-ownership policy failure."""

    path: Path
    line_number: int
    reason: str

    def render(self, root: Path) -> str:
        try:
            display_path = self.path.relative_to(root)
        except ValueError:
            display_path = self.path
        return f"{display_path}:{self.line_number}: {self.reason}"


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def executable_lines(text: str) -> list[tuple[int, str]]:
    """Return the lines of a workflow that can execute, with their numbers.

    Trigger and path filters are configuration, not behaviour: a workflow may
    legitimately list the queue sources in its ``paths:`` filter. Only lines that
    could run a command are inspected.
    """

    result: list[tuple[int, str]] = []
    skip_indent: int | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent_of(raw)
        if skip_indent is not None:
            if stripped.startswith("- ") or indent > skip_indent:
                continue
            skip_indent = None
        if _LIST_KEY_RE.match(stripped):
            skip_indent = indent
            continue
        result.append((number, raw))
    return result


def _flow_mapping_keys(text: str) -> list[str]:
    """Return the top-level keys of a YAML flow mapping.

    ``on: {issues: {types: [labeled]}}`` is valid YAML and declares the same
    trigger as the block form, so the flow form must be read too.
    """

    inner = text.strip()
    if not (inner.startswith("{") and inner.endswith("}")):
        return []
    inner = inner[1:-1]
    parts: list[str] = []
    depth = 0
    current = ""
    for char in inner:
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    keys: list[str] = []
    for part in parts:
        name = part.split(":", 1)[0].strip().strip("\"'")
        if name:
            keys.append(name)
    return keys


def workflow_triggers(text: str) -> list[str]:
    """Return the event names a workflow triggers on."""

    triggers: set[str] = set()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = _TRIGGER_KEY_RE.match(stripped)
        if not match:
            continue
        remainder = match.group(1).strip()
        if remainder:
            inline = _INLINE_LIST_RE.match(remainder)
            if inline:
                triggers.update(
                    part.strip().strip("\"'")
                    for part in inline.group(1).split(",")
                    if part.strip()
                )
            else:
                flow = _flow_mapping_keys(remainder)
                if flow:
                    triggers.update(flow)
                else:
                    triggers.add(remainder.strip("\"'"))
            break
        for following in lines[index + 1 :]:
            candidate = following.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if _indent_of(following) <= _indent_of(line):
                break
            if _indent_of(following) == _indent_of(line) + 2 and candidate.endswith(":"):
                triggers.add(candidate[:-1].strip("\"'"))
        break
    return sorted(triggers)


def queue_commands(line: str) -> list[str]:
    """Return the queue commands a workflow line invokes.

    Both the file form (``python tools/agent_queue.py claim``) and the module
    form (``python -m tools.agent_queue claim``) are recognised, so the check
    cannot be side-stepped by changing how the CLI is started.
    """

    commands: list[str] = []
    for match in QUEUE_ENTRY_POINT_RE.finditer(line):
        command = QUEUE_COMMAND_RE.match(line[match.end() :])
        if command:
            commands.append(command.group(1))
    return commands


def scan_workflow(path: Path) -> list[Violation]:
    """Return the scheduler-ownership violations of a single workflow file."""

    text = path.read_text(encoding="utf-8")
    violations: list[Violation] = []

    for trigger in workflow_triggers(text):
        if trigger in FORBIDDEN_TRIGGERS:
            violations.append(
                Violation(
                    path=path,
                    line_number=1,
                    reason=(
                        f"workflow triggers on '{trigger}'; issue-driven selection belongs "
                        "to the OpenHands VPS scheduler, not to GitHub Actions"
                    ),
                )
            )

    for number, line in executable_lines(text):
        for label in CONTRACT_LABELS:
            if label in line:
                violations.append(
                    Violation(
                        path=path,
                        line_number=number,
                        reason=(
                            f"workflow references the implementation label '{label}'; "
                            "queue labels are owned by the OpenHands VPS scheduler"
                        ),
                    )
                )
                break
        if BRANCH_PREFIX in line:
            violations.append(
                Violation(
                    path=path,
                    line_number=number,
                    reason=(
                        f"workflow references the reserved branch namespace "
                        f"'{BRANCH_PREFIX}'; implementation branches are created by the "
                        "OpenHands VPS scheduler"
                    ),
                )
            )
        for command in queue_commands(line):
            if command in STATE_CHANGING_COMMANDS:
                violations.append(
                    Violation(
                        path=path,
                        line_number=number,
                        reason=(
                            f"workflow runs the queue command '{command}'; "
                            "only the OpenHands VPS scheduler may change queue state"
                        ),
                    )
                )
                break
    return violations


def iter_workflows(root: Path) -> Iterable[Path]:
    """Return every workflow file of the repository."""

    workflow_root = root / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    return sorted(
        path
        for path in workflow_root.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    )


def scan_repository(root: Path) -> list[Violation]:
    """Return every scheduler-ownership violation of the repository."""

    violations: list[Violation] = []
    for path in iter_workflows(root):
        violations.extend(scan_workflow(path))
    return violations


def validate_contract_document(root: Path) -> list[str]:
    """Return the contract-documentation problems of the repository."""

    path = root / CONTRACT_DOC
    if not path.is_file():
        return [f"{CONTRACT_DOC}: missing queue contract document"]

    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    for section in REQUIRED_CONTRACT_SECTIONS:
        if section not in text:
            problems.append(f"{CONTRACT_DOC}: missing required section '{section}'")
    for label in CONTRACT_LABELS:
        if label not in text:
            problems.append(f"{CONTRACT_DOC}: undocumented contract label '{label}'")
    if BRANCH_PREFIX not in text:
        problems.append(
            f"{CONTRACT_DOC}: undocumented branch namespace '{BRANCH_PREFIX}'"
        )
    for variable in QUEUE_ENVIRONMENT_VARIABLES:
        if variable not in text:
            problems.append(
                f"{CONTRACT_DOC}: undocumented queue environment variable '{variable}'"
            )
    for command in STATE_CHANGING_COMMANDS + READ_ONLY_COMMANDS:
        if f"`{command}`" not in text:
            problems.append(f"{CONTRACT_DOC}: undocumented queue command '{command}'")
    documented = (
        f"**{DEFAULT_LEASE_SECONDS}** seconds",
        f"**{DEFAULT_HEARTBEAT_SECONDS}** seconds",
        f"**{DEFAULT_MAX_ATTEMPTS}** attempts",
    )
    for phrase in documented:
        if phrase not in text:
            problems.append(
                f"{CONTRACT_DOC}: documented '{phrase}' does not match the executable contract"
            )
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject GitHub Actions that could select implementation work."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to scan (default: inferred repository root).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()

    violations = scan_repository(root)
    problems = validate_contract_document(root)

    if violations or problems:
        print("Scheduler ownership policy violations:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation.render(root)}", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    checked = len(list(iter_workflows(root)))
    print(
        f"Scheduler ownership policy passed for {checked} workflow(s); "
        f"the queue contract document '{CONTRACT_DOC}' matches the executable contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
