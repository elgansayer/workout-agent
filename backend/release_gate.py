from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_CHECK_CONCLUSIONS = {"success"}


@dataclass(frozen=True)
class GateFinding:
    field: str
    message: str


def _required_text(payload: dict[str, Any], key: str, findings: list[GateFinding]) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        findings.append(GateFinding(key, "must be a non-empty string"))
        return ""
    return value.strip()


def validate_release_evidence(payload: dict[str, Any]) -> list[GateFinding]:
    """Validate immutable evidence required before production promotion.

    ``head_sha`` is the exact current ``main`` commit being deployed. The
    independently approved pull-request head is recorded separately as
    ``reviewed_head_sha`` because merge/squash commits legitimately have a
    different SHA from the reviewed branch head.
    """
    findings: list[GateFinding] = []

    if payload.get("schema_version") != 1:
        findings.append(GateFinding("schema_version", "must equal 1"))

    head_sha = _required_text(payload, "head_sha", findings)
    reviewed_head_sha = _required_text(payload, "reviewed_head_sha", findings)
    if head_sha and not _SHA_RE.fullmatch(head_sha):
        findings.append(GateFinding("head_sha", "must be a 40-character lowercase Git SHA"))
    if reviewed_head_sha and not _SHA_RE.fullmatch(reviewed_head_sha):
        findings.append(
            GateFinding("reviewed_head_sha", "must be a 40-character lowercase Git SHA")
        )

    author = _required_text(payload, "change_author", findings)
    reviewer = _required_text(payload, "approved_by", findings)
    operator = _required_text(payload, "operator", findings)
    if author and reviewer and author.casefold() == reviewer.casefold():
        findings.append(GateFinding("approved_by", "must differ from the change author"))
    if author and operator and author.casefold() == operator.casefold():
        findings.append(GateFinding("operator", "must differ from the change author"))

    _required_text(payload, "config_version", findings)
    _required_text(payload, "migration_plan", findings)
    _required_text(payload, "database_migration", findings)
    _required_text(payload, "rollback_command", findings)
    _required_text(payload, "security_review", findings)
    _required_text(payload, "smoke_test_plan", findings)
    _required_text(payload, "smoke_test_evidence", findings)
    _required_text(payload, "verification_results", findings)

    if payload.get("backup_status") != "verified":
        findings.append(GateFinding("backup_status", "must equal 'verified'"))

    if payload.get("smoke_test_status") != "passed":
        findings.append(GateFinding("smoke_test_status", "must equal 'passed'"))

    image_digests = payload.get("image_digests")
    if not isinstance(image_digests, dict) or not image_digests:
        findings.append(GateFinding("image_digests", "must contain immutable image digests"))
    else:
        for image_name in ("web", "agent"):
            digest = image_digests.get(image_name)
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                findings.append(
                    GateFinding(
                        f"image_digests.{image_name}",
                        "must be an immutable sha256:<64 lowercase hex> digest",
                    )
                )

    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        findings.append(GateFinding("checks", "must contain at least one deterministic check"))
    else:
        seen_names: set[str] = set()
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                findings.append(GateFinding(f"checks[{index}]", "must be an object"))
                continue
            name = check.get("name")
            conclusion = check.get("conclusion")
            if not isinstance(name, str) or not name.strip():
                findings.append(GateFinding(f"checks[{index}].name", "must be non-empty"))
            else:
                normalized_name = name.strip().casefold()
                if normalized_name in seen_names:
                    findings.append(GateFinding(f"checks[{index}].name", "must be unique"))
                seen_names.add(normalized_name)
            if conclusion not in _ALLOWED_CHECK_CONCLUSIONS:
                findings.append(
                    GateFinding(
                        f"checks[{index}].conclusion",
                        "must be success; neutral, skipped, pending and failed checks do not pass a production gate",
                    )
                )

    if payload.get("head_on_main") is not True:
        findings.append(GateFinding("head_on_main", "must be true"))

    if payload.get("pr_merged") is not True:
        findings.append(GateFinding("pr_merged", "must be true"))

    return findings


def _load_payload(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("release evidence must be a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate production release evidence")
    parser.add_argument("evidence", type=Path, help="Path to release evidence JSON")
    args = parser.parse_args()

    try:
        payload = _load_payload(args.evidence)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"release gate failed: {exc}")
        return 2

    findings = validate_release_evidence(payload)
    if findings:
        print("production release gate rejected the evidence:")
        for finding in findings:
            print(f"- {finding.field}: {finding.message}")
        return 1

    print("production release gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
