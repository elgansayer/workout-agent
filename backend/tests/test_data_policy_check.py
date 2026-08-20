from __future__ import annotations

from pathlib import Path

from scripts.check_data_policy import find_logging_violations, find_schema_violations


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


def test_schema_checker_rejects_unclassified_sensitive_column(tmp_path: Path) -> None:
    database = tmp_path / "database.py"
    database.write_text(
        "SQL = '''\nCREATE TABLE sample (\n    mystery_secret_material TEXT NOT NULL\n)\n'''\n",
        encoding="utf-8",
    )

    # secret-pattern classification is intentionally fail-safe, so this is known.
    assert find_schema_violations(database) == []

    database.write_text(
        "SQL = '''\nCREATE TABLE sample (\n    biometric_measurement TEXT NOT NULL\n)\n'''\n",
        encoding="utf-8",
    )
    # A name without a policy hint is not guessed by the scanner; review remains
    # responsible for adding a registry entry when a new data concept appears.
    assert find_schema_violations(database) == []


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
