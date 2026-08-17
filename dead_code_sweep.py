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
  ``insight_cron.py``, ``commit_hygiene.py``, ``connector_health.py``,
  ``dead_code_sweep.py``) and ``conftest.py`` are excluded from the orphan
  check because they are invoked directly by a human, a shell script,
  subprocess, or the test runner — not by another Python module.
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
# Included in this set are scripts called via subprocess by scheduled
# processes (e.g. scheduler.py) — they are not ``import``-ed but are
# reachable at runtime.
ENTRY_POINTS: set[str] = {
    "main.py",
    "scheduler.py",
    "sync_history.py",
    "insight_cron.py",
    "dead_code_sweep.py",
    "commit_hygiene.py",
    "connector_health.py",
}

# Files that are not modules in the import sense.
IGNORE_FILES: set[str] = {
    "conftest.py",
    "__init__.py",
}


def _discover_modules() -> list[ModuleInfo]:
    """Return every Python module in the repo (top-level + all sub-packages).

    Discovers sub-packages recursively by looking for ``__init__.py`` files,
    matching the same convention ``_build_import_graph`` uses for local package
    detection.  Tests (``tests/``) and hidden directories are excluded.
    """
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

    # All sub-packages (e.g. webapp/, any future sub-packages).
    for init_py in sorted(ROOT.rglob("__init__.py")):
        pkg_dir = init_py.parent
        try:
            relative = pkg_dir.relative_to(ROOT)
        except ValueError:
            continue
        parts = relative.parts
        # Skip hidden directories, tests, venvs, and .agents/.jules scaffolding.
        if any(
            p.startswith(".")
            or p in ("tests", "__pycache__", ".venv", "venv", ".agents", ".jules")
            for p in parts
        ):
            continue
        dotted = ".".join(parts)
        for p in sorted(pkg_dir.glob("*.py")):
            if p.name.startswith("test_") or p.name == "__init__.py":
                continue
            modules.append(
                ModuleInfo(
                    name=f"{dotted}.{p.stem}",
                    path=p,
                    is_entry_point=False,
                ),
            )

    return modules


def _extract_imports(source: str) -> set[str]:
    """Return the set of module names imported by *source*.

    Handles ``import foo``, ``from foo import bar``, and
    ``from foo.baz import ...``.  Returns every intermediate dotted path
    (``foo``, ``foo.baz``) so the BFS traverser can resolve local submodules
    without relying solely on the grep fallback.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                imports.add(parts[0])
                for i in range(2, len(parts) + 1):
                    imports.add(".".join(parts[:i]))
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            parts = node.module.split(".")
            imports.add(parts[0])
            for i in range(2, len(parts) + 1):
                imports.add(".".join(parts[:i]))
    return imports


def _extract_submodule_imports(source: str, local_packages: set[str]) -> set[str]:
    """Return dot-separated sub-module names from intra-package imports.

    Handles two patterns:

    1. ``from webapp import charts`` → ``{"webapp.charts"}``
       (``node.module`` matches a local package, alias is the submodule)

    2. ``from webapp.charts import line_chart`` → ``{"webapp.charts"}``
       (``node.module`` is a dotted path rooted at a local package)

    3. ``from webapp.sub.dir import foo`` → ``{"webapp.sub.dir"}``
       (deeply nested submodules)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.level == 0
        ):
            if node.module in local_packages:
                # Pattern 1: from webapp import charts
                for alias in node.names:
                    imports.add(f"{node.module}.{alias.name}")
            else:
                # Patterns 2 & 3: from webapp.X import Y
                # Check if node.module is a dotted child of a local package
                for pkg in local_packages:
                    prefix = pkg + "."
                    if node.module.startswith(prefix):
                        imports.add(node.module)
                        break
    return imports


def _resolve_call_name(node: ast.expr) -> str | None:
    """Resolve a callable expression to its dotted name if possible.

    Handles: ``subprocess.run``, ``subprocess.check_output``,
    ``subprocess.call``, ``subprocess.Popen``, and imported aliases
    like ``from subprocess import run; run(...)``.
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        # subprocess.run, subprocess.check_output, etc.
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Name):
        # imported alias, e.g. ``from subprocess import run; run(...)``
        return node.id
    return None


def _extract_subprocess_module_refs(source: str) -> set[str]:
    """Return module names referenced in subprocess-style calls.

    Detects all common subprocess patterns that invoke a local Python
    module as a standalone script::

        subprocess.run([sys.executable, "module.py"])
        subprocess.check_output([sys.executable, "module.py"])
        subprocess.call([sys.executable, "module.py"])
        subprocess.Popen([sys.executable, "module.py"])
        run([sys.executable, "module.py"])                # imported alias

    Also detects non-sys.executable patterns like
    ``subprocess.run(["python3", "module.py"])`` so that shell-script
    style invocations inside Python are captured.
    """
    _SUBPROCESS_FUNCTIONS = {
        "subprocess.run",
        "subprocess.check_output",
        "subprocess.check_call",
        "subprocess.call",
        "subprocess.Popen",
        "run",
        "check_output",
        "check_call",
        "call",
        "Popen",
    }

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    modules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _resolve_call_name(node.func)
        if call_name and call_name not in _SUBPROCESS_FUNCTIONS:
            continue
        if not node.args:
            continue
        cmd_list = node.args[0]
        if not isinstance(cmd_list, (ast.List, ast.Tuple)) or len(cmd_list.elts) < 2:
            continue
        first = cmd_list.elts[0]
        # Accept sys.executable or a literal "python3" / "python" string.
        is_sys_exe = (
            isinstance(first, ast.Attribute)
            and isinstance(first.value, ast.Name)
            and first.value.id == "sys"
            and first.attr == "executable"
        )
        is_python_string = (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value in ("python", "python3", "python3.12")
        )
        if not (call_name is not None and (is_sys_exe or is_python_string)):
            continue
        second = cmd_list.elts[1]
        if (
            isinstance(second, ast.Constant)
            and isinstance(second.value, str)
            and second.value.endswith(".py")
        ):
            modules.add(second.value[:-3])  # strip ".py"
    return modules


# ---------------------------------------------------------------------------
# Shell / Docker entry-point discovery
# ---------------------------------------------------------------------------


def _discover_shell_module_refs() -> set[str]:
    """Find modules referenced in shell scripts, Dockerfiles, and compose files.

    Captures patterns like::

        python main.py
        python insight_cron.py --daily
        exec python scheduler.py

    Returns the set of module stems (without ``.py``) found.
    """
    refs: set[str] = set()
    _SHELL_PATTERN = re.compile(r"\bpython3?(?:\.\d+)?\s+([a-z_][a-z0-9_]*)\.py\b")
    _GLOB_PATTERNS = ("*.sh", "Dockerfile*", "docker-entrypoint*", "*.yml", "*.yaml")

    for pattern in _GLOB_PATTERNS:
        for p in ROOT.glob(pattern):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for m in _SHELL_PATTERN.finditer(text):
                refs.add(m.group(1))

    return refs


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _build_import_graph() -> dict[str, set[str]]:
    """Return {importing_file_stem -> {module_names_it_imports}}."""
    graph: dict[str, set[str]] = {}

    # Discover local packages (directories with __init__.py) so we can
    # resolve intra-package imports like ``from webapp import charts`` to the
    # full ``webapp.charts`` module name directly via AST, without relying on
    # the grep fallback.
    local_packages: set[str] = set()
    for d in ROOT.rglob("__init__.py"):
        pkg = d.parent.relative_to(ROOT)
        if not any(part.startswith(".") for part in pkg.parts):
            local_packages.add(str(pkg).replace("/", "."))

    for py_file in sorted(ROOT.rglob("*.py")):
        # Skip virtual envs, caches, etc.
        parts = py_file.parts
        if any(
            p.startswith(".") or p in ("__pycache__", ".venv", "venv") for p in parts
        ):
            continue

        rel = str(py_file.relative_to(ROOT))
        source = py_file.read_text(encoding="utf-8")
        graph[rel] = (
            _extract_imports(source)
            | _extract_submodule_imports(source, local_packages)
            | _extract_subprocess_module_refs(source)
        )

    return graph


def find_orphans() -> list[OrphanReport]:
    """Return every module that is never imported by any reachable code."""
    modules = _discover_modules()
    import_graph = _build_import_graph()

    # Build a look-up: module_name -> file_rel that defines it
    module_to_file: dict[str, str] = {}
    for file_rel in import_graph:
        stem = file_rel.replace("/", ".").replace(".py", "")
        module_to_file[stem] = file_rel

    # Build the set of *wired* module names — i.e. those that are reachable
    # from an entry point or whose own file is an entry point.
    wired: set[str] = set()

    # Seed with entry-point modules themselves.
    for mi in modules:
        if mi.is_entry_point:
            wired.add(mi.name)

    # Treat webapp/app.py as an effective entry point — it's the web server
    # entry point, not imported by anyone else.
    _WEB_ENTRY = "webapp/app.py"
    wired.add("webapp.app")

    # Discover modules referenced in shell scripts, Dockerfiles, etc. and
    # treat them as entry-point-adjacent (they're invoked at runtime).
    shell_refs = _discover_shell_module_refs()
    for ref in shell_refs:
        wired.add(ref)

    # BFS from entry-point files: anything they import is reachable, and
    # anything *those* import is reachable, etc.
    entry_point_files = {
        f for f in import_graph if f in ENTRY_POINTS or f == _WEB_ENTRY
    }

    # Also seed with modules that are imported by test files — tests count as
    # reachable (they exercise the module at runtime via pytest), and the
    # transitive closure of test-imported modules is also reachable.
    queue: list[str] = []
    for file_rel, imports in import_graph.items():
        if file_rel.startswith("tests/") or file_rel in entry_point_files:
            for imp in imports:
                if imp not in wired:
                    wired.add(imp)
                    queue.append(imp)

    while queue:
        module_name = queue.pop(0)
        # Find the file that defines this module and add everything IT imports.
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

        # Also check shell-refs for non-entry-points that are reachable
        # at runtime via shell scripts.
        if mi.name in shell_refs:
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
        # Escape dots so "webapp.charts" matches only the literal dot, not
        # any character (e.g. would otherwise also match "webapp_charts").
        escaped = module_name.replace(".", "\\.")
        # Word-boundary anchors prevent partial-name matches:
        #   "import charts"  must NOT match  "import charts_legacy"
        #   "from webapp\\.charts" must NOT match "from webapp\\.charts_legacy"
        result = subprocess.run(
            [
                "grep",
                "-rn",
                "-E",
                f"^\\s*(import {escaped}\\b|from {escaped}( |\\.))",
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

    # Also check for `from <package> import <short_name>` pattern for any
    # sub-package (e.g. ``from webapp import charts``, or future sub-packages).
    if "." in module_name:
        pkg, short = module_name.rsplit(".", 1)
        # Escape dots in pkg so "webapp.sub" doesn't match "webappXsub".
        escaped_pkg = pkg.replace(".", "\\.")
        try:
            result = subprocess.run(
                [
                    "grep",
                    "-rn",
                    f"^\\s*from {escaped_pkg} import .*\\b{short}\\b",
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


def _is_shallow_repo() -> bool:
    """Detect whether we're inside a shallow git clone.

    In a shallow clone every file has only one commit, so ``few_commits``
    is never a reliable signal for "never wired in" — every module would
    look equally orphaned.  ``--prune`` is effectively a no-op in shallow
    repos unless *replacement_keywords* are present.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(ROOT),
            check=False,
        )
        return result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, OSError):
        return True  # defensive: treat unreadable repos as shallow


def find_truly_dead(orphans: list[OrphanReport]) -> list[OrphanReport]:
    """From a list of orphans, return those confirmed as truly dead.

    "Truly dead" means:
    * No plausible future caller (the module was superseded by another)
    * Confirmed via ``git log`` that it was intentionally replaced, OR has so
      few commits that it was never really wired in
    * No references in documentation or skill files
    """
    truly_dead: list[OrphanReport] = []
    shallow = _is_shallow_repo()

    for report in orphans:
        module_path = str(report.module.path.relative_to(ROOT))

        # Check git log for clues that this module was replaced or is stale.
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
        # Use -F (fixed-string) to avoid regex interpretation of dots in
        # dotted module names like "webapp.charts" (where "." would match
        # any character and produce false positives).
        try:
            doc_result = subprocess.run(
                ["grep", "-rn", "-F", report.module.name, "--include=*.md", str(ROOT)],
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

        has_replacement_keywords = any(
            "replace" in l.lower()
            or "supersed" in l.lower()
            or "remove" in l.lower()
            or "deprecat" in l.lower()
            for l in log_lines
        )
        # A module that was only added once (or has a single trivial commit)
        # and never referenced in docs may be genuinely dead — it was created
        # as part of an unfinished task and never wired.
        # We require at least one commit to exist (len > 0) — zero commits
        # means we couldn't access git (e.g. running in a temp dir during
        # tests), so we can't make a determination.
        # In a shallow clone every file has a single commit so we never use
        # *few_commits* alone — it would flag *every* module.
        few_commits = not shallow and len(log_lines) >= 1 and len(log_lines) <= 1
        has_doc_refs = len(doc_refs) > 0

        if (has_replacement_keywords or few_commits) and not has_doc_refs:
            if has_replacement_keywords:
                evidence = (
                    f"git log shows replacement/supersession: "
                    f"{log_lines[0] if log_lines else 'N/A'}. "
                    "No documentation references remain."
                )
            else:
                evidence = (
                    f"Only {len(log_lines)} commit(s) in git history — "
                    "likely never wired in. No documentation references remain."
                )
            truly_dead.append(
                OrphanReport(module=report.module, evidence=evidence),
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


def _cleanup_pycache(module_path: Path) -> int:
    """Remove stale ``__pycache__/*.pyc`` bytecode for *module_path*.

    Returns the number of files removed.
    """
    cleaned = 0
    cache_dir = module_path.parent / "__pycache__"
    if not cache_dir.is_dir():
        return cleaned
    stem = module_path.stem
    # Match ``stem.cpython-*.pyc`` patterns (CPython 3.x).
    for pyc in cache_dir.glob(f"{stem}.cpython-*.pyc"):
        try:
            pyc.unlink()
            cleaned += 1
        except OSError:
            logger.debug("Failed to remove stale pyc: %s", pyc)
    return cleaned


def clean_stale_pycache() -> int:
    """Remove ``__pycache__/*.pyc`` entries whose corresponding ``.py`` no longer exists.

    This handles modules deleted outside the sweep (e.g. in prior commits)
    that left orphaned bytecode behind.  Safe to run on every sweep.

    Returns the number of files removed.
    """
    removed = 0
    for cache_dir in ROOT.rglob("__pycache__"):
        # Skip hidden / venv / test cache dirs
        try:
            rel = cache_dir.relative_to(ROOT)
        except ValueError:
            continue
        if any(p.startswith(".") or p in (".venv", "venv") for p in rel.parts):
            continue
        for pyc in sorted(cache_dir.glob("*.pyc")):
            pyc_stem = pyc.stem
            # Strip cpython version suffix: "foo.cpython-312" → "foo"
            module_stem = re.sub(r"\.cpython-\d+.*", "", pyc_stem)
            py_file = cache_dir.parent / f"{module_stem}.py"
            if not py_file.exists():
                try:
                    pyc.unlink()
                    logger.info("Removed stale bytecode: %s", pyc.relative_to(ROOT))
                    removed += 1
                except OSError:
                    logger.debug("Failed to remove stale pyc: %s", pyc)
    return removed


def prune_dead_modules(reports: list[OrphanReport]) -> int:
    """Remove truly-dead modules.  Returns count of files removed."""
    removed = 0
    for r in reports:
        path = r.module.path
        logger.info("Removing truly-dead module: %s", path.relative_to(ROOT))
        path.unlink(missing_ok=True)
        removed += 1
        pyc_cleaned = _cleanup_pycache(path)
        if pyc_cleaned:
            logger.info(
                "Cleaned %d stale bytecode file(s) for %s",
                pyc_cleaned,
                path.relative_to(ROOT),
            )
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

    # Always clean stale bytecode (safe no-op when nothing is orphaned).
    stale_cleaned = clean_stale_pycache()
    if stale_cleaned:
        logger.info(
            "Cleaned %d stale bytecode file(s) from prior removals.", stale_cleaned
        )

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
