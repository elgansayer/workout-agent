<<<<<<< HEAD
"""Hourly commit hygiene sweep.

Automates the checks defined in `.agents/automations/task-hourly-commit-hygiene.md`:
1. Review last 10 commits for descriptive messages (informational only).
2. Check no sensitive files (``.env``, ``.db``, ``data/``) were committed.
3. Confirm ``.gitignore`` covers required patterns.
4. Confirm no large binary/log files outside ``data/``.

When run with ``--create-issues``, files GitHub issues for any problems that
require human attention (e.g. potential secret leaks).
=======
"""Hourly commit-hygiene audit.

Prevents sensitive files and bloated binaries from landing in commits,
and validates that commit messages are descriptive (per AGENTS.md).

Design
------
* Runs ``git log -10 --stat`` to inspect commit messages.
* Runs ``git log -p -10 --`` with a comprehensive set of glob patterns
  covering ``.env``, database (``*.db``, ``*.sqlite``, ``*.sqlite3``),
  log files (``*.log``), and ``data/`` to detect accidentally-committed
  secrets or database files.
* Checks ``.gitignore`` for required patterns (``*.db``, ``*.sqlite``,
  ``*.sqlite3``, ``*.log``, ``.env``, ``__pycache__/``, ``.pytest_cache/``,
  ``.venv/``).
* Scans for tracked files >1 MB outside ``data/`` (gitignored).

When an issue is found the script:
1. Adds missing ``.gitignore`` entries when safe (non-destructive fix).
2. For security issues (committed secrets): emits a ``SECURITY:``-flagged
   report and exits non-zero so the calling automation can file a GitHub
   issue for human-supervised history rewrite.
3. For large files: emits a warning and exits non-zero.
>>>>>>> origin/main

Usage
-----
::

    python commit_hygiene.py               # report only; exit 1 if issues found
    python commit_hygiene.py --json        # machine-readable output
<<<<<<< HEAD
    python commit_hygiene.py --create-issues  # file GitHub issues for problems
=======
    python commit_hygiene.py --create-issues  # file GitHub issues for findings
>>>>>>> origin/main
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
<<<<<<< HEAD
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
=======
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
>>>>>>> origin/main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("commit_hygiene")

ROOT = Path(__file__).resolve().parent

<<<<<<< HEAD
# Patterns that must be present in .gitignore
REQUIRED_GITIGNORE_PATTERNS: list[str] = [
    "*.db",
    ".env",
    "__pycache__/",
    ".pytest_cache/",
    ".venv/",
]

# Sensitive path patterns that must never appear in git history
SENSITIVE_PATTERNS: list[str] = [
=======
# Patterns that .gitignore must cover
REQUIRED_GITIGNORE = [
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.db-journal",
    ".env",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".venv/",
    "venv/",
    "data/",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite-journal",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.sqlite3-journal",
    "*.log",
    "agent.log",
]

# Glob patterns for sensitive files to scan via git log (beyond .env and data/)
SENSITIVE_GLOBS = [
>>>>>>> origin/main
    ".env",
    ".env.*",
    "data/",
    "*.db",
<<<<<<< HEAD
]

# File extension categories that indicate binary/log bloat
LARGE_FILE_EXTENSIONS: set[str] = {
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".log", ".dump", ".core",
    ".mp4", ".avi", ".mov", ".mkv",
    ".msi", ".deb", ".rpm",
    ".whl", ".egg",
}

# Maximum file size before flagging (5 MB)
MAX_FILE_SIZE_MB: float = 5.0

=======
    "*.db-wal",
    "*.db-shm",
    "*.db-journal",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.sqlite-journal",
    "*.sqlite3",
    "*.sqlite3-wal",
    "*.sqlite3-shm",
    "*.sqlite3-journal",
    "*.log",
    "agent.log",
]

# Size threshold: files larger than this (in bytes) outside data/ are suspicious
MAX_FILE_SIZE = 3 * 1024 * 1024  # 3 MB
>>>>>>> origin/main

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


<<<<<<< HEAD
class Finding(NamedTuple):
    """A single hygiene finding."""

    severity: str  # "info", "warning", "security"
    check: str  # which check produced this
    detail: str  # human-readable description
=======
@dataclass
class HygieneFinding:
    severity: str  # "info", "warning", "error", "security"
    category: str  # "commit-message", "sensitive-file", "gitignore", "large-file"
    message: str
    details: str = ""
    sha: str = ""  # relevant commit SHA if applicable
>>>>>>> origin/main


@dataclass
class HygieneReport:
<<<<<<< HEAD
    """Aggregated results of a commit hygiene sweep."""

    findings: list[Finding] = field(default_factory=list)
    low_quality_commits: list[str] = field(default_factory=list)
    sensitive_commits: list[str] = field(default_factory=list)
    missing_gitignore_patterns: list[str] = field(default_factory=list)
    large_files: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return (
            len(self.findings) > 0
            or len(self.sensitive_commits) > 0
            or len(self.missing_gitignore_patterns) > 0
            or len(self.large_files) > 0
        )

    @property
    def has_security_issues(self) -> bool:
        return len(self.sensitive_commits) > 0


# ---------------------------------------------------------------------------
# Check 1: Commit message quality (informational)
# ---------------------------------------------------------------------------


def _check_commit_messages() -> list[str]:
    """Review last 10 commits for non-descriptive messages.

    Returns list of short/non-descriptive commit summaries.
    """
    try:
        result = subprocess.run(
            ["git", "log", "-10", "--format=%h %s"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(ROOT),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not check commit messages: %s", e)
        return []

    low_quality: list[str] = []
    # Heuristic: messages shorter than ~15 chars or with common low-quality patterns
    LOW_QUALITY_PATTERNS = re.compile(
        r"^(fix|wip|update|changes|test|tweak|stuff|oops|tmp|temp)$",
        re.IGNORECASE,
    )

    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        # line format: "<sha> <message>"
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, msg = parts[0], parts[1].strip()
        if len(msg) < 15 or LOW_QUALITY_PATTERNS.match(msg):
            low_quality.append(f"{sha}: {msg}")

    return low_quality


# ---------------------------------------------------------------------------
# Check 2: Sensitive files in history
# ---------------------------------------------------------------------------


# Files that look sensitive but are intentionally committed as templates.
_ALLOWED_SENSITIVE_LIKE: set[str] = {".env.example", ".env.swarm.example"}


def _check_sensitive_files() -> list[str]:
    """Check git log for commits that touched .env, .db, or data/ files.

    Returns list of offending commit SHAs (excluding intentionally-committed
    example/template files like ``.env.example``).
    """
    try:
        result = subprocess.run(
            [
                "git", "log", "-p", "-10", "--",
                ".env", ".env.*", "data/", "*.db",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ROOT),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not check sensitive files in history: %s", e)
        return []

    output = result.stdout.strip()
    if not output:
        return []

    # Parse commit SHAs and changed file paths from the diff output
    commits: dict[str, set[str]] = {}
    current_sha: str = ""
    for line in output.splitlines():
        if line.startswith("commit "):
            current_sha = line.split()[1][:9]
            commits.setdefault(current_sha, set())
        elif line.startswith("diff --git "):
            # Extract the file path from "diff --git a/<path> b/<path>"
            parts = line.split(" ")
            if len(parts) >= 3:
                b_path = parts[2][2:]  # strip "b/" prefix
                commits.setdefault(current_sha, set()).add(b_path)

    # Only flag commits whose changed files are NOT all in the allow-list.
    offending: list[str] = []
    for sha, paths in commits.items():
        truly_sensitive = paths - _ALLOWED_SENSITIVE_LIKE
        if truly_sensitive:
            logger.info(
                "Commit %s touched sensitive path(s): %s",
                sha,
                ", ".join(sorted(truly_sensitive)),
            )
            offending.append(sha)

    return sorted(set(offending))


def _check_sensitive_files_on_disk() -> list[str]:
    """Check for .env or .db files currently **tracked by git**.

    Uses ``git ls-files`` to only report files that are in the index
    (ignored files are excluded by git itself).  Returns list of paths.

    Unlike a recursive ``rglob``, this only flags files that would
    actually be committed — not cache/build artifacts that are already
    gitignored.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--", ".env", "*.db", "*.sqlite", "*.sqlite3"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(ROOT),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not check tracked sensitive files: %s", e)
        return []

    offending: list[str] = []
    for line in result.stdout.strip().splitlines():
        path = line.strip()
        if not path:
            continue
        # Exclude intentionally committed template files
        if path in _ALLOWED_SENSITIVE_LIKE:
            continue
        offending.append(path)
    return sorted(offending)


# ---------------------------------------------------------------------------
# Check 3: .gitignore coverage
# ---------------------------------------------------------------------------


def _check_gitignore() -> list[str]:
    """Confirm `.gitignore` covers all required patterns.

    Returns list of missing patterns.
    """
    gitignore_path = ROOT / ".gitignore"
    if not gitignore_path.exists():
        return list(REQUIRED_GITIGNORE_PATTERNS)

    content = gitignore_path.read_text(encoding="utf-8")
    missing: list[str] = []
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        # Simple substring check — pattern must appear as its own line
        if pattern not in content.splitlines():
            missing.append(pattern)
    return missing


# ---------------------------------------------------------------------------
# Check 4: Large files outside data/
# ---------------------------------------------------------------------------


def _check_large_files() -> list[str]:
    """Find files larger than MAX_FILE_SIZE_MB outside data/ and .git/.

    Returns list of "path (size MB)" strings.
    """
    large: list[str] = []
    exclude_dirs = {
        ".git", "data", ".venv", "venv", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
    }

    for entry in ROOT.rglob("*"):
        if not entry.is_file():
            continue
        # Skip excluded directories
        parts = set(entry.parts)
        if parts & exclude_dirs:
            continue

        try:
            size_mb = entry.stat().st_size / (1024 * 1024)
        except OSError:
            continue

        if size_mb > MAX_FILE_SIZE_MB:
            rel = str(entry.relative_to(ROOT))
            large.append(f"{rel} ({size_mb:.1f} MB)")

    return sorted(large)


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


def run_hygiene_sweep() -> HygieneReport:
    """Run all four hygiene checks and return an aggregated report."""
    report = HygieneReport()

    # Check 1: Commit message quality (informational)
    low_quality = _check_commit_messages()
    report.low_quality_commits = low_quality
    for c in low_quality:
        report.findings.append(Finding(
            severity="info",
            check="commit-message-quality",
            detail=f"Low-quality commit message: {c}",
        ))

    # Check 2: Sensitive files in history
    sensitive_shas = _check_sensitive_files()
    report.sensitive_commits = sensitive_shas
    for sha in sensitive_shas:
        report.findings.append(Finding(
            severity="security",
            check="sensitive-files",
            detail=f"SECURITY: secret possibly committed in {sha}, needs human-supervised history rewrite",
        ))

    # Check 2b: Sensitive files on disk (current tree)
    on_disk = _check_sensitive_files_on_disk()
    for path in on_disk:
        report.findings.append(Finding(
            severity="security",
            check="sensitive-files-on-disk",
            detail=f"Sensitive file present in working tree: {path} — add to .gitignore and remove",
        ))

    # Check 3: .gitignore coverage
    missing = _check_gitignore()
    report.missing_gitignore_patterns = missing
    for m in missing:
        report.findings.append(Finding(
            severity="warning",
            check="gitignore-coverage",
            detail=f"Missing .gitignore pattern: {m}",
        ))

    # Check 4: Large files
    large = _check_large_files()
    report.large_files = large
    for lf in large:
        report.findings.append(Finding(
            severity="warning",
            check="large-files",
            detail=f"Large file committed outside data/: {lf}",
        ))

    return report


# ---------------------------------------------------------------------------
# GitHub issue helpers
# ---------------------------------------------------------------------------


def _get_github_repo() -> tuple[str, str] | None:
    """Parse `owner/repo` from the git remote origin URL."""
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
        m = re.search(r"github\.com[:/]([^/]+)/([^/\s.]+?)(?:\.git)?$", url)
        if m:
            return m.group(1), m.group(2)
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _get_github_token() -> str | None:
    """Return a GitHub token from env or the git remote URL credential."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
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
        m = re.search(r"://x-access-token:([^@]+)@", url)
        if m:
            return m.group(1)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _create_issue(title: str, body: str, labels: list[str]) -> str:
    """Create a GitHub issue. Returns the issue URL or error message."""
    token = _get_github_token()
    if not token:
        return "ERROR: No GitHub token available"

    repo = _get_github_repo()
    if not repo:
        return "ERROR: Could not determine GitHub repo"

    owner, repo_name = repo
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/issues"

    import urllib.error
    import urllib.request

    payload = json.dumps({"title": title, "body": body, "labels": labels}).encode("utf-8")
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
            return issue_data.get("html_url", "unknown")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return f"ERROR: HTTP {e.code} — {err_body[:300]}"
    except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
        return f"ERROR: {e}"


def create_security_issues(report: HygieneReport) -> list[str]:
    """Create GitHub issues for security findings that need human review."""
    created: list[str] = []

    for sha in report.sensitive_commits:
        title = f"SECURITY: secret possibly committed in {sha}, needs human-supervised history rewrite"
        body = (
            f"## Security Alert: Sensitive file committed\n\n"
            f"**Commit**: `{sha}`\n\n"
            f"A file matching `.env`, `.env.*`, `data/`, or `*.db` was found in this commit's diff.\n\n"
            f"### Required action (human-in-the-loop)\n"
            f"1. Verify whether the commit contains real credentials or sensitive data.\n"
            f"2. If yes: perform a supervised history rewrite (e.g. `git filter-branch` or `bfg`).\n"
            f"3. If no (false positive, e.g. `.env.example`): close this issue.\n\n"
            f"**Do NOT force-push unattended.**\n\n"
            f"> Auto-filed by `commit_hygiene.py` hourly sweep.\n"
        )
        url = _create_issue(title, body, ["security", "commit-hygiene", "human-review"])
        if url:
            created.append(url)
            logger.info("Created security issue for %s: %s", sha, url)
=======
    findings: list[HygieneFinding] = field(default_factory=list)

    @property
    def has_security_issues(self) -> bool:
        return any(f.severity == "security" for f in self.findings)

    @property
    def has_errors(self) -> bool:
        return any(f.severity in ("error", "security") for f in self.findings)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command in the repo root."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def check_commit_messages() -> list[HygieneFinding]:
    """Review the last 10 commit messages for quality."""
    findings: list[HygieneFinding] = []
    result = _run_git(["log", "-10", "--format=%H %s"])
    if result.returncode != 0:
        logger.warning("git log failed: %s", result.stderr)
        return findings

    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    short_msgs = ["wip", "fix", "WIP", "Fix", "wip.", "fix.", ".", "...", "temp", "tmp"]
    for line in lines:
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, subject = parts[0], parts[1]
        if subject.lower().rstrip(".") in short_msgs:
            findings.append(
                HygieneFinding(
                    severity="info",
                    category="commit-message",
                    message=f"Low-quality commit message: '{subject}'",
                    details=f"Commit {sha[:8]} has a non-descriptive subject.",
                    sha=sha,
                )
            )
    return findings


def check_sensitive_files() -> list[HygieneFinding]:
    """Check for committed .env, database, sqlite, log, or data/ files."""
    findings: list[HygieneFinding] = []
    result = _run_git(["log", "-p", "-10", "--"] + SENSITIVE_GLOBS)
    if result.returncode != 0:
        logger.warning("git log for sensitive files failed: %s", result.stderr)
        return findings

    output = result.stdout.strip()
    if not output:
        return findings

    # Parse out the SHAs and filenames from the diff output
    current_sha = ""
    for line in output.split("\n"):
        if line.startswith("commit "):
            current_sha = line.split()[1][:8]
        elif line.startswith(("+++ b/", "--- a/")):
            path = line[6:]
            if path == "/dev/null":
                continue
            # Skip .env.example variants (explicitly whitelisted)
            if path.endswith((".env.example", ".env.swarm.example")):
                continue
            findings.append(
                HygieneFinding(
                    severity="security",
                    category="sensitive-file",
                    message=f"Sensitive file committed: {path}",
                    details=(
                        f"File '{path}' was found in commit {current_sha}. "
                        "Add to .gitignore, remove from tree, and file a "
                        "human-supervised history rewrite task."
                    ),
                    sha=current_sha,
                )
            )

    return findings


def check_gitignore() -> list[HygieneFinding]:
    """Verify .gitignore covers required patterns."""
    findings: list[HygieneFinding] = []
    gi_path = ROOT / ".gitignore"

    if not gi_path.exists():
        findings.append(
            HygieneFinding(
                severity="error",
                category="gitignore",
                message=".gitignore missing",
                details="No .gitignore file found in repository root.",
            )
        )
        return findings

    content = gi_path.read_text()
    for pattern in REQUIRED_GITIGNORE:
        # Match pattern as a standalone line (or end-of-line anchored)
        escaped = re.escape(pattern)
        if not re.search(rf"^{escaped}\s*$", content, re.MULTILINE):
            findings.append(
                HygieneFinding(
                    severity="error",
                    category="gitignore",
                    message=f".gitignore missing entry: {pattern}",
                    details=f"Required pattern '{pattern}' not found in .gitignore.",
                )
            )
    return findings


def check_large_files() -> list[HygieneFinding]:
    """Check for tracked files larger than MAX_FILE_SIZE outside data/."""
    findings: list[HygieneFinding] = []
    data_abs = str((ROOT / "data").resolve()) + os.sep

    # List all tracked files with their sizes
    result = _run_git(["ls-files", "-z"])
    if result.returncode != 0:
        logger.warning("git ls-files failed: %s", result.stderr)
        return findings

    files = result.stdout.split("\0")
    for f in files:
        if not f:
            continue
        fpath = ROOT / f
        if not fpath.exists():
            continue
        # Skip files under data/ (gitignored)
        abs_path = str(fpath.resolve())
        if abs_path.startswith(data_abs):
            continue
        size = fpath.stat().st_size
        if size > MAX_FILE_SIZE:
            findings.append(
                HygieneFinding(
                    severity="error",
                    category="large-file",
                    message=f"Large tracked file: {f} ({size / (1024 * 1024):.1f} MB)",
                    details=(
                        f"File '{f}' is {size / (1024 * 1024):.1f} MB — "
                        "larger than the {MAX_FILE_SIZE / (1024*1024):.0f} MB limit. "
                        "Consider gitignoring or storing externally."
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Fix helpers (non-destructive only)
# ---------------------------------------------------------------------------


def fix_missing_gitignore_entries(findings: list[HygieneFinding]) -> list[str]:
    """Add missing .gitignore entries. Returns list of patterns added."""
    gi_path = ROOT / ".gitignore"
    if not gi_path.exists():
        logger.error("Cannot fix — .gitignore does not exist")
        return []

    content = gi_path.read_text()
    added: list[str] = []
    for f in findings:
        if f.category != "gitignore":
            continue
        # Extract missing pattern from message
        match = re.search(r"missing entry: (.+)$", f.message)
        if not match:
            continue
        pattern = match.group(1)
        escaped = re.escape(pattern)
        if re.search(rf"^{escaped}\s*$", content, re.MULTILINE):
            continue
        if not content.endswith("\n"):
            content += "\n"
        content += f"{pattern}\n"
        added.append(pattern)

    if added:
        gi_path.write_text(content)
        logger.info("Added %d missing .gitignore entries: %s", len(added), added)
    return added


def remove_sensitive_from_tree(findings: list[HygieneFinding]) -> list[str]:
    """Remove sensitive files from the working tree (not history)."""
    removed: list[str] = []
    for f in findings:
        if f.category != "sensitive-file":
            continue
        # Extract path from message
        match = re.search(r"committed: (.+)$", f.message)
        if not match:
            continue
        path = match.group(1)
        fpath = ROOT / path
        if fpath.exists():
            _run_git(["rm", "--cached", path])
            removed.append(path)
            logger.warning("Removed %s from index (not history)", path)
    return removed


# ---------------------------------------------------------------------------
# GitHub integration
# ---------------------------------------------------------------------------


def _get_github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _get_github_repo() -> str | None:
    result = _run_git(["remote", "get-url", "origin"])
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    # git@github.com:owner/repo.git or https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?\s*$", url)
    if m:
        return m.group(1)
    return None


def create_github_issues(report: HygieneReport) -> list[str]:
    """File GitHub issues for hygiene findings. Returns list of issue URLs."""
    token = _get_github_token()
    repo = _get_github_repo()
    if not token or not repo:
        logger.warning("Cannot create GitHub issues — missing token or repo")
        return []

    created: list[str] = []
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Group security issues into a single issue
    security_findings = [f for f in report.findings if f.severity == "security"]
    if security_findings:
        body_lines = [
            "## SECURITY: Secret possibly committed — needs human-supervised history rewrite\n",
        ]
        for f in security_findings:
            body_lines.append(f"- **{f.message}**")
            body_lines.append(f"  {f.details}")
            if f.sha:
                body_lines.append(f"  SHA: `{f.sha}`")
            body_lines.append("")

        body = "\n".join(body_lines)
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(
                    {
                        "title": "SECURITY: secret possibly committed, needs human-supervised history rewrite",
                        "body": body,
                        "labels": ["security", "ai-agent-task"],
                    }
                ).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                created.append(data["html_url"])
                logger.info("Created security issue: %s", data["html_url"])
        except (urllib.error.URLError, OSError) as e:
            logger.error("Failed to create security issue: %s", e)

    # File individual issues for errors that aren't security
    error_findings = [f for f in report.findings if f.severity == "error"]
    if len(error_findings) > 1:
        body_lines = ["## Hygiene errors found\n"]
        for f in error_findings:
            body_lines.append(f"- **{f.message}**")
            body_lines.append(f"  {f.details}")
            body_lines.append("")
        body = "\n".join(body_lines)
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(
                    {
                        "title": "Commit hygiene: issues found",
                        "body": body,
                        "labels": ["ai-agent-task", "hourly"],
                    }
                ).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                created.append(data["html_url"])
                logger.info("Created hygiene issue: %s", data["html_url"])
        except (urllib.error.URLError, OSError) as e:
            logger.error("Failed to create hygiene issue: %s", e)
>>>>>>> origin/main

    return created


# ---------------------------------------------------------------------------
<<<<<<< HEAD
# Reporting
# ---------------------------------------------------------------------------


def print_report(report: HygieneReport, *, json_output: bool = False) -> int:
    """Print the hygiene report. Returns exit code (0 = clean, 1 = issues)."""
    if json_output:
        payload = {
            "status": "issues_found" if report.has_issues else "clean",
            "findings": [
                {"severity": f.severity, "check": f.check, "detail": f.detail}
                for f in report.findings
            ],
            "low_quality_commits": len(report.low_quality_commits),
            "sensitive_commits": len(report.sensitive_commits),
            "missing_gitignore_patterns": report.missing_gitignore_patterns,
            "large_files": report.large_files,
        }
        print(json.dumps(payload, indent=2))
    else:
        if not report.has_issues:
            logger.info("Commit hygiene check passed — no issues found.")
            print("Commit hygiene check passed — no issues found.")
            return 0

        print(f"=== Commit Hygiene Report: {len(report.findings)} finding(s) ===")
        for f in report.findings:
            tag = f"[{f.severity.upper()}]"
            print(f"  {tag} {f.check}: {f.detail}")

        if report.low_quality_commits:
            print(f"\n  Low-quality commit messages: {len(report.low_quality_commits)} (informational)")
        if report.sensitive_commits:
            print(f"\n  ⚠️  SENSITIVE COMMITS: {len(report.sensitive_commits)} — needs human review")
        if report.missing_gitignore_patterns:
            print(f"\n  Missing .gitignore patterns: {', '.join(report.missing_gitignore_patterns)}")
        if report.large_files:
            print(f"\n  Large files outside data/: {len(report.large_files)}")

    return 1 if report.has_issues else 0
=======
# Report
# ---------------------------------------------------------------------------


def run_all_checks() -> HygieneReport:
    report = HygieneReport(findings=[])
    report.findings.extend(check_commit_messages())
    report.findings.extend(check_sensitive_files())
    report.findings.extend(check_gitignore())
    report.findings.extend(check_large_files())
    return report


def print_report(report: HygieneReport) -> None:
    if report.is_clean:
        print("=== Commit hygiene: CLEAN ===")
        return

    severity_order = {"security": 0, "error": 1, "warning": 2, "info": 3}
    sorted_findings = sorted(
        report.findings, key=lambda f: severity_order.get(f.severity, 99)
    )

    for f in sorted_findings:
        tag = f"[{f.severity.upper()}]"
        print(f"{tag} [{f.category}] {f.message}")
        if f.details:
            print(f"    {f.details}")
        if f.sha:
            print(f"    SHA: {f.sha}")

    summary = f"=== {len(report.findings)} finding(s) ==="
    print(summary)


def report_json(report: HygieneReport) -> str:
    payload = {
        "status": "clean" if report.is_clean else "issues_found",
        "count": len(report.findings),
        "findings": [
            {
                "severity": f.severity,
                "category": f.category,
                "message": f.message,
                "details": f.details,
                "sha": f.sha,
            }
            for f in report.findings
        ],
    }
    return json.dumps(payload, indent=2)
>>>>>>> origin/main


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
<<<<<<< HEAD
        description="Hourly commit hygiene sweep",
=======
        description="Hourly commit-hygiene audit",
>>>>>>> origin/main
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
<<<<<<< HEAD
        help="Create GitHub issues for security findings needing human review.",
=======
        help="Create GitHub issues for hygiene findings via the GitHub API.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply non-destructive fixes (missing .gitignore entries, remove from index).",
>>>>>>> origin/main
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    os.chdir(ROOT)
<<<<<<< HEAD

    report = run_hygiene_sweep()
    exit_code = print_report(report, json_output=args.json_output)

    if args.create_issues and report.has_security_issues:
        created = create_security_issues(report)
        if created:
            logger.info("Created %d security issue(s).", len(created))
            for url in created:
                if not url.startswith("ERROR"):
                    logger.info("  %s", url)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
=======
    report = run_all_checks()

    if args.json_output:
        print(report_json(report))
    else:
        print_report(report)

    if report.is_clean:
        return 0

    # Apply non-destructive fixes if requested
    if args.fix:
        gi_findings = [f for f in report.findings if f.category == "gitignore"]
        if gi_findings:
            fix_missing_gitignore_entries(gi_findings)

        sec_findings = [f for f in report.findings if f.severity == "security"]
        if sec_findings:
            remove_sensitive_from_tree(sec_findings)

    # Create GitHub issues for serious findings
    if args.create_issues and (report.has_errors or report.has_security_issues):
        create_github_issues(report)

    return 1 if report.has_errors or report.has_security_issues else 0


if __name__ == "__main__":
    sys.exit(main())
>>>>>>> origin/main
