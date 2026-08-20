#!/usr/bin/env python3
"""Supply-chain policy helpers used by CI.

The module deliberately uses only the Python standard library so exception
validation and provenance generation do not depend on packages being installed
from the network before the security checks run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

MAX_EXCEPTION_DAYS = 90
_METADATA_RE = re.compile(
    r"^# owner=(?P<owner>[^;]+); tracking=(?P<tracking>[^;]+); reason=(?P<reason>.+)$"
)
_EXCEPTION_RE = re.compile(r"^(?P<finding>\S+)\s+exp:(?P<expires>\d{4}-\d{2}-\d{2})$")

MATERIAL_PATTERNS = (
    "backend/requirements*.txt",
    "frontend/package.json",
    "frontend/package-lock.json",
    "Dockerfile*",
    "frontend/Dockerfile",
    "docker-compose*.yml",
    ".github/workflows/supply-chain-security.yml",
    ".trivyignore",
    "tools/supply_chain.py",
    "tools/test_supply_chain.py",
)


def _iter_materials(root: Path, patterns: Iterable[str] = MATERIAL_PATTERNS) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        for candidate in root.glob(pattern):
            if candidate.is_file():
                paths.add(candidate)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_trivy_ignore(
    path: Path,
    *,
    today: date | None = None,
    max_exception_days: int = MAX_EXCEPTION_DAYS,
) -> list[str]:
    """Validate that every Trivy suppression is documented and expires soon."""

    today = today or datetime.now(timezone.utc).date()
    errors: list[str] = []
    seen: set[str] = set()
    pending_metadata: tuple[str, str, str] | None = None

    if not path.exists():
        return [f"{path}: missing exception file"]

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        metadata_match = _METADATA_RE.match(line)
        if metadata_match:
            pending_metadata = (
                metadata_match.group("owner").strip(),
                metadata_match.group("tracking").strip(),
                metadata_match.group("reason").strip(),
            )
            if not all(pending_metadata):
                errors.append(f"{path}:{line_number}: exception metadata cannot be empty")
            continue

        if line.startswith("#"):
            continue

        match = _EXCEPTION_RE.match(line)
        if not match:
            errors.append(
                f"{path}:{line_number}: use '<finding-id> exp:YYYY-MM-DD'; permanent suppressions are forbidden"
            )
            pending_metadata = None
            continue

        finding = match.group("finding")
        if finding in seen:
            errors.append(f"{path}:{line_number}: duplicate exception for {finding}")
        seen.add(finding)

        if pending_metadata is None:
            errors.append(
                f"{path}:{line_number}: exception {finding} needs a preceding metadata comment"
            )
        else:
            owner, tracking, reason = pending_metadata
            if not owner.startswith("@"):
                errors.append(
                    f"{path}:{line_number}: owner must be a GitHub handle beginning with '@'"
                )
            if not (tracking.startswith("#") or tracking.startswith("https://")):
                errors.append(
                    f"{path}:{line_number}: tracking must be an issue/PR number or https URL"
                )
            if len(reason) < 12:
                errors.append(
                    f"{path}:{line_number}: reason must explain the risk acceptance"
                )

        try:
            expires = date.fromisoformat(match.group("expires"))
        except ValueError:
            errors.append(f"{path}:{line_number}: invalid expiry date")
            pending_metadata = None
            continue

        if expires < today:
            errors.append(f"{path}:{line_number}: exception {finding} expired on {expires}")
        days = (expires - today).days
        if days > max_exception_days:
            errors.append(
                f"{path}:{line_number}: exception {finding} lasts {days} days; maximum is {max_exception_days}"
            )

        pending_metadata = None

    if pending_metadata is not None:
        errors.append(f"{path}: trailing exception metadata is not followed by a finding")

    return errors


def build_provenance(
    root: Path,
    *,
    commit_sha: str,
    repository: str,
    source_ref: str,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Build a stable manifest tying scan artifacts to source and dependency inputs."""

    if not commit_sha or commit_sha == "unknown":
        raise ValueError("commit_sha is required for traceable supply-chain evidence")
    if not repository:
        raise ValueError("repository is required for traceable supply-chain evidence")

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    materials = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
        for path in _iter_materials(root)
    ]

    return {
        "schema_version": 1,
        "repository": repository,
        "commit_sha": commit_sha,
        "source_ref": source_ref,
        "generated_at": generated_at,
        "materials": materials,
    }


def write_provenance(
    output: Path,
    root: Path,
    *,
    commit_sha: str,
    repository: str,
    source_ref: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_provenance(
        root,
        commit_sha=commit_sha,
        repository=repository,
        source_ref=source_ref,
    )
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _validate_command(args: argparse.Namespace) -> int:
    errors = validate_trivy_ignore(Path(args.file))
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f"Validated time-bounded supply-chain exceptions in {args.file}")
    return 0


def _provenance_command(args: argparse.Namespace) -> int:
    write_provenance(
        Path(args.output),
        Path(args.root).resolve(),
        commit_sha=args.commit_sha,
        repository=args.repository,
        source_ref=args.source_ref,
    )
    print(f"Wrote supply-chain provenance to {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-exceptions")
    validate.add_argument("--file", default=".trivyignore")
    validate.set_defaults(func=_validate_command)

    provenance = subparsers.add_parser("provenance")
    provenance.add_argument("--root", default=".")
    provenance.add_argument("--output", required=True)
    provenance.add_argument("--commit-sha", default=os.environ.get("GITHUB_SHA", ""))
    provenance.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    provenance.add_argument("--source-ref", default=os.environ.get("GITHUB_REF", ""))
    provenance.set_defaults(func=_provenance_command)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
