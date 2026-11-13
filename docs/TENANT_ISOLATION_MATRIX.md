# Tenant-isolation matrix

`backend/tests/test_tenant_matrix.py` proves, for every surface the code exposes, that one signed-in user (Bob) cannot **read**, **change**, **export**, **queue** or **receive** another user's (Alice's) data. It is *generated*: the cells come from the live route table, the ownership registry, the scheduler and the outbound senders, so adding a surface without a cell fails the build instead of quietly leaving it unproven.

It runs the real FastAPI application, repositories, credential vault and scheduler against a real SQLite file holding two valid accounts. Only the AI SDK and Hevy are replaced, by recorders that note which credential they were given and what they were sent. All data is synthetic.

## What is generated

| Surface | Discovered from | Operation |
| --- | --- | --- |
| Every route behind the login boundary | `webapp.app.app.routes`, minus `webapp.auth_boundary.is_public_path` and the single-page-application shell | `GET` is a **read** probe; every other method is a **change** probe |
| Routes that also call a provider, run a job or deliver | the `also=` field of the route's `Attack` | **export**, **queue**, **receive** |
| Every user-owned store | `database.USER_OWNED_TABLES`, confirmed against the live schema | each holds a canary for both accounts |
| Every scheduled job | the kinds in `_run_per_owner(config, "<kind>", ...)` calls in `scheduler.py` | **queue** |
| Every outbound sender | each top-level `send_*` function in production code | **receive** |
| Anything that looks like an export | any route path containing `export` or `download` | **export** |

Every row of every store carries the owner's *canary* (a marker string, a distinctive number, the account's e-mail and its stored credentials), so a leak from any store shows up as text in someone else's response, prompt or notification.

## What each probe proves

- **Read.** Each `GET` route is called as Bob and as Alice. Neither response, nor its headers, may contain the other account's canary; neither may contain a *stored secret*, even the caller's own (credentials are write-only); and nothing a read does may change the other account's rows. Routes that return data must show the caller's own canary, so a pass is never an empty answer.
- **Change.** Bob calls every mutating route, naming Alice's objects (notification, session and connection ids) or claiming her user id in the body, query or nested JSON. Alice's complete row snapshot (every table carrying `user_id`, plus her account row) must be unchanged; an object of Alice's must answer *exactly* like one that never existed; owner claims are refused. **Control:** the same call by Alice succeeds and, where it should, changes her rows.
- **Export.** Whatever leaves the system (AI prompts, Hevy calls) is recorded. The credential used must be the caller's own and the payload must hold only the caller's data; the owner's control call must reach the provider with the owner's key. `main._scope_config_to_user` never lends the server's or another account's connector key, and retained exports live in one directory per owner.
- **Queue.** Each scheduled job runs for every account with that account's credential and data (the prompts and what the job wrote carry no other canary). The Google Health sync presents only the owner's refresh token and rotates only the owner's stored token (the neighbour's is untouched). A deletion queued by Bob and executed by the scheduler erases Bob's data and leaves Alice's identical.
- **Receive.** Notifications, push registrations and model prompts reach only their owner. A push endpoint is a browser profile, so registering it hands the device to the registering account and the database refuses a second owner.

## Run it

```bash
.venv/bin/python -m pytest -q backend/tests/test_tenant_matrix.py
TENANT_MATRIX_REPORT=/tmp/tenant-matrix.md .venv/bin/python -m pytest -q -s \
  backend/tests/test_tenant_matrix.py -k renders
```

The rendered report is build output and is not committed; the code is authoritative.

## Adding a surface

- **A `GET` route**: nothing to do; it is probed automatically. Add it to `READ_REQUESTS` if it needs a path parameter or query, and to `CARRIES_OWNER_DATA` if it returns seeded data.
- **A mutating route**: add an `Attack` in `backend/tests/tenant_matrix.py` with a request the owner can really make. `test_the_matrix_is_complete` fails until you do.
- **A user-owned table**: register it in `database.USER_OWNED_TABLES` and seed a canary-bearing row in `seed_account`.
- **A scheduled job**: add the kind to `_run_job` in the test.
- **An outbound sender**: add a receive probe and an entry in `CHANNELS`.
- **An export route**: add a probe; `test_no_export_route_exists_without_a_probe` fails until you do.

## What building it changed

- **A push endpoint could belong to two accounts.** On a shared browser, the account that left the device kept its registration, so its notifications (which carry coaching and health-derived text) were still delivered to whoever signed in next. `save_push_subscription` now hands the device to the registering account in one transaction, and `idx_push_subscriptions_endpoint` (unique) makes a second owner impossible. See [Database migration safety](DATABASE_MIGRATIONS.md).
- **An AI call with no owner used the shared server key.** `ai_resolver.resolve_provider(user_id=None)` fell back to the server's Gemini key for anyone. It now raises `TenantRequiredError` unless the explicit local single-user mode is on, and rejects a blank id.
- **Readiness checks decrypted secrets.** Setup state and Coach capability read every stored credential in plaintext only to ask "is one configured?". They now call `SecretVault.has_usable_credential`, which answers yes/no inside the vault. `test_credential_boundary_policy.py` fails when production code calls the unaudited `database.save_user_api_key` family, names the `user_api_keys` table or imports the encryption primitives outside the short allow-lists.

## Acceptance criteria for the multi-user foundation (issue #900)

| Criterion | Evidence |
| --- | --- |
| A production user model and migration assign every existing user-scoped record to one explicit owner | [User model](USER_MODEL.md); `legacy_ownership.OWNED_TABLES` plus `NOT_TRANSFERRED_TABLES` must classify every table holding `user_id`, otherwise the assignment refuses to run (`test_legacy_ownership.py`); the matrix seeds every owned store for two accounts |
| Requests, repositories, caches, jobs, sync cursors, AI contexts and secrets require an immutable authenticated identity | Requests: the owner is the signed session, never a parameter (change probes with owner claims). Jobs, caches, cursors and AI contexts: `OwnerIdentity` / `authorize_owner` ([Tenant isolation](TENANT_ISOLATION.md)). AI contexts: `resolve_provider` refuses an ownerless call. Secrets: the vault keys, seals and audits every credential per user |
| Per-user AI and connector credentials are encrypted, write-only, testable, rotatable, revocable and audited | `secret_vault.py`: AES-GCM sealed per user and field; no route returns a stored secret (read and change probes assert it); `verify_stored` / `verify_candidate`; `rotate`; `revoke`; every lifecycle change writes `credential_audit_events` in the same transaction; `test_credential_boundary_policy.py` keeps feature code on that interface |
| A generated tenant-isolation matrix proves user A cannot read, change, export, queue or receive user B data | `backend/tests/test_tenant_matrix.py` (this page) |

## Known limits

- **`ENCRYPTION_KEY` rotation.** Rotating a *credential* is supported; there is still no tool that re-encrypts every stored secret under a new master key. Changing the key makes existing credentials report `key_mismatch` until they are replaced (`docs/OPERATIONS_RUNBOOKS.md`).
- **Signing out does not unregister the browser.** The device is handed over when the next account enables notifications on it. Until then the previous account's registration remains. Unregistering at sign-out needs a front-end change.
- **No user-facing export route exists yet.** The probe gate requires one the moment a route path contains `export` or `download`.
- **The staged `health_*` modules identify tenants with an integer** (`SyncCursor`, `sync_lock_key`, `HealthExport`, `ExportedWorkout`) while `users.id` is text. They have no production caller, so the matrix cannot probe them; whoever wires them must reconcile the type first (see [Tenant isolation](TENANT_ISOLATION.md)).
- **The coaching job** is proven at dispatch (every account is started once, with its own id). What happens inside a full daily run is covered by `test_tenant_isolation_concurrency.py`.
