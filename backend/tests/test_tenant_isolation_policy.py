"""Policy: production code never calls a tenant-aware database helper unscoped.

``database.py`` keeps single-tenant fallbacks for callers that omit ``user_id``:
some resolve the legacy tenant, others read or delete across *every* tenant.
Request handlers and workers must therefore always say whose data they mean.
This scan fails when a production module calls a helper that accepts
``user_id`` without supplying one, so a forgotten argument cannot silently
widen a query to all accounts (see ``docs/TENANT_ISOLATION.md``).
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
_SKIPPED_DIRS = {"tests", "__pycache__"}
_DEFINITION_MODULE = "database.py"


def tenant_aware_functions(source: str) -> dict[str, int | None]:
    """Map helper name -> positional index of ``user_id`` (None if keyword-only)."""
    found: dict[str, int | None] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        positional = [arg.arg for arg in node.args.args]
        keyword_only = [arg.arg for arg in node.args.kwonlyargs]
        if "user_id" in positional:
            found[node.name] = positional.index("user_id")
        elif "user_id" in keyword_only:
            found[node.name] = None
    return found


def unscoped_calls(source: str, scoped: dict[str, int | None]) -> list[tuple[int, str]]:
    """Return (line, helper) for each call that does not pass an owner."""
    violations: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name not in scoped:
            continue
        index = scoped[name]
        passes_owner = (
            any(keyword.arg == "user_id" for keyword in node.keywords)
            or any(keyword.arg is None for keyword in node.keywords)  # **kwargs
            or (index is not None and len(node.args) > index)
            or any(isinstance(arg, ast.Starred) for arg in node.args)
        )
        if not passes_owner:
            violations.append((node.lineno, name))
    return violations


def _production_modules() -> list[Path]:
    return sorted(
        path
        for path in BACKEND.rglob("*.py")
        if not _SKIPPED_DIRS.intersection(path.relative_to(BACKEND).parts)
        and path.name != _DEFINITION_MODULE
    )


def test_production_code_never_calls_tenant_aware_helpers_without_an_owner() -> None:
    scoped = tenant_aware_functions((BACKEND / _DEFINITION_MODULE).read_text())
    assert len(scoped) > 20, "the scanner no longer finds the tenant-aware helpers"

    violations = [
        f"{path.relative_to(BACKEND)}:{line}: {name}() called without user_id"
        for path in _production_modules()
        for line, name in unscoped_calls(path.read_text(), scoped)
    ]

    assert violations == [], "\n".join(violations)


def test_the_scanner_finds_the_known_unscoped_fallback_helpers() -> None:
    scoped = tenant_aware_functions((BACKEND / _DEFINITION_MODULE).read_text())
    # These helpers read or delete across tenants when user_id is omitted.
    for name in (
        "get_meta",
        "get_body_metrics",
        "save_body_metrics",
        "get_routine_record",
    ):
        assert name in scoped


def test_the_scanner_flags_an_omitted_owner_and_accepts_a_supplied_one() -> None:
    scoped = tenant_aware_functions(
        "def get_meta(key, db_path=None, *, user_id=None): ...\n"
        "def save_note(db_path, user_id=None): ...\n"
        "def unrelated(key, db_path): ...\n"
    )
    # keyword-only owner, owner at positional index 1, and a non-tenant helper
    assert scoped == {"get_meta": None, "save_note": 1}

    source = (
        "get_meta('key', db)\n"  # 1: flagged
        "get_meta('key', db, user_id=owner)\n"  # 2: ok
        "database.get_meta('key', db)\n"  # 3: flagged (attribute call)
        "save_note(db)\n"  # 4: positional owner missing -> flagged
        "save_note(db, owner)\n"  # 5: ok
        "get_meta('key', db, **scope)\n"  # 6: ok (cannot be proven wrong)
        "save_note(*args)\n"  # 7: ok (cannot be proven wrong)
        "unrelated('key', db)\n"  # 8: not a tenant-aware helper
    )

    assert unscoped_calls(source, scoped) == [
        (1, "get_meta"),
        (3, "get_meta"),
        (4, "save_note"),
    ]
