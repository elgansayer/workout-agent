"""Static guards that keep the tenant ownership boundary from eroding.

These are architecture tests: they read the production source and fail when a new
repository or service method accepts a bare user identifier, executes SQL outside
the reviewed modules, forges an owner context, keeps a process-wide cache, or when
a route stops deriving its owner from the signed session.  Every exception is
listed with the reason it is acceptable, so adding one is a reviewed decision.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_SKIPPED_PARTS = {"tests", "__pycache__"}

#: Parameter names that carry a bare tenant identifier.
_RAW_IDENTIFIER_NAMES = frozenset(
    {"user_id", "uid", "owner_id", "tenant_id", "account_id"}
)

#: ``module:function`` pairs that may accept a bare identifier, each for a
#: reviewed reason.  Everything else must take an ``AuthenticatedOwner``.
_RAW_IDENTIFIER_ALLOWLIST: dict[str, str] = {
    "ownership.py:owner_for_user": (
        "the factory that verifies an identifier against the users table and "
        "mints an AuthenticatedOwner"
    ),
    "ownership.py:__init__": (
        "the sealed AuthenticatedOwner constructor; it rejects a call without the "
        "private mint token"
    ),
    "scheduler.py:_run_coaching": (
        "process boundary: a server-selected id from the users table is passed to "
        "'main.py --user-id', which re-verifies it with owner_for_user"
    ),
    "webapp/mutation_security.py:_claims_match_authenticated_user": (
        "HTTP boundary helper that compares client-supplied owner claims with the "
        "signed session identity; it never selects a record owner"
    ),
}

#: The staged health platform and connector contracts use the integer health
#: tenant identity, which is not yet unified with ``users.id``.  Their writers
#: validate against an authenticated identity (``authenticated_user_id``) and
#: their tables enforce the non-null, positive, indexed, immutable owner.
_STAGED_IDENTITY_PREFIXES = ("health_", "connectors/")

#: Production modules that execute SQL.  Anything else must go through the
#: repository layer in ``database.py``.
_SQL_MODULES: dict[str, str] = {
    "database.py": "the tenant-scoped repository layer",
    "ownership.py": "owner existence checks and the ownership contract audit",
    "webapp/csrf_security.py": "single-use CSRF nonces bound to an owner context",
    "webapp/health.py": "read-only authenticated diagnostics for one owner context",
    "scripts/sync_history.py": "owner-scoped history rebuild",
    "health_migration.py": "additive staged health schema",
    "health_sqlite_repository.py": "staged health repository (integer identity)",
}

#: Modules whose SQL binds a tenant id and must obtain it from ``owner_id``.
_OWNER_ID_REQUIRED = (
    "database.py",
    "webapp/csrf_security.py",
    "webapp/health.py",
    "scripts/sync_history.py",
)

#: Repository functions that intentionally have no owner argument.
_OWNERLESS_REPOSITORY_FUNCTIONS: dict[str, str] = {
    "init_db": "schema creation and migration",
    "get_or_create_user": "creates the user row for a verified sign-in email",
    "get_all_users": "tenant enumeration for the background-job dispatcher only",
    "get_legacy_user_id": "single-tenant compatibility bootstrap",
    "get_programme_templates": "deprecated empty shim; reads no data",
}


def _production_modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(_BACKEND.rglob("*.py")):
        relative = path.relative_to(_BACKEND)
        if set(relative.parts) & _SKIPPED_PARTS:
            continue
        yield relative.as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _is_staged_identity_module(module: str) -> bool:
    return module.startswith(_STAGED_IDENTITY_PREFIXES)


def test_no_function_accepts_a_bare_user_identifier_without_review() -> None:
    offenders: list[str] = []
    used_exceptions: set[str] = set()
    for module, tree in _production_modules():
        if _is_staged_identity_module(module):
            continue
        for function in _functions(tree):
            names = _parameter_names(function) & _RAW_IDENTIFIER_NAMES
            if not names:
                continue
            key = f"{module}:{function.name}"
            if key in _RAW_IDENTIFIER_ALLOWLIST:
                used_exceptions.add(key)
                continue
            offenders.append(f"{key}({', '.join(sorted(names))})")

    assert not offenders, (
        "These functions accept a bare tenant identifier. Take an "
        "AuthenticatedOwner instead, or add a reviewed entry with a reason to "
        f"_RAW_IDENTIFIER_ALLOWLIST: {offenders}"
    )
    stale = set(_RAW_IDENTIFIER_ALLOWLIST) - used_exceptions
    assert not stale, f"Remove allowlist entries that no longer exist: {sorted(stale)}"


def test_every_repository_function_requires_an_authenticated_owner() -> None:
    tree = next(tree for module, tree in _production_modules() if module == "database.py")
    owned: list[str] = []
    ownerless: set[str] = set()
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        if node.name in _OWNERLESS_REPOSITORY_FUNCTIONS:
            ownerless.add(node.name)
            continue
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        owner_args = [a for a in args if a.arg == "owner"]
        if len(owner_args) != 1:
            problems.append(f"{node.name}: no 'owner' parameter")
            continue
        annotation = ast.unparse(owner_args[0].annotation) if owner_args[0].annotation else ""
        if annotation != "AuthenticatedOwner":
            problems.append(f"{node.name}: owner is annotated {annotation!r}")
        owned.append(node.name)
        # A repository function must never also accept a bare identifier that
        # could be mistaken for the tenant.
        leaked = _parameter_names(node) & _RAW_IDENTIFIER_NAMES
        if leaked:
            problems.append(f"{node.name}: also accepts {sorted(leaked)}")

    assert not problems, problems
    assert len(owned) >= 40, "the repository surface is unexpectedly small"
    assert ownerless == set(_OWNERLESS_REPOSITORY_FUNCTIONS)


def test_repository_sql_binds_the_owner_only_through_the_checked_accessor() -> None:
    by_module = dict(_production_modules())
    problems: list[str] = []
    for module in _OWNER_ID_REQUIRED:
        tree = by_module[module]
        imports_owner_id = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "ownership"
            and any(alias.name == "owner_id" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imports_owner_id:
            problems.append(f"{module} does not import owner_id")
        # A tenant id must not be read straight off the context object.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "user_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == "owner"
            ):
                problems.append(f"{module}:{node.lineno} reads owner.user_id directly")
    assert not problems, problems


_SQL_CALL_ATTRIBUTES = frozenset({"execute", "executemany", "executescript"})


def _executes_sql(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in _SQL_CALL_ATTRIBUTES:
            return True
        if (
            node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
        ):
            return True
    return False


def test_sql_is_only_executed_by_reviewed_modules() -> None:
    found = {module for module, tree in _production_modules() if _executes_sql(tree)}
    assert found == set(_SQL_MODULES), (
        "A module executes SQL outside the reviewed set. Route the access through "
        "database.py (tenant-scoped) or add a reviewed entry with a reason: "
        f"unexpected={sorted(found - set(_SQL_MODULES))} "
        f"missing={sorted(set(_SQL_MODULES) - found)}"
    )


def test_owner_contexts_are_only_minted_by_the_ownership_module() -> None:
    constructors: list[str] = []
    mint_token_users: list[str] = []
    for module, tree in _production_modules():
        if module == "ownership.py":
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AuthenticatedOwner"
            ):
                constructors.append(f"{module}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id == "_MINT_TOKEN":
                mint_token_users.append(f"{module}:{node.lineno}")
    assert not constructors, f"AuthenticatedOwner(...) called outside ownership.py: {constructors}"
    assert not mint_token_users, f"_MINT_TOKEN used outside ownership.py: {mint_token_users}"

    # Tests may mint synthetic contexts, but only through the shared fixture so
    # the escape hatch stays in one reviewable place.
    token_in_tests = [
        path.name
        for path in sorted((_BACKEND / "tests").glob("*.py"))
        if "_MINT_TOKEN" in path.read_text(encoding="utf-8")
    ]
    assert set(token_in_tests) <= {
        "tenant_fixtures.py",
        "test_ownership.py",
        "test_tenant_boundaries.py",
    }, token_in_tests


def test_tenant_enumeration_is_reserved_for_the_job_dispatcher() -> None:
    importers = {
        module
        for module, tree in _production_modules()
        if module != "database.py"
        and any(
            (isinstance(node, ast.ImportFrom) and any(a.name == "get_all_users" for a in node.names))
            or (isinstance(node, ast.Attribute) and node.attr == "get_all_users")
            for node in ast.walk(tree)
        )
    }
    assert importers == {"scheduler.py"}, (
        "get_all_users returns every tenant's email; only the scheduler may use it, "
        f"found {sorted(importers)}"
    )


def test_every_provider_resolution_names_its_owner() -> None:
    definitions = {"ai_resolver.py", "ai_provider.py"}
    problems: list[str] = []
    calls = 0
    for module, tree in _production_modules():
        if module in definitions:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name != "resolve_provider":
                continue
            calls += 1
            if "owner" not in {kw.arg for kw in node.keywords}:
                problems.append(f"{module}:{node.lineno}")
    assert calls >= 4, "expected the webapp, agent, insight and check-in call sites"
    assert not problems, (
        "resolve_provider() without owner= would silently fall back to the shared "
        f"server key: {problems}"
    )


def test_no_process_wide_cache_can_hold_tenant_data_unreviewed() -> None:
    cached_functions: list[str] = []
    cache_globals: list[str] = []
    for module, tree in _production_modules():
        for function in _functions(tree):
            for decorator in function.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                if name in {"lru_cache", "cache", "cached_property"}:
                    cached_functions.append(f"{module}:{function.name}")
        for node in tree.body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Name) and "cache" in target.id.lower():
                    cache_globals.append(f"{module}:{target.id}")
    assert not cached_functions, (
        "Memoised functions can serve one tenant's result to another. Key the "
        f"cache by owner and review it: {cached_functions}"
    )
    assert not cache_globals, f"Module-level cache without review: {cache_globals}"


_ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "api_route"}
#: Routes under these prefixes expose or change tenant data.
_TENANT_ROUTE_PREFIXES = ("/api/", "/google-health/")
_ROUTES_WITHOUT_OWNER_CONTEXT: dict[str, str] = {
    "/api/me": "returns only the signed session's own identity; reads no records",
}


def _route_paths(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    paths: list[str] = []
    for decorator in function.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in _ROUTE_DECORATORS
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "app"
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ):
            paths.append(decorator.args[0].value)
    return paths


def test_every_tenant_route_derives_its_owner_from_the_session() -> None:
    tree = next(tree for module, tree in _production_modules() if module == "webapp/app.py")
    checked = 0
    problems: list[str] = []
    for function in _functions(tree):
        for path in _route_paths(function):
            if not path.startswith(_TENANT_ROUTE_PREFIXES):
                continue
            if path in _ROUTES_WITHOUT_OWNER_CONTEXT:
                continue
            checked += 1
            calls_auth = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_check_api_auth"
                for node in ast.walk(function)
            )
            if not calls_auth:
                problems.append(f"{path} ({function.name})")
    assert checked >= 25, f"only {checked} tenant routes were found"
    assert not problems, (
        "These routes do not call _check_api_auth, so they have no authenticated "
        f"owner context: {problems}"
    )


_OWNER_PARAMETER_NAMES = frozenset(
    {
        "user_id",
        "owner_id",
        "uid",
        "owner",
        "account_id",
        "tenant_id",
        "subject_user_id",
        "target_user_id",
    }
)


def _model_field_names(annotation: Any, seen: set[type]) -> set[str]:
    """Collect field names of any Pydantic model reachable from *annotation*."""

    from pydantic import BaseModel

    if get_origin(annotation) is not None:
        names: set[str] = set()
        for argument in get_args(annotation):
            names |= _model_field_names(argument, seen)
        return names
    if (
        isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
        and annotation not in seen
    ):
        seen.add(annotation)
        names = set()
        for name, field in annotation.model_fields.items():
            names |= {name, field.alias or name}
            names |= _model_field_names(field.annotation, seen)
        return names
    return set()


def test_no_route_accepts_a_client_supplied_owner() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("authlib")
    from fastapi.routing import APIRoute

    app_module = importlib.import_module("webapp.app")
    inspected = 0
    offenders: list[str] = []
    for route in app_module.app.routes:
        if not isinstance(route, APIRoute):
            continue
        inspected += 1
        dependant = route.dependant
        declared: set[str] = set()
        for group in (
            dependant.path_params,
            dependant.query_params,
            dependant.header_params,
            dependant.cookie_params,
        ):
            for parameter in group:
                declared |= {parameter.name, parameter.alias}
        for parameter in dependant.body_params:
            declared |= {parameter.name, parameter.alias}
            annotation = getattr(parameter, "type_", None) or parameter.field_info.annotation
            declared |= _model_field_names(annotation, set())
        clash = {name.lower() for name in declared} & _OWNER_PARAMETER_NAMES
        if clash:
            offenders.append(f"{route.path}: {sorted(clash)}")
    assert inspected >= 35, "the route inventory is unexpectedly small"
    assert not offenders, (
        "A route declares an owner identifier the client can set. Ownership must "
        f"come from the signed session only: {offenders}"
    )


def test_registered_health_tables_are_the_only_ones_without_an_owner_foreign_key() -> None:
    """The deferred foreign key stays confined to the staged health tables."""

    from ownership import USER_SCOPED_TABLES

    deferred = sorted(t.name for t in USER_SCOPED_TABLES if not t.require_foreign_key)
    assert deferred == ["health_connections", "health_metrics", "health_sync_runs"]
    for module in ("health_sqlite_repository.py", "health_schema.py"):
        assert (_BACKEND / module).exists()
