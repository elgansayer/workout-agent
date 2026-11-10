"""Account data export: archive builder, asynchronous job service, maintenance.

An authenticated account can request a portable copy of the data held about it.
The request only records a job. A bounded background worker then builds a ZIP
archive from one read-only database snapshot, stores it privately on disk, and it
is served to its owner alone until it expires.

Safety properties (each is covered by ``backend/tests/test_account_export*.py``):

* the owner of a job is fixed at request time from the authenticated session and
  is re-read from the stored row by the worker, never taken from a caller;
* the archive contains only the reviewed collections in
  :mod:`account_export_sections`; credentials, internal security data and other
  accounts' records are excluded, and a final deterministic guard refuses any
  credential-class key before it is written;
* artifacts have random server-generated names, owner-only file permissions, a
  size cap and a retention period, and are removed by request-time hygiene, the
  scheduled sweep and a start-up sweep;
* a worker that dies cannot wedge an account: jobs carry a heartbeat lease and a
  stale job is failed so the owner can request again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

import account_export_store as store
from account_export_sections import (
    COLLECTIONS,
    EXCLUSIONS,
    SECTIONS,
    Collection,
    find_credential_keys,
    iter_records,
)
from account_export_store import ExportJob, ExportRateLimited

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "workout-agent-account-export"
EXPORT_SCHEMA_VERSION = 1

DEFAULT_RETENTION_HOURS = 24
MIN_RETENTION_HOURS = 1
MAX_RETENTION_HOURS = 168
MAX_REQUESTS_PER_WINDOW = 5
REQUEST_WINDOW = timedelta(hours=24)
MAX_READY_EXPORTS = 3
LISTED_EXPORTS = 10
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_BUILD_SECONDS = 15 * 60
HEARTBEAT_SECONDS = 5.0
RUNNING_LEASE = timedelta(minutes=2)
QUEUED_LEASE = timedelta(minutes=5)
ORPHAN_GRACE = timedelta(minutes=10)
FINISHED_ROW_RETENTION = timedelta(days=30)
WORKER_COUNT = 2

_RECORDS_PER_CHECKPOINT = 100
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ARTIFACT_FILE = re.compile(r"([0-9a-f]{32})\.zip(?:\.partial)?")
_DATE_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}")


class ExportError(Exception):
    """Base error. ``code`` is a stable identifier that is safe to show a user."""

    code = "export_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__(self.code)


class ExportDisabled(ExportError):
    code = "export_disabled"


class ExportNotFound(ExportError):
    code = "not_found"


class ExportNotReady(ExportError):
    code = "not_ready"


class ExportGone(ExportError):
    code = "gone"


class ExportTooLarge(ExportError):
    code = "export_too_large"


class ExportTimedOut(ExportError):
    code = "export_timeout"


class ExportCancelled(ExportError):
    code = "cancelled"


class ExportPolicyViolation(ExportError):
    code = "policy_violation"


@dataclass(frozen=True, slots=True)
class BuildResult:
    size_bytes: int
    checksum_sha256: str
    record_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    expired: int
    interrupted: int
    orphans_removed: int
    pruned: int


@dataclass(slots=True)
class ExportDownload:
    """An opened artifact. The caller must close ``file``."""

    job: ExportJob
    file: BinaryIO
    size_bytes: int
    filename: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def exports_enabled() -> bool:
    """Operator kill switch (``ACCOUNT_EXPORT_ENABLED=0``) for incident response."""

    raw = os.environ.get("ACCOUNT_EXPORT_ENABLED", "")
    return raw.strip().lower() not in _FALSE_VALUES


def retention_period() -> timedelta:
    """How long a finished export stays downloadable (``ACCOUNT_EXPORT_TTL_HOURS``)."""

    raw = os.environ.get("ACCOUNT_EXPORT_TTL_HOURS", "").strip()
    try:
        hours = int(raw) if raw else DEFAULT_RETENTION_HOURS
    except ValueError:
        logger.warning("Ignoring non-numeric ACCOUNT_EXPORT_TTL_HOURS.")
        hours = DEFAULT_RETENTION_HOURS
    return timedelta(hours=min(max(hours, MIN_RETENTION_HOURS), MAX_RETENTION_HOURS))


def export_dir_for(db_path: str) -> Path:
    """Directory that holds artifacts (``ACCOUNT_EXPORT_DIR``).

    The default sits beside the database so the web and agent containers, which
    share one data volume, agree on it without extra configuration.
    """

    configured = os.environ.get("ACCOUNT_EXPORT_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(db_path).resolve().parent / "account-exports"


def artifact_path(export_dir: Path, export_id: str) -> Path:
    """Return the artifact path for a server-generated export id."""

    if not store.EXPORT_ID_PATTERN.fullmatch(export_id):
        raise ValueError("export ids must be 32 lowercase hexadecimal characters")
    return export_dir / f"{export_id}.zip"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Archive builder
# ---------------------------------------------------------------------------


@contextmanager
def _snapshot(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open the database read-only inside one transaction.

    Every collection is read from the same point in time, and the connection
    cannot write even if a query were wrong.
    """

    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


class _Budget:
    """Size, time and cancellation limits for one build."""

    def __init__(
        self,
        job_id: str,
        *,
        db_path: str,
        max_bytes: int | None = None,
        max_seconds: float | None = None,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.job_id = job_id
        self.db_path = db_path
        self.max_bytes = MAX_UNCOMPRESSED_BYTES if max_bytes is None else max_bytes
        self.max_seconds = MAX_BUILD_SECONDS if max_seconds is None else max_seconds
        self.heartbeat_seconds = (
            HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        )
        self.bytes_written = 0
        self._started = time.monotonic()
        self._last_beat = float("-inf")

    def add_bytes(self, count: int) -> None:
        self.bytes_written += count
        if self.bytes_written > self.max_bytes:
            raise ExportTooLarge()

    def checkpoint(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if now - self._started > self.max_seconds:
            raise ExportTimedOut()
        if force or now - self._last_beat >= self.heartbeat_seconds:
            if not store.touch_job(self.job_id, now=_utcnow(), db_path=self.db_path):
                raise ExportCancelled()
            self._last_beat = now


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _dump(value: Any) -> str:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "allow_nan": False,
        "separators": (",", ":"),
        "default": str,
    }
    try:
        return json.dumps(value, **options)
    except ValueError:  # NaN and Infinity are not valid JSON
        return json.dumps(_finite(value), **options)


def _zip_time(moment: datetime) -> tuple[int, int, int, int, int, int]:
    utc = moment.astimezone(timezone.utc)
    return (max(utc.year, 1980), utc.month, utc.day, utc.hour, utc.minute, utc.second)


def _zip_entry(name: str, moment: datetime) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_zip_time(moment))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _write_collection(
    archive: zipfile.ZipFile,
    conn: sqlite3.Connection,
    collection: Collection,
    owner_id: str,
    budget: _Budget,
    generated_at: datetime,
) -> tuple[int, str]:
    """Stream one collection into the archive; return (record count, sha256)."""

    digest = hashlib.sha256()
    count = 0
    header = _dump(
        {
            "schema_version": collection.schema_version,
            "section": collection.section,
            "collection": collection.name,
            "description": collection.description,
            "provenance": collection.provenance.as_dict(),
        }
    )
    with archive.open(_zip_entry(collection.path, generated_at), "w") as out:

        def emit(text: str) -> None:
            data = text.encode("utf-8", "replace")
            budget.add_bytes(len(data))
            digest.update(data)
            out.write(data)

        emit(header[:-1] + ',"records":[')
        for record in iter_records(conn, collection, owner_id):
            leaked = find_credential_keys(record)
            if leaked:
                logger.error(
                    "Account export blocked: credential-class fields in %s: %s",
                    collection.key,
                    ", ".join(leaked),
                )
                raise ExportPolicyViolation()
            emit(("," if count else "") + "\n" + _dump(record))
            count += 1
            if count % _RECORDS_PER_CHECKPOINT == 0:
                budget.checkpoint()
        emit(f'\n],"record_count":{count}}}\n')
    return count, digest.hexdigest()


def _build_manifest(
    job: ExportJob,
    generated_at: datetime,
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sections = []
    for section_key, title in SECTIONS:
        collections = [
            entries[collection.key]
            for collection in COLLECTIONS
            if collection.section == section_key
        ]
        sections.append({"section": section_key, "title": title, "collections": collections})
    return {
        "format": EXPORT_FORMAT,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "export_id": job.id,
        "generated_at": store.utc_iso(generated_at),
        "account": {"user_id": job.user_id},
        "consistency": "All collections were read from one read-only database snapshot.",
        "sections": sections,
        "exclusions": list(EXCLUSIONS),
    }


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove export file %s.", path.name)


def build_archive(
    job: ExportJob,
    *,
    db_path: str,
    destination: Path,
    generated_at: datetime,
    budget: _Budget | None = None,
) -> BuildResult:
    """Write the owner's export archive to *destination* (created owner-only).

    The owner is taken from *job*, which the caller loaded from the stored row.
    """

    budget = budget or _Budget(job.id, db_path=db_path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _unlink_quiet(destination)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    entries: dict[str, dict[str, Any]] = {}
    with os.fdopen(fd, "wb") as raw:
        with _snapshot(db_path) as conn, zipfile.ZipFile(
            raw, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for collection in COLLECTIONS:
                budget.checkpoint(force=True)
                count, checksum = _write_collection(
                    archive, conn, collection, job.user_id, budget, generated_at
                )
                entries[collection.key] = {
                    "name": collection.name,
                    "path": collection.path,
                    "description": collection.description,
                    "schema_version": collection.schema_version,
                    "record_count": count,
                    "sha256": checksum,
                    "provenance": collection.provenance.as_dict(),
                }
            manifest = _dump(_build_manifest(job, generated_at, entries))
            budget.add_bytes(len(manifest))
            archive.writestr(_zip_entry("manifest.json", generated_at), manifest)
        raw.flush()
        os.fsync(raw.fileno())

    with destination.open("rb") as written:
        checksum_sha256 = hashlib.file_digest(written, "sha256").hexdigest()
    return BuildResult(
        size_bytes=destination.stat().st_size,
        checksum_sha256=checksum_sha256,
        record_counts={key: entry["record_count"] for key, entry in entries.items()},
    )


# ---------------------------------------------------------------------------
# Job service
# ---------------------------------------------------------------------------


def _remove_artifact(export_dir: Path, export_id: str) -> None:
    final = artifact_path(export_dir, export_id)
    _unlink_quiet(final)
    _unlink_quiet(final.with_name(final.name + ".partial"))


def _housekeeping(
    user_id: str | None, *, db_path: str, export_dir: Path, now: datetime
) -> tuple[list[ExportJob], list[ExportJob]]:
    """Fail stale jobs and expire due exports (one owner, or every owner)."""

    interrupted = store.reap_stale(
        now,
        running_lease=RUNNING_LEASE,
        queued_lease=QUEUED_LEASE,
        user_id=user_id,
        db_path=db_path,
    )
    expired = store.expire_due(now, user_id=user_id, db_path=db_path)
    for job in (*interrupted, *expired):
        _remove_artifact(export_dir, job.id)
    return interrupted, expired


def request_export(
    user_id: str,
    *,
    db_path: str,
    export_dir: Path,
    now: datetime | None = None,
) -> tuple[ExportJob, bool]:
    """Queue an export for the authenticated owner.

    Returns ``(job, created)``; when an export is already in flight it is returned
    unchanged so a double submit never starts a second build. The caller submits a
    newly created job to the runner.
    """

    if not exports_enabled():
        raise ExportDisabled()
    moment = now or _utcnow()
    _housekeeping(user_id, db_path=db_path, export_dir=export_dir, now=moment)
    job, created = store.create_job(
        user_id,
        uuid.uuid4().hex,
        schema_version=EXPORT_SCHEMA_VERSION,
        now=moment,
        max_requests=MAX_REQUESTS_PER_WINDOW,
        window=REQUEST_WINDOW,
        db_path=db_path,
    )
    if created:
        for stale in store.expire_oldest_ready(
            user_id, keep=MAX_READY_EXPORTS - 1, now=moment, db_path=db_path
        ):
            _remove_artifact(export_dir, stale.id)
    return job, created


def list_exports(
    user_id: str,
    *,
    db_path: str,
    export_dir: Path,
    now: datetime | None = None,
) -> list[ExportJob]:
    _housekeeping(user_id, db_path=db_path, export_dir=export_dir, now=now or _utcnow())
    return store.list_jobs(user_id, limit=LISTED_EXPORTS, db_path=db_path)


def get_export(
    user_id: str,
    export_id: str,
    *,
    db_path: str,
    export_dir: Path,
    now: datetime | None = None,
) -> ExportJob:
    """Return the owner's job; unknown, foreign and deleted ids look identical."""

    _housekeeping(user_id, db_path=db_path, export_dir=export_dir, now=now or _utcnow())
    job = store.get_job(user_id, export_id, db_path=db_path)
    if job is None or job.status == store.DELETED:
        raise ExportNotFound()
    return job


def delete_export(
    user_id: str,
    export_id: str,
    *,
    db_path: str,
    export_dir: Path,
    now: datetime | None = None,
) -> None:
    """Delete the owner's export and its artifact; an in-flight build is cancelled."""

    if not store.delete_job(user_id, export_id, now=now or _utcnow(), db_path=db_path):
        raise ExportNotFound()
    _remove_artifact(export_dir, export_id)


def _export_filename(job: ExportJob) -> str:
    stamp = (job.completed_at or job.requested_at)[:10]
    if _DATE_PREFIX.fullmatch(stamp):
        return f"workout-agent-export-{stamp}.zip"
    return "workout-agent-export.zip"


def _open_artifact(export_dir: Path, job: ExportJob) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(artifact_path(export_dir, job.id), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (
            job.size_bytes is not None and info.st_size != job.size_bytes
        ):
            raise ExportGone("artifact_unavailable")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def open_export(
    user_id: str,
    export_id: str,
    *,
    db_path: str,
    export_dir: Path,
    now: datetime | None = None,
) -> ExportDownload:
    """Open the owner's ready, unexpired artifact for download and audit it."""

    if not exports_enabled():
        raise ExportDisabled()
    moment = now or _utcnow()
    job = get_export(user_id, export_id, db_path=db_path, export_dir=export_dir, now=moment)
    if job.status in (store.QUEUED, store.RUNNING):
        raise ExportNotReady()
    if job.status != store.READY:
        raise ExportGone()
    try:
        handle = _open_artifact(export_dir, job)
    except (OSError, ExportGone) as exc:
        # The artifact vanished, was replaced or was truncated: retire the export honestly
        # rather than serving a partial file.
        store.expire_job(user_id, export_id, now=moment, db_path=db_path)
        _remove_artifact(export_dir, export_id)
        raise ExportGone("artifact_unavailable") from exc
    store.record_download(user_id, export_id, now=moment, db_path=db_path)
    return ExportDownload(
        job=job,
        file=handle,
        size_bytes=job.size_bytes or 0,
        filename=_export_filename(job),
    )


def public_view(job: ExportJob) -> dict[str, Any]:
    """The only shape of a job that leaves the server; it never names the owner."""

    ready = job.status == store.READY
    return {
        "id": job.id,
        "status": job.status,
        "schema_version": job.schema_version,
        "requested_at": job.requested_at,
        "completed_at": job.completed_at,
        "expires_at": job.expires_at,
        "size_bytes": job.size_bytes,
        "checksum_sha256": job.checksum_sha256,
        "record_counts": job.record_counts,
        "error_code": job.error_code,
        "download_url": f"/api/account/exports/{job.id}/download" if ready else None,
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def run_export(
    export_id: str, *, db_path: str, export_dir: Path
) -> ExportJob | None:
    """Build one queued export and record the outcome on its job.

    Build failures are recorded as a stable error code and never raised. If the
    database itself is unavailable the error propagates to the runner, which logs
    it; the job is then failed as ``interrupted`` by stale-job recovery.
    """

    job = store.claim_job(export_id, now=_utcnow(), db_path=db_path)
    if job is None:
        return None  # cancelled, already claimed, or not a queued job

    final = artifact_path(export_dir, job.id)
    partial = final.with_name(final.name + ".partial")
    try:
        if not exports_enabled():
            raise ExportDisabled()
        result = build_archive(
            job, db_path=db_path, destination=partial, generated_at=_utcnow()
        )
        try:
            os.replace(partial, final)
        except FileNotFoundError as exc:  # deleted while it was being finalised
            raise ExportCancelled() from exc
        published = store.complete_job(
            job.id,
            now=_utcnow(),
            expires_at=_utcnow() + retention_period(),
            size_bytes=result.size_bytes,
            checksum_sha256=result.checksum_sha256,
            record_counts=result.record_counts,
            db_path=db_path,
        )
        if not published:  # cancelled or reaped while finishing
            _remove_artifact(export_dir, job.id)
            return None
        logger.info(
            "Account export %s ready: %d bytes, %d records.",
            job.id,
            result.size_bytes,
            sum(result.record_counts.values()),
        )
        return store.get_job(job.user_id, job.id, db_path=db_path)
    except ExportCancelled:
        _remove_artifact(export_dir, job.id)
        return None
    except ExportError as exc:
        _remove_artifact(export_dir, job.id)
        store.fail_job(job.id, now=_utcnow(), error_code=exc.code, db_path=db_path)
        logger.warning("Account export %s failed: %s.", job.id, exc.code)
    except Exception:  # recorded on the job; the worker must survive
        _remove_artifact(export_dir, job.id)
        store.fail_job(job.id, now=_utcnow(), error_code="build_failed", db_path=db_path)
        logger.exception("Account export %s failed unexpectedly.", job.id)
    return store.get_job(job.user_id, job.id, db_path=db_path)


class ExportRunner:
    """Bounded in-process worker pool.

    The database row is the source of truth; the pool only decides when a queued
    row is built. A pool of two keeps a burst of requests from competing with
    interactive traffic, and queued work waits here rather than in the web
    server's own thread pool.
    """

    def __init__(self, workers: int = WORKER_COUNT) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="account-export"
        )

    def submit(self, export_id: str, *, db_path: str, export_dir: Path) -> None:
        self._executor.submit(self._run, export_id, db_path, export_dir)

    @staticmethod
    def _run(export_id: str, db_path: str, export_dir: Path) -> None:
        try:
            run_export(export_id, db_path=db_path, export_dir=export_dir)
        except Exception:  # keep the pool thread alive
            logger.exception("Account export worker crashed for %s.", export_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


_runner: ExportRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> ExportRunner:
    """Return the process-wide runner, creating it on first use."""

    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = ExportRunner()
        return _runner


def shutdown_runner() -> None:
    global _runner
    with _runner_lock:
        runner, _runner = _runner, None
    if runner is not None:
        runner.shutdown()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def _remove_orphans(export_dir: Path, *, db_path: str, now: datetime) -> int:
    """Delete artifact-shaped files that no live job owns."""

    if not export_dir.is_dir():
        return 0
    live = store.live_export_ids(db_path=db_path)
    cutoff = now.timestamp() - ORPHAN_GRACE.total_seconds()
    removed = 0
    with os.scandir(export_dir) as entries:
        for entry in entries:
            match = _ARTIFACT_FILE.fullmatch(entry.name)
            if match is None or match.group(1) in live:
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                if entry.stat().st_mtime > cutoff:
                    continue
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
    return removed


def maintain_exports(
    *, db_path: str, export_dir: Path, now: datetime | None = None
) -> MaintenanceReport:
    """Scheduled sweep across all accounts.

    Fails stale jobs, expires exports past retention and deletes their artifacts,
    removes unowned artifact files and prunes old finished job rows. Idempotent,
    so concurrent replicas and repeated runs are safe.
    """

    moment = now or _utcnow()
    interrupted, expired = _housekeeping(
        None, db_path=db_path, export_dir=export_dir, now=moment
    )
    return MaintenanceReport(
        expired=len(expired),
        interrupted=len(interrupted),
        orphans_removed=_remove_orphans(export_dir, db_path=db_path, now=moment),
        pruned=store.prune_terminal(moment - FINISHED_ROW_RETENTION, db_path=db_path),
    )


__all__ = [
    "BuildResult",
    "ExportCancelled",
    "ExportDisabled",
    "ExportDownload",
    "ExportError",
    "ExportGone",
    "ExportJob",
    "ExportNotFound",
    "ExportNotReady",
    "ExportPolicyViolation",
    "ExportRateLimited",
    "ExportRunner",
    "ExportTimedOut",
    "ExportTooLarge",
    "MaintenanceReport",
    "build_archive",
    "delete_export",
    "export_dir_for",
    "exports_enabled",
    "get_export",
    "get_runner",
    "list_exports",
    "maintain_exports",
    "open_export",
    "public_view",
    "request_export",
    "retention_period",
    "run_export",
    "shutdown_runner",
]
