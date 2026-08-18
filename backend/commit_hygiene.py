"""Hourly commit hygiene sweep.

Audits git history and workspace for:
1. Low-quality commit messages
2. Sensitive files committed (.env, databases, logs)
3. Required patterns in .gitignore
4. Large files outside data/
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("commit_hygiene")

ROOT = Path(__file__).resolve().parent

REQUIRED_GITIGNORE: list[str] = [
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

MAX_FILE_SIZE: int = 5 * 1024 * 1024  # 5 MB


@dataclass
class HygieneFinding:
    severity: str  # "info", "error", "security"
    category: str
    message: str
    details: str | None = None
    sha: str | None = None
    file: str | None = None


@dataclass
class HygieneReport:
    findings: list[HygieneFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return len(self.findings) == 0

    @property
    def has_errors(self) -> bool:
        return any(f.severity in ("error", "security") for f in self.findings)

    @property
    def has_security_issues(self) -> bool:
        return any(f.severity == "security" for f in self.findings)


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(cwd or ROOT),
        check=False,
    )


def check_commit_messages() -> list[HygieneFinding]:
    res = _run_git(["log", "-10", "--format=%h %s"])
    if res.returncode != 0 or not res.stdout.strip():
        return []

    findings: list[HygieneFinding] = []
    low_quality = re.compile(r"^(fix|wip|update|changes|test|tweak|stuff|oops|tmp|temp)$", re.IGNORECASE)

    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, msg = parts[0], parts[1].strip()
        if len(msg) < 15 or low_quality.match(msg):
            findings.append(
                HygieneFinding(
                    severity="info",
                    category="commit-message",
                    message=f"Low-quality commit message: {msg}",
                    sha=sha,
                )
            )
    return findings


def check_sensitive_files() -> list[HygieneFinding]:
    res = _run_git(["log", "-10", "-p"])
    if res.returncode != 0 or not res.stdout:
        return []

    findings: list[HygieneFinding] = []
    current_sha: str | None = None

    for line in res.stdout.splitlines():
        if line.startswith("commit "):
            current_sha = line.split()[1]
        elif line.startswith("diff --git a/"):
            parts = line.split()
            if len(parts) >= 3:
                filename = parts[2].removeprefix("a/")
                if filename == ".env.example":
                    continue
                if (
                    filename.startswith(".env")
                    or filename.endswith((".db", ".sqlite", ".sqlite3", ".log"))
                    or filename == "agent.log"
                ):
                    findings.append(
                        HygieneFinding(
                            severity="security",
                            category="sensitive-file",
                            message=f"Sensitive file committed: {filename}",
                            sha=current_sha,
                            file=filename,
                        )
                    )
    return findings


def check_gitignore() -> list[HygieneFinding]:
    gi_path = ROOT / ".gitignore"
    if not gi_path.exists():
        return [
            HygieneFinding(
                severity="error",
                category="gitignore",
                message=".gitignore file is missing",
            )
        ]

    content = gi_path.read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")}

    findings: list[HygieneFinding] = []
    for pattern in REQUIRED_GITIGNORE:
        if pattern not in lines:
            findings.append(
                HygieneFinding(
                    severity="error",
                    category="gitignore",
                    message=f".gitignore missing entry: {pattern}",
                    details="Required pattern",
                )
            )
    return findings


def check_large_files() -> list[HygieneFinding]:
    res = _run_git(["ls-files", "-z"])
    if res.returncode != 0:
        return []

    findings: list[HygieneFinding] = []
    for filename in res.stdout.split("\0"):
        if not filename:
            continue
        p = ROOT / filename
        if p.is_file() and not filename.startswith("data/"):
            try:
                size = p.stat().st_size
                if size > MAX_FILE_SIZE:
                    findings.append(
                        HygieneFinding(
                            severity="error",
                            category="large-file",
                            message=f"Large file tracked in git: {filename} ({size} bytes)",
                            file=filename,
                        )
                    )
            except OSError:
                pass
    return findings


def fix_missing_gitignore_entries(findings: list[HygieneFinding]) -> list[str]:
    gi_path = ROOT / ".gitignore"
    if not gi_path.exists():
        return []

    content = gi_path.read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")}

    added: list[str] = []
    for f in findings:
        if f.category == "gitignore" and f.message.startswith(".gitignore missing entry: "):
            pattern = f.message.removeprefix(".gitignore missing entry: ").strip()
            if pattern not in lines:
                lines.add(pattern)
                added.append(pattern)

    if added:
        new_content = content.rstrip() + "\n" + "\n".join(added) + "\n"
        gi_path.write_text(new_content, encoding="utf-8")
    return added


def remove_sensitive_from_tree(findings: list[HygieneFinding]) -> list[str]:
    removed: list[str] = []
    for f in findings:
        target = f.file
        if not target and f.category == "sensitive-file":
            target = f.message.removeprefix("Sensitive file committed: ").strip()
        if target:
            res = _run_git(["rm", "--cached", "-f", target])
            if res.returncode == 0:
                removed.append(target)
    return removed


def run_all_checks() -> HygieneReport:
    report = HygieneReport()
    report.findings.extend(check_commit_messages())
    report.findings.extend(check_sensitive_files())
    report.findings.extend(check_gitignore())
    report.findings.extend(check_large_files())
    return report


def report_json(report: HygieneReport) -> str:
    status = "clean" if report.is_clean else "issues_found"
    data = {
        "status": status,
        "count": len(report.findings),
        "has_errors": report.has_errors,
        "has_security_issues": report.has_security_issues,
        "findings": [
            {
                "severity": f.severity,
                "category": f.category,
                "message": f.message,
                "details": f.details,
                "sha": f.sha,
                "file": f.file,
            }
            for f in report.findings
        ],
    }
    return json.dumps(data, indent=2)


def create_github_issues(report: HygieneReport) -> list[str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return []
    # If token present, create issues for security findings
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit commit hygiene")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--fix", action="store_true", help="Auto-fix missing gitignore patterns")
    parser.add_argument("--create-issues", action="store_true", help="Create GitHub issues")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    report = run_all_checks()

    if args.fix:
        fix_missing_gitignore_entries(report.findings)

    if args.json:
        print(report_json(report))
        return 0

    if args.create_issues:
        create_github_issues(report)

    if report.is_clean:
        print("[CLEAN] Commit hygiene check passed.")
    else:
        print(f"Found {len(report.findings)} findings:")
        for f in report.findings:
            print(f"  - [{f.severity.upper()}] {f.message}")

    return 1 if report.has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
