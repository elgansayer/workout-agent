"""Policy: stored credentials are reached only through the secret vault.

``secret_vault.SecretVault`` is the one interface to AI-provider keys, connector
keys and OAuth tokens: it seals them per user, never returns them to the browser
and writes an audit row for every lifecycle change. The older helpers in
``database.py`` (``save_user_api_key`` and friends) can read a decrypted secret
or change a credential without an audit row, so production code must not call
them, and the table and the encryption primitives stay inside a short, named
list of modules (see ``docs/TENANT_ISOLATION.md``).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
_SKIPPED_DIRS = {"tests", "__pycache__"}

#: Helpers that hand a decrypted secret to the caller or bypass the audit trail.
UNAUDITED_CREDENTIAL_HELPERS = frozenset(
    {
        "save_user_api_key",
        "get_user_api_key",
        "get_user_api_keys",
        "delete_user_api_key",
    }
)

#: Modules that may name the ``user_api_keys`` table, with the reason.
CREDENTIAL_TABLE_MODULES = {
    "backend/database.py": "schema, ownership guards and the legacy helpers",
    "backend/secret_vault.py": "the vault itself",
    "backend/legacy_ownership.py": "re-seals credentials when assigning legacy data",
    "backend/account_deletion.py": "erasure registry",
    "backend/webapp/health.py": "read-only readiness probe: schema presence only",
    "tools/legacy_data_migration.py": "re-seals credentials when migrating a database",
}

#: Modules that may import the encryption primitives.
ENCRYPTION_MODULES = {
    "backend/secret_vault.py": "the vault itself",
    "backend/database.py": "the legacy helpers and the vault schema upgrade",
    "backend/legacy_ownership.py": "re-seals credentials when assigning legacy data",
    "tools/legacy_data_migration.py": "re-seals credentials when migrating a database",
}


def _production_modules() -> list[Path]:
    backend = [
        path
        for path in BACKEND.rglob("*.py")
        if not _SKIPPED_DIRS.intersection(path.relative_to(BACKEND).parts)
    ]
    tools = [
        path
        for path in (ROOT / "tools").glob("*.py")
        if not path.name.startswith("test_")
    ]
    return sorted([*backend, *tools])


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def helper_uses(source: str) -> list[tuple[int, str]]:
    """Return (line, helper) for every call or import of an unaudited helper."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name in UNAUDITED_CREDENTIAL_HELPERS
            )
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in UNAUDITED_CREDENTIAL_HELPERS:
                found.append((node.lineno, name))
    return found


def imports_encryption(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            alias.name == "encryption" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "encryption":
            return True
    return False


def test_the_scanner_recognises_what_it_forbids() -> None:
    assert helper_uses("from database import get_user_api_keys\n") == [
        (1, "get_user_api_keys")
    ]
    assert helper_uses("database.save_user_api_key(u, 'hevy', k)\n") == [
        (1, "save_user_api_key")
    ]
    assert helper_uses("vault.store(u, 'hevy', api_key=k)\n") == []
    assert imports_encryption("import encryption\n")
    assert imports_encryption("from encryption import seal\n")
    assert not imports_encryption("import hashlib\n")


def test_production_code_never_uses_the_unaudited_credential_helpers() -> None:
    violations = [
        f"{_relative(path)}:{line}: {name}"
        for path in _production_modules()
        if path.name != "database.py"  # the definitions
        for line, name in helper_uses(path.read_text())
    ]
    assert not violations, (
        "go through secret_vault.SecretVault (store/rotate/revoke/delete, "
        "has_usable_credential for readiness):\n" + "\n".join(violations)
    )


def test_only_named_modules_touch_the_credential_table() -> None:
    offenders = [
        _relative(path)
        for path in _production_modules()
        if "user_api_keys" in path.read_text()
        and _relative(path) not in CREDENTIAL_TABLE_MODULES
    ]
    assert not offenders, (
        "use secret_vault.SecretVault instead of reading user_api_keys directly: "
        + ", ".join(offenders)
    )


def test_only_named_modules_import_the_encryption_primitives() -> None:
    offenders = [
        _relative(path)
        for path in _production_modules()
        if path.name != "encryption.py"
        and imports_encryption(path.read_text())
        and _relative(path) not in ENCRYPTION_MODULES
    ]
    assert not offenders, (
        "encryption is the vault's primitive; feature code must not seal or open "
        "secrets itself: " + ", ".join(offenders)
    )


def test_every_allowlisted_module_still_needs_its_entry() -> None:
    """A stale allow-list entry would silently widen the boundary."""
    for allowed, text in ((CREDENTIAL_TABLE_MODULES, "user_api_keys"),):
        stale = [
            module for module in allowed if text not in (ROOT / module).read_text()
        ]
        assert not stale, f"remove {stale} from the allow-list"
    stale_encryption = [
        module
        for module in ENCRYPTION_MODULES
        if not imports_encryption((ROOT / module).read_text())
    ]
    assert not stale_encryption, f"remove {stale_encryption} from the allow-list"
