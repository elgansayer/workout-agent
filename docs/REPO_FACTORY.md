# Repo Factory integration

Repo Factory is the single VPS-hosted autonomous engineering control plane for
this repository. GitHub Actions remains deterministic verification only.

## Admission policy

- Only issues carrying `factory-ready` are admitted.
- At most one new issue is admitted every 3,600 seconds.
- Existing pull request, review and repair work runs before a new issue.
- The instance runs at most one job concurrently.
- Repository architecture generation is disabled during the initial rollout.

## Required merge evidence

Autonomous merge requires literal success from both SHA-scoped checks:

- `CI / required`
- `factory/independent-review`

Skipped, neutral, stale, missing or non-success conclusions block merge. Repo
Factory does not use administrator override. Retired GitHub-hosted autonomous
workflows remain manual-only.

## Local verification profile

```bash
.venv/bin/python tools/check_openhands_control_plane.py
.venv/bin/python -m unittest tools.test_openhands_control_plane -v
.venv/bin/python -m compileall -q backend
.venv/bin/python -m pytest -q
cd frontend
npm ci --legacy-peer-deps
npm run build
npm test -- --watch=false
```

Full-tree Ruff and mypy are not initial merge gates because the current main
baseline is not clean. They must be introduced through a separately measured
baseline migration, not enabled and then softened after Repo Factory activation.
