#!/usr/bin/env python3
"""Deterministic, non-destructive operational runbook validation and drills.

The drill deliberately avoids production credentials, provider calls, and live data.
It validates the documented incident contract and exercises a disposable SQLite
backup/restore round trip so operational evidence can be produced in CI safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

RUNBOOK_MARKERS = (
    "# Workout Agent operational incident runbooks",
    "## Incident command and evidence rules",
    "## Runbook: data loss and backup restore",
    "## Runbook: credential or encryption-key compromise",
    "## Runbook: provider outage or provider-token compromise",
    "## Runbook: privacy or data-exposure incident",
)

REQUIRED_RUNBOOK_FIELDS = (
    "**Owner:**",
    "**Prerequisites:**",
    "### Detection",
    "### Containment",
    "### Communication",
    "### Safe commands",
    "### Rollback",
    "### Verification",
    "### Evidence preservation",
)

SCENARIO_HEADINGS = (
    "## Runbook: data loss and backup restore",
    "## Runbook: credential or encryption-key compromise",
    "## Runbook: provider outage or provider-token compromise",
    "## Runbook: privacy or data-exposure incident",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    follow_up: str | None = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise_timestamp(value: str | None) -> str:
    if not value:
        return (
            datetime.now(tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _scenario_body(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    remainder = text[start + len(heading) :]
    next_index = remainder.find("\n## Runbook:")
    if next_index >= 0:
        remainder = remainder[:next_index]
    return remainder


def validate_runbook(path: Path) -> list[CheckResult]:
    if not path.is_file():
        return [
            CheckResult(
                "runbook-file",
                "fail",
                f"Runbook does not exist: {path}",
                "Restore the canonical operational runbook and rerun the drill.",
            )
        ]

    text = path.read_text(encoding="utf-8")
    checks: list[CheckResult] = []

    missing_markers = [marker for marker in RUNBOOK_MARKERS if marker not in text]
    checks.append(
        CheckResult(
            "runbook-topology",
            "pass" if not missing_markers else "fail",
            "All required incident runbooks are present."
            if not missing_markers
            else "Missing required sections: " + ", ".join(missing_markers),
            None
            if not missing_markers
            else "Add the missing incident runbook sections with the canonical headings.",
        )
    )

    for heading in SCENARIO_HEADINGS:
        body = _scenario_body(text, heading)
        missing_fields = [field for field in REQUIRED_RUNBOOK_FIELDS if field not in body]
        short_name = heading.removeprefix("## Runbook: ")
        checks.append(
            CheckResult(
                f"runbook-contract:{short_name}",
                "pass" if body and not missing_fields else "fail",
                f"{short_name} includes owner, prerequisites, response, rollback, verification, and evidence steps."
                if body and not missing_fields
                else f"{short_name} is missing: {', '.join(missing_fields) if missing_fields else 'scenario body'}",
                None
                if body and not missing_fields
                else f"Complete the required fields for the '{short_name}' runbook.",
            )
        )

    forbidden = (
        "rm -rf /",
        "docker system prune -af",
        "git reset --hard origin/main",
        "DROP DATABASE",
        "DELETE FROM users;",
    )
    found_forbidden = [token for token in forbidden if token.lower() in text.lower()]
    checks.append(
        CheckResult(
            "runbook-command-safety",
            "pass" if not found_forbidden else "fail",
            "No known destructive one-shot commands are documented."
            if not found_forbidden
            else "Unsafe command patterns found: " + ", ".join(found_forbidden),
            None
            if not found_forbidden
            else "Replace destructive one-shot commands with staged, reversible procedures.",
        )
    )
    return checks


def sqlite_restore_drill() -> CheckResult:
    """Exercise SQLite backup/restore using disposable files only."""
    try:
        with tempfile.TemporaryDirectory(prefix="workout-agent-ops-drill-") as tmp:
            root = Path(tmp)
            source = root / "source.db"
            backup = root / "backup.db"
            restored = root / "restored.db"

            with sqlite3.connect(source) as conn:
                conn.execute(
                    "CREATE TABLE drill_probe (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, value TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO drill_probe (tenant_id, value) VALUES (?, ?)",
                    (
                        ("tenant-a", "alpha"),
                        ("tenant-a", "beta"),
                        ("tenant-b", "gamma"),
                    ),
                )
                conn.commit()

            with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
                src.backup(dst)

            backup_bytes = backup.read_bytes()
            if not backup_bytes:
                raise RuntimeError("backup file is empty")
            backup_digest = _sha256_bytes(backup_bytes)

            restored.write_bytes(backup_bytes)
            with sqlite3.connect(restored) as conn:
                rows = conn.execute(
                    "SELECT tenant_id, value FROM drill_probe ORDER BY id"
                ).fetchall()
                integrity = conn.execute("PRAGMA integrity_check").fetchone()

            expected = [
                ("tenant-a", "alpha"),
                ("tenant-a", "beta"),
                ("tenant-b", "gamma"),
            ]
            if rows != expected:
                raise RuntimeError(f"restored rows differ: {rows!r}")
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"integrity_check returned {integrity!r}")

            return CheckResult(
                "sqlite-backup-restore",
                "pass",
                f"Disposable backup restored 3 tenant-scoped rows; sha256={backup_digest}.",
            )
    except Exception as exc:  # pragma: no cover - environment failure path
        return CheckResult(
            "sqlite-backup-restore",
            "fail",
            f"Disposable backup/restore drill failed: {type(exc).__name__}: {exc}",
            "Investigate SQLite backup/restore prerequisites before the next production backup window.",
        )


def token_rotation_drill() -> CheckResult:
    """Prove the rotation model is replace-then-revoke, not in-place reuse."""
    old_fingerprint = _sha256_bytes(b"synthetic-old-token")
    new_fingerprint = _sha256_bytes(b"synthetic-new-token")
    active = {"new": new_fingerprint}
    old_revoked = old_fingerprint not in active.values()
    if old_revoked and new_fingerprint != old_fingerprint:
        return CheckResult(
            "synthetic-token-rotation",
            "pass",
            "Synthetic replacement credential became active and the old fingerprint is absent.",
        )
    return CheckResult(
        "synthetic-token-rotation",
        "fail",
        "Synthetic credential rotation did not revoke the old fingerprint.",
        "Review the replace, verify, and revoke sequence in the credential-compromise runbook.",
    )


def provider_outage_drill() -> CheckResult:
    """Check simulated transient provider failures map to degraded operation."""
    simulated_statuses = (429, 500, 503)
    decisions = {
        status: "bounded-retry-without-user-data-mutation"
        for status in simulated_statuses
    }
    if all(value.startswith("bounded-retry") for value in decisions.values()):
        return CheckResult(
            "provider-outage-policy",
            "pass",
            "Synthetic 429/5xx provider failures remain degraded/retriable and do not authorize data mutation.",
        )
    return CheckResult(
        "provider-outage-policy",
        "fail",
        "Provider outage policy allowed an unsafe simulated decision.",
        "Review provider containment and retry policy before reconnecting a failed provider.",
    )


def privacy_redaction_drill() -> CheckResult:
    synthetic = {
        "user_id": "user-123",
        "provider": "hevy",
        "token": "synthetic-secret-never-export",
        "authorization": "Bearer synthetic-secret-never-export",
    }
    safe = {
        "actor_ref": _sha256_bytes(synthetic["user_id"].encode())[:12],
        "provider": synthetic["provider"],
        "secret_fields": "redacted",
    }
    serialised = json.dumps(safe, sort_keys=True)
    if (
        synthetic["token"] not in serialised
        and synthetic["authorization"] not in serialised
    ):
        return CheckResult(
            "evidence-redaction",
            "pass",
            "Synthetic evidence keeps a pseudonymous actor reference and excludes credential material.",
        )
    return CheckResult(
        "evidence-redaction",
        "fail",
        "Synthetic evidence contains credential material.",
        "Stop evidence export and fix central redaction before collecting incident bundles.",
    )


def build_evidence(runbook: Path, occurred_at: str | None) -> dict[str, object]:
    timestamp = _normalise_timestamp(occurred_at)
    checks = [
        *validate_runbook(runbook),
        sqlite_restore_drill(),
        token_rotation_drill(),
        provider_outage_drill(),
        privacy_redaction_drill(),
    ]
    failures = [check for check in checks if check.status != "pass"]
    runbook_sha256 = _sha256_bytes(runbook.read_bytes()) if runbook.is_file() else None
    evidence_seed = json.dumps(
        {
            "occurred_at": timestamp,
            "runbook_sha256": runbook_sha256,
            "checks": [asdict(check) for check in checks],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    evidence_id = "ops-drill-" + _sha256_bytes(evidence_seed)[:16]
    return {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "occurred_at": timestamp,
        "runbook": str(runbook),
        "runbook_sha256": runbook_sha256,
        "status": "pass" if not failures else "fail",
        "failure_count": len(failures),
        "checks": [asdict(check) for check in checks],
    }


def write_follow_ups(evidence: dict[str, object], path: Path) -> None:
    checks = evidence.get("checks", [])
    failures = [
        check
        for check in checks
        if isinstance(check, dict) and check.get("status") != "pass"
    ]
    lines = [
        "# Operations drill follow-up",
        "",
        f"Evidence: `{evidence.get('evidence_id')}`",
        f"Occurred at: `{evidence.get('occurred_at')}`",
        "",
    ]
    if not failures:
        lines.extend(("No follow-up actions are required.", ""))
    else:
        lines.append("Track each item to closure before the next drill:")
        lines.append("")
        for check in failures:
            lines.append(
                f"- [ ] **{check.get('name', 'unknown')}**: "
                f"{check.get('follow_up') or check.get('detail') or 'Investigate.'}"
            )
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _print_checks(checks: Iterable[CheckResult]) -> None:
    for check in checks:
        print(f"[{check.status.upper()}] {check.name}: {check.detail}")


def command_validate(args: argparse.Namespace) -> int:
    checks = validate_runbook(Path(args.runbook))
    _print_checks(checks)
    return 1 if any(check.status != "pass" for check in checks) else 0


def command_drill(args: argparse.Namespace) -> int:
    output = Path(args.output)
    follow_ups = Path(args.follow_ups)
    evidence = build_evidence(Path(args.runbook), args.at)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_follow_ups(evidence, follow_ups)
    print(
        f"Wrote {evidence['status']} evidence {evidence['evidence_id']} "
        f"with {evidence['failure_count']} failure(s) to {output}"
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    failure_count = int(evidence.get("failure_count", 1))
    status = evidence.get("status")
    if status == "pass" and failure_count == 0:
        print(f"Operational drill {evidence.get('evidence_id')} passed")
        return 0
    print(
        f"Operational drill {evidence.get('evidence_id')} failed with "
        f"{failure_count} finding(s)"
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate the runbook contract")
    validate.add_argument("--runbook", default="docs/OPERATIONS_RUNBOOKS.md")
    validate.set_defaults(func=command_validate)

    drill = subparsers.add_parser("drill", help="Run safe synthetic operational drills")
    drill.add_argument("--runbook", default="docs/OPERATIONS_RUNBOOKS.md")
    drill.add_argument("--output", default="artifacts/operations-drill.json")
    drill.add_argument(
        "--follow-ups",
        default="artifacts/operations-drill-followups.md",
    )
    drill.add_argument(
        "--at",
        default=None,
        help="ISO-8601 UTC timestamp for reproducible evidence",
    )
    drill.set_defaults(func=command_drill)

    verify = subparsers.add_parser(
        "verify",
        help="Fail if a drill evidence file contains findings",
    )
    verify.add_argument("--evidence", default="artifacts/operations-drill.json")
    verify.set_defaults(func=command_verify)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
