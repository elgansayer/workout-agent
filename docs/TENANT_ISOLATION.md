# Tenant isolation for caches, schedulers and background workers

Request handlers prove who owns data through the signed session. Schedulers, jobs and shared in-process state run without one, so a leak there is silent: a cache key without an owner, a credential looked up "for whoever is first", or an id taken from a flag or a queue. `backend/tenant_isolation.py` is the single place that defines how a worker knows its owner, and every recurring job must go through it.

The executable proof for every user-owned surface is the generated [tenant-isolation matrix](TENANT_ISOLATION_MATRIX.md). It runs two synthetic tenants through the production repositories, export assembly, worker guards, sessions, notification delivery and secret vault, and records whether user A could read, change, export, queue or receive user B's data. Regenerate it with `python backend/scripts/tenant_isolation_matrix.py`; `--check` fails when a probe regresses or the document is stale, and `backend/tests/test_tenant_isolation_matrix.py` enforces that every user-owned table stays covered.

## Contract

1. **Identity is a value.** `OwnerIdentity` is a frozen dataclass. It cannot be reassigned after construction, and blank or malformed ids are rejected.
2. **A claimed id is not proof.** A `--user-id` flag, `SCHEDULER_USER_ID`, a queued payload or an id listed by the scheduler is only a *claim*. `authorize_owner(db_path, claim)` checks it against the `users` table and returns the identity built from the stored row. An empty claim, an unknown user and an unreachable store all raise `OwnershipError`; there is no fallback to shared or unscoped data.
3. **Authorize when the work executes, not when it is queued.** `run_owned_job` authorizes immediately before the work runs, so a user removed after the job was listed is denied rather than resurrected. `main.run`, `main._sync_history`, `insight_cron` and `scripts/sync_history.sync_all` authorize on entry as well, so a subprocess or CLI dispatch is re-checked even when the caller already did it.
4. **Keys are derived from the owner.** Cache, lock, cursor and notification-batch keys are built with `owner_scoped_key(owner, namespace, *parts)`. The owner is always the second segment and every later segment is percent-encoded, so a caller-supplied part can neither impersonate another owner nor collide with a different key.
5. **Locks never span owners.** `OwnerLocks` is keyed by owner and job kind. An overlapping duplicate for the same owner is skipped; one owner's slow job never delays another's.
6. **Prompt and notification input carries its owner.** `OwnedContext` binds the owner to the data read for them. Persistence and delivery take the owner from the context, never from an ambient loop variable.
7. **Credentials resolve per owner.** `main._scope_config_to_user` gives an owner only their own stored connector keys. The server-wide `HEVY_API_KEY`, `GOOGLE_HEALTH_*` and `HEALTH_CONNECT_FILE` settings belong to the legacy tenant (`legacy@local`) and are withheld from every other user. `ai_resolver.resolve_provider` uses the owner's key, and the server Gemini key is a Gemini-only fallback.
8. **Database helpers always receive `user_id`.** Several helpers in `database.py` read or delete across *all* tenants when `user_id` is omitted. `backend/tests/test_tenant_isolation_policy.py` scans every production module and fails when such a helper is called without an owner.
9. **A failure stays inside its job.** `run_owned_job` returns a `JobOutcome` (`completed`, `failed`, `denied` or `skipped`). A failed outcome carries only the exception class name, never its message, and never reaches the next owner's job.

## Where each surface is enforced

| Surface | Owner binding | Tests |
| --- | --- | --- |
| Daily coaching, daily insight and weekly correlation dispatch | `scheduler._run_per_owner` runs each user as an `OwnedJob` through `run_owned_job` | `test_scheduler.py`, `test_tenant_isolation.py` |
| Daily cycle (`main.run`) | `authorize_owner` before any credential, history or notification is touched | `test_tenant_isolation.py`, `test_tenant_isolation_concurrency.py` |
| AI insight jobs | `insight_cron` authorizes, builds an `OwnedContext`, and persists under `context.owner` | `test_tenant_isolation.py`, `test_tenant_isolation_concurrency.py` |
| Hevy history rebuild (CLI, `main --sync-history`, web) | `authorize_owner`; the destructive rebuild only deletes the owner's rows | `test_sync_history.py`, `test_tenant_isolation.py` |
| Google Health sync cursor and refresh token | `sync_body_metrics` requires `user_id` and reads and rotates the token only under it | `test_google_health_client.py`, `test_tenant_isolation_concurrency.py` |
| Notifications and push subscriptions | Written and read with the job owner's id | `test_tenant_isolation_concurrency.py` |
| Web request throttle (`_RATE_LIMITS`) | Signed-in requests use `owner_scoped_key(owner, "rate-limit")`; only anonymous requests use the client address | `test_tenant_isolation_rate_limit.py` |
| HTTP and service-worker caches | Personalised responses are `private, no-store` with `Vary: Cookie, Authorization`; the service worker never stores them | `test_web_cache_security.py`, `test_service_worker_cache_policy.py` |
| CSRF tokens | Bound to the user and the exact signed session cookie | `test_csrf_security.py` |

## Adding a job, cache or lock

```python
from tenant_isolation import OwnedJob, OwnerIdentity, owner_scoped_key, run_owned_job


def sync_for(owner: OwnerIdentity) -> int:
    cursor_key = owner_scoped_key(owner, "connector-cursor", provider)
    ...  # read and write only under ``owner``


outcome = run_owned_job(
    OwnedJob("connector-sync", OwnerIdentity(user["id"])),
    config.database_path,
    sync_for,
)
```

- Take the owner from the `OwnerIdentity` that `run_owned_job` hands to your function, not from the variable you queued.
- Never key shared in-process state (a dict, `lru_cache`, a lock) by anything a client or an environment variable controls. Include the owner.
- Pass `user_id` to every `database.py` helper that accepts it.
- Add a concurrency test that runs two or more owners at the same moment and asserts that neither sees the other's data, credentials or output. `test_tenant_isolation_concurrency.py` shows the pattern: real worker code against a real SQLite file, with only the network edges faked.

## Limits and prerequisites

- `OwnerLocks` are in-process. The `agent` container must run exactly one scheduler process (see the `scheduler-job` skill); running several would need a database-backed lock built on `owner_scoped_key`.
- The staged `health_*` platform (`health_sync_lock`, `health_cursor`, `health_connection`, `health_schema`) is not wired into any production caller yet. It identifies tenants with an integer `user_id`, but `users.id` is text. Whoever wires it must reconcile that type and derive its lock and cursor keys through `owner_scoped_key` before it runs inside a worker.
- `insight_scheduler.py` and `scheduler.run_scheduler` are older loops that the container does not launch (`docker-entrypoint.sh` runs `scheduler.py`, which calls `run_schedule`). Their per-user dispatch ends in `main.run` or `insight_cron`, which re-authorize on entry, but new jobs belong in `run_schedule`.

## Verification

```bash
.venv/bin/python -m pytest -q \
  backend/tests/test_tenant_isolation.py \
  backend/tests/test_tenant_isolation_concurrency.py \
  backend/tests/test_tenant_isolation_policy.py \
  backend/tests/test_tenant_isolation_rate_limit.py \
  backend/tests/test_tenant_isolation_matrix.py \
  backend/tests/test_scheduler.py
python backend/scripts/tenant_isolation_matrix.py --check
```
