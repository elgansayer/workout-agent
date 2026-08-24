#!/usr/bin/env python3
"""Fail closed when GitHub Actions dependencies are not immutable.

External actions and reusable workflows must use a full 40-character commit SHA
and keep a human-readable version comment on the same line. Local actions are
allowed only through an explicit ``./`` repository-relative reference.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<value>.+?)\s*$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
VERSION_COMMENT_RE = re.compile(
    r"^v\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$"
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line_number: int
    reference: str
    reason: str

    def render(self, root: Path) -> str:
        try:
            display_path = self.path.relative_to(root)
        except ValueError:
            display_path = self.path
        return (
            f"{display_path}:{self.line_number}: {self.reason} "
            f"(uses: {self.reference})"
        )


def _split_yaml_comment(value: str) -> tuple[str, str | None]:
    """Split a scalar from its inline YAML comment without a YAML dependency."""

    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return value[:index].rstrip(), value[index + 1 :].strip()
    return value.rstrip(), None


def _normalise_reference(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def validate_uses_line(line: str) -> tuple[str, str] | None:
    """Return ``(reference, reason)`` when a uses line violates policy."""

    match = USES_RE.match(line)
    if not match:
        return None

    scalar, comment = _split_yaml_comment(match.group("value"))
    reference = _normalise_reference(scalar)

    if not reference:
        return reference, "empty action reference"

    # Repository-relative actions are the only approved mutable exception. They
    # execute code from the same reviewed commit as the calling workflow.
    if reference.startswith("./"):
        return None

    if reference.startswith("docker://"):
        return reference, "external Docker action is not pinned to a Git commit SHA"

    if "@" not in reference:
        return reference, "external action is missing an immutable @<commit-sha> ref"

    action, ref = reference.rsplit("@", 1)
    if not action or "/" not in action:
        return reference, "external action must use owner/repository@<commit-sha>"

    if not FULL_SHA_RE.fullmatch(ref):
        return reference, "external action ref must be a full 40-character commit SHA"

    if comment is None or not VERSION_COMMENT_RE.fullmatch(comment):
        return reference, "pinned action must have an adjacent '# v<version>' comment"

    return None


def iter_action_files(root: Path) -> Iterable[Path]:
    workflow_root = root / ".github" / "workflows"
    if workflow_root.is_dir():
        for suffix in ("*.yml", "*.yaml"):
            yield from workflow_root.rglob(suffix)

    actions_root = root / ".github" / "actions"
    if actions_root.is_dir():
        for filename in ("action.yml", "action.yaml"):
            yield from actions_root.rglob(filename)


def scan_repository(root: Path) -> list[Violation]:
    root = root.resolve()
    violations: list[Violation] = []
    for path in sorted(set(iter_action_files(root))):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            result = validate_uses_line(line)
            if result is None:
                continue
            reference, reason = result
            violations.append(
                Violation(
                    path=path,
                    line_number=line_number,
                    reference=reference,
                    reason=reason,
                )
            )
    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject mutable external GitHub Actions references."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to scan (default: inferred repository root).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    violations = scan_repository(root)
    if violations:
        print("GitHub Actions pinning policy violations:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation.render(root)}", file=sys.stderr)
        return 1

    checked = len(set(iter_action_files(root)))
    print(f"GitHub Actions pinning policy passed for {checked} action/workflow files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
