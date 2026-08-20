# Encrypted backup and restore runbook

This runbook defines the production backup contract for the Workout Agent SQLite database. It complements `docs/OPERATIONS_RUNBOOKS.md`; incident command, evidence preservation, communication, and credential-compromise procedures remain canonical there.

## Service objectives

The production baseline is:

- **Recovery point objective (RPO): 6 hours.** Create an encrypted backup at least every six hours. A deployment with stricter requirements may run more frequently.
- **Recovery time objective (RTO): 60 minutes.** From declaring a database-loss incident, the target is to restore, verify, migrate, and return a healthy application to service within one hour.
- **Retention baseline: 35 days, never fewer than the 14 newest valid backups.** The `prune` command is deliberately conservative and skips malformed files instead of deleting them.
- **Restore verification: weekly and after every database migration change.** CI performs a synthetic restore drill without production data. Production operations must additionally drill the newest real encrypted backup in an isolated environment.

These are operational targets, not guarantees. A missed RPO/RTO or failed drill is an incident/follow-up item and must not be silently waived.

## Backup format and encryption

`tools/backup_restore.py` creates `.wab` (Workout Agent Backup) files. The format is versioned and fail-closed:

1. SQLite's online backup API creates a consistent snapshot, including WAL-backed databases without manually copying `-wal` or `-shm` files.
2. The snapshot is checked with `PRAGMA integrity_check` and `PRAGMA foreign_key_check`.
3. An encrypted manifest records the format version, creation time, key ID, database digest, schema digest, SQLite version markers, and per-table record counts.
4. The database and manifest are encrypted and authenticated as one Fernet payload.
5. The small outer envelope contains only format version, creation time, and key ID. Creation time and key ID are duplicated inside the authenticated manifest and must match during verification.
6. Backup files are written atomically with mode `0600`.

Plaintext API keys, health data, workout data, prompts, and user records never appear in the `.wab` container. Backups must still be treated as highly sensitive because possession of both a backup and its encryption key grants access to the database.

The normal application `ENCRYPTION_KEY` is intentionally **not** used for backup encryption. Backups have an independent keyring so database-secret rotation and backup-key rotation can be managed separately.

## Key management and rotation

Set a dedicated versioned Fernet keyring in the backup execution environment:

```bash
export BACKUP_ENCRYPTION_KEYS='2026-08:BASE64_FERNET_KEY'
export BACKUP_ACTIVE_KEY_ID='2026-08'
```

Generate a key with:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

For rotation, add the new key, make it active, and retain old keys until every backup encrypted by them has expired and been verified as no longer required:

```bash
export BACKUP_ENCRYPTION_KEYS='2026-08:OLD_KEY,2026-11:NEW_KEY'
export BACKUP_ACTIVE_KEY_ID='2026-11'
```

Never commit the keyring, place it in a command-line argument, include it in CI logs, or store it alongside the backup objects. Production keys should come from the deployment secret store with access limited to the backup writer and authorised restore operators. If a backup key is suspected compromised, follow the credential-compromise runbook in `docs/OPERATIONS_RUNBOOKS.md`, rotate the key immediately, assess every backup encrypted under it, and preserve incident evidence.

## Create and verify a production backup

Create an encrypted backup to a staging path on protected storage:

```bash
python tools/backup_restore.py create \
  --source /data/workout_agent.db \
  --output /srv/workout-agent-backups/workout-agent-$(date -u +%Y%m%dT%H%M%SZ).wab
```

Immediately verify it without restoring over any live database:

```bash
python tools/backup_restore.py verify \
  --backup /srv/workout-agent-backups/workout-agent-20260820T180000Z.wab
```

A backup job is successful only when **both** commands return zero. Copy the resulting `.wab` file to off-host access-controlled object storage. Enable object/version retention or immutability where the storage platform supports it. The storage transport and server-side encryption are defence in depth and do not replace application-level `.wab` encryption.

The scheduler or platform running these commands must alert on a non-zero exit. Do not treat file creation alone as proof of a usable backup.

## Retention

Preview the baseline retention decision:

```bash
python tools/backup_restore.py prune \
  --directory /srv/workout-agent-backups \
  --max-age-days 35 \
  --minimum-latest 14
```

Only after reviewing the output, apply it:

```bash
python tools/backup_restore.py prune \
  --directory /srv/workout-agent-backups \
  --max-age-days 35 \
  --minimum-latest 14 \
  --apply
```

The command never selects the newest 14 valid backups for deletion, even if they exceed the age threshold. Invalid or unreadable `.wab` files are skipped and reported so an operator can investigate rather than accidentally erase evidence.

Retention changes require an explicit policy decision. Do not shorten retention merely to recover disk space during an incident.

## Restore drill

### Synthetic CI drill

The repository workflow runs this without production secrets or data:

```bash
python tools/backup_restore.py drill \
  --repo-root . \
  --backend-dir backend \
  --output artifacts/backup-restore-drill.json
```

The synthetic drill:

- creates the current schema in a disposable SQLite database;
- writes synthetic tenant-labelled records;
- creates and authenticates an encrypted backup with an ephemeral key;
- restores it to a new path;
- verifies the encrypted manifest, record counts, schema digest, `integrity_check`, and foreign keys;
- runs the current `database.init_db()` against the restored database to exercise migrations;
- proves no pre-existing table lost records during migration;
- starts the production ASGI entry point against the restored database and performs a local request.

### Real-backup drill

At least weekly, test the newest real backup in an isolated host/container that has the production backup keyring but no public traffic:

```bash
python tools/backup_restore.py drill \
  --repo-root . \
  --backend-dir backend \
  --backup /secure-restore-input/latest.wab \
  --output /secure-restore-evidence/backup-restore-drill.json
```

The drill decrypts only inside a temporary directory and removes the restored plaintext copy when complete. Evidence intentionally contains safe status metadata rather than table contents or production row counts.

A real-backup drill is successful only when it verifies authentication, pre/post migration integrity, non-decreasing counts for every pre-existing table, and application startup. A failure blocks confidence in the backup set and requires operator investigation before the next backup window.

## Production recovery procedure

1. **Declare and contain.** Follow `docs/OPERATIONS_RUNBOOKS.md`. Stop writers if the active database is corrupt or data loss is ongoing. Preserve logs and the damaged database as incident evidence; do not modify it in place.
2. **Choose a recovery point.** Select the newest backup that predates the damaging event and is within the expected RPO. Confirm its key ID is available.
3. **Run a drill first.** Execute the real-backup drill above against the selected `.wab`. Do not proceed with a backup that fails authentication, integrity, migration, count, or startup checks.
4. **Restore to a new path.** The restore command refuses to overwrite an existing database:

   ```bash
   python tools/backup_restore.py restore \
     --backup /secure-restore-input/selected.wab \
     --output /data/workout_agent.restored.db
   ```

5. **Protect the damaged database.** Keep the old database read-only in protected incident storage if evidence retention is required. It must not be placed in source control or an unencrypted shared location.
6. **Cut over while writers are stopped.** Atomically move the verified restored database into the configured `DATABASE_PATH` according to the deployment platform's maintenance procedure. Never merge rows manually during an emergency unless a separate reviewed recovery plan requires it.
7. **Start and verify.** Start the web/agent services, confirm `/livez` and `/readyz` when those endpoints are deployed, perform an authenticated smoke test, and verify recent tenant-scoped records. Continue monitoring database and job errors.
8. **Record evidence.** Record the selected backup timestamp/key ID, incident ID, drill evidence ID, cutover time, observed data-loss window, and achieved RPO/RTO. Do not record keys or plaintext user data.
9. **Close or escalate.** If the achieved RPO exceeds 6 hours, RTO exceeds 60 minutes, counts are inconsistent, or the restored application does not remain healthy, keep the incident open and escalate.

## Monitoring and failure alerting

`.github/workflows/encrypted-backup-restore.yml` runs a restore drill on pull requests that can affect the backup/restore boundary, on `main`, on a weekly schedule, and on manual dispatch. Scheduled/main failures create or update a canonical GitHub issue titled `Backup restore drill failed` so repeated failures remain visible without generating issue spam.

GitHub CI validates the mechanics with synthetic data only. Production backup creation and real-backup drills belong in the deployment scheduler because production keys and backup objects must not be copied into repository CI. Configure that scheduler's non-zero exit path to page or notify the operator responsible for the backup SLO.

## Access-control checklist

- Backup writer: read access to the SQLite source, write-only/limited access to the backup destination, read access to the active backup encryption key.
- Restore operator: read access to selected backup objects and the required historical key, permission to write only to a controlled restore destination.
- Application runtime: does not need access to backup objects or backup keys.
- CI: uses only an ephemeral synthetic key and synthetic database.
- Backup storage: private, off-host, versioned/immutable where supported, transport encrypted, and audited.
- Evidence: contains no secret values or user payloads and follows the operational evidence-retention rules.
