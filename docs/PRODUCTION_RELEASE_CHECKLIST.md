# Production release checklist

Production deployment is a separate promotion step from building and publishing container images. A green merge to `main` can publish immutable images, but it does not by itself authorize production rollout.

The canonical promotion path is `.github/workflows/production-release.yml`. It validates release evidence with `backend/release_gate.py`, targets the protected GitHub `production` environment, and triggers Portainer only after every gate passes.

## Required GitHub environment protection

Create a GitHub environment named `production` and configure it with required reviewers who are not the author of the change being promoted. Restrict deployment branches to `main`. Keep `PORTAINER_WEBHOOK` as an environment secret rather than a repository-wide secret when possible.

The workflow also enforces two-person delivery in code: it discovers the merged pull request for the requested head SHA, requires an approved review from someone other than the PR author, and rejects a release when the workflow operator is the PR author. Environment approval remains an independent control so the automation that authored a change cannot silently waive its own failed gate.

## Before promotion

- [ ] The exact 40-character commit SHA being promoted is merged to `main`.
- [ ] The merged pull request has at least one GitHub `APPROVED` review from someone other than the change author.
- [ ] All deterministic GitHub check runs for the SHA are complete and green (`success`, `neutral`, or `skipped`); queued, in-progress, cancelled, timed-out, action-required, or failed checks block promotion.
- [ ] The `workout-agent-web` and `workout-agent` images are identified by immutable `sha256:` digests, not mutable tags such as `latest`.
- [ ] A configuration version is recorded.
- [ ] Database migration intent is explicit. Use `none` only when the release genuinely has no schema/data migration.
- [ ] Migration and rollback commands have been reviewed against the exact release.
- [ ] Backup status is `verified`, including restore-readiness for any release that changes schema or persisted data.
- [ ] A security review result is recorded, including any accepted risk or explicitly stating that there are no unresolved release-blocking findings.
- [ ] A smoke-test plan is recorded for authentication, readiness/health, tenant isolation, key product routes, workers and affected connectors.
- [ ] A rollback command is known before promotion and refers to an immutable prior image/configuration where applicable.

## Running the release workflow

Open **Actions → Production release → Run workflow** and provide:

- `head_sha`: reviewed SHA on `main`;
- `web_image_digest`: immutable digest for the web image;
- `agent_image_digest`: immutable digest for the agent image;
- `config_version`: operator-visible deployment configuration version;
- `migration_plan`: reviewed migration intent and sequencing;
- `database_migration`: exact migration identifier/command or `none`;
- `backup_status`: must be `verified`;
- `security_review`: concise evidence/result, not secrets;
- `smoke_test_plan`: exact post-deploy checks to run;
- `rollback_command`: executable rollback command with no secrets embedded.

Do not place credentials, OAuth codes, raw health data, session cookies, provider tokens, or API keys in workflow inputs. Inputs are retained in GitHub workflow history.

## Automated gate behaviour

The workflow fails before deployment when any of the following is true:

1. the SHA is malformed or is not contained in `origin/main`;
2. GitHub cannot find a merged pull request for that SHA;
3. the pull request has no independent approved review;
4. the release operator authored the pull request;
5. any check run is pending or has a non-green conclusion;
6. backup status is not explicitly `verified`;
7. an image reference is not an immutable SHA-256 digest;
8. migration, configuration, security, smoke-test, or rollback evidence is missing;
9. the protected `production` environment does not approve the deployment;
10. the `PORTAINER_WEBHOOK` environment secret is unavailable.

There is no `continue-on-error` path around the evidence validator or deploy dependency. A failed gate must be corrected and the workflow rerun; do not edit the workflow or weaken validation as part of the release being promoted.

## Deployment record

Each successful workflow emits `release-record.json` and uploads it as the `production-release-record` artifact. The record contains:

- released head SHA and merged PR number;
- change author, approving reviewer, and release operator;
- UTC release timestamp and workflow run URL;
- immutable web/agent image digests;
- configuration version;
- database migration identifier/command;
- migration plan, backup status, security review, smoke-test plan, and rollback command;
- the deterministic check-run names and conclusions used by the gate.

Retain release records alongside operational evidence so an incident responder can answer exactly what was promoted, by whom, under which configuration, and with which verification evidence.

## Post-deploy verification

After Portainer accepts the webhook, execute the recorded smoke-test plan against production. At minimum verify:

- readiness/health endpoints;
- authenticated login/session flow;
- an authenticated dashboard/API read for a dedicated test tenant;
- a negative tenant-isolation check;
- affected background jobs/connectors without duplicate side effects;
- no new error-rate or security alerts.

If a smoke test fails, stop further promotion and run the recorded rollback command. Preserve the failed release record and verification output rather than overwriting it with a later successful run.
