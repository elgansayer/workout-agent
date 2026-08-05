"""Tests for commit_hygiene.py: hourly commit-hygiene audit."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import commit_hygiene

# ---------------------------------------------------------------------------
# check_commit_messages
# ---------------------------------------------------------------------------


class TestCheckCommitMessages:
    def test_empty_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_commit_messages() == []

    def test_good_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    "abc12345 Add user_id column to workout_history\n"
                    "def67890 Fix #42: Handle Hevy API timeout gracefully\n"
                    "11122233 Refactor insight_cron for per-user timezones\n"
                ),
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_commit_messages() == []

    def test_wip_message_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0,
                stdout="abc12345 wip\n",
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_commit_messages()
        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert findings[0].category == "commit-message"
        assert "wip" in findings[0].message

    def test_fix_message_is_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0,
                stdout="abc12345 fix\n",
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_commit_messages()
        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert findings[0].category == "commit-message"

    def test_git_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="fatal")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_commit_messages() == []


# ---------------------------------------------------------------------------
# check_sensitive_files
# ---------------------------------------------------------------------------


class TestCheckSensitiveFiles:
    def test_no_sensitive_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_sensitive_files() == []

    def test_env_committed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit abc123456789abcdef\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 20:55:21 2026 +0100\n\n"
            "    some message\n\n"
            "diff --git a/.env b/.env\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/.env\n"
            "@@ -0,0 +1 @@\n"
            "+SECRET=value\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) >= 1
        assert any(f.severity == "security" for f in findings)
        assert any(".env" in f.message for f in findings)

    def test_env_example_is_whitelisted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit abc123456789abcdef\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 20:55:21 2026 +0100\n\n"
            "    Add .env.example\n\n"
            "diff --git a/.env.example b/.env.example\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/.env.example\n"
            "@@ -0,0 +1,77 @@\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) == 0

    def test_db_committed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit abc123456789abcdef\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 20:55:21 2026 +0100\n\n"
            "    oops\n\n"
            "diff --git a/workout.db b/workout.db\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/workout.db\n"
            "@@ -0,0 +1 @@\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) >= 1
        assert any("workout.db" in f.message for f in findings)

    def test_git_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="fatal")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_sensitive_files() == []


# ---------------------------------------------------------------------------
# check_gitignore
# ---------------------------------------------------------------------------


class TestCheckGitignore:
    def test_all_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(
            "*.db\n"
            ".env\n"
            "__pycache__/\n"
            ".pytest_cache/\n"
            ".venv/\n"
        )
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert findings == []

    def test_missing_pycache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "__pycache__/" in findings[0].message

    def test_missing_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(".env\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert "*.db" in findings[0].message

    def test_missing_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert ".env" in findings[0].message

    def test_missing_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# almost empty\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) >= 4

    def test_gitignore_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "missing" in findings[0].message.lower()


# ---------------------------------------------------------------------------
# check_large_files
# ---------------------------------------------------------------------------


class TestCheckLargeFiles:
    def test_no_large_files(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Create a small tracked file
        small_file = tmp_path / "small.py"
        small_file.write_text("hello")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0, stdout="small.py\x00",
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(
            commit_hygiene, "MAX_FILE_SIZE", 10 * 1024 * 1024,
        )
        findings = commit_hygiene.check_large_files()
        assert findings == []

    def test_large_file_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        large_file = tmp_path / "big.log"
        # Create a real large-ish file
        large_file.write_text("x" * 1000)
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0, stdout="big.log\x00",
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        # Set threshold to a very small value
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100)
        findings = commit_hygiene.check_large_files()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert findings[0].category == "large-file"
        assert "big.log" in findings[0].message

    def test_large_file_under_data_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        large_file = data_dir / "big.csv"
        large_file.write_text("x" * 2000)
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [], 0, stdout="data/big.csv\x00",
            )
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100)
        findings = commit_hygiene.check_large_files()
        assert findings == []

    def test_git_failure_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="fatal")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        assert commit_hygiene.check_large_files() == []


# ---------------------------------------------------------------------------
# fix_missing_gitignore_entries
# ---------------------------------------------------------------------------


class TestFixMissingGitignore:
    def test_adds_missing_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = [
            commit_hygiene.HygieneFinding(
                severity="error",
                category="gitignore",
                message=".gitignore missing entry: __pycache__/",
            ),
        ]
        added = commit_hygiene.fix_missing_gitignore_entries(findings)
        assert added == ["__pycache__/"]
        assert "__pycache__/" in gi.read_text()

    def test_already_present_not_duplicated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n__pycache__/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = [
            commit_hygiene.HygieneFinding(
                severity="error",
                category="gitignore",
                message=".gitignore missing entry: __pycache__/",
            ),
        ]
        added = commit_hygiene.fix_missing_gitignore_entries(findings)
        assert added == []

    def test_no_gitignore_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = [
            commit_hygiene.HygieneFinding(
                severity="error",
                category="gitignore",
                message=".gitignore missing entry: *.db",
            ),
        ]
        added = commit_hygiene.fix_missing_gitignore_entries(findings)
        assert added == []


# ---------------------------------------------------------------------------
# remove_sensitive_from_tree
# ---------------------------------------------------------------------------


class TestRemoveSensitiveFromTree:
    def test_removes_existing_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("SECRET=foo")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        calls: list[list[str]] = []
        def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)

        findings = [
            commit_hygiene.HygieneFinding(
                severity="security",
                category="sensitive-file",
                message="Sensitive file committed: .env",
                sha="abc12345",
            ),
        ]
        removed = commit_hygiene.remove_sensitive_from_tree(findings)
        assert removed == [".env"]
        assert any("rm" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# HygieneReport
# ---------------------------------------------------------------------------


class TestHygieneReport:
    def test_empty_is_clean(self) -> None:
        report = commit_hygiene.HygieneReport()
        assert report.is_clean is True
        assert report.has_errors is False
        assert report.has_security_issues is False

    def test_info_only_is_not_error(self) -> None:
        report = commit_hygiene.HygieneReport(
            findings=[
                commit_hygiene.HygieneFinding(
                    severity="info",
                    category="commit-message",
                    message="Low-quality commit message: wip",
                ),
            ]
        )
        assert report.is_clean is False
        assert report.has_errors is False
        assert report.has_security_issues is False

    def test_error_is_error(self) -> None:
        report = commit_hygiene.HygieneReport(
            findings=[
                commit_hygiene.HygieneFinding(
                    severity="error",
                    category="gitignore",
                    message=".gitignore missing entry: *.db",
                ),
            ]
        )
        assert report.has_errors is True

    def test_security_is_error_and_security(self) -> None:
        report = commit_hygiene.HygieneReport(
            findings=[
                commit_hygiene.HygieneFinding(
                    severity="security",
                    category="sensitive-file",
                    message="Sensitive file committed: .env",
                    sha="abc12345",
                ),
            ]
        )
        assert report.has_errors is True
        assert report.has_security_issues is True


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------


class TestRunAllChecks:
    def test_clean_repo(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)

        report = commit_hygiene.run_all_checks()
        assert report.is_clean is True


# ---------------------------------------------------------------------------
# report_json
# ---------------------------------------------------------------------------


class TestReportJson:
    def test_clean_json(self) -> None:
        report = commit_hygiene.HygieneReport()
        data = json.loads(commit_hygiene.report_json(report))
        assert data["status"] == "clean"
        assert data["count"] == 0

    def test_issues_json(self) -> None:
        report = commit_hygiene.HygieneReport(
            findings=[
                commit_hygiene.HygieneFinding(
                    severity="error",
                    category="gitignore",
                    message=".gitignore missing entry: *.db",
                    details="Required pattern",
                ),
            ]
        )
        data = json.loads(commit_hygiene.report_json(report))
        assert data["status"] == "issues_found"
        assert data["count"] == 1
        assert data["findings"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# create_github_issues
# ---------------------------------------------------------------------------


class TestCreateGitHubIssues:
    def test_no_token_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        report = commit_hygiene.HygieneReport(
            findings=[
                commit_hygiene.HygieneFinding(
                    severity="security",
                    category="sensitive-file",
                    message="Sensitive file committed: .env",
                    sha="abc12345",
                ),
            ]
        )
        created = commit_hygiene.create_github_issues(report)
        assert created == []


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(sys, "argv", ["commit_hygiene.py"])
        assert commit_hygiene.main() == 0

    def test_main_with_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# empty\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(sys, "argv", ["commit_hygiene.py"])
        assert commit_hygiene.main() == 1

    def test_main_json_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(sys, "argv", ["commit_hygiene.py", "--json"])
        exit_code = commit_hygiene.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "clean"

    def test_main_fix_adds_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")
        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(sys, "argv", ["commit_hygiene.py", "--fix"])
        commit_hygiene.main()
        content = gi.read_text()
        assert ".env" in content
        assert "__pycache__/" in content


# ---------------------------------------------------------------------------
# Integration: run as subprocess
# ---------------------------------------------------------------------------


def test_commit_hygiene_script_runs() -> None:
    """Smoke test: the script runs without crashing."""
    result = subprocess.run(
        [sys.executable, "commit_hygiene.py"],
        capture_output=True,
        text=True,
        cwd=commit_hygiene.ROOT,
        check=False,
    )
    # May exit 0 or 1 depending on current repo state, but should not crash
    assert result.returncode in (0, 1)
    assert "CLEAN" in result.stdout or "finding" in result.stdout.lower()