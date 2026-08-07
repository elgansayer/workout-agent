"""Tests for connector_health.py: daily connector health check."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from connector_health import (
    HealthReport,
    _check_env_docs,
    _check_failure_isolation,
    _check_per_user_credentials,
    build_parser,
    create_github_issues,
    main,
    report_health,
    run_health_check,
)

# ---------------------------------------------------------------------------
# HealthReport
# ---------------------------------------------------------------------------


class TestHealthReport:
    def test_empty_is_clean(self) -> None:
        r = HealthReport()
        assert r.is_clean

    def test_with_finding_is_not_clean(self) -> None:
        r = HealthReport()
        r.add("mod", "error", "cat", "msg")
        assert not r.is_clean

    def test_add_appends(self) -> None:
        r = HealthReport()
        r.add("a", "warn", "cat", "msga", line=10)
        r.add("b", "error", "cat2", "msgb")
        assert len(r.findings) == 2
        assert r.findings[0].module == "a"
        assert r.findings[0].severity == "warn"
        assert r.findings[0].line == 10


# ---------------------------------------------------------------------------
# _check_failure_isolation
# ---------------------------------------------------------------------------

WELL_PROTECTED_SOURCE = """
import requests
import logging

logger = logging.getLogger(__name__)

def fetch_something(api_key):
    try:
        r = requests.get("https://api.example.com", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.warning("fail: %s", e)
        return None
"""

UNPROTECTED_NETWORK_CALL = """
import requests

def bad_fetch():
    r = requests.get("https://oh.no", timeout=5)
    r.raise_for_status()
    return r.json()
"""

BARE_EXCEPT_SOURCE = """
import requests

def fetch():
    try:
        r = requests.get("https://x.com", timeout=5)
        return r.json()
    except:
        return None
"""


class TestCheckFailureIsolation:
    def test_well_protected_passes(self, tmp_path: Path) -> None:
        f = tmp_path / "well.py"
        f.write_text(WELL_PROTECTED_SOURCE)
        findings = _check_failure_isolation("well", f)
        assert len(findings) == 0

    def test_unprotected_network_call_is_found(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text(UNPROTECTED_NETWORK_CALL)
        findings = _check_failure_isolation("bad", f)
        assert len(findings) >= 1
        assert any("not wrapped in try/except" in finding.message for finding in findings)

    def test_bare_except_warns(self, tmp_path: Path) -> None:
        f = tmp_path / "bare.py"
        f.write_text(BARE_EXCEPT_SOURCE)
        findings = _check_failure_isolation("bare", f)
        # The network call IS in a try/except, but it's bare
        has_bare = any("Bare except" in finding.message for finding in findings)
        assert has_bare

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.py"
        f.write_text("")
        findings = _check_failure_isolation("empty", f)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# _check_env_docs
# ---------------------------------------------------------------------------


class TestCheckEnvDocs:
    def test_all_documented_is_clean(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When .env.example covers all connector vars, no findings."""
        import connector_health as ch

        # Create a temporary .env.example
        env_path = tmp_path / ".env.example"
        env_path.write_text("HEVY_API_KEY=\nTELEGRAM_BOT_TOKEN=\n")

        # Create a fake connector module
        conn_path = tmp_path / "hevy_client.py"
        conn_path.write_text(
            'import os\nkey = os.environ.get("HEVY_API_KEY", "")'
        )

        monkeypatch.setattr(ch, "ROOT", tmp_path)
        monkeypatch.setattr(
            ch,
            "CONNECTOR_MODULES",
            {"hevy_client": conn_path},
        )

        findings = _check_env_docs()
        assert len(findings) == 0

    def test_undocumented_var_is_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import connector_health as ch

        env_path = tmp_path / ".env.example"
        env_path.write_text("HEVY_API_KEY=\n")

        conn_path = tmp_path / "hevy_client.py"
        conn_path.write_text(
            'import os\n'
            'key = os.environ.get("HEVY_API_KEY", "")\n'
            'undoc = os.environ.get("UNDOCUMENTED_VAR", "")\n'
        )

        monkeypatch.setattr(ch, "ROOT", tmp_path)
        monkeypatch.setattr(
            ch,
            "CONNECTOR_MODULES",
            {"hevy_client": conn_path},
        )

        findings = _check_env_docs()
        assert any("UNDOCUMENTED_VAR" in f.message for f in findings)

    def test_missing_env_example(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import connector_health as ch

        monkeypatch.setattr(ch, "ROOT", tmp_path)
        findings = _check_env_docs()
        assert any(".env.example does not exist" in f.message for f in findings)


# ---------------------------------------------------------------------------
# _check_per_user_credentials
# ---------------------------------------------------------------------------


class TestCheckPerUserCredentials:
    def test_known_global_vars_produce_info(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import connector_health as ch

        conn_path = tmp_path / "hevy_client.py"
        conn_path.write_text(
            'import os\nkey = os.environ.get("HEVY_API_KEY", "")'
        )

        monkeypatch.setattr(
            ch,
            "CONNECTOR_MODULES",
            {"hevy_client": conn_path},
        )

        findings = _check_per_user_credentials()
        assert any("HEVY_API_KEY" in f.message for f in findings)
        assert all(f.severity == "info" for f in findings)


# ---------------------------------------------------------------------------
# run_health_check integration
# ---------------------------------------------------------------------------


class TestRunHealthCheck:
    def test_real_modules_are_clean(self) -> None:
        """The actual connector modules must pass the health check."""
        report = run_health_check()
        errors = [f for f in report.findings if f.severity == "error"]
        assert len(errors) == 0, (
            f"Health check found errors: {[e.message for e in errors]}"
        )


# ---------------------------------------------------------------------------
# report_health
# ---------------------------------------------------------------------------


class TestReportHealth:
    def test_clean_report_json(self, capsys: pytest.CaptureFixture) -> None:
        report = HealthReport()
        rc = report_health(report, json_output=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert rc == 0
        assert data["status"] == "clean"
        assert data["count"] == 0

    def test_findings_report_json(self, capsys: pytest.CaptureFixture) -> None:
        report = HealthReport()
        report.add("mod", "error", "cat", "broken", line=5)
        rc = report_health(report, json_output=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert rc == 1
        assert data["status"] == "findings"
        assert data["count"] == 1

    def test_clean_report_text(self) -> None:
        report = HealthReport()
        rc = report_health(report, json_output=False)
        assert rc == 0

    def test_findings_report_text(self) -> None:
        report = HealthReport()
        report.add("mod", "error", "cat", "broken")
        rc = report_health(report, json_output=False)
        assert rc == 1


# ---------------------------------------------------------------------------
# create_github_issues
# ---------------------------------------------------------------------------


class TestCreateGitHubIssues:
    def test_no_token_returns_empty(self) -> None:
        report = HealthReport()
        report.add("m", "error", "cat", "msg")
        with patch("connector_health._get_github_token", return_value=None):
            issues = create_github_issues(report)
            assert issues == []

    def test_clean_report_skips(self) -> None:
        report = HealthReport()
        issues = create_github_issues(report)
        assert issues == []

    def test_handles_http_error_gracefully(self) -> None:
        report = HealthReport()
        report.add("mod", "error", "failure-isolation", "bad thing")
        with patch("connector_health._get_github_token", return_value="tk"), \
             patch("connector_health._get_github_repo", return_value=("org", "repo")), \
             patch("connector_health.logger") as mock_logger, \
             patch("urllib.request.urlopen", side_effect=OSError("network down")):
            issues = create_github_issues(report)
            assert issues == []
            # Should have logged a warning about the failure
            assert mock_logger.warning.called


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_build_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--json"])
        assert args.json

    def test_main_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import connector_health as ch

        monkeypatch.setattr("sys.argv", ["connector_health.py"])
        # Mock run_health_check to return clean
        monkeypatch.setattr(ch, "run_health_check", lambda: HealthReport())
        rc = main()
        assert rc == 0

    def test_main_with_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import connector_health as ch

        monkeypatch.setattr("sys.argv", ["connector_health.py"])
        report = HealthReport()
        report.add("m", "error", "cat", "msg")
        monkeypatch.setattr(ch, "run_health_check", lambda: report)
        rc = main()
        assert rc == 1