"""Hourly dead-code & orphaned-module sweep.

Prevents half-finished modules from silently rotting, unreferenced, in the
repo (per AGENTS.md §4's "never leave dead orphaned modules" rule).

This script is a companion to ruff's ``F401``/``F841`` line-level checks — it
operates at **module-level granularity**, answering the question "does this
``.py`` file exist in the repo but never get imported by anything reachable at
runtime?"

Design
------
* Import-discovery is AST-based (not ``grep``) so we don't false-positive on
  string literals or comments that happen to contain a module name.
* Entry-point modules (``main.py``, ``scheduler.py``, ``sync_history.py``,
  ``insight_cron.py``) and ``conftest.py`` are excluded from the orphan check
  because they are invoked directly by a human, a shell script, or the test
  runner — not by another Python module.
* Web-app sub-modules (``webapp/*.py``) are likewise checked.
* When an orphan is discovered the script **does not delete it** — it emits a
  structured report and exits non-zero so the calling automation can file a
  ``task_add()`` entry (GitHub issue) for a dedicated human-or-AI task to
  decide whether to wire or remove it.
* When truly dead code is found (superseded by another module, confirmed via
  ``git log`` that it was replaced, no plausible future caller), the script
  can optionally remove it when ``--prune`` is passed.

Usage
-----
::

    python dead_code_sweep.py               # report only; exit 1 if orphans found
    python dead_code_sweep.py --prune       # also remove confirmed-dead modules
    python dead_code_sweep.py --json        # machine-readable output
    python dead_code_sweep.py --create-issues  # file GitHub issues for orphans
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("dead_code_sweep")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ModuleInfo(NamedTuple):
    """Lightweight record for one Python module."""

    name: str  # dotted name, e.g. "webapp.charts"
    path: Path  # filesystem path
    is_entry_point: bool  # invoked directly, not imported


class OrphanReport(NamedTuple):
    module: ModuleInfo
    evidence: str  # human-readable explanation


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent

# Scripts invoked directly (not imported by another Python module).
ENTRY_POINTS: set[str] = {
    "main.py",
    "scheduler.py",
    "sync_history.py",
    "insight_cron.py",
}

# Files that are not modules in the import sense.
IGNORE_FILES: set[str] = {
    "conftest.py",
    "__init__.py",
}


def _discover_modules() -> list[ModuleInfo]:
    """Return every Python module in the repo (top-level + webapp/)."""
    modules: list[ModuleInfo] = []

    # Top-level
    for p in sorted(ROOT.glob("*.py")):
        if p.name.startswith("test_") or p.name in IGNORE_FILES:
            continue
        modules.append(
            ModuleInfo(
                name=p.stem,
                path=p,
                is_entry_point=p.name in ENTRY_POINTS,
            ),
        )

    # webapp/ sub-package
    webapp_dir = ROOT / "webapp"
    if webapp_dir.is_dir():
        for p in sorted(webapp_dir.glob("*.py")):
            if p.name.startswith("test_") or p.name == "__init__.py":
                continue
            modules.append(
                ModuleInfo(
                    name=f"webapp.{p.stem}",
                    path=p,
                    is_entry_point=False,  # webapp modules are never entry points
                ),
            )

    return modules


def _extract_imports(source: str) -> set[str]:
    """Return the set of top-level module names imported by *source*.

    Handles ``import foo``, ``from foo import bar``, and ``from foo.baz import ...``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module.split(".")[0])
    return imports


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _build_import_graph() -> dict[str, set[str]]:
    """Return {importing_file_stem -> {module_names_it_imports}}."""
    graph: dict[str, set[str]] = {}

    for py_file in sorted(ROOT.rglob("*.py")):
        # Skip virtual envs, caches, etc.
        parts = py_file.parts
        if any(
            p.startswith(".") or p in ("__pycache__", ".venv", "venv") for p in parts
        ):
            continue

        rel = str(py_file.relative_to(ROOT))
        source = py_file.read_text(encoding="utf-8")
        graph[rel] = _extract_imports(source)

    return graph


def find_orphans() -> list[OrphanReport]:
    """Return every module that is never imported by any reachable code."""
    modules = _discover_modules()
    import_graph = _build_import_graph()

    # Build the set of *wired* module names — i.e. those that are reachable
    # from an entry point or whose own file is an entry point.
    wired: set[str] = set()

    # Seed with entry-point modules themselves.
    for mi in modules:
        if mi.is_entry_point:
            wired.add(mi.name)

    # Also seed with modules that are imported by test files — tests count as
    # reachable (they exercise the module at runtime via pytest).
    for file_rel, imports in import_graph.items():
        if file_rel.startswith("tests/"):
            wired |= imports

    # Treat webapp/app.py as an effective entry point — it's the web server
    # entry point, not imported by anyone else.
    _WEB_ENTRY = "webapp/app.py"

    # BFS from entry-point files: anything they import is reachable, and
    # anything *those* import is reachable, etc.
    entry_point_files = {
        f for f in import_graph if f in ENTRY_POINTS or f == _WEB_ENTRY
    }

    # Also seed wired with webapp.app so it's never flagged as orphan
    wired.add("webapp.app")

    # Build a look-up: module_name -> file_rel that defines it
    module_to_file: dict[str, str] = {}
    for file_rel in import_graph:
        stem = file_rel.replace("/", ".").replace(".py", "")
        module_to_file[stem] = file_rel

    queue: list[str] = []
    for ep in entry_point_files:
        for imp in import_graph.get(ep, set()):
            if imp not in wired:
                wired.add(imp)
                queue.append(imp)

    while queue:
        module_name = queue.pop(0)
        # Find the file that defines this module and add everything IT imports.
        # (The old approach looked at files that import module_name and added
        # what those files import — that only discovers transitive
        # dependencies by coincidence when two modules share a common importer.)
        defining_file = module_to_file.get(module_name)
        if defining_file is not None:
            for transitive in import_graph.get(defining_file, set()):
                if transitive not in wired:
                    wired.add(transitive)
                    queue.append(transitive)

    # Now check each module
    orphans: list[OrphanReport] = []
    for mi in modules:
        if mi.name in wired:
            continue
        if mi.is_entry_point:
            continue

        # Double-check with a simple grep: is the module imported via dynamic
        # patterns that AST can't catch (e.g. __import__ or importlib)?
        grep_hits = _grep_import(mi.name)
        if grep_hits:
            logger.debug(
                "%s: AST missed but grep found imports in %s",
                mi.name,
                grep_hits,
            )
            continue

        orphans.append(
            OrphanReport(
                module=mi,
                evidence=f"No imports of '{mi.name}' found in any reachable module.",
            ),
        )

    return orphans


def _grep_import(module_name: str) -> list[str]:
    """Brute-force grep for *module_name* in import-like contexts.

    Returns the list of files (relative paths) that reference the module
    (excluding the module's own file and its test file).
    """
    hits: list[str] = []
    own_file = f"{module_name.replace('.', '/')}.py"
    test_file = f"tests/test_{module_name.replace('webapp.', '')}.py"

    try:
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "-E",
                f"^(import {module_name}|from {module_name}( |\\.))",
                "--include=*.py",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        for line in result.stdout.splitlines():
            file_part = line.split(":", 1)[0]
            rel = os.path.relpath(file_part, ROOT)
            if rel != own_file and rel != test_file:
                hits.append(rel)
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Also check for `from webapp import <short_name>` pattern
    if module_name.startswith("webapp."):
        short = module_name.split(".")[1]
        try:
            result = subprocess.run(
                [
                    "grep",
                    "-rn",
                    f"from webapp import .*{short}",
                    "--include=*.py",
                    str(ROOT),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            for line in result.stdout.splitlines():
                file_part = line.split(":", 1)[0]
                rel = os.path.relpath(file_part, ROOT)
                if rel != own_file and rel != test_file:
                    hits.append(rel)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return sorted(set(hits))


# ---------------------------------------------------------------------------
# Truly-dead-code detection
# ---------------------------------------------------------------------------


def find_truly_dead(orphans: list[OrphanReport]) -> list[OrphanReport]:
    """From a list of orphans, return those confirmed as truly dead.

    "Truly dead" means:
    * No plausible future caller (the module was superseded by another)
    * Confirmed via ``git log`` that it was intentionally replaced
    * No references in documentation or skill files
    """
    truly_dead: list[OrphanReport] = []

    for report in orphans:
        module_path = str(report.module.path.relative_to(ROOT))

        # Check git log for clues that this module was replaced
        try:
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--", module_path],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(ROOT),
                check=False,
            )
            log_lines = [l for l in log_result.stdout.splitlines() if l.strip()]
        except (subprocess.TimeoutExpired, OSError):
            log_lines = []

        # Check if referenced in docs / skill files
        try:
            doc_result = subprocess.run(
                ["grep", "-rn", report.module.name, "--include=*.md", str(ROOT)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            doc_refs = [
                l
                for l in doc_result.stdout.splitlines()
                if ".agents/automations/" not in l  # exclude this sweep's own docs
            ]
        except (subprocess.TimeoutExpired, OSError):
            doc_refs = []

        # A module is "truly dead" if it has no recent commits (wasn't
        # recently created for a purpose) AND no documentation references.
        # We err on the side of NOT marking something as dead — the default
        # orphan path (file a task) is always safer.
        has_recent_commits = any(
            "replace" in l.lower()
            or "supersed" in l.lower()
            or "remove" in l.lower()
            or "deprecat" in l.lower()
            for l in log_lines
        )
        has_doc_refs = len(doc_refs) > 0

        if has_recent_commits and not has_doc_refs:
            truly_dead.append(
                OrphanReport(
                    module=report.module,
                    evidence=(
                        f"git log shows replacement/supersession: {log_lines[0] if log_lines else 'N/A'}. "
                        "No documentation references remain."
                    ),
                ),
            )

    return truly_dead


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _task_add_text(report: OrphanReport) -> str:
    """Generate the body for a ``task_add()`` / GitHub issue."""
    mod = report.module
    return (
        f"## Orphaned module: `{mod.name}`\n\n"
        f"**File**: `{mod.path.relative_to(ROOT)}`\n\n"
        f"**Evidence**: {report.evidence}\n\n"
        f"### Required action\n"
        f"Determine whether this module should be:\n"
        f"1. Wired into `main.py` or `webapp/app.py` (or a module they import "
        f"transitively), or\n"
        f"2. Removed if it has been superseded.\n\n"
        f"**Skill tag**: `programme-builder-ui` or the skill most relevant to "
        f"the module's domain.\n\n"
        f"> Auto-filed by `dead_code_sweep.py` hourly sweep.\n"
    )


def _get_github_repo() -> tuple[str, str] | None:
    """Parse `owner/repo` from the git remote origin URL.

    Returns a ``(owner, repo)`` tuple on success, or ``None`` if the remote
    cannot be identified (e.g. non-GitHub hosting).
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(ROOT),
            check=False,
        )
        url = result.stdout.strip()
        # Handles both https://github.com/owner/repo.git and
        # https://x-access-token:TOKEN@github.com/owner/repo.git
        m = re.search(r"github\.com[:/]([^/]+)/([^/\s.]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _get_github_token() -> str | None:
    """Return a GitHub token from env or the git remote URL credential."""
    # Check GITHUB_TOKEN env var first
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token

    # Fall back to extracting from the remote URL
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(ROOT),
            check=False,
        )
        url = result.stdout.strip()
        # x-access-token:TOKEN@github.com/...
        m = re.search(r"://x-access-token:([^@]+)@", url)
        if m:
            return m.group(1)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def create_github_issues(reports: list[OrphanReport]) -> list[str]:
    """Create a GitHub issue for each orphaned module.

    Returns a list of created issue URLs (or error strings).
    """
    token = _get_github_token()
    if not token:
        logger.warning(
            "No GitHub token found (set GITHUB_TOKEN or configure git remote credentials). "
            "Skipping issue creation.",
        )
        return []

    repo = _get_github_repo()
    if not repo:
        logger.warning("Could not determine GitHub owner/repo from git remote.")
        return []

    owner, repo_name = repo
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"
    created: list[str] = []

    for report in reports:
        title = f"Orphaned module: `{report.module.name}` needs wiring or removal"
        body = _task_add_text(report)
        labels = ["orphaned-module", "dead-code-sweep"]

        payload = json.dumps({"title": title, "body": body, "labels": labels}).encode(
            "utf-8",
        )

        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                issue_data = json.loads(resp.read().decode("utf-8"))
                issue_url = issue_data.get("html_url", "unknown")
                logger.info(
                    "Created issue for '%s': %s",
                    report.module.name,
                    issue_url,
                )
                created.append(issue_url)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            msg = f"Failed to create issue for '{report.module.name}': HTTP {e.code} — {err_body[:300]}"
            logger.error(msg)
            created.append(f"ERROR: {msg}")
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            msg = f"Failed to create issue for '{report.module.name}': {e}"
            logger.error(msg)
            created.append(f"ERROR: {msg}")

    return created


def _rel_path(p: Path) -> str:
    """Return *p* relative to ROOT if possible, else its string representation."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def report_orphans(orphans: list[OrphanReport], *, json_output: bool = False) -> int:
    """Print orphan report.  Returns exit code (0 = clean, 1 = orphans found)."""
    if not orphans:
        if json_output:
            print('{"status":"clean","orphans":[]}')
        else:
            logger.info("No orphaned modules found — repo is clean.")
            print("No orphaned modules found — repo is clean.")
        return 0

    if json_output:
        import json

        payload = {
            "status": "orphans_found",
            "count": len(orphans),
            "orphans": [
                {
                    "module": r.module.name,
                    "path": _rel_path(r.module.path),
                    "evidence": r.evidence,
                }
                for r in orphans
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        msg = f"=== {len(orphans)} orphaned module(s) found ==="
        logger.warning(msg)
        print(msg)
        for r in orphans:
            line = f"  {r.module.name}  ({_rel_path(r.module.path)})"
            logger.warning(line)
            print(line)
            detail = f"    {r.evidence}"
            logger.warning(detail)
            print(detail)

    return 1


def prune_dead_modules(reports: list[OrphanReport]) -> int:
    """Remove truly-dead modules.  Returns count of files removed."""
    removed = 0
    for r in reports:
        path = r.module.path
        logger.info("Removing truly-dead module: %s", path.relative_to(ROOT))
        path.unlink(missing_ok=True)
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hourly dead-code & orphaned-module sweep",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove confirmed-dead modules (superseded, unreferenced).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON to stdout.",
    )
    parser.add_argument(
        "--create-issues",
        action="store_true",
        dest="create_issues",
        help="Create GitHub issues for orphaned modules via the GitHub API.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    os.chdir(ROOT)

    orphans = find_orphans()

    if not orphans:
        report_orphans([], json_output=args.json_output)
        return 0

    # Separate truly-dead from merely-orphaned
    truly_dead = find_truly_dead(orphans)
    merely_orphaned = [r for r in orphans if r not in truly_dead]

    exit_code = 0

    if merely_orphaned:
        exit_code = report_orphans(merely_orphaned, json_output=args.json_output)
        if not args.json_output:
            for r in merely_orphaned:
                logger.info(
                    "→ File a task_add() for '%s' (tag: programme-builder-ui or relevant skill)",
                    r.module.name,
                )
                logger.info("  Suggested issue body:\n%s", _task_add_text(r))

        if args.create_issues:
            created = create_github_issues(merely_orphaned)
            if created:
                logger.info("Created %d GitHub issue(s).", len(created))

    if truly_dead:
        if args.prune:
            removed = prune_dead_modules(truly_dead)
            logger.info("Pruned %d truly-dead module(s).", removed)
        else:
            if not args.json_output:
                logger.warning(
                    "=== %d truly-dead module(s) found (re-run with --prune to remove) ===",
                    len(truly_dead),
                )
            for r in truly_dead:
                if not args.json_output:
                    logger.warning(
                        "  %s  (%s)",
                        r.module.name,
                        r.module.path.relative_to(ROOT),
                    )
                    logger.warning("    %s", r.evidence)
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
