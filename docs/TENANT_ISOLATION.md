# Tenant isolation for caches, schedulers and background workers

Request handlers prove who owns data through the signed session. Schedulers, jobs and shared in-process state run without one, so a leak there is silent: a cache key without an owner, a credential looked up "for whoever is first", or an id taken from a flag or a queue. `backend/tenant_isolation.py` is the single place that defines how a worker knows its owner, and every recurring job must go through it.

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
10. **Repositories and services take an authenticated owner, not an id.** `authorize_owner(db_path, claim)` returns an `AuthenticatedOwner` (a subclass of `OwnerIdentity`): the identity read back from the `users` row. It is the only way to obtain one - `AuthenticatedOwner(...)` and `dataclasses.replace(owner, user_id=...)` both raise `OwnershipError`, and `run_owned_job` hands one to your work function. `ProgrammeRepository` and `ProgrammeService` call `require_owner(owner)` first, so a `str` (a request value, a queued payload, a flag) or an unverified `OwnerIdentity` is refused at runtime before any SQL runs. New repositories and services follow the same shape and are added to `OWNER_CONTEXT_CLASSES` in `test_owner_context_policy.py`, which keeps them converted. Equality ignores authentication (same account, same value); authority is decided by type.
11. **A client never nominates the owner.** The owner is always the signed session. No route parameter and no request model declares `user_id`, `owner_id`, `account_id`, `tenant_id` and the like (`test_owner_context_policy.py` scans every handler and model), and `mutation_security` additionally rejects a mutation whose body, query or headers claim a different account with a non-enumerating 404.
12. **The database enforces ownership too.** Application filters are not the only line of defence: `init_db()` installs triggers so that, whichever code path writes, every user-scoped row has an owner that exists, an owner that never changes, and references that stay inside the owner's tenant. See [Database migration safety](DATABASE_MIGRATIONS.md).

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
| Programme repository and service | Take an `AuthenticatedOwner`; a bare id is refused before SQL runs | `test_owner_context_policy.py`, `test_programme_domain.py`, `test_idor_matrix.py` |
| Every user-scoped table | Owner exists, owner immutable, same-tenant references (database triggers) | `test_owner_integrity.py`, `test_idor_matrix.py` |

## IDOR matrix

`backend/tests/test_idor_matrix.py` runs the real FastAPI app, real repositories and a real SQLite file with two valid accounts. Alice owns a record in every user-scoped store; Bob tries to reach it. Every probe also proves the same call works for its owner, so a pass cannot be an empty response.

| Operation | What Bob attempts | Proven |
| --- | --- | --- |
| Read | every tenant-data `GET` route, each as Alice and as Bob | Bob never receives Alice's records, e-mail or credential; each owner receives their own |
| Write | name Alice's notification, rotate/revoke/test her credential, write his own data on her date or endpoint, inject her id in the JSON body, query string or nested JSON | refused (404) or limited to Bob's own rows; a snapshot of every row Alice owns is unchanged |
| Export | build or address an export for another account | export envelopes reject another user; retained-export directories are per owner and reject hostile ids |
| Delete | delete/revoke/disconnect-and-purge Alice's notification, session, connection or credential; bulk clear, revoke-others and health-data erasure as Bob | single deletes 404; bulk operations remove only Bob's rows |
| Background jobs | a job queued for a missing or closed owner, a failing neighbour, a forged id, a straggler write after closure | denied without running; one owner's failure never reaches the next; the database refuses the late write |
| Caches | the same cache key (`context_id`) for two owners, per-owner throttle buckets, spoofed `X-Forwarded-For`, scoped-key collisions, `Cache-Control` on every personalised response and refusal | owner-scoped lookups; private, `no-store`; no shared bucket or key |
| Indirect object references | sequential notification ids, session row ids, connection ids, provenance keys, programme/revision ids through the service, and hand-written SQL that points a child row into another user's parent | a foreign id answers exactly like a missing one; the database rejects the cross-tenant reference |

`test_every_route_with_an_object_identifier_has_a_probe` fails when a route with a path parameter is added without an entry in `OBJECT_ROUTES`, so a new object-addressed route cannot ship without an IDOR probe.

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

- The owner-context requirement is enforced for the programme domain (`ProgrammeRepository`, `ProgrammeService`). The older function-style helpers in `database.py` and the other stores still take a `user_id` string; they are called only with the id the signed session produced, `test_tenant_isolation_policy.py` fails when a call omits it, and the database guards above are the backstop. Converting another repository or service means giving its public methods an `owner: AuthenticatedOwner` first parameter, calling `require_owner(owner)`, and adding it to `OWNER_CONTEXT_CLASSES`.
- `PRAGMA foreign_keys` stays off for runtime connections (turning it on would change delete ordering for every erase path). The triggers provide the owner-exists and same-tenant guarantees without it. The legacy adoption tool, which enables foreign keys, is unaffected.

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
  backend/tests/test_owner_context_policy.py \
  backend/tests/test_owner_integrity.py \
  backend/tests/test_idor_matrix.py \
  backend/tests/test_scheduler.py
```
