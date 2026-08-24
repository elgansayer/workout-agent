# Workout Agent

Workout Agent is a multi-user fitness intelligence platform for importing workout and health data, building adaptive training programmes, and delivering AI-assisted coaching. The application uses a FastAPI backend and an Angular frontend, with tenant-scoped storage and per-user integrations.

> Public repository policy: documentation and fixtures must never contain real-user health, identity, credential, or training-profile data. Any example profile or biometric payload must be explicitly synthetic. See [Data classification policy](docs/DATA_CLASSIFICATION.md) and [Public-data history review](docs/PUBLIC_DATA_HISTORY_REVIEW.md).

## Capabilities

- Import workouts and routines from Hevy.
- Build or infer programmes from imported training history.
- Track workout progress and body/recovery metrics.
- Connect supported health providers.
- Configure AI providers and models per user.
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

Install any web-specific dependencies required by the current branch, then start the FastAPI application using the repository's documented runtime entrypoint or Docker Compose configuration.

A typical local backend invocation is:

```bash
pip install -r requirements.txt -r requirements-web.txt
uvicorn webapp.app:app --host 0.0.0.0 --port 8770
```

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
- `HEVY_API_KEY`
- AI-provider credentials
- health-provider OAuth credentials
- notification-provider credentials

Use `.env.example` for the current supported inventory. Never paste real values into documentation, issues, fixtures, tests, screenshots, or commits.

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

Automated implementation uses the current OpenHands-based workflow described by the repository engineering guidance. Do not revive retired agent/swarm implementations.

## Security reporting and history hygiene

Removing sensitive material from the current tree does not remove it from Git history. If personal or secret data is discovered in committed history, follow `docs/PUBLIC_DATA_HISTORY_REVIEW.md` before rewriting history. History rewriting is disruptive and must be coordinated with branch protection, open pull requests, deployments, forks, and local clones.

Credentials found in history must be rotated or revoked regardless of whether history is later rewritten. Personal health or training data does not by itself imply a credential rotation, but it may justify a coordinated history rewrite when deletion from public history is required.
