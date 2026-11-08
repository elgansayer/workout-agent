# Workout Agent

Workout Agent is a multi-user fitness intelligence platform for importing workout and health data, building adaptive training programmes, and delivering AI-assisted coaching. The application uses a FastAPI backend and an Angular frontend, with tenant-scoped storage and per-user integrations.

> Public repository policy: documentation and fixtures must never contain real-user health, identity, credential, or training-profile data. Any example profile or biometric payload must be explicitly synthetic. See [Data classification policy](docs/DATA_CLASSIFICATION.md) and [Public-data history review](docs/PUBLIC_DATA_HISTORY_REVIEW.md).

## Capabilities

- Import workouts and routines from Hevy.
- Build or infer programmes from imported training history.
- Track workout progress and body/recovery metrics.
- Connect supported health providers.
- Configure AI providers and models per user.
  The latest evidence is recorded in the [AI provider parity audit](docs/AI_PROVIDER_PARITY.md).
- Use the web dashboard, Coach, programme builder, history, progress, settings, and check-in views.
- Deliver notifications through supported user-configured channels.

## Architecture

```text
Angular web client
       |
       v
FastAPI application
       |
       +--> tenant-scoped persistence
       +--> Hevy and health connectors
       +--> AI provider abstraction
       +--> scheduled/background processing
       +--> notification adapters
```

The repository currently contains both mature and transitional components. `AGENTS.md` is the engineering contract for contributors and automated coding agents; issue-specific acceptance criteria remain authoritative for feature work.

## Data and tenant safety

Workout Agent handles identifiers, health metrics, workouts, prompts, analytics, and credentials. These classes have different storage, logging, export, and retention requirements.

Core rules:

- Every tenant-owned record must be scoped to the authenticated user.
- Caches, locks, cursors and background jobs must be keyed by an immutable owner identity and re-authorize their owner when they run; see [Tenant isolation](docs/TENANT_ISOLATION.md).
- Accounts have immutable identifiers and a lifecycle status, and production has no shared fallback user: an operation that cannot name its account fails closed. Data written before accounts existed is assigned to a verified owner through an explicit, audited plan. See [User model](docs/USER_MODEL.md).
- Raw credentials must never be logged or committed.
- Raw health, workout, or prompt payloads must not be written to logs.
- Public source documentation and test fixtures must use synthetic data only.
- Example email addresses must use reserved example domains.
- Sensitive examples must be clearly marked as synthetic so they cannot be mistaken for production data.

The executable classification and redaction rules live in `backend/data_classification.py`. Repository-level policy checks live in `backend/scripts/check_data_policy.py` and run in CI.

## Synthetic example programme

The repository includes `current-workout.md` only as a synthetic demonstration of a programme-shaped document. It is not a real person's programme and must remain marked with `synthetic-profile: true`.

Product behaviour must come from each authenticated user's imported routines, preferences, programme state, and connector data rather than from this example.

## Local setup

### Python backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the environment template and provide only the integrations you intend to use:

```bash
cp .env.example .env
```

Never commit `.env` or real credential values.

Run the agent once:

```bash
python main.py
```

For a non-delivery preview where supported:

```bash
python main.py --preview
```

### Web application

The canonical web entrypoint is `webapp.secure_app:app`. It installs the
fail-closed authentication, CSRF, cache, proxy-host, and security-header
boundaries described in `docs/`. Direct `webapp.app:app` imports exist only for
focused unit tests and must not be used for a deployed process.

Every backend process must set `APP_ENV` explicitly. For local read-only
development, opt in to anonymous access deliberately:

```bash
pip install -r requirements.txt -r requirements-web.txt
APP_ENV=development ALLOW_ANONYMOUS_WEB=1 WEB_PUBLIC_URL=http://localhost:8770 \
  uvicorn webapp.secure_app:app --host 127.0.0.1 --port 8770
```

For production, use the Docker image (or `webapp.secure_app:app`) with a complete
Google login configuration and a canonical HTTPS origin. Startup fails closed
when `APP_ENV` is missing or unsupported, when authentication is partial, when
`WEB_AUTH_SECRET` is shorter than 32 characters, or when production omits
`WEB_PUBLIC_URL`. See `.env.example`, `docs/REVERSE_PROXY_SECURITY.md`, and
`docs/CSRF_SECURITY.md`.

### Docker

```bash
docker compose up -d --build
```

Use the compose and deployment files in the repository as the source of truth for services, ports, health checks, volumes, and environment variables.

## Authentication and secrets

Authentication and integration credentials are configured through environment variables and per-user encrypted settings. Depending on enabled features, deployments may use values such as:

- `WEB_AUTH_SECRET`
- `WEB_GOOGLE_CLIENT_ID`
- `WEB_GOOGLE_CLIENT_SECRET`
- `ALLOWED_EMAILS` and `INVITE_ONLY` (who may sign in; see below)
- `HEVY_API_KEY`
- AI-provider credentials
- health-provider OAuth credentials
- notification-provider credentials

Use `.env.example` for the current supported inventory. Never paste real values into documentation, issues, fixtures, tests, screenshots, or commits.

Who may sign in to a deployment is decided by the server, not by the UI: `ALLOWED_EMAILS` restricts sign-in to a list of addresses, `INVITE_ONLY=1` requires an invitation before a new account is created, and a suspended or deleted account cannot sign in or keep using an existing session. See [Account access policy](docs/ACCOUNT_ACCESS_POLICY.md).

Browser sessions use a `Secure`, `HttpOnly`, `SameSite=Lax`, host-only cookie backed by a server-side registry, so they expire after an idle and an absolute limit (`SESSION_IDLE_TIMEOUT_SECONDS`, `SESSION_ABSOLUTE_TIMEOUT_SECONDS`), rotate at every sign-in, and can be revoked per device or for all devices. See [Session security](docs/SESSION_SECURITY.md).

## Health and workout examples

When documentation needs a payload, use obviously synthetic values and mark the example. For example:

```json
{
  "synthetic_profile": true,
  "date": "2030-01-15",
  "sleep_hours": 7.25,
  "weight_kg": 75.0,
  "resting_hr": 60
}
```

Do not copy a real user's exported Hevy, Garmin, Google Health, Health Connect, Fitbit, Apple Health, or other provider payload into the repository, even for debugging. Reduce a bug to the smallest synthetic fixture that reproduces it.

## Testing

Run the repository test suite with:

```bash
pytest
```

The focused data-policy checks can be run with:

```bash
cd backend
python -m pytest -q tests/test_data_classification.py tests/test_data_policy_check.py
python scripts/check_data_policy.py
```

CI is expected to reject newly introduced public-source identity or sensitive-profile leaks covered by the repository policy checker.

## Development workflow

Work is tracked in GitHub Issues. Before implementation:

1. Check open and closed issues and pull requests for overlapping work.
2. Start from the latest `main`.
3. Keep all user-owned reads and writes tenant-scoped.
4. Add deterministic tests for new behaviour and relevant failure paths.
5. Run applicable verification before merging.
6. Keep public docs and fixtures synthetic.

### Autonomous implementation

The OpenHands Agent Canvas on the VPS is the single autonomous scheduler for this
repository. It selects eligible GitHub issues, claims them under a lease, and opens
branches and pull requests. GitHub Actions remain deterministic verification and
never select implementation work.

The queue contract — eligible labels, claim lease and heartbeat, cancellation,
branch naming, pull-request creation and review handling — is documented in
[docs/AUTONOMOUS_QUEUE_CONTRACT.md](docs/AUTONOMOUS_QUEUE_CONTRACT.md) and
implemented in `tools/agent_queue.py`. The ownership boundary is enforced by
`tools/check_scheduler_ownership.py`, which fails when a workflow could select work
or when the document and the executable contract drift apart:

```bash
python -m unittest tools.test_agent_queue tools.test_scheduler_ownership -v
python tools/check_scheduler_ownership.py --root .
```

## Security reporting and history hygiene

Removing sensitive material from the current tree does not remove it from Git history. If personal or secret data is discovered in committed history, follow `docs/PUBLIC_DATA_HISTORY_REVIEW.md` before rewriting history. History rewriting is disruptive and must be coordinated with branch protection, open pull requests, deployments, forks, and local clones.

Credentials found in history must be rotated or revoked regardless of whether history is later rewritten. Personal health or training data does not by itself imply a credential rotation, but it may justify a coordinated history rewrite when deletion from public history is required.
