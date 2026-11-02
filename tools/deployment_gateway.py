"""Authenticated backup-before-Arcane production deployment gateway."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from tools.backup_restore import BackupError, create_backup, load_keyring, verify_backup

DEPLOY_PATH = "/v1/deployments/workout-agent"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^ghcr\.io/[a-z0-9_.-]+/workout-agent(?:-web)?@sha256:[0-9a-f]{64}$"
)
MAX_REQUEST_BYTES = 16_384
MAX_ARCANE_RESPONSE_BYTES = 65_536


class Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class DeploymentError(RuntimeError):
    """A deployment could not cross the fail-closed host boundary."""


@dataclass(frozen=True)
class GatewayConfig:
    database: Path
    backup_directory: Path
    record_directory: Path
    arcane_webhook_url: str
    arcane_bearer_token: str | None = None
    arcane_timeout_seconds: float = 90.0


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validated_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"commit_sha", "images"}:
        raise DeploymentError("request must contain only commit_sha and images")
    commit_sha, images = payload.get("commit_sha"), payload.get("images")
    if not isinstance(commit_sha, str) or not SHA_RE.fullmatch(commit_sha):
        raise DeploymentError("commit_sha must be a full lowercase commit SHA")
    if not isinstance(images, dict) or set(images) != {"agent", "web"}:
        raise DeploymentError("images must contain exactly agent and web")
    for name, image in images.items():
        if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
            raise DeploymentError(
                f"{name} must be an immutable Workout Agent GHCR digest"
            )
    return {"commit_sha": commit_sha, "images": images}


def _validate_arcane_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DeploymentError("ARCANE_WEBHOOK_URL must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise DeploymentError(
            "unencrypted Arcane webhooks are permitted only on loopback"
        )


def _read_bounded(response: Readable) -> bytes:
    body = response.read(MAX_ARCANE_RESPONSE_BYTES + 1)
    if len(body) > MAX_ARCANE_RESPONSE_BYTES:
        raise DeploymentError("Arcane response exceeded the evidence size limit")
    return body


def deploy(
    config: GatewayConfig, payload: object, *, opener: Callable[..., Any] = urlopen
) -> dict[str, Any]:
    """Create and verify a backup, then synchronously forward to Arcane."""
    request_payload = _validated_request(payload)
    _validate_arcane_url(config.arcane_webhook_url)
    deployment_id = "deploy-" + secrets.token_hex(12)
    requested_at = _utc_now()
    backup_path = (
        config.backup_directory / f"{requested_at.replace(':', '')}-{deployment_id}.wab"
    )
    record_path = config.record_directory / f"{deployment_id}.json"
    record: dict[str, Any] = {
        "schema_version": 1,
        "deployment_id": deployment_id,
        "requested_at": requested_at,
        "commit_sha": request_payload["commit_sha"],
        "images": request_payload["images"],
        "status": "backup_pending",
    }
    _atomic_json(record_path, record)
    try:
        if not config.database.is_file():
            raise DeploymentError("configured production database does not exist")
        keyring = load_keyring()
        manifest = create_backup(config.database, backup_path, keyring)
        verified = verify_backup(backup_path, keyring)
        if verified != manifest or verified.get("integrity_check") != "ok":
            raise DeploymentError(
                "backup verification did not reproduce a valid manifest"
            )
        backup_evidence = {
            key: verified[key]
            for key in (
                "created_at",
                "key_id",
                "database_sha256",
                "database_size_bytes",
                "schema_sha256",
                "integrity_check",
                "foreign_key_violations",
            )
        }
        backup_evidence["backup_id"] = backup_path.name
        record.update(status="backup_verified", backup=backup_evidence)
        _atomic_json(record_path, record)

        body = json.dumps(request_payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if config.arcane_bearer_token:
            headers["Authorization"] = f"Bearer {config.arcane_bearer_token}"
        request = Request(
            config.arcane_webhook_url, data=body, headers=headers, method="POST"
        )
        with opener(request, timeout=config.arcane_timeout_seconds) as response:
            response_body = _read_bounded(response)
            response_status = int(response.status)
            content_type = response.headers.get("Content-Type", "")
        if response_status != HTTPStatus.ACCEPTED:
            raise DeploymentError(
                f"Arcane returned HTTP {response_status}; expected HTTP 202"
            )
        response_sha256 = hashlib.sha256(response_body).hexdigest()
        arcane_reference: str | None = None
        try:
            decoded = json.loads(response_body)
            candidate = (
                decoded.get("deployment_id") or decoded.get("id")
                if isinstance(decoded, dict)
                else None
            )
            if isinstance(candidate, (str, int)):
                arcane_reference = str(candidate)[:256]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        record.update(
            status="arcane_accepted",
            completed_at=_utc_now(),
            arcane={
                "http_status": response_status,
                "content_type": content_type[:256],
                "response_sha256": response_sha256,
                "response_size_bytes": len(response_body),
                "deployment_reference": arcane_reference,
                "response_body": response_body.decode("utf-8", errors="replace"),
            },
        )
        _atomic_json(record_path, record)
    except HTTPError as exc:
        response_body = _read_bounded(exc)
        record.update(
            status="failed",
            completed_at=_utc_now(),
            arcane={
                "http_status": exc.code,
                "response_sha256": hashlib.sha256(response_body).hexdigest(),
                "response_size_bytes": len(response_body),
                "response_body": response_body.decode("utf-8", errors="replace"),
            },
            error={
                "type": type(exc).__name__,
                "message": f"Arcane returned HTTP {exc.code}",
            },
        )
        _atomic_json(record_path, record)
        raise DeploymentError("deployment failed before Arcane acceptance") from exc
    except (BackupError, URLError, OSError, DeploymentError) as exc:
        record.update(
            status="failed",
            completed_at=_utc_now(),
            error={"type": type(exc).__name__, "message": str(exc)[:1000]},
        )
        _atomic_json(record_path, record)
        raise DeploymentError("deployment failed before Arcane acceptance") from exc
    return {
        "schema_version": 1,
        "deployment_id": deployment_id,
        "commit_sha": request_payload["commit_sha"],
        "images": request_payload["images"],
        "status": "arcane_accepted",
        "backup": backup_evidence,
        "arcane": {
            "http_status": response_status,
            "response_sha256": response_sha256,
            "deployment_reference": arcane_reference,
        },
        "record_id": record_path.name,
    }


class DeploymentHandler(BaseHTTPRequestHandler):
    server: DeploymentServer

    def do_POST(self) -> None:
        if self.path != DEPLOY_PATH:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.webhook_token}"
        ):
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return
        status = HTTPStatus.OK
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise DeploymentError("invalid request body length")
            result = deploy(
                self.server.gateway_config, json.loads(self.rfile.read(length))
            )
        except (ValueError, json.JSONDecodeError, DeploymentError) as exc:
            status, result = (
                HTTPStatus.BAD_GATEWAY,
                {"status": "rejected", "error": str(exc)},
            )
        body = json.dumps(result, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"deployment-gateway: {self.address_string()} {format % args}")


class DeploymentServer(ThreadingHTTPServer):
    def __init__(
        self, address: tuple[str, int], config: GatewayConfig, webhook_token: str
    ) -> None:
        super().__init__(address, DeploymentHandler)
        self.gateway_config, self.webhook_token = config, webhook_token


def attach_evidence(response_path: Path, release_path: Path, expected_sha: str) -> None:
    response = json.loads(response_path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "deployment_id",
        "commit_sha",
        "status",
        "images",
        "backup",
        "arcane",
        "record_id",
    }
    if not isinstance(response, dict) or not required.issubset(response):
        raise DeploymentError("deployment response is missing required evidence")
    images = response["images"]
    backup, arcane = response["backup"], response["arcane"]
    if (
        response["schema_version"] != 1
        or response["commit_sha"] != expected_sha
        or response["status"] != "arcane_accepted"
        or not isinstance(backup, dict)
        or backup.get("integrity_check") != "ok"
        or backup.get("foreign_key_violations") != []
        or not re.fullmatch(r"[0-9a-f]{64}", str(backup.get("database_sha256", "")))
        or not isinstance(images, dict)
        or set(images) != {"agent", "web"}
        or any(
            not isinstance(image, str) or not IMAGE_RE.fullmatch(image)
            for image in images.values()
        )
        or not isinstance(arcane, dict)
        or arcane.get("http_status") != HTTPStatus.ACCEPTED
        or not re.fullmatch(r"[0-9a-f]{64}", str(arcane.get("response_sha256", "")))
    ):
        raise DeploymentError(
            "deployment response did not prove backup and Arcane acceptance"
        )
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if release.get("sha") != expected_sha or release.get("decision") != "eligible":
        raise DeploymentError("release evidence does not match the deployment commit")
    release["deployment"] = response
    _atomic_json(release_path, release)


def _config_from_environment() -> tuple[GatewayConfig, str]:
    names = (
        "DATABASE_PATH",
        "BACKUP_DIRECTORY",
        "DEPLOYMENT_RECORD_DIRECTORY",
        "ARCANE_WEBHOOK_URL",
        "DEPLOY_WEBHOOK_TOKEN",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise DeploymentError("missing required configuration: " + ", ".join(missing))
    return GatewayConfig(
        Path(values["DATABASE_PATH"]),
        Path(values["BACKUP_DIRECTORY"]),
        Path(values["DEPLOYMENT_RECORD_DIRECTORY"]),
        values["ARCANE_WEBHOOK_URL"],
        os.environ.get("ARCANE_BEARER_TOKEN", "").strip() or None,
    ), values["DEPLOY_WEBHOOK_TOKEN"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8792)
    record = commands.add_parser("record-response")
    record.add_argument("--response", type=Path, required=True)
    record.add_argument("--release-evidence", type=Path, required=True)
    record.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    if args.command == "record-response":
        attach_evidence(args.response, args.release_evidence, args.expected_sha)
    else:
        config, token = _config_from_environment()
        DeploymentServer((args.host, args.port), config, token).serve_forever()


if __name__ == "__main__":
    main()
