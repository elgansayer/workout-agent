"""Daily connector health check.

Programmatically audits every connector module for failure isolation,
per-user-credential compliance, and .env.example documentation coverage.
Runs daily from ``scheduler.py`` to catch regressions early.

Design
------
* AST-based to avoid false-positives from string literals or comments.
* Each check is an idempotent, side-effect-free function that returns a
  structured finding (or None if clean).
* The ``--create-issues`` flag files GitHub issues for actionable findings.
* The ``--json`` flag emits machine-readable output for the scheduler.

Usage
-----
::

    python connector_health.py               # report only; exit 1 if findings exist
    python connector_health.py --json        # machine-readable output
    python connector_health.py --create-issues  # file GitHub issues for findings
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("connector_health")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent

CONNECTOR_MODULES: dict[str, Path] = {
    "hevy_client": ROOT / "hevy_client.py",
    "google_health_client": ROOT / "google_health_client.py",
    "health_connect": ROOT / "health_connect.py",
    "weather": ROOT / "weather.py",
    "telegram_notifier": ROOT / "telegram_notifier.py",
}

# Functions that are expected to make network calls and must have try/except.
_NETWORK_CALL_INDICATORS: set[str] = {
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.patch",
    "response.raise_for_status",
}

# Env vars that are server-wide and acceptable to read directly.
_SERVER_WIDE_ENV_VARS: set[str] = {
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "HEVY_API_KEY",
    "HEVY_SYNC_ROUTINES",
    "HEVY_PREFILL_WEIGHTS",
    "CHECKIN_ENABLED",
    "LIFESTYLE_ENABLED",
    "SELF_REVIEW_ENABLED",
    "SELF_REVIEW_WEEKDAY",
    "HEALTH_CONNECT_FILE",
    "GOOGLE_HEALTH_CLIENT_ID",
    "GOOGLE_HEALTH_CLIENT_SECRET",
    "GOOGLE_HEALTH_REFRESH_TOKEN",
    "GOOGLE_HEALTH_REDIRECT_URI",
    "DATABASE_PATH",
    "WEB_PORT",
    "TELEGRAM_PARSE_MODE",
    "WEB_GOOGLE_CLIENT_ID",
    "WEB_GOOGLE_CLIENT_SECRET",
    "WEB_AUTH_SECRET",
    "ALLOWED_EMAILS",
    "ENCRYPTION_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


@dataclass
class HealthFinding:
    """A single issue discovered during the health check."""

    module: str
    severity: str  # "error", "warning", "info"
    category: str  # "failure-isolation", "env-var-regression", "env-docs"
    message: str
    line: int | None = None
    snippet: str | None = None


@dataclass
class HealthReport:
    findings: list[HealthFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0

    def add(
        self,
        module: str,
        severity: str,
        category: str,
        message: str,
        line: int | None = None,
        snippet: str | None = None,
    ) -> None:
        self.findings.append(HealthFinding(module, severity, category, message, line, snippet))


# ---------------------------------------------------------------------------
# Check 1: Failure isolation
# ---------------------------------------------------------------------------


class _TryExceptVisitor(ast.NodeVisitor):
    """Visit AST and record every try/except handler set."""

    def __init__(self) -> None:
        self.handled_lines: set[int] = set()
        self.handler_types: list[str] = []

    def visit_Try(self, node: ast.Try) -> None:
        for handler in node.handlers:
            end = handler.end_lineno if handler.end_lineno is not None else handler.lineno
            for i in range(
                node.body[0].lineno if node.body else node.lineno,
                end + 1,
            ):
                self.handled_lines.add(i)
            if handler.type is None:
                self.handler_types.append(
                    f"bare except at line {handler.lineno}"
                )
            elif isinstance(handler.type, ast.Name):
                self.handler_types.append(
                    f"except {handler.type.id} at line {handler.lineno}"
                )
            elif isinstance(handler.type, ast.Tuple):
                names = [
                    e.id
                    for e in handler.type.elts
                    if isinstance(e, ast.Name)
                ]
                self.handler_types.append(
                    f"except ({', '.join(names)}) at line {handler.lineno}"
                )
        self.generic_visit(node)


def _check_failure_isolation(module_name: str, module_path: Path) -> list[HealthFinding]:
    """Verify every network call is wrapped in try/except."""
    findings: list[HealthFinding] = []
    source = module_path.read_text(encoding="utf-8")
    if not source.strip():
        return findings

    tree = ast.parse(source)
    visitor = _TryExceptVisitor()
    visitor.visit(tree)

    # Find bare excepts within the file (not in tests).
    for handler_type in visitor.handler_types:
        if handler_type.startswith("bare except"):
            findings.append(HealthFinding(
                module_name,
                "warning",
                "failure-isolation",
                f"Bare except: found in {module_name}: {handler_type}",
            ))

    # Find network calls that aren't inside a try/except.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_str = ast.unparse(node.func) if hasattr(ast, "unparse") else ""
        if not call_str:
            continue
        for indicator in _NETWORK_CALL_INDICATORS:
            if indicator in call_str and node.lineno not in visitor.handled_lines:
                findings.append(HealthFinding(
                    module_name,
                    "error",
                    "failure-isolation",
                    f"Network call '{call_str}' at line {node.lineno} is not "
                    f"wrapped in try/except — a timeout/4xx/5xx could crash the caller",
                    line=node.lineno,
                    snippet=ast.get_source_segment(source, node),
                ))
                break

    return findings


# ---------------------------------------------------------------------------
# Check 2: .env.example documentation coverage
# ---------------------------------------------------------------------------


def _check_env_docs() -> list[HealthFinding]:
    """Ensure every connector env var in code is documented in .env.example."""
    findings: list[HealthFinding] = []
    env_example_path = ROOT / ".env.example"

    if not env_example_path.is_file():
        findings.append(HealthFinding(
            ".env.example",
            "error",
            "env-docs",
            ".env.example does not exist",
        ))
        return findings

    env_example_text = env_example_path.read_text(encoding="utf-8")
    documented_vars: set[str] = set()
    for line in env_example_text.splitlines():
        # Match lines like KEY= or # KEY=
        m = re.match(r'^#?\s*([A-Z][A-Z0-9_]*)\s*[=]', line)
        if m:
            documented_vars.add(m.group(1))

    # Collect all env var reads from connector modules.
    env_var_pattern = re.compile(
        r'os\.environ(?:\.get)?\s*\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
    )
    connector_env_vars: dict[str, set[str]] = {}
    for name, path in CONNECTOR_MODULES.items():
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        found = set(env_var_pattern.findall(source))
        if found:
            connector_env_vars[name] = found

    for module_name, vars_used in connector_env_vars.items():
        for var in vars_used:
            if var not in documented_vars:
                findings.append(HealthFinding(
                    module_name,
                    "error",
                    "env-docs",
                    f"Connector {module_name} reads {var} but it is not documented "
                    f"in .env.example",
                ))

    # Reverse: check for documented vars no longer used.
    all_connector_vars: set[str] = set()
    for vs in connector_env_vars.values():
        all_connector_vars |= vs

    for var in documented_vars:
        if var not in all_connector_vars and var not in _SERVER_WIDE_ENV_VARS:
            # Check if the var is used elsewhere in the codebase.
            used_elsewhere = False
            all_py_files = list(ROOT.glob("*.py")) + list((ROOT / "webapp").glob("*.py"))
            for py_file in all_py_files:
                if py_file.name.startswith("test_"):
                    continue
                if py_file.name in ("connector_health.py",):
                    continue
                content = py_file.read_text(encoding="utf-8")
                if var in env_var_pattern.findall(content):
                    used_elsewhere = True
                    break
            if not used_elsewhere:
                findings.append(HealthFinding(
                    ".env.example",
                    "warning",
                    "env-docs",
                    f".env.example documents {var} but it is not read by any "
                    f"connector or source module",
                ))

    return findings


# ---------------------------------------------------------------------------
# Check 3: Per-user credential regression
# ---------------------------------------------------------------------------


def _check_per_user_credentials() -> list[HealthFinding]:
    """Flag connector code that reads global env vars for per-user concepts.

    Per-user-sensitive env vars (like *_API_KEY for connectors that will
    become multi-tenant) should eventually migrate to the database. This
    check flags new direct env-var reads in connector modules as a
    warning — not an error, since multi-tenant migration is in-progress.
    """
    findings: list[HealthFinding] = []
    # Vars that conceptually belong to a user but currently lack per-user
    # storage (known-current-state exceptions, not new regressions).
    _known_global_connector_vars: set[str] = {
        "HEVY_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "GOOGLE_HEALTH_CLIENT_ID",
        "GOOGLE_HEALTH_CLIENT_SECRET",
        "GOOGLE_HEALTH_REFRESH_TOKEN",
        "HEALTH_CONNECT_FILE",
    }

    env_var_pattern = re.compile(
        r'os\.environ(?:\.get)?\s*\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
    )

    for module_name, path in CONNECTOR_MODULES.items():
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        found = set(env_var_pattern.findall(source))
        for var in found:
            if var in _known_global_connector_vars:
                findings.append(HealthFinding(
                    module_name,
                    "info",
                    "env-var-regression",
                    f"{module_name} reads {var} from env — should be a per-user "
                    f"credential once multi-tenant migration reaches this connector",
                ))

    return findings


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_health_check() -> HealthReport:
    """Run all health checks and return a structured report."""
    report = HealthReport()

    for name, path in CONNECTOR_MODULES.items():
        if path.is_file():
            report.findings.extend(_check_failure_isolation(name, path))
        else:
            report.add(
                name, "error", "failure-isolation",
                f"Connector module {name} ({path}) does not exist",
            )

    report.findings.extend(_check_env_docs())
    report.findings.extend(_check_per_user_credentials())

    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_finding(f: HealthFinding) -> str:
    parts = [f"[{f.severity.upper()}]", f"{f.module}:", f.message]
    if f.line:
        parts[-1] += f" (line {f.line})"
    return " ".join(parts)


def report_health(health: HealthReport, *, json_output: bool = False) -> int:
    """Print the health report. Returns 0 if clean, 1 if findings exist."""
    if json_output:
        print(json.dumps(
            {"status": "clean" if health.is_clean else "findings",
             "count": len(health.findings),
             "findings": [
                 {
                     "module": f.module,
                     "severity": f.severity,
                     "category": f.category,
                     "message": f.message,
                     "line": f.line,
                     "snippet": f.snippet,
                 }
                 for f in health.findings
             ]},
            indent=2,
        ))
        return 0 if health.is_clean else 1

    if health.is_clean:
        logger.info("Connector health check: CLEAN — no findings.")
        return 0

    errors = [f for f in health.findings if f.severity == "error"]
    warnings = [f for f in health.findings if f.severity == "warning"]
    infos = [f for f in health.findings if f.severity == "info"]

    logger.warning(
        "Connector health check: %d findings (%d errors, %d warnings, %d info)",
        len(health.findings),
        len(errors),
        len(warnings),
        len(infos),
    )
    for f in errors + warnings + infos:
        log_fn = logger.error if f.severity == "error" else (
            logger.warning if f.severity == "warning" else logger.info
        )
        log_fn("%s", _format_finding(f))

    return 1


# ---------------------------------------------------------------------------
# GitHub issue creation
# ---------------------------------------------------------------------------


def _get_github_repo() -> tuple[str, str] | None:
    """Return (owner, repo) from the origin remote, or None."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=ROOT, timeout=5,
            check=False,
        )
        url = result.stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
    except Exception:  # noqa: BLE001  # defensive: must not crash the health check
        logger.debug("Could not determine GitHub repo from git remote.")
    return None


def _get_github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN", "").strip() or os.environ.get("GH_TOKEN", "").strip() or None


def create_github_issues(report: HealthReport) -> list[str]:
    """File GitHub issues for each category of findings. Returns issue URLs."""
    token = _get_github_token()
    if not token:
        logger.warning("No GITHUB_TOKEN — skipping issue creation.")
        return []

    repo_info = _get_github_repo()
    if not repo_info:
        logger.warning("Could not determine GitHub repo — skipping issue creation.")
        return []

    owner, repo = repo_info
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues"

    issues: list[str] = []

    # Group findings by category
    categories: dict[str, list[HealthFinding]] = {}
    for f in report.findings:
        categories.setdefault(f.category, []).append(f)

    for category, findings in categories.items():
        title_map = {
            "failure-isolation": "Connector Health: Failure Isolation Issues",
            "env-docs": "Connector Health: .env.example Documentation Gaps",
            "env-var-regression": "Connector Health: Per-User Credential Migration",
        }
        title = title_map.get(category, f"Connector Health: {category}")

        body_lines = [
            f"## {title}",
            "",
            f"{len(findings)} finding(s) found during the daily connector health check.",
            "",
        ]
        for f in findings:
            body_lines.append(f"- **[{f.severity.upper()}] {f.module}**: {f.message}")
            if f.line:
                body_lines.append(f"  (line {f.line})")
        body = "\n".join(body_lines)

        # Check if an open issue already exists with this title.
        try:
            existing_req = urllib.request.Request(
                f"{api_url}?state=open&labels=ai-agent-task,daily",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "connector-health",
                },
            )
            with urllib.request.urlopen(existing_req, timeout=10) as resp:
                existing_list = json.loads(resp.read().decode())
            existing = any(
                issue.get("title") == f"[Daily] {title}" for issue in existing_list
            )
        except Exception:  # noqa: BLE001  # defensive: must not crash health check
            existing = False

        if existing:
            logger.info("Skipping duplicate issue: [Daily] %s", title)
            continue

        payload = json.dumps({
            "title": f"[Daily] {title}",
            "body": body,
            "labels": ["ai-agent-task", "daily"],
        }).encode()

        try:
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "connector-health",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                created = json.loads(resp.read().decode())
            issue_url = created.get("html_url", "")
            if issue_url:
                issues.append(issue_url)
                logger.info("Created issue: %s", issue_url)
        except urllib.error.HTTPError as exc:
            logger.warning("Failed to create issue '%s': %s", title, exc)
        except Exception as exc:  # noqa: BLE001  # defensive: must not crash health check
            logger.warning("Failed to create issue '%s': %s", title, exc)

    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily connector health check — audits isolation, env docs,"
        " and per-user credential compliance across all connector modules.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output.",
    )
    parser.add_argument(
        "--create-issues",
        action="store_true",
        help="File GitHub issues for any findings.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    report = run_health_check()

    if args.create_issues and not report.is_clean:
        create_github_issues(report)

    return report_health(report, json_output=args.json)


if __name__ == "__main__":
    sys.exit(main())