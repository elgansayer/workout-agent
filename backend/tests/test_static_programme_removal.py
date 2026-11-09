"""Regression guard: static programme concepts must not return.

The removal is intentionally allow-listed by exact path so a broad exclusion
cannot hide a reintroduced runtime dependency.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "coverage",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".ts",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".yml",
    ".yaml",
    ".sh",
    ".toml",
    ".ini",
    ".cfg",
    ".example",
}

# Architecture/ADR and migration history may discuss the retired concept.
HYBRID_ALLOWLIST = {
    "AGENTS.md",
    "docs/HEVY_NATIVE_DYNAMIC_PROGRAMMES.md",
    ".agents/skills/programme-builder-ui/SKILL.md",
    "backend/database.py",  # idempotent archive migration for legacy revisions
}

GENERIC_RUNTIME_MODULES = [
    "backend/main.py",
    "backend/checkin.py",
    "backend/gemini_engine.py",
    "backend/lifestyle.py",
    "backend/programme_runtime.py",
    "backend/dynamic_programme.py",
    "backend/programme_inference.py",
]

SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()

HYBRID_RE = re.compile(r"hybrid[_ ]powerbuilding", re.IGNORECASE)
PROGRAM_IMPORT_RE = re.compile(
    r"(?:from\s+program\s+import|import\s+program\b|"
    r"from\s+backend\.program\b|backend\.program\b)"
)
WEEK_OF_12_RE = re.compile(r"\bWeek\s+\{?[A-Za-z0-9_]+\}?\s+of\s+12\b")
LOG_LIFT_RE = re.compile(r"deadlift|pull[- ]?ups?", re.IGNORECASE)


def _iter_text_files() -> list[Path]:
    paths: list[Path] = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            path = Path(current) / name
            if path.name.startswith("factory-agent-prompt"):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or name == ".env.example":
                paths.append(path)
    return paths


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_static_programme_module_is_deleted() -> None:
    assert not (ROOT / "backend" / "program.py").exists()


def test_no_module_imports_static_programme() -> None:
    violations = []
    for path in _iter_text_files():
        if path.suffix != ".py":
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if PROGRAM_IMPORT_RE.search(line):
                violations.append(f"{_relative(path)}:{line_number}: {line.strip()}")
    assert violations == []


def test_no_hybrid_powerbuilding_outside_allowlist() -> None:
    violations = []
    for path in _iter_text_files():
        relative = _relative(path)
        if relative == SELF or relative in HYBRID_ALLOWLIST:
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if HYBRID_RE.search(line):
                violations.append(f"{relative}:{line_number}: {line.strip()}")
    assert violations == []


def test_static_template_surfaces_are_gone() -> None:
    forbidden = (
        '"/api/programmes/select"',
        "def get_programme_templates",
        "Available Templates",
        "Active Split Preview",
    )
    code_suffixes = {".py", ".ts", ".html", ".sh", ".yml", ".yaml"}
    violations = []
    for path in _iter_text_files():
        if _relative(path) == SELF or path.suffix.lower() not in code_suffixes:
            continue
        text = _read(path)
        for marker in forbidden:
            if marker in text:
                violations.append(f"{_relative(path)}: {marker}")
    assert violations == []


def test_no_hardcoded_twelve_week_programme_copy() -> None:
    violations = []
    for path in _iter_text_files():
        if _relative(path) == SELF:
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if WEEK_OF_12_RE.search(line):
                violations.append(f"{_relative(path)}:{line_number}: {line.strip()}")
    assert violations == []


def test_generic_runtime_modules_do_not_special_case_named_lifts() -> None:
    violations = []
    for relative in GENERIC_RUNTIME_MODULES:
        path = ROOT / relative
        if not path.exists():
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if LOG_LIFT_RE.search(line):
                violations.append(f"{relative}:{line_number}: {line.strip()}")
    assert violations == []
