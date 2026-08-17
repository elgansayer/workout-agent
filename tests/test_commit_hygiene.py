"""Tests for commit_hygiene.py — the hourly commit hygiene sweep."""

from __future__ import annotations

import json
import subprocess
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
