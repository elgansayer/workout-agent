#!/usr/bin/env python3
"""Fail CI on common data-classification and public-source policy violations.

The checker intentionally targets high-confidence mistakes. It does not replace
code review: it catches credential values passed directly to logging calls,
verifies sensitive SQLite columns are classified, and rejects likely real-user
identity/health data in public documentation and fixtures.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_classification import DataClass, classify_field  # noqa: E402

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_SAFE_WRAPPERS = {"safe_log_value", "redact_for_log"}
_SQL_COLUMN_RE = re.compile(r"^\s*([a-z][a-z0-9_]*)\s+(?:TEXT|REAL|INTEGER|BLOB)\b", re.MULTILINE)
_SENSITIVE_SCHEMA_HINTS = (
    "api_key",
    "secret",
    "token",
    "auth",
    "p256dh",
    "user_id",
    "email",
    "payload",
    "weight",
    "body_fat",
    "muscle",
    "resting_hr",
    "hrv",
    "exercise",
    "workout",
    "routine",
    "prompt",
    "reasoning",
    "insight",
)

_SYNTHETIC_MARKERS = (
    "synthetic-profile: true",
    '"synthetic_profile": true',
    "'synthetic_profile': true",
)
_PUBLIC_DATA_EXTENSIONS = {".md", ".json", ".jsonl", ".csv", ".yaml", ".yml", ".py"}
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
_EXAMPLE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid", "test.invalid"}
_NAMED_SUBJECT_RE = re.compile(
    r"\b(?:athlete|trainee|client|member|user)\s*(?:named\s+|[:(]\s*)([A-Z][A-Za-z'’-]{2,})"
)
_FIRST_PERSON_RE = re.compile(r"\b(?:i\s+have|i\s+am|i'm|my|mine)\b", re.IGNORECASE)
_SENSITIVE_PROFILE_RE = re.compile(
    r"\b(?:diagnos(?:is|ed)|gout|injur(?:y|ies)|joint pain|bad toes|body[ -]?fat|"
    r"bodyweight|weight_kg|resting(?:_| )heart(?:_| )rate|resting_hr|hrv|sleep(?:_| )hours?|"
    r"medical|medication|caloric deficit|protein target|training constraint)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _sensitive_names(node: ast.AST) -> set[str]:
    """Collect credential-like value names, excluding approved redaction calls."""
    if isinstance(node, ast.Call) and _call_name(node) in _SAFE_WRAPPERS:
        return set()

    names: set[str] = set()
    if isinstance(node, ast.Name) and classify_field(node.id) is DataClass.CREDENTIAL:
        names.add(node.id)
    elif isinstance(node, ast.Attribute) and classify_field(node.attr) is DataClass.CREDENTIAL:
        names.add(node.attr)

    for child in ast.iter_child_nodes(node):
        names.update(_sensitive_names(child))
    return names


def find_logging_violations(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Finding(str(path), exc.lineno or 1, f"cannot parse Python source: {exc.msg}")]

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _LOG_METHODS:
            continue
        names: set[str] = set()
        for arg in node.args:
            names.update(_sensitive_names(arg))
        for keyword in node.keywords:
            names.update(_sensitive_names(keyword.value))
        if names:
            findings.append(
                Finding(
                    str(path),
                    node.lineno,
                    "credential value passed directly to logging call: "
                    + ", ".join(sorted(names))
                    + "; wrap with safe_log_value()/redact_for_log()",
                )
            )
    return findings


def find_schema_violations(database_path: Path) -> list[Finding]:
    source = database_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for match in _SQL_COLUMN_RE.finditer(source):
        field_name = match.group(1)
        if not any(hint in field_name for hint in _SENSITIVE_SCHEMA_HINTS):
            continue
        if classify_field(field_name) is DataClass.INTERNAL:
            line = source.count("\n", 0, match.start()) + 1
            findings.append(
                Finding(
                    str(database_path),
                    line,
                    f"sensitive-looking schema field '{field_name}' has no classification",
                )
            )
    return findings


def _line_for(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _has_synthetic_marker(source: str) -> bool:
    lowered = source.lower()
    return any(marker in lowered for marker in _SYNTHETIC_MARKERS)


def find_public_data_violations(path: Path, *, require_synthetic_marker: bool = False) -> list[Finding]:
    """Detect high-confidence real-user data leaks in public docs/fixtures."""
    source = path.read_text(encoding="utf-8")
    synthetic = _has_synthetic_marker(source)
    findings: list[Finding] = []

    for match in _EMAIL_RE.finditer(source):
        domain = match.group(2).lower()
        if domain not in _EXAMPLE_EMAIL_DOMAINS:
            findings.append(
                Finding(
                    str(path),
                    _line_for(source, match.start()),
                    "non-example email address in public documentation/fixture; replace with synthetic data",
                )
            )

    if not synthetic:
        named = _NAMED_SUBJECT_RE.search(source)
        if named:
            findings.append(
                Finding(
                    str(path),
                    _line_for(source, named.start()),
                    f"named {named.group(0).split()[0].lower()} profile in public source; use an anonymous synthetic profile",
                )
            )

        first_person = _FIRST_PERSON_RE.search(source)
        sensitive = _SENSITIVE_PROFILE_RE.search(source)
        if first_person and sensitive:
            findings.append(
                Finding(
                    str(path),
                    min(_line_for(source, first_person.start()), _line_for(source, sensitive.start())),
                    "first-person health/training constraint in public source; use synthetic data",
                )
            )

        if require_synthetic_marker and sensitive:
            findings.append(
                Finding(
                    str(path),
                    _line_for(source, sensitive.start()),
                    "sensitive example fixture is missing an explicit synthetic-profile marker",
                )
            )

    return findings


def _iter_public_data_files(repo_root: Path) -> list[tuple[Path, bool]]:
    candidates: dict[Path, bool] = {}

    for name in ("README.md", "current-workout.md"):
        path = repo_root / name
        if path.exists():
            candidates[path] = name == "current-workout.md"

    docs = repo_root / "docs"
    if docs.exists():
        for path in docs.rglob("*.md"):
            candidates[path] = False

    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _PUBLIC_DATA_EXTENSIONS:
            continue
        lowered_parts = {part.lower() for part in path.parts}
        if "fixtures" in lowered_parts or "samples" in lowered_parts:
            candidates[path] = True

    return sorted(candidates.items(), key=lambda item: str(item[0]))


def scan(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        if path.name == "test_data_classification.py":
            continue
        findings.extend(find_logging_violations(path))

    database_path = root / "database.py"
    if database_path.exists():
        findings.extend(find_schema_violations(database_path))

    repo_root = root.parent
    for path, require_synthetic_marker in _iter_public_data_files(repo_root):
        findings.extend(
            find_public_data_violations(path, require_synthetic_marker=require_synthetic_marker)
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()

    findings = scan(args.root.resolve())
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.message}")
    if findings:
        print(f"Data classification policy check failed with {len(findings)} finding(s).")
        return 1
    print("Data classification policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
