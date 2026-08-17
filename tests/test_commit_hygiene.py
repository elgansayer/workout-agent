<<<<<<< HEAD
"""Tests for commit_hygiene.py — the hourly commit hygiene sweep."""
=======
"""Tests for commit_hygiene.py: hourly commit-hygiene audit."""
>>>>>>> origin/main

from __future__ import annotations

import json
import subprocess
<<<<<<< HEAD
from pathlib import Path
from unittest.mock import MagicMock, patch

import commit_hygiene
from commit_hygiene import (
    Finding,
    HygieneReport,
    _check_commit_messages,
    _check_gitignore,
    _check_large_files,
    _check_sensitive_files,
    _check_sensitive_files_on_disk,
    _create_issue,
    create_security_issues,
    print_report,
    run_hygiene_sweep,
)


class TestCheckCommitMessages:
    def test_returns_list_of_low_quality_messages(self) -> None:
        """Low-quality commit messages (short or matching known patterns) are flagged."""
        # Simulate git log output with mixed quality messages
        mock_output = (
            "abc1234 wip\n"
            "def5678 fix\n"
            "ghi9012 Add type annotations to test_google_health_client.py tests\n"
            "jkl3456 update\n"
            "mno7890 Wire ai_provider into webapp chat endpoint with fallback\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mock_output)

            result = _check_commit_messages()

        # "wip", "fix", and "update" should be flagged; the long ones should not
        assert len(result) >= 2  # at least the short ones
        flagged_msgs = " ".join(result)
        assert "wip" in flagged_msgs.lower() or "fix" in flagged_msgs.lower()

    def test_handles_git_error_gracefully(self) -> None:
        """If git fails, return empty list instead of crashing."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            result = _check_commit_messages()
        assert result == []

    def test_returns_empty_for_all_descriptive_messages(self) -> None:
        """Well-written messages should not be flagged."""
        mock_output = (
            "abc1234 Add type annotations to test_google_health_client.py tests\n"
            "def5678 Wire ai_provider into webapp chat endpoint with fallback\n"
            "ghi9012 Refactor scheduler to use unified dispatch loop per user\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mock_output)

            result = _check_commit_messages()

        assert result == []


class TestCheckSensitiveFiles:
    def test_flags_real_sensitive_files(self) -> None:
        """A commit that touches a real .env or .db should be flagged."""
        mock_output = (
            "commit abc1234567890abc\n"
            "Author: test\n"
            "Date: today\n"
            "\n"
            "    accidental commit\n"
            "diff --git a/.env b/.env\n"
            "new file mode 100644\n"
            "+SECRET_KEY=hunter2\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mock_output)

            result = _check_sensitive_files()

        assert "abc123456" in result

    def test_allows_env_example(self) -> None:
        """A commit that only touches .env.example should NOT be flagged."""
        mock_output = (
            "commit def5678901def\n"
            "Author: test\n"
            "Date: today\n"
            "\n"
            "    add env example\n"
            "diff --git a/.env.example b/.env.example\n"
            "new file mode 100644\n"
            "+GEMINI_API_KEY=\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mock_output)

            result = _check_sensitive_files()

        assert result == []

    def test_mixed_commit_flags_only_sensitive_paths(self) -> None:
        """A commit that touches both .env.example and .env should be flagged."""
        mock_output = (
            "commit ghi9012345ghi\n"
            "Author: test\n"
            "Date: today\n"
            "\n"
            "    mixed commit\n"
            "diff --git a/.env.example b/.env.example\n"
            "+GEMINI_API_KEY=\n"
            "diff --git a/.env b/.env\n"
            "+GEMINI_API_KEY=real-key-123\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=mock_output)

            result = _check_sensitive_files()

        assert "ghi901234" in result

    def test_no_output_returns_empty(self) -> None:
        """Empty git output means no sensitive files found."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")

            result = _check_sensitive_files()

        assert result == []

    def test_handles_git_error(self) -> None:
        """Git failures should be handled gracefully."""
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = _check_sensitive_files()
        assert result == []


class TestCheckSensitiveFilesOnDisk:
    def test_no_sensitive_files_found(self) -> None:
        """When no sensitive files are tracked by git, returns empty."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = _check_sensitive_files_on_disk()
        assert result == []

    def test_flags_tracked_env_file(self) -> None:
        """A tracked .env file is flagged."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=".env\n")
            result = _check_sensitive_files_on_disk()
        assert ".env" in result

    def test_allows_tracked_env_example(self) -> None:
        """A tracked .env.example is NOT flagged (intentionally committed)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=".env.example\n")
            result = _check_sensitive_files_on_disk()
        assert result == []

    def test_flags_tracked_db_files(self) -> None:
        """Tracked .db files are flagged."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="data/production.db\n")
            result = _check_sensitive_files_on_disk()
        assert "data/production.db" in result

    def test_handles_git_error(self) -> None:
        """Git failures should be handled gracefully."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            result = _check_sensitive_files_on_disk()
        assert result == []

    def test_filters_mixed_output(self) -> None:
        """Mixed output: flags .env but not .env.example."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=".env\n.env.example\n")
            result = _check_sensitive_files_on_disk()
        assert ".env" in result
        assert ".env.example" not in result


class TestCheckGitignore:
    def test_all_patterns_present(self, tmp_path: Path) -> None:
        """Returns empty when all required patterns are in .gitignore."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(
            "*.db\n"
            ".env\n"
            "__pycache__/\n"
            ".pytest_cache/\n"
            ".venv/\n"
        )

        with patch.object(commit_hygiene, "ROOT", tmp_path):
            result = _check_gitignore()
        assert result == []

    def test_missing_patterns_detected(self, tmp_path: Path) -> None:
        """Missing patterns are reported."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.db\n.env\n")

        with patch.object(commit_hygiene, "ROOT", tmp_path):
            result = _check_gitignore()
        assert "__pycache__/" in result
        assert ".pytest_cache/" in result
        assert ".venv/" in result

    def test_no_gitignore_file(self, tmp_path: Path) -> None:
        """All patterns reported as missing when no .gitignore exists."""
        with patch.object(commit_hygiene, "ROOT", tmp_path):
            result = _check_gitignore()
        assert len(result) == len(commit_hygiene.REQUIRED_GITIGNORE_PATTERNS)


class TestCheckLargeFiles:
    def test_no_large_files(self, tmp_path: Path) -> None:
        """Returns empty when all files are small."""
        f = tmp_path / "small.txt"
        f.write_text("hello")

        with patch.object(Path, "rglob", return_value=[f]):
            result = _check_large_files()
        assert result == []

    def test_flags_large_file(self, tmp_path: Path) -> None:
        """A file over the size threshold outside data/ is flagged."""
        large_file = MagicMock(spec=Path)
        large_file.is_file.return_value = True
        large_file.parts = ("workspace", "large.bin")
        large_file.stat.return_value.st_size = 6 * 1024 * 1024  # 6 MB
        large_file.relative_to.return_value = Path("large.bin")

        with patch.object(Path, "rglob", return_value=[large_file]):
            result = _check_large_files()
        assert len(result) == 1
        assert "large.bin" in result[0]

    def test_excludes_data_directory(self, tmp_path: Path) -> None:
        """Large files in data/ are not flagged."""
        data_file = MagicMock(spec=Path)
        data_file.is_file.return_value = True
        data_file.parts = ("workspace", "data", "big.log")
        data_file.stat.return_value.st_size = 100 * 1024 * 1024  # 100 MB
        data_file.relative_to.return_value = Path("data/big.log")

        with patch.object(Path, "rglob", return_value=[data_file]):
            result = _check_large_files()
        assert result == []


class TestHygieneReport:
    def test_empty_report_has_no_issues(self) -> None:
        """A fresh report has no issues."""
        report = HygieneReport()
        assert not report.has_issues
        assert not report.has_security_issues

    def test_report_with_findings_has_issues(self) -> None:
        """A report with findings shows issues."""
        report = HygieneReport()
        report.findings.append(
            Finding(severity="warning", check="test", detail="test finding")
        )
        assert report.has_issues

    def test_report_with_sensitive_commits_has_security_issues(self) -> None:
        """A report with sensitive commits has security issues."""
        report = HygieneReport()
        report.sensitive_commits.append("abc123")
        assert report.has_issues
        assert report.has_security_issues


class TestRunHygieneSweep:
    def test_returns_report(self) -> None:
        """The sweep returns a HygieneReport even without git history."""
        with patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=[]),
            _check_sensitive_files=MagicMock(return_value=[]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[]),
            _check_gitignore=MagicMock(return_value=[]),
            _check_large_files=MagicMock(return_value=[]),
        ):
            report = run_hygiene_sweep()
            assert isinstance(report, HygieneReport)

    def test_sweep_captures_all_issue_types(self) -> None:
        """When checks find issues, the report aggregates them."""
        with patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=["abc: fix"]),
            _check_sensitive_files=MagicMock(return_value=["deadbeef"]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[".env"]),
            _check_gitignore=MagicMock(return_value=["__pycache__/"]),
            _check_large_files=MagicMock(return_value=["big.log (6.0 MB)"]),
        ):
            report = run_hygiene_sweep()
            assert report.has_issues
            assert report.has_security_issues
            assert len(report.low_quality_commits) == 1
            assert len(report.sensitive_commits) == 1
            assert len(report.missing_gitignore_patterns) == 1
            assert len(report.large_files) == 1


class TestPrintReport:
    def test_clean_report_returns_zero(self) -> None:
        """A clean report exits 0."""
        report = HygieneReport()
        exit_code = print_report(report)
        assert exit_code == 0

    def test_issues_report_returns_one(self) -> None:
        """A report with issues exits 1."""
        report = HygieneReport()
        report.findings.append(
            Finding(severity="warning", check="test", detail="test")
        )
        exit_code = print_report(report)
        assert exit_code == 1

    def test_json_output_is_valid(self, capsys) -> None:
        """JSON output is parseable and contains expected keys."""
        report = HygieneReport()
        report.missing_gitignore_patterns.append("*.db")
        exit_code = print_report(report, json_output=True)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "issues_found"
        assert "*.db" in data["missing_gitignore_patterns"]
        assert exit_code == 1

    def test_json_output_clean(self, capsys) -> None:
        """Clean report outputs clean status in JSON."""
        report = HygieneReport()
        exit_code = print_report(report, json_output=True)

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "clean"
        assert exit_code == 0


class TestCreateSecurityIssues:
    def test_creates_issues_for_sensitive_commits(self) -> None:
        """Security issues are created for each sensitive commit."""
        report = HygieneReport()
        report.sensitive_commits = ["abc123", "def456"]

        with patch.object(
            commit_hygiene,
            "_create_issue",
            return_value="https://github.com/test/repo/issues/42",
        ) as mock_create:
            created = create_security_issues(report)
            assert len(created) == 2
            assert mock_create.call_count == 2

    def test_no_issues_when_no_sensitive_commits(self) -> None:
        """No issues created when report is clean."""
        report = HygieneReport()
        created = create_security_issues(report)
        assert created == []

    def test_issue_title_contains_sha(self) -> None:
        """The generated issue title mentions the offending SHA."""
        report = HygieneReport()
        report.sensitive_commits = ["abc123def"]

        with patch.object(commit_hygiene, "_create_issue", return_value="http://example.com/1") as mock:
            create_security_issues(report)

        call_args = mock.call_args[0]
        title = call_args[0]
        assert "abc123def" in title
        assert "SECURITY" in title


class TestCreateIssue:
    def test_returns_error_without_token(self) -> None:
        """Without a token, issue creation returns an error string."""
        with patch.object(commit_hygiene, "_get_github_token", return_value=None):
            result = _create_issue("title", "body", ["label"])
        assert result.startswith("ERROR")

    def test_returns_error_without_repo(self) -> None:
        """Without a repo, issue creation returns an error string."""
        with (
            patch.object(commit_hygiene, "_get_github_token", return_value="fake"),
            patch.object(commit_hygiene, "_get_github_repo", return_value=None),
        ):
            result = _create_issue("title", "body", ["label"])
        assert result.startswith("ERROR")


class TestCLI:
    def test_main_no_issues(self) -> None:
        """CLI exits 0 when no issues found."""
        with patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=[]),
            _check_sensitive_files=MagicMock(return_value=[]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[]),
            _check_gitignore=MagicMock(return_value=[]),
            _check_large_files=MagicMock(return_value=[]),
        ):
            with patch("sys.argv", ["commit_hygiene.py"]):
                exit_code = commit_hygiene.main()
            assert exit_code == 0

    def test_main_with_issues(self) -> None:
        """CLI exits 1 when issues found."""
        with patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=["abc: fix"]),
            _check_sensitive_files=MagicMock(return_value=[]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[]),
            _check_gitignore=MagicMock(return_value=[]),
            _check_large_files=MagicMock(return_value=[]),
        ):
            with patch("sys.argv", ["commit_hygiene.py"]):
                exit_code = commit_hygiene.main()
            assert exit_code == 1

    def test_main_json_output(self, capsys) -> None:
        """--json flag produces JSON output."""
        with patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=[]),
            _check_sensitive_files=MagicMock(return_value=[]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[]),
            _check_gitignore=MagicMock(return_value=[]),
            _check_large_files=MagicMock(return_value=[]),
        ):
            with patch("sys.argv", ["commit_hygiene.py", "--json"]):
                exit_code = commit_hygiene.main()

            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["status"] == "clean"
            assert exit_code == 0

    def test_main_create_issues_skips_when_no_security_issues(self) -> None:
        """--create-issues does nothing when no security issues exist."""
        mockers = patch.multiple(
            "commit_hygiene",
            _check_commit_messages=MagicMock(return_value=["abc: fix"]),
            _check_sensitive_files=MagicMock(return_value=[]),
            _check_sensitive_files_on_disk=MagicMock(return_value=[]),
            _check_gitignore=MagicMock(return_value=[]),
            _check_large_files=MagicMock(return_value=[]),
        )
        with (
            mockers,
            patch("sys.argv", ["commit_hygiene.py", "--create-issues"]),
            patch.object(commit_hygiene, "create_security_issues") as mock_create,
        ):
            commit_hygiene.main()
            mock_create.assert_not_called()
=======
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
                [],
                0,
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
                [],
                0,
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
                [],
                0,
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

    def test_sqlite_committed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit abc123456789abcdef\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 20:55:21 2026 +0100\n\n"
            "    oops\n\n"
            "diff --git a/database.sqlite b/database.sqlite\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/database.sqlite\n"
            "@@ -0,0 +1 @@\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)

        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) >= 1
        assert any(f.severity == "security" for f in findings)
        assert any("database.sqlite" in f.message for f in findings)

    def test_sqlite3_committed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit def9876543210fed\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 21:00:00 2026 +0100\n\n"
            "    oops\n\n"
            "diff --git a/workout.sqlite3 b/workout.sqlite3\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/workout.sqlite3\n"
            "@@ -0,0 +1 @@\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)

        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) >= 1
        assert any("workout.sqlite3" in f.message for f in findings)

    def test_log_committed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        output = (
            "commit 1111222233334444\n"
            "Author: Test\n"
            "Date:   Wed Aug 5 21:30:00 2026 +0100\n\n"
            "    oops\n\n"
            "diff --git a/agent.log b/agent.log\n"
            "new file mode 100644\n"
            "index 0000000..d48b437\n"
            "--- /dev/null\n"
            "+++ b/agent.log\n"
            "@@ -0,0 +1 @@\n"
        )

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout=output)

        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        findings = commit_hygiene.check_sensitive_files()
        assert len(findings) >= 1
        assert any("agent.log" in f.message for f in findings)


# ---------------------------------------------------------------------------
# check_gitignore
# ---------------------------------------------------------------------------


class TestCheckGitignore:
    _BASE_PATTERNS = (
        "*.db\n*.db-wal\n*.db-shm\n*.db-journal\n"
        ".env\n__pycache__/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n"
        ".venv/\nvenv/\ndata/\n"
        "*.sqlite\n*.sqlite-wal\n*.sqlite-shm\n*.sqlite-journal\n"
        "*.sqlite3\n*.sqlite3-wal\n*.sqlite3-shm\n*.sqlite3-journal\n"
        "*.log\nagent.log\n"
    )

    def test_all_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("*.db\n.env\n__pycache__/\n.pytest_cache/\n.venv/\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert findings == []

    def test_missing_pycache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(self._BASE_PATTERNS.replace("__pycache__/\n", ""))
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "__pycache__/" in findings[0].message

    def test_missing_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(self._BASE_PATTERNS.replace("*.db\n", ""))
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert findings[0].message == ".gitignore missing entry: *.db"

    def test_missing_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(self._BASE_PATTERNS.replace(".env\n", ""))
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert ".env" in findings[0].message

    def test_missing_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text("# almost empty\n")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == len(commit_hygiene.REQUIRED_GITIGNORE)

    def test_gitignore_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        findings = commit_hygiene.check_gitignore()
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "missing" in findings[0].message.lower()


# ---------------------------------------------------------------------------
# check_large_files
# ---------------------------------------------------------------------------


class TestCheckLargeFiles:
    def test_no_large_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Create a small tracked file
        small_file = tmp_path / "small.py"
        small_file.write_text("hello")
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [],
                0,
                stdout="small.py\x00",
            )

        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(
            commit_hygiene,
            "MAX_FILE_SIZE",
            10 * 1024 * 1024,
        )
        findings = commit_hygiene.check_large_files()
        assert findings == []

    def test_large_file_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        large_file = tmp_path / "big.log"
        # Create a real large-ish file
        large_file.write_text("x" * 1000)
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [],
                0,
                stdout="big.log\x00",
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        large_file = data_dir / "big.csv"
        large_file.write_text("x" * 2000)
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                [],
                0,
                stdout="data/big.csv\x00",
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
    def test_adds_missing_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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

    def test_no_gitignore_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
    def test_removes_existing_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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
        gi.write_text(TestCheckGitignore._BASE_PATTERNS)
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
        gi.write_text(TestCheckGitignore._BASE_PATTERNS)
        monkeypatch.setattr(commit_hygiene, "ROOT", tmp_path)
        monkeypatch.setattr(commit_hygiene, "MAX_FILE_SIZE", 100 * 1024 * 1024)

        def _run(_args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, stdout="")

        monkeypatch.setattr(commit_hygiene, "_run_git", _run)
        monkeypatch.setattr(sys, "argv", ["commit_hygiene.py"])
        assert commit_hygiene.main() == 0

    def test_main_with_error_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        gi = tmp_path / ".gitignore"
        gi.write_text(TestCheckGitignore._BASE_PATTERNS)
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
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
        assert "*.sqlite" in content
        assert "*.sqlite3" in content
        assert "*.log" in content


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
>>>>>>> origin/main
