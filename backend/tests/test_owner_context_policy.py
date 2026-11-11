"""Policy: repositories and services require an authenticated owner context.

A repository or service method must not accept an arbitrary user identifier. It
takes the :class:`~tenant_isolation.AuthenticatedOwner` that
:func:`~tenant_isolation.authorize_owner` issued after re-reading the account,
and it refuses anything else at runtime. These checks keep that true as the code
grows: the context cannot be forged, converted repositories cannot regress to a
``user_id: str`` parameter, and no HTTP route or request model lets a client
nominate the owner. See ``docs/TENANT_ISOLATION.md``.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import pytest
from database import get_or_create_user, init_db
from programme_domain import ProgrammeRepository, ProgrammeService
from tenant_isolation import (
    AuthenticatedOwner,
    OwnerIdentity,
    OwnershipError,
    authorize_owner,
    require_owner,
)

BACKEND = Path(__file__).resolve().parents[1]
_SKIPPED_DIRS = {"tests", "__pycache__"}

#: Classes whose public methods must take ``owner: AuthenticatedOwner``. Add a
#: repository or service here when it is converted; the check keeps it converted.
OWNER_CONTEXT_CLASSES: dict[str, tuple[str, ...]] = {
    "programme_domain/repository.py": ("ProgrammeRepository",),
    "programme_domain/service.py": ("ProgrammeService",),
}

#: Identifiers that name an account. A client must never be able to send one.
OWNER_FIELD_NAMES = frozenset(
    {
        "user_id",
        "owner",
        "owner_id",
        "account_id",
        "tenant_id",
        "subject_user_id",
        "target_user_id",
    }
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "owner-context.db")
    init_db(path)
    return path


def _user(db: str, email: str) -> str:
    return str(get_or_create_user(email, db_path=db)["id"])


def _production_modules() -> list[Path]:
    return sorted(
        path
        for path in BACKEND.rglob("*.py")
        if not _SKIPPED_DIRS.intersection(path.relative_to(BACKEND).parts)
    )


# ---------------------------------------------------------------------------
# The context cannot be forged
# ---------------------------------------------------------------------------


def test_authorize_owner_issues_a_context_for_the_stored_account(db: str) -> None:
    user_id = _user(db, "owner@example.test")
    owner = authorize_owner(db, user_id)
    assert isinstance(owner, AuthenticatedOwner)
    assert isinstance(owner, OwnerIdentity)
    assert owner.user_id == user_id
    assert require_owner(owner) is owner


def test_equality_follows_the_account_but_authority_follows_the_type(db: str) -> None:
    user_id = _user(db, "owner@example.test")
    owner = authorize_owner(db, user_id)
    claim = OwnerIdentity(user_id)
    assert owner == claim and hash(owner) == hash(claim)
    # ... yet the unverified claim never satisfies a repository.
    with pytest.raises(OwnershipError):
        require_owner(claim)


@pytest.mark.parametrize(
    "forged",
    [
        "a-user-id-from-a-request",
        None,
        42,
        {"user_id": "someone"},
        OwnerIdentity("someone"),
    ],
)
def test_nothing_but_an_issued_context_satisfies_require_owner(forged: Any) -> None:
    with pytest.raises(OwnershipError):
        require_owner(forged)


def test_a_context_cannot_be_constructed_or_redirected(db: str) -> None:
    owner = authorize_owner(db, _user(db, "owner@example.test"))
    with pytest.raises(OwnershipError):
        AuthenticatedOwner("someone-else")
    with pytest.raises(OwnershipError):
        AuthenticatedOwner(user_id="someone-else")
    # Copying an issued context to name another account is the obvious bypass.
    with pytest.raises(OwnershipError):
        dataclasses.replace(owner, user_id="someone-else")
    with pytest.raises(dataclasses.FrozenInstanceError):
        owner.user_id = "someone-else"  # type: ignore[misc]


@pytest.mark.parametrize("claim", [None, "", "no-such-owner", "bad\nid"])
def test_a_context_is_never_issued_for_an_unverifiable_claim(db: str, claim: Any) -> None:
    with pytest.raises(OwnershipError):
        authorize_owner(db, claim)


def test_only_the_tenant_isolation_module_issues_contexts() -> None:
    offenders: list[str] = []
    for path in _production_modules():
        if path.name == "tenant_isolation.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name in {"AuthenticatedOwner", "_issue"}:
                    offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == [], "contexts must come from authorize_owner: " + ", ".join(offenders)


# ---------------------------------------------------------------------------
# Repositories and services refuse anything else
# ---------------------------------------------------------------------------


def _public_methods(cls: type) -> list[tuple[str, Any]]:
    return [
        (name, member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    ]


@pytest.mark.parametrize("cls", [ProgrammeRepository, ProgrammeService])
def test_every_public_method_refuses_a_bare_user_id_before_touching_data(
    db: str, cls: type
) -> None:
    instance = cls(db)
    methods = _public_methods(cls)
    assert len(methods) >= 10, "the policy no longer finds the public methods"
    user_id = _user(db, "owner@example.test")
    for name, _member in methods:
        if name == "transaction":  # a connection helper, not an owner-scoped operation
            continue
        bound = getattr(instance, name)
        parameters = list(inspect.signature(bound).parameters.values())
        required = [p for p in parameters if p.default is inspect.Parameter.empty]
        positional = [p for p in required if p.kind is not inspect.Parameter.KEYWORD_ONLY]
        keyword_only = {
            p.name: None for p in required if p.kind is inspect.Parameter.KEYWORD_ONLY
        }
        for candidate in (user_id, OwnerIdentity(user_id)):
            args = [candidate] + [None] * (len(positional) - 1)
            with pytest.raises(OwnershipError):
                bound(*args, **keyword_only)


def _class_methods(source: str, class_name: str) -> list[ast.FunctionDef]:
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [n for n in node.body if isinstance(n, ast.FunctionDef)]
    raise AssertionError(f"{class_name} not found")


def test_converted_classes_take_an_owner_and_never_a_user_id() -> None:
    violations: list[str] = []
    for relative, class_names in OWNER_CONTEXT_CLASSES.items():
        source = (BACKEND / relative).read_text()
        for class_name in class_names:
            for method in _class_methods(source, class_name):
                if method.name.startswith("_") or method.name == "transaction":
                    continue
                params = [a for a in method.args.args if a.arg != "self"]
                where = f"{relative}:{class_name}.{method.name}"
                names = {a.arg for a in (*params, *method.args.kwonlyargs)}
                if names & OWNER_FIELD_NAMES - {"owner"}:
                    violations.append(f"{where} accepts a raw owner identifier")
                if not params or params[0].arg != "owner":
                    violations.append(f"{where} must take 'owner' first")
                elif ast.unparse(params[0].annotation) != "AuthenticatedOwner":
                    violations.append(f"{where}: owner must be AuthenticatedOwner")
    assert violations == [], "\n".join(violations)


# ---------------------------------------------------------------------------
# A client can never nominate the owner
# ---------------------------------------------------------------------------


def _web_modules() -> list[Path]:
    return sorted((BACKEND / "webapp").glob("*.py"))


def _route_handlers(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    handlers: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            target = call.func if call else decorator
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in {"app", "router"}
                and target.attr
                in {"get", "post", "put", "patch", "delete", "api_route", "websocket"}
            ):
                handlers.append(node)
                break
    return handlers


def test_no_route_parameter_lets_a_client_choose_the_owner() -> None:
    offenders: list[str] = []
    seen = 0
    for path in _web_modules():
        tree = ast.parse(path.read_text())
        for handler in _route_handlers(tree):
            seen += 1
            arguments = handler.args
            for arg in (*arguments.args, *arguments.kwonlyargs):
                if arg.arg in OWNER_FIELD_NAMES:
                    offenders.append(f"{path.name}:{handler.lineno} {handler.name}({arg.arg})")
    assert seen > 40, "the scanner no longer finds the route handlers"
    assert offenders == [], "\n".join(offenders)


def test_no_request_model_declares_an_owner_field() -> None:
    offenders: list[str] = []
    models = 0
    for path in _web_modules():
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(ast.unparse(base).endswith("BaseModel") for base in node.bases):
                continue
            models += 1
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id in OWNER_FIELD_NAMES
                ):
                    offenders.append(f"{path.name}:{node.name}.{statement.target.id}")
    assert models >= 3, "the scanner no longer finds the request models"
    assert offenders == [], "\n".join(offenders)
