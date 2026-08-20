"""Generate canonical route, environment, connector, and workflow inventories."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
PUBLIC_ROUTES = {
    "/login",
    "/login/google",
    "/logout",
    "/auth",
    "/google-health/callback",
    "/favicon.ico",
    "/sw.js",
}
PUBLIC_PREFIXES = ("/static", "/assets")
SENSITIVE_ENV_TOKENS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "API_KEY",
    "PRIVATE_KEY",
    "ENCRYPTION_KEY",
)
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
}
ENV_REGEX = re.compile(r"\$\{([A-Z][A-Z0-9_]+)(?::-[^}]*)?\}")
WORKFLOW_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def _owner_for_path(path: Path) -> str:
    parts = path.parts
    if not parts:
        return "repository"
    if parts[0] == ".github":
        return "github-actions"
    if parts[0] == "frontend":
        return "frontend"
    if parts[0] == "backend":
        if len(parts) > 1 and parts[1] == "connectors":
            return "connectors"
        if len(parts) > 1 and parts[1] == "webapp":
            return "webapp"
        return "backend"
    if parts[0].startswith("docker") or path.name.startswith("docker-compose"):
        return "deployment"
    return parts[0]


def _test_files(root: Path) -> list[Path]:
    tests = []
    for path in (root / "backend" / "tests").glob("test_*.py"):
        if path.is_file():
            tests.append(path)
    frontend_src = root / "frontend" / "src"
    if frontend_src.exists():
        for path in frontend_src.rglob("*.spec.ts"):
            if path.is_file():
                tests.append(path)
    return sorted(tests)


def _read_test_corpus(root: Path) -> dict[Path, str]:
    corpus: dict[Path, str] = {}
    for path in _test_files(root):
        try:
            corpus[path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return corpus


def _coverage_for(
    needles: Iterable[str], corpus: dict[Path, str], root: Path
) -> dict[str, Any]:
    normalized = [needle for needle in needles if needle]
    matches = []
    for path, text in corpus.items():
        if any(needle in text for needle in normalized):
            matches.append(path.relative_to(root).as_posix())
    return {"status": "covered" if matches else "uncovered", "files": sorted(matches)}


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _route_sensitivity(path: str, is_public: bool) -> str:
    if is_public:
        return "public"
    lowered = path.lower()
    if any(token in lowered for token in ("settings", "auth", "google-health", "key")):
        return "credentials"
    if any(
        token in lowered
        for token in ("progress", "stats", "history", "checkin", "health", "metric")
    ):
        return "health-data"
    if any(
        token in lowered
        for token in ("programme", "program", "plan", "dashboard", "workout")
    ):
        return "workout-data"
    if "chat" in lowered or "rag" in lowered or "reasoning" in lowered:
        return "conversation-data"
    return "personal-data"


def discover_routes(root: Path, corpus: dict[Path, str]) -> list[dict[str, Any]]:
    app_path = root / "backend" / "webapp" / "app.py"
    if not app_path.exists():
        return []
    tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    routes: list[dict[str, Any]] = []
    methods_by_decorator = {
        "get": ["GET"],
        "post": ["POST"],
        "put": ["PUT"],
        "patch": ["PATCH"],
        "delete": ["DELETE"],
        "head": ["HEAD"],
        "options": ["OPTIONS"],
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(
                decorator.func, ast.Attribute
            ):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                continue
            route_path = _literal_str(decorator.args[0] if decorator.args else None)
            if not route_path:
                continue
            name = decorator.func.attr
            methods = methods_by_decorator.get(name)
            if name == "api_route":
                methods = []
                for kw in decorator.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        methods = [
                            value
                            for elt in kw.value.elts
                            if (value := _literal_str(elt))
                        ]
                if not methods:
                    methods = ["GET"]
            if not methods:
                continue
            public = route_path in PUBLIC_ROUTES or route_path.startswith(PUBLIC_PREFIXES)
            coverage = _coverage_for((route_path, node.name), corpus, root)
            for method in sorted(set(methods)):
                route_id = f"{method} {route_path}"
                routes.append(
                    {
                        "id": route_id,
                        "method": method,
                        "path": route_path,
                        "handler": node.name,
                        "source": app_path.relative_to(root).as_posix(),
                        "owner": "webapp",
                        "sensitivity": _route_sensitivity(route_path, public),
                        "authentication": "public" if public else "session-required",
                        "test_coverage": coverage,
                    }
                )
    return sorted(routes, key=lambda item: (item["path"], item["method"], item["handler"]))


def _python_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr == "getenv"
            ):
                name = _literal_str(node.args[0] if node.args else None)
                if name:
                    names.add(name)
            if (
                isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "os"
                and node.func.value.attr == "environ"
                and node.func.attr == "get"
            ):
                name = _literal_str(node.args[0] if node.args else None)
                if name:
                    names.add(name)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
        ):
            name = _literal_str(node.slice)
            if name:
                names.add(name)
    return names


def _iter_source_files(root: Path) -> Iterable[Path]:
    suffixes = {".py", ".yml", ".yaml", ".toml", ".json", ".sh", ".ts", ".html"}
    named = {".env.example", "Dockerfile", "Dockerfile.web"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix in suffixes or path.name in named or path.name.startswith("docker-compose"):
            yield path


def discover_environment(root: Path, corpus: dict[Path, str]) -> list[dict[str, Any]]:
    locations: dict[str, set[str]] = defaultdict(set)
    for path in _iter_source_files(root):
        relative = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            for name in _python_env_names(path):
                locations[name].add(relative)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in ENV_REGEX.findall(text):
            locations[name].add(relative)
    example_path = root / ".env.example"
    documented_text = (
        example_path.read_text(encoding="utf-8", errors="replace")
        if example_path.exists()
        else ""
    )
    result = []
    for name, sources in sorted(locations.items()):
        sensitive = any(token in name.upper() for token in SENSITIVE_ENV_TOKENS)
        owner = _owner_for_path(Path(sorted(sources)[0])) if sources else "repository"
        result.append(
            {
                "id": name,
                "name": name,
                "sources": sorted(sources),
                "owner": owner,
                "sensitivity": "secret" if sensitive else "configuration",
                "authentication": "secret-value" if sensitive else "not-applicable",
                "documented_in_env_example": name in documented_text,
                "test_coverage": _coverage_for((name,), corpus, root),
            }
        )
    return result


def _extract_string_collection(node: ast.AST | None) -> list[str]:
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        values = [_literal_str(elt) for elt in node.elts]
        return sorted(value for value in values if value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
    ):
        return _extract_string_collection(node.args[0])
    return []


def discover_connectors(root: Path, corpus: dict[Path, str]) -> list[dict[str, Any]]:
    connectors_dir = root / "backend" / "connectors"
    result = []
    if not connectors_dir.exists():
        return result
    for path in sorted(connectors_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            provider = None
            authorize = None
            metrics: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "provider":
                            provider = _literal_str(stmt.value)
                        if (
                            isinstance(target, ast.Name)
                            and target.id == "capabilities"
                            and isinstance(stmt.value, ast.Call)
                        ):
                            for kw in stmt.value.keywords:
                                if kw.arg == "authorize":
                                    authorize = _literal_bool(kw.value)
                                elif kw.arg == "metrics":
                                    metrics = _extract_string_collection(kw.value)
            if not provider:
                continue
            rel = path.relative_to(root).as_posix()
            result.append(
                {
                    "id": provider,
                    "provider": provider,
                    "class": node.name,
                    "source": rel,
                    "owner": "connectors",
                    "sensitivity": "health-data",
                    "authentication": "oauth" if authorize else "provider-specific",
                    "authorize_supported": bool(authorize),
                    "metrics": metrics,
                    "test_coverage": _coverage_for((provider, node.name), corpus, root),
                }
            )
    return sorted(result, key=lambda item: item["id"])


def _workflow_triggers(text: str) -> list[str]:
    lines = text.splitlines()
    triggers: set[str] = set()
    on_indent = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "on:":
            on_indent = len(line) - len(line.lstrip())
            for following in lines[idx + 1 :]:
                if not following.strip() or following.lstrip().startswith("#"):
                    continue
                indent = len(following) - len(following.lstrip())
                if indent <= on_indent:
                    break
                if indent == on_indent + 2:
                    token = following.strip().split(":", 1)[0].strip()
                    if token and not token.startswith("-"):
                        triggers.add(token)
            break
        if stripped.startswith("on: [") and stripped.endswith("]"):
            inner = stripped[len("on: [") : -1]
            triggers.update(part.strip() for part in inner.split(",") if part.strip())
            break
        if stripped.startswith("on:") and stripped != "on:":
            token = stripped.split(":", 1)[1].strip()
            if token:
                triggers.add(token)
            break
    return sorted(triggers)


def discover_workflows(root: Path, corpus: dict[Path, str]) -> list[dict[str, Any]]:
    workflow_dir = root / ".github" / "workflows"
    result = []
    if not workflow_dir.exists():
        return result
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = WORKFLOW_NAME_RE.search(text)
        name = match.group(1).strip(" '\"") if match else path.stem
        secret_bearing = "secrets." in text or "GITHUB_TOKEN" in text
        rel = path.relative_to(root).as_posix()
        result.append(
            {
                "id": rel,
                "name": name,
                "source": rel,
                "owner": "github-actions",
                "sensitivity": "secret-context" if secret_bearing else "build-metadata",
                "authentication": (
                    "github-token"
                    if secret_bearing or "permissions:" in text
                    else "not-applicable"
                ),
                "triggers": _workflow_triggers(text),
                "uses_secrets": secret_bearing,
                "test_coverage": _coverage_for((path.name, name), corpus, root),
            }
        )
    return result


def build_views(inventory: dict[str, Any]) -> dict[str, Any]:
    routes = inventory["routes"]
    env = inventory["environment_variables"]
    connectors = inventory["connectors"]
    workflows = inventory["workflows"]
    return {
        "security": {
            "routes": [item["id"] for item in routes if item["sensitivity"] != "public"],
            "environment_variables": [
                item["id"] for item in env if item["sensitivity"] == "secret"
            ],
            "connectors": [item["id"] for item in connectors],
            "workflows": [item["id"] for item in workflows if item["uses_secrets"]],
        },
        "onboarding": {
            "environment_variables": [
                item["id"] for item in env if item["documented_in_env_example"]
            ],
            "connectors": [item["id"] for item in connectors],
        },
        "documentation": {
            "routes": [item["id"] for item in routes],
            "connectors": [item["id"] for item in connectors],
            "workflows": [item["id"] for item in workflows],
        },
        "tests": {
            "uncovered_routes": [
                item["id"]
                for item in routes
                if item["test_coverage"]["status"] == "uncovered"
            ],
            "uncovered_environment_variables": [
                item["id"]
                for item in env
                if item["test_coverage"]["status"] == "uncovered"
            ],
            "uncovered_connectors": [
                item["id"]
                for item in connectors
                if item["test_coverage"]["status"] == "uncovered"
            ],
        },
    }


def build_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve()
    corpus = _read_test_corpus(root)
    inventory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "routes": discover_routes(root, corpus),
        "environment_variables": discover_environment(root, corpus),
        "connectors": discover_connectors(root, corpus),
        "workflows": discover_workflows(root, corpus),
    }
    inventory["views"] = build_views(inventory)
    return inventory


def validate_inventory(inventory: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if inventory.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version is missing or unsupported")
    required = ("id", "owner", "sensitivity", "authentication", "test_coverage")
    for section in ("routes", "environment_variables", "connectors", "workflows"):
        items = inventory.get(section)
        if not isinstance(items, list) or not items:
            errors.append(f"{section} inventory is empty")
            continue
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                errors.append(f"{section} contains a non-object entry")
                continue
            for field in required:
                if field not in item:
                    errors.append(f"{section}:{item.get('id', '?')} missing {field}")
            item_id = str(item.get("id", ""))
            if item_id in seen:
                errors.append(f"{section} contains duplicate id {item_id}")
            seen.add(item_id)
            coverage = item.get("test_coverage")
            if not isinstance(coverage, dict) or coverage.get("status") not in {
                "covered",
                "uncovered",
            }:
                errors.append(f"{section}:{item_id} has invalid test_coverage metadata")
    views = inventory.get("views")
    if not isinstance(views, dict) or set(views) != {
        "security",
        "onboarding",
        "documentation",
        "tests",
    }:
        errors.append(
            "consumer views must include security, onboarding, documentation, and tests"
        )
    return errors


def render_markdown(inventory: dict[str, Any]) -> str:
    lines = [
        "# Repository inventory",
        "",
        f"Schema version: {inventory['schema_version']}",
        "",
        "| Inventory | Count |",
        "| --- | ---: |",
        f"| Routes | {len(inventory['routes'])} |",
        f"| Environment variables | {len(inventory['environment_variables'])} |",
        f"| Connectors | {len(inventory['connectors'])} |",
        f"| Workflows | {len(inventory['workflows'])} |",
        "",
        "## Routes",
        "",
        "| Method | Path | Owner | Sensitivity | Authentication | Tests |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in inventory["routes"]:
        tests = item["test_coverage"]["status"]
        lines.append(
            f"| {item['method']} | `{item['path']}` | {item['owner']} | "
            f"{item['sensitivity']} | {item['authentication']} | {tests} |"
        )
    lines.extend(
        [
            "",
            "## Environment variables",
            "",
            "| Variable | Owner | Sensitivity | .env.example | Tests |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in inventory["environment_variables"]:
        documented = "yes" if item["documented_in_env_example"] else "no"
        lines.append(
            f"| `{item['name']}` | {item['owner']} | {item['sensitivity']} | "
            f"{documented} | {item['test_coverage']['status']} |"
        )
    lines.extend(
        [
            "",
            "## Connectors",
            "",
            "| Provider | Class | Authentication | Metrics | Tests |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in inventory["connectors"]:
        metrics = ", ".join(item["metrics"]) or "n/a"
        lines.append(
            f"| {item['provider']} | `{item['class']}` | {item['authentication']} | "
            f"{metrics} | {item['test_coverage']['status']} |"
        )
    lines.extend(
        [
            "",
            "## Workflows",
            "",
            "| Workflow | Triggers | Secrets | Tests |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in inventory["workflows"]:
        triggers = ", ".join(item["triggers"]) or "unknown"
        secrets = "yes" if item["uses_secrets"] else "no"
        lines.append(
            f"| {item['name']} | {triggers} | {secrets} | "
            f"{item['test_coverage']['status']} |"
        )
    lines.extend(
        [
            "",
            "## Shared consumer views",
            "",
            "Security, onboarding, documentation, and test-gap views are derived "
            "from the same canonical inventory object. They are available in the "
            "JSON output under `views` and can be selected with `--view`.",
            "",
        ]
    )
    return "\n".join(lines)


def select_view(inventory: dict[str, Any], view: str | None) -> dict[str, Any]:
    if not view:
        return inventory
    return {
        "schema_version": inventory["schema_version"],
        "view": view,
        "inventory": inventory["views"][view],
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--view", choices=("security", "onboarding", "documentation", "tests")
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate canonical inventory metadata"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    inventory = build_inventory(args.root)
    errors = validate_inventory(inventory)
    if args.check and errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    payload = select_view(inventory, args.view)
    if args.format == "markdown":
        if args.view:
            rendered = "```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```\n"
        else:
            rendered = render_markdown(inventory)
    else:
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    elif not args.check:
        print(rendered, end="")
    if args.check:
        print(
            f"Validated {len(inventory['routes'])} routes, "
            f"{len(inventory['environment_variables'])} environment variables, "
            f"{len(inventory['connectors'])} connectors, and "
            f"{len(inventory['workflows'])} workflows."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
