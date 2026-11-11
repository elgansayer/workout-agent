#!/usr/bin/env python3
"""Verify the security audit hash chain and summarise recent alerts.

Run by an operator (for example nightly) against the production database:

    python backend/scripts/verify_security_audit.py --db /data/workout_agent.db

Exit status: 0 every row verified, 1 tampering, damage or a row that cannot be
authenticated (see the flags below), 2 the database or key configuration is
unusable. The same ``AUDIT_LOG_HMAC_KEY`` / ``WEB_AUTH_SECRET`` environment as the
web service must be present so keyed digests can be recomputed. Output contains
only pseudonymous references, never credentials or raw addresses.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from security_audit import (
    AuditKey,
    ChainVerification,
    SecurityAuditConfigurationError,
    count_events,
    iter_alert_records,
    resolve_audit_key,
    verify_chain,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--db",
        default=os.environ.get("DATABASE_PATH", "workout_agent.db"),
        help="SQLite database path (default: $DATABASE_PATH or workout_agent.db)",
    )
    parser.add_argument("--alerts", type=int, default=10, help="recent alerts to list")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--require-keyed",
        action="store_true",
        help=(
            "fail when no key material is configured or any row is unkeyed, because "
            "an unkeyed chain can be rewritten by anyone who can write the database "
            "(use for deployments whose chain has always been keyed)"
        ),
    )
    parser.add_argument(
        "--allow-unverifiable",
        action="store_true",
        help=(
            "accept rows signed with a key that is not configured (link-checked "
            "only), for example after a deliberate key rotation; verify those rows "
            "separately by running with the earlier key"
        ),
    )
    return parser.parse_args(argv)


def _policy_failures(
    args: argparse.Namespace, key: AuditKey, result: ChainVerification
) -> list[str]:
    """Return reasons the run must not pass even though the chain links up."""

    failures = []
    if result.unverifiable and not args.allow_unverifiable:
        failures.append(
            f"{result.unverifiable} rows were signed with a key that is not "
            "configured; run with that key, or pass --allow-unverifiable after a "
            "deliberate rotation"
        )
    if args.require_keyed:
        if not key.keyed:
            failures.append(
                "key material is required: set AUDIT_LOG_HMAC_KEY or WEB_AUTH_SECRET"
            )
        if result.unkeyed:
            failures.append(
                f"{result.unkeyed} rows are unkeyed; a keyed chain must contain none"
            )
    return failures


def _render_text(
    result: ChainVerification,
    key_id: str,
    counts: dict[str, int],
    alerts: list[dict[str, object]],
    policy_failures: list[str],
) -> str:
    lines = []
    if not result.ok:
        lines.append(
            f"Security audit chain: FAILED at seq {result.failure_seq} "
            f"({result.failure_reason})"
        )
    else:
        label = "NOT FULLY AUTHENTICATED" if policy_failures else "OK"
        lines.append(
            f"Security audit chain: {label} ({result.checked} events, head seq "
            f"{result.head_seq}, head hash {result.head_hash})"
        )
    lines.extend(f"FAILED: {failure}" for failure in policy_failures)
    lines.append(f"Keyed digest: {'yes' if result.keyed else 'no'} (key id {key_id})")
    if result.unverifiable:
        lines.append(
            f"Rows signed with an unavailable key (link-checked only): {result.unverifiable}"
        )
    if result.unkeyed:
        lines.append(f"Unkeyed rows: {result.unkeyed}")
    lines.append("Events by type:")
    lines.extend(f"  {name}: {count}" for name, count in counts.items())
    lines.append("Recent alerts:" if alerts else "Recent alerts: none")
    lines.extend(
        f"  seq {item['seq']} {item['occurred_at']} {item['severity']} "
        f"{item['event_type']} {item['subject_type']}"
        for item in alerts
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not Path(args.db).expanduser().is_file():
        print(
            "Security audit verification failed: database does not exist.",
            file=sys.stderr,
        )
        return 2
    try:
        key = resolve_audit_key()
        result = verify_chain(args.db, key=key)
        counts = count_events(args.db)
        alerts = [
            {
                "seq": record.seq,
                "occurred_at": record.occurred_at,
                "severity": record.severity,
                "event_type": record.event_type,
                "subject_type": record.subject_type,
                "metadata": record.metadata,
            }
            for record in iter_alert_records(args.db, limit=args.alerts)
        ]
    except (SecurityAuditConfigurationError, sqlite3.Error, OSError) as exc:
        print(
            f"Security audit verification failed: {type(exc).__name__}.",
            file=sys.stderr,
        )
        return 2

    policy_failures = _policy_failures(args, key, result)
    passed = result.ok and not policy_failures
    if args.json:
        print(
            json.dumps(
                {
                    **result.as_dict(),
                    "key_id": key.key_id,
                    "passed": passed,
                    "policy_failures": policy_failures,
                    "events_by_type": counts,
                    "recent_alerts": alerts,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render_text(result, key.key_id, counts, alerts, policy_failures))

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
