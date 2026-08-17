"""Hourly commit hygiene sweep.

Automates the checks defined in `.agents/automations/task-hourly-commit-hygiene.md`:
1. Review last 10 commits for descriptive messages (informational only).
2. Check no sensitive files (``.env``, ``.db``, ``data/``) were committed.
3. Confirm ``.gitignore`` covers required patterns.
4. Confirm no large binary/log files outside ``data/``.

When run with ``--create-issues``, files GitHub issues for any problems that
require human attention (e.g. potential secret leaks).

Usage
-----
::

    python commit_hygiene.py               # report only; exit 1 if issues found
    python commit_hygiene.py --json        # machine-readable output
    python commit_hygiene.py --create-issues  # file GitHub issues for problems
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("commit_hygiene")

ROOT = Path(__file__).resolve().parent

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
    ".env",
    ".env.*",
    "data/",
    "*.db",
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class Finding(NamedTuple):
    """A single hygiene finding."""

    severity: str  # "info", "warning", "security"
    check: str  # which check produced this
    detail: str  # human-readable description


@dataclass
class HygieneReport:
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

    return created


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hourly commit hygiene sweep",
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
        help="Create GitHub issues for security findings needing human review.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    os.chdir(ROOT)

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
