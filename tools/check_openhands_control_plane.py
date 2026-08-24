#!/usr/bin/env python3
"""Enforce the repository's single autonomous engineering control plane.

The policy is intentionally dependency-free so it can run in a clean GitHub
Actions runner before project dependencies are installed.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

CANONICAL_DOCUMENTS: dict[str, tuple[str, ...]] = {
    "AGENTS.md": (
        "OpenHands Agent Canvas",
        "VPS",
        "single autonomous engineering control plane",
        "GitHub Actions",
        "deterministic",
    ),
    ".openhands_instructions": (
        "OpenHands Agent Canvas",
        "VPS",
        "single autonomous engineering control plane",
        "GitHub Actions",
        "deterministic",
    ),
    "docs/AUTONOMOUS_ENGINEERING.md": (
        "OpenHands Agent Canvas",
        "VPS",
        "single autonomous engineering control plane",
        "GitHub Actions",
        "Failure recovery runbook",
        "Ownership matrix",
    ),
}

RETIRED_AUTONOMOUS_WORKFLOWS = (
    ".github/workflows/agent-daily.yml",
    ".github/workflows/agent-hourly.yml",
    ".github/workflows/agent-weekly.yml",
    ".github/workflows/architect.yml",
    ".github/workflows/auto-dispatcher.yml",
    ".github/workflows/openhands.yml",
    ".github/workflows/on-failure.yml",
    ".github/workflows/pr-reviewer.yml",
)

_MUTATING_TRIGGER = re.compile(
    r"^(?: {0,2})(?:schedule|issues|issue_comment|pull_request|"
    r"pull_request_target|push|workflow_run|repository_dispatch):\s*$",
    re.MULTILINE,
)
_WRITE_PERMISSION = re.compile(r"^\s+[a-z-]+:\s*write\s*$", re.MULTILINE)


def _read(root: Path, relative_path: str) -> str:
    path = root / relative_path
    if not path.is_file():
        raise ValueError(f"required control-plane file is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


def validate_control_plane(root: Path) -> list[str]:
    """Return deterministic policy violations for ``root``."""

    errors: list[str] = []

    for relative_path, required_phrases in CANONICAL_DOCUMENTS.items():
        try:
            content = _read(root, relative_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        for phrase in required_phrases:
            if phrase.lower() not in content.lower():
                errors.append(
                    f"{relative_path}: missing canonical control-plane phrase {phrase!r}"
                )

    for relative_path in RETIRED_AUTONOMOUS_WORKFLOWS:
        try:
            content = _read(root, relative_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if not re.search(r"^name:\s*Retired\b", content, re.MULTILINE):
            errors.append(f"{relative_path}: retired autonomous workflow is not named Retired")
        if not re.search(r"^\s*workflow_dispatch:\s*$", content, re.MULTILINE):
            errors.append(f"{relative_path}: retired workflow must be manual-only")
        trigger = _MUTATING_TRIGGER.search(content)
        if trigger:
            errors.append(
                f"{relative_path}: retired workflow regained active trigger "
                f"{trigger.group(0).strip()!r}"
            )
        permission = _WRITE_PERMISSION.search(content)
        if permission:
            errors.append(
                f"{relative_path}: retired workflow regained write permission "
                f"{permission.group(0).strip()!r}"
            )
        if "contents: read" not in content:
            errors.append(f"{relative_path}: retired workflow must declare contents: read")
        if "retired" not in content.lower():
            errors.append(f"{relative_path}: retirement rationale is missing")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to the parent of tools/)",
    )
    args = parser.parse_args()

    errors = validate_control_plane(args.root.resolve())
    if errors:
        print("OpenHands control-plane policy failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "OpenHands control-plane policy passed: canonical VPS ownership is "
        f"documented and {len(RETIRED_AUTONOMOUS_WORKFLOWS)} retired workflows are inert."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
