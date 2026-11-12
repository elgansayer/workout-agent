from __future__ import annotations

from pathlib import Path

from scripts.check_data_policy import (
    find_logging_setup_violations,
    find_logging_violations,
    find_public_data_violations,
    find_schema_violations,
)

BACKEND = Path(__file__).resolve().parents[1]


def test_logging_checker_rejects_direct_secret_values(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.py"
    source.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(refresh_token: str) -> None:\n"
        "    logger.info('refresh=%s', refresh_token)\n",
        encoding="utf-8",
    )

    findings = find_logging_violations(source)

    assert len(findings) == 1
    assert "refresh_token" in findings[0].message


def test_logging_checker_accepts_central_redaction(tmp_path: Path) -> None:
    source = tmp_path / "safe.py"
    source.write_text(
        "import logging\n"
        "from data_classification import safe_log_value\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(refresh_token: str) -> None:\n"
        "    logger.info('refresh=%s', safe_log_value('refresh_token', refresh_token))\n",
        encoding="utf-8",
    )

    assert find_logging_violations(source) == []


def test_logging_checker_rejects_prompt_payloads(tmp_path: Path) -> None:
    source = tmp_path / "unsafe_prompt.py"
    source.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(user_prompt: str) -> None:\n"
        "    logger.info('asked: %s', user_prompt)\n",
        encoding="utf-8",
    )

    findings = find_logging_violations(source)

    assert len(findings) == 1
    assert "user_prompt" in findings[0].message


def test_logging_checker_rejects_raw_http_response_bodies(tmp_path: Path) -> None:
    source = tmp_path / "unsafe_body.py"
    source.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(response, exc) -> None:\n"
        "    logger.error('reply: %s', response.text)\n"
        "    logger.error('reply: %s', exc.response.json())\n"
        "    logger.error('status: %s', response.status_code)\n",
        encoding="utf-8",
    )

    findings = find_logging_violations(source)

    assert [finding.line for finding in findings] == [4, 5]
    assert all("raw HTTP response body" in finding.message for finding in findings)


def test_logging_checker_accepts_central_redaction_of_bodies(tmp_path: Path) -> None:
    source = tmp_path / "safe_body.py"
    source.write_text(
        "import logging\n"
        "from log_redaction import redact_text\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(response) -> None:\n"
        "    logger.error('reply: %s', redact_text(response.text))\n",
        encoding="utf-8",
    )

    assert find_logging_violations(source) == []


def test_logging_setup_must_install_log_redaction(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe_entrypoint.py"
    unsafe.write_text(
        "import logging\nlogging.basicConfig(level=logging.INFO)\n", encoding="utf-8"
    )
    safe = tmp_path / "safe_entrypoint.py"
    safe.write_text(
        "import logging\n"
        "from log_redaction import install_log_redaction\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "install_log_redaction()\n",
        encoding="utf-8",
    )
    dict_config = tmp_path / "dict_config.py"
    dict_config.write_text(
        "import logging.config\nlogging.config.dictConfig({'version': 1})\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text(
        "class Tool:\n    def basicConfig(self): ...\nTool().basicConfig()\n",
        encoding="utf-8",
    )

    assert [f.line for f in find_logging_setup_violations(unsafe)] == [2]
    assert "install_log_redaction" in find_logging_setup_violations(unsafe)[0].message
    assert find_logging_setup_violations(safe) == []
    assert [f.line for f in find_logging_setup_violations(dict_config)] == [2]
    assert find_logging_setup_violations(unrelated) == []


def test_every_production_entrypoint_that_configures_logging_installs_redaction(
    tmp_path: Path,
) -> None:
    modules = [
        path
        for path in sorted(BACKEND.rglob("*.py"))
        if "tests" not in path.relative_to(BACKEND).parts
        and ".venv" not in path.parts
        and "logging.basicConfig(" in path.read_text(encoding="utf-8")
    ]
    names = {path.name for path in modules}
    assert {"main.py", "scheduler.py", "insight_cron.py", "sync_history.py"} <= names

    for path in modules:
        assert find_logging_setup_violations(path) == [], path

        # The detector must see the real file shape: removing the install call
        # from an actual entry point has to be caught.
        stripped = tmp_path / path.name
        stripped.write_text(
            path.read_text(encoding="utf-8").replace("install_log_redaction()", "pass"),
            encoding="utf-8",
        )
        assert find_logging_setup_violations(stripped), path


def test_schema_checker_rejects_unclassified_sensitive_column(tmp_path: Path) -> None:
    database = tmp_path / "database.py"
    database.write_text(
        "SQL = '''\nCREATE TABLE sample (\n    raw_payload TEXT NOT NULL\n)\n'''\n",
        encoding="utf-8",
    )

    findings = find_schema_violations(database)

    assert len(findings) == 1
    assert "raw_payload" in findings[0].message


def test_schema_checker_accepts_current_sensitive_field_patterns(tmp_path: Path) -> None:
    database = tmp_path / "database.py"
    database.write_text(
        "SQL = '''\nCREATE TABLE sample (\n"
        "    user_id TEXT NOT NULL,\n"
        "    api_key TEXT NOT NULL,\n"
        "    hevy_payload TEXT NOT NULL,\n"
        "    weight_kg REAL\n"
        ")\n'''\n",
        encoding="utf-8",
    )

    assert find_schema_violations(database) == []


def test_public_checker_rejects_named_real_user_profile(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text(
        "The coach must respect these facts about the athlete (Alice): joint pain and a caloric deficit.\n",
        encoding="utf-8",
    )

    findings = find_public_data_violations(source)

    assert any("anonymous synthetic profile" in finding.message for finding in findings)


def test_public_checker_rejects_real_email_address_in_sensitive_fixture(tmp_path: Path) -> None:
    source = tmp_path / "fixture.json"
    source.write_text('{"email": "person@company.co.uk"}\n', encoding="utf-8")

    findings = find_public_data_violations(source, require_synthetic_marker=True)

    assert len(findings) == 1
    assert "non-example email" in findings[0].message


def test_public_checker_accepts_reserved_example_email(tmp_path: Path) -> None:
    source = tmp_path / "fixture.json"
    source.write_text('{"email": "athlete@example.com"}\n', encoding="utf-8")

    assert find_public_data_violations(source, require_synthetic_marker=True) == []


def test_public_checker_allows_project_email_in_non_sensitive_docs(tmp_path: Path) -> None:
    source = tmp_path / "SUPPORT.md"
    source.write_text("Contact support@workout-agent.dev for deployment help.\n", encoding="utf-8")

    assert find_public_data_violations(source) == []


def test_public_checker_requires_first_person_and_sensitive_term_to_be_nearby(tmp_path: Path) -> None:
    source = tmp_path / "GUIDE.md"
    source.write_text(
        "My preferred deployment uses Docker.\n"
        + ("Architecture detail. " * 30)
        + "Medical data is classified as sensitive and must be redacted.\n",
        encoding="utf-8",
    )

    assert find_public_data_violations(source) == []


def test_sensitive_fixture_requires_explicit_synthetic_marker(tmp_path: Path) -> None:
    source = tmp_path / "health-fixture.json"
    source.write_text('{"weight_kg": 75.0, "resting_hr": 60}\n', encoding="utf-8")

    findings = find_public_data_violations(source, require_synthetic_marker=True)

    assert len(findings) == 1
    assert "synthetic-profile marker" in findings[0].message


def test_sensitive_fixture_accepts_explicit_synthetic_marker(tmp_path: Path) -> None:
    source = tmp_path / "health-fixture.json"
    source.write_text(
        '{"synthetic_profile": true, "weight_kg": 75.0, "resting_hr": 60}\n',
        encoding="utf-8",
    )

    assert find_public_data_violations(source, require_synthetic_marker=True) == []
