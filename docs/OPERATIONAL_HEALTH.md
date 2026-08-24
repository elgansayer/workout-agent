# Operational health endpoints

The production ASGI entrypoint exposes three operational health surfaces with deliberately different trust boundaries.

## Public probes

`GET /livez` is a shallow process-liveness probe. It returns HTTP 200 with `{"status":"ok"}` without opening the database, resolving an AI provider, reading connector credentials, or making an outbound request. Optional provider outages therefore cannot make liveness fail.

`GET /readyz` is the deployment/readiness probe. It opens the existing SQLite database read-only, verifies a trivial query, and confirms that the minimum current application schema exists. A missing database, inaccessible database, or incomplete migration returns HTTP 503 with only `{"status":"not_ready"}`. Paths, SQL errors, schema names, credentials, connector state, and provider state are intentionally not exposed publicly.

Both public responses use `Cache-Control: no-store`. They are served outside the interactive authentication middleware so container/orchestrator probes work before login, but remain inside the production `SecurityHeadersMiddleware` boundary.

The Compose `web` service uses `/readyz` for its health check. The check uses Python's standard library from inside the container, so no additional curl/wget package is required.

## Authenticated dependency diagnostics

`GET /api/diagnostics/health` requires an authenticated tenant session even when legacy anonymous development mode is otherwise enabled. It returns only safe aggregate state:

- database readiness plus a stable non-secret reason category;
- whether the authenticated user's selected AI provider has local credentials available, without returning any credential value;
- registered/configured connector counts, without reading or returning secret values;
- the current worker-dependency state.

The endpoint does not ping AI or connector providers. External provider availability is optional for serving the application and should be observed by provider/sync telemetry rather than turning an HTTP readiness probe into a slow or rate-limited network dependency.

## Worker dependency policy

The current deployment has a separate scheduled `agent` service sharing SQLite with the web service, but no external queue or worker broker is required to serve an authenticated HTTP request. Diagnostics therefore report `worker_dependencies: not_applicable`. When a durable queue/broker becomes a serving dependency, readiness must add a bounded local dependency check before that dependency is considered production-critical.

## Readiness and migrations

The repository does not yet have a separate canonical migration-version service. Readiness therefore treats the presence of the core tables created by the current `init_db` path as the migration-completion sentinel. The probe is read-only and never creates a missing database or repairs schema. A future migration-version mechanism should replace this table sentinel with an explicit expected schema version while preserving the same public response contract.
