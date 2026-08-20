# Workout Agent operational incident runbooks

These runbooks are the canonical response procedures for Workout Agent data loss, credential compromise, provider outages, and privacy incidents. They are written for the production Docker deployment but keep commands staged, tenant-safe, reversible where possible, and free of secret values. Never paste credentials, raw health payloads, session cookies, OAuth codes, encryption keys, or private notification destinations into tickets, chat, screenshots, CI logs, or evidence bundles.

## Incident command and evidence rules

**Owner:** The primary on-call engineer is incident commander until explicitly handed over. The incident commander assigns an operations owner for service recovery and a privacy/security owner whenever credentials or user data may be involved.

**Prerequisites:** Access to the production host, deployment configuration, encrypted backups, GitHub deployment history, provider consoles, and the private incident channel. At least two people should be available for destructive recovery or key-rotation actions whenever practical.

For every incident:

1. Create an incident record with UTC start time, severity, incident commander, affected capabilities, known tenant scope, current production commit/image digest, and a link to evidence storage.
2. Freeze unrelated deploys and automated mutations. Verification workflows may continue; autonomous code changes must not be used as an incident-response control plane.
3. Prefer read-only inspection first. Record commands before running them. Use copied or mounted snapshots for forensic queries instead of modifying the original database.
4. Collect only necessary evidence. Hash files before and after copying, record UTC timestamps and source paths, redact secrets centrally, and restrict access to the smallest incident group.
5. Do not claim recovery from a single green request. Verify data integrity, authentication, tenant isolation, background processing, provider state, and user-visible behaviour.
6. Track every unresolved drill or incident finding as follow-up work with an owner and due date. The automated drill writes a dated evidence JSON file plus a checkbox follow-up document and CI opens or updates a canonical follow-up issue when findings exist.

Severity guide: **SEV-1** confirmed cross-user disclosure, destructive data loss, active credential abuse, or production takeover; **SEV-2** material outage or recoverable corruption affecting multiple users; **SEV-3** isolated degraded provider/service with no known confidentiality or integrity impact.

## Runbook: data loss and backup restore

**Owner:** Incident commander owns go/no-go decisions; operations owner performs restore; privacy/security owner validates tenant boundaries before traffic resumes.

**Prerequisites:** A read-only copy of the affected database/volume, a known-good encrypted backup, the production commit and schema version that created the backup, sufficient free disk space for two restored copies, and a maintenance path that prevents concurrent writers.

### Detection

Treat missing tables/rows, impossible row-count drops, SQLite integrity errors, failed migrations, empty user histories, corrupted volumes, or unexplained production rollback as possible data-loss events. Record the first observed UTC time, affected endpoints/jobs, current database size/hash, filesystem health, container/image digest, and relevant deployment/migration events.

### Containment

Stop write-capable web/worker/scheduler processes or place the service in maintenance mode. Do not run migrations, cleanup jobs, provider sync, account deletion, or automated programme adaptation against the suspect database. Snapshot the database file plus WAL/SHM files when present before any repair attempt. Keep the original evidence read-only.

### Communication

For SEV-1/2, notify the incident channel immediately with impact, current containment, backup timestamp under consideration, and next verification point. If user data may be lost or exposed, involve the privacy/security owner before external messaging. User-facing statements must distinguish confirmed facts from scope still being investigated.

### Safe commands

Run from the deployment directory after substituting explicit paths. These commands inspect or copy; they do not delete the original database.

```bash
# Record current deployment and database metadata.
docker compose ps
docker compose images
stat --printf='%n %s bytes %y\n' /absolute/path/workout_agent.db
sha256sum /absolute/path/workout_agent.db

# Verify a copied candidate before promotion.
cp --reflink=auto --preserve=all /absolute/path/backup/workout_agent.db /absolute/path/restore-candidate.db
sqlite3 /absolute/path/restore-candidate.db 'PRAGMA integrity_check;'
sqlite3 /absolute/path/restore-candidate.db '.tables'
sqlite3 /absolute/path/restore-candidate.db 'SELECT COUNT(*) FROM users;'
```

Before swapping files, stop all writers and create a final timestamped copy of the current database. Promote by an atomic rename on the same filesystem where possible. Preserve file owner/mode expected by the container. Start one web instance first, verify read-only paths, then workers/schedulers.

### Rollback

If restored schema, tenant counts, authentication, or smoke checks fail, stop writers again and atomically restore the pre-recovery snapshot. Do not merge rows ad hoc during an incident. If both current and backup copies contain unique valid data, keep both immutable and perform a separately reviewed reconciliation after service safety is established.

### Verification

Require `PRAGMA integrity_check` = `ok`, expected critical tables, plausible per-tenant row counts, successful login for a dedicated test account, negative cross-user access checks, read-only dashboard/history checks, and one controlled background-job smoke test. Compare backup timestamp against provider sync ledgers so later re-ingestion cannot double-count data. Record exact commands and outputs in the evidence bundle.

### Evidence preservation

Store hashes, file sizes, backup source/timestamp, production commit/image digest, migration logs, row-count summaries, and restore verification output. Never attach the raw production database to a GitHub issue. Raw backups and forensic copies remain encrypted in restricted storage with retention set by the privacy policy.

## Runbook: credential or encryption-key compromise

**Owner:** Security/privacy owner directs rotation and revocation; operations owner deploys replacement configuration; incident commander controls sequencing and user impact.

**Prerequisites:** Inventory of affected secret names and consumers, access to the authoritative secret store/provider consoles, a secure channel for replacement values, current deployment configuration, audit logs, and a tested way to restart/reload each consumer without printing secrets.

### Detection

Trigger this runbook for secret scanner findings, leaked `.env`/backup/log content, suspicious provider/API use, unexpected authentication from a token, exposed GitHub/deployment credentials, or evidence that the application encryption key was copied. Record only secret identifiers, fingerprints where safe, first/last known exposure time, and consumers. Never paste the secret itself into the incident record.

### Containment

Disable automation that can use the affected credential. If abuse is active, revoke the compromised credential immediately even if this causes a temporary outage. Otherwise create a replacement first, restrict it to minimum scopes, verify the new credential on a single controlled consumer, then revoke the old credential. A compromised application encryption key is a SEV-1/2 event because stored per-user secrets may need re-encryption and provider-side revocation.

### Communication

Notify the incident commander and security/privacy owner immediately. Identify affected secret classes and capabilities, not values. If user-owned provider tokens may be exposed, prepare provider-specific revoke/reconnect guidance and assess notification obligations before sending user communications.

### Safe commands

Use commands that inspect variable names or hashes, never values.

```bash
# Confirm expected variable names are present without printing values.
docker compose config --environment | cut -d= -f1 | sort -u

# Restart only after replacement values are installed in the secret source.
docker compose up -d --no-deps --force-recreate web agent

# Verify containers restarted; inspect timestamps/status only.
docker compose ps
```

For GitHub, deployment, OAuth, AI, Hevy, Telegram/Discord/Web Push, and future health-provider credentials: create replacement with least privilege, update the secret source, verify a non-destructive status/test call, revoke the old credential in the upstream console, then verify old credentials are rejected. For the application encryption key, use the repository's reviewed key-rotation/migration tooling when available; do not bulk-decrypt/re-encrypt with shell one-liners.

### Rollback

Before old credential revocation, rollback means restore the previous deployment configuration while keeping the new credential available. After confirmed compromise or revocation, never reactivate the old secret; rollback must use a second clean replacement credential or disable the affected capability. For encryption-key rotation, keep an encrypted escrow of the immediately previous key only for the bounded migration rollback window, then destroy it according to policy.

### Verification

Verify the replacement credential works only for intended capabilities/tenant, the old credential is rejected upstream, logs contain no secret values, affected jobs recover without duplicate side effects, and audit entries identify rotation/revocation without values. Search repository history and retained CI/log artifacts for the exposed fingerprint and record remediation if historical removal is required.

### Evidence preservation

Preserve scanner alert IDs, provider audit-event IDs, secret names, safe fingerprints, rotation/revocation UTC timestamps, deployment commit/image digest, and redacted verification output. Evidence must never contain plaintext credentials, authorization headers, OAuth codes, or encryption keys.

## Runbook: provider outage or provider-token compromise

**Owner:** Operations owner manages service degradation; connector owner manages provider communication and retries; security/privacy owner takes control if a token or provider account is compromised.

**Prerequisites:** Provider status/admin access, connector sync ledger/metrics, affected tenant IDs or pseudonymous references, current cursor/checkpoint state, provider rate-limit documentation, and a way to disable one connector without disabling unrelated providers.

### Detection

Detect sustained 401/403/429/5xx responses, timeouts, webhook verification failures, cursor stalls, stale-data thresholds, unusual quota use, or provider-side incident notices. Separate authentication failure from quota/transient outage. Establish affected tenants and last known successful sync without exposing provider payloads.

### Containment

Open the connector circuit or pause its scheduled jobs; keep unrelated providers and the core app available. Stop retries that amplify 429/5xx conditions. Preserve sync cursors and idempotency state. Do not delete local canonical data because an upstream provider is unavailable. For suspected token compromise, follow the credential-compromise runbook and prevent writes/publishing before reconnecting.

### Communication

Mark the integration degraded in operator/user status surfaces and state the last successful sync time. Do not present stale provider data as current. For prolonged outages, communicate which features remain available and which recommendations are withheld because freshness/confidence is insufficient.

### Safe commands

```bash
# Read-only deployment and recent service state.
docker compose ps
docker compose logs --since=30m --tail=500 web agent > /tmp/workout-agent-provider-incident.log

# Hash the copied log before redaction/storage.
sha256sum /tmp/workout-agent-provider-incident.log
```

Use application-supported connector pause/reconnect/test controls rather than editing database cursors manually. Resume with a bounded incremental sync before any large backfill. Respect `Retry-After` and provider quotas.

### Rollback

If reconnect or resume creates duplicates, unexpected mutations, or cross-tenant mismatches, pause the connector again and restore the pre-resume cursor/checkpoint snapshot. Keep provider records and local canonical records separate until reconciliation is reviewed.

### Verification

Confirm provider authentication/status, one tenant-safe incremental sync, idempotent replay, cursor advancement, no duplicate canonical records, freshness timestamps, and correct degraded-to-ready UI/API state. Publishing connectors additionally require a no-op/diff preview before writes resume.

### Evidence preservation

Store safe error categories, request correlation IDs, provider incident/audit IDs, rate-limit headers that contain no secrets, sync-run IDs/cursor ranges, affected tenant pseudonyms, and reconnect verification. Redact URLs/query strings if they can contain tokens.

## Runbook: privacy or data-exposure incident

**Owner:** Privacy/security owner leads scope and notification decisions; incident commander coordinates containment; operations owner supplies evidence and technical remediation.

**Prerequisites:** Data-flow inventory, tenant-isolation model, authentication/session architecture, log/cache/backup retention locations, provider/notification destination mappings, and access to revoke sessions/tokens and disable affected routes/jobs.

### Detection

Trigger for suspected IDOR/cross-user reads, cache or service-worker leakage, misrouted notifications, logs containing health/credential data, incorrect export/deletion scope, provider records attached to the wrong local account, or unauthorized admin access. Preserve the exact request/time/account context using pseudonymous identifiers.

### Containment

Disable the smallest affected route/job/channel or place the service in maintenance mode if tenant boundaries cannot be trusted. Revoke affected sessions and credentials where needed. Stop exports, notifications, provider writes, and automated sync paths implicated in the incident. Do not purge evidence as part of containment.

### Communication

Escalate immediately as SEV-1 when cross-user disclosure or active unauthorized access is confirmed. Keep a decision log for legal/regulatory notification assessment, affected-user identification, facts known/unknown, and message approvals. Do not disclose one user's identity or health data while notifying another user.

### Safe commands

```bash
# Capture deployment identity and bounded logs for offline redaction/review.
docker compose images
docker compose logs --since=60m --tail=1000 web agent > /tmp/workout-agent-privacy-incident.log
sha256sum /tmp/workout-agent-privacy-incident.log

# Preserve a database copy for restricted forensic review; do not attach it to tickets.
cp --reflink=auto --preserve=all /absolute/path/workout_agent.db /restricted/evidence/workout_agent-incident.db
sha256sum /restricted/evidence/workout_agent-incident.db
```

Perform queries on a copied database and select only columns necessary for scope analysis. Use hashed/pseudonymous user references in working notes. Rotate any exposed credentials using the credential-compromise runbook.

### Rollback

Do not re-enable a vulnerable route/job because a user-facing symptom disappeared. Roll back to a known-safe reviewed commit only after confirming schema compatibility and data integrity. If a bad deployment wrote cross-tenant data, freeze the affected records and perform a reviewed reconciliation; never blindly delete rows to make tests pass.

### Verification

Reproduce the original access path with dedicated test tenants and prove the negative case: user A cannot read, mutate, export, queue, notify, or attach provider data for user B. Verify caches/session state, background jobs, connector ownership, notification destinations, exports/deletion, and logs. Run the relevant deterministic authorization/tenant tests before traffic resumes.

### Evidence preservation

Preserve request/job/sync correlation IDs, deployment identity, redacted logs, scoped query results, affected data categories, access timestamps, session/token revocation times, and verification evidence. Hash evidence at collection and after transfer. Restrict raw health data and database snapshots to approved evidence storage; GitHub issues receive summaries only.

## Automated drill and follow-up process

Run locally or in CI without production secrets or network access:

```bash
python -m unittest tools.test_ops_drill
python tools/ops_drill.py validate --runbook docs/OPERATIONS_RUNBOOKS.md
python tools/ops_drill.py drill \
  --runbook docs/OPERATIONS_RUNBOOKS.md \
  --output artifacts/operations-drill.json \
  --follow-ups artifacts/operations-drill-followups.md
python tools/ops_drill.py verify --evidence artifacts/operations-drill.json
```

The drill validates every runbook contract, rejects known destructive command patterns, performs a disposable SQLite backup/restore with tenant-labelled records, simulates replace-then-revoke credential rotation, verifies provider-outage decisions remain bounded/non-mutating, and proves the generated evidence excludes synthetic credential material. CI uploads the dated evidence and follow-up file. Scheduled/manual failures create or update one canonical GitHub follow-up issue using a stable fingerprint rather than creating an issue storm.

A successful synthetic drill does **not** prove a production backup is restorable. Operators must still run a periodic restricted restore test against the actual encrypted backup format and deployment schema, recording only redacted evidence and hashes in the incident/drill record.
