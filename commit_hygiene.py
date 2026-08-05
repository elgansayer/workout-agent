"""Hourly commit-hygiene audit.

Prevents sensitive files and bloated binaries from landing in commits,
and validates that commit messages are descriptive (per AGENTS.md).

Design
------
* Runs ``git log -10 --stat`` to inspect commit messages.
* Runs ``git log -p -10 -- .env .env.* data/ '*.db'`` to detect
  accidentally-committed secrets or database files.
* Checks ``.gitignore`` for required patterns (``*.db``, ``.env``,
  ``__pycache__/``, ``.pytest_cache/``, ``.venv/``).
* Scans for tracked files >1 MB outside ``data/`` (gitignored).

When an issue is found the script:
1. Adds missing ``.gitignore`` entries when safe (non-destructive fix).
2. For security issues (committed secrets): emits a ``SECURITY:``-flagged
   report and exits non-zero so the calling automation can file a GitHub
   issue for human-supervised history rewrite.
3. For large files: emits a warning and exits non-zero.

Usage
-----
::

    python commit_hygiene.py               # report only; exit 1 if issues found
    python commit_hygiene.py --json        # machine-readable output
    python commit_hygiene.py --create-issues  # file GitHub issues for findings
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("commit_hygiene")

ROOT = Path(__file__).resolve().parent

# Patterns that .gitignore must cover
REQUIRED_GITIGNORE = [
    "*.db",
    ".env",
    "__pycache__/",
    ".pytest_cache/",
    ".venv/",
]

# Size threshold: files larger than this (in bytes) outside data/ are suspicious
MAX_FILE_SIZE = 3 * 1024 * 1024  # 3 MB

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class HygieneFinding:
    severity: str  # "info", "warning", "error", "security"
    category: str  # "commit-message", "sensitive-file", "gitignore", "large-file"
    message: str
    details: str = ""
    sha: str = ""  # relevant commit SHA if applicable


@dataclass
class HygieneReport:
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
    """Check for committed .env, database, or data/ files."""
    findings: list[HygieneFinding] = []
    result = _run_git(["log", "-p", "-10", "--", ".env", ".env.*", "data/", "*.db"])
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
            # Skip .env.example (explicitly whitelisted)
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
        content += f"\n{pattern}\n"
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

    return created


# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hourly commit-hygiene audit",
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
        help="Create GitHub issues for hygiene findings via the GitHub API.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply non-destructive fixes (missing .gitignore entries, remove from index).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    os.chdir(ROOT)
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
