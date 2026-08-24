# Production release checklist

Production deployment is a separate promotion step from building and publishing container images. A green merge to `main` may publish immutable images, but it does not authorize production rollout.

The canonical promotion path is `.github/workflows/production-release.yml`. It validates release evidence with `backend/release_gate.py`, targets the protected GitHub `production` environment, verifies the exact GHCR images for the current `main` commit, and triggers Portainer only after every gate passes.

`.github/workflows/build-images.yml` is deliberately build-and-publish only. It must never invoke `PORTAINER_WEBHOOK`; this prevents a normal merge or the automation that authored it from bypassing the promotion boundary.

## Required GitHub environment protection

Create a GitHub environment named `production` and configure it with required reviewers who are not the author of the change being promoted. Restrict deployment branches to `main`. Keep `PORTAINER_WEBHOOK` as an environment secret rather than a repository-wide secret.

The workflow also enforces independent delivery in code. It resolves the merged PR associated with the exact current `main` commit, requires an `APPROVED` review on the PR's exact reviewed head SHA from someone other than the change author, and rejects promotion when the workflow operator is the change author. Environment approval is an additional independent control, so an automation cannot silently waive a failed gate by editing or dispatching the same release path.

## Before promotion

- [ ] `head_sha` is the exact current 40-character commit SHA at `origin/main`, not merely an older ancestor.
- [ ] That commit is associated with a merged pull request targeting `main`.
- [ ] The PR's exact final head SHA has at least one GitHub `APPROVED` review from someone other than the change author.
- [ ] Every latest deterministic GitHub check run for the deployed `main` SHA is complete with conclusion `success`. `neutral`, `skipped`, queued, in-progress, cancelled, timed-out, action-required and failed checks block promotion.
- [ ] The GHCR `sha-<short-sha>` images exist for both `workout-agent-web` and `workout-agent`.
- [ ] Each image's `latest` tag resolves to the same immutable SHA-256 digest as the exact-main SHA tag. The workflow derives and records these digests rather than trusting operator-supplied values.
- [ ] A production configuration version is recorded.
- [ ] Database migration intent is explicit. Use `none` only when the release genuinely has no schema/data migration.
- [ ] Migration and rollback commands have been reviewed against the exact release.
- [ ] Backup status is `verified`, including restore readiness for any release that changes schema or persisted data.
- [ ] A security review result is recorded, including accepted risk or explicitly stating that there are no unresolved release-blocking findings.
- [ ] Candidate smoke tests have already passed and an evidence reference is recorded.
- [ ] A post-deploy production smoke-test plan is recorded for authentication, readiness/health, tenant isolation, key product routes, workers and affected connectors.
- [ ] Pre-release verification results or an evidence reference are recorded.
- [ ] A rollback command is known before promotion and refers to an immutable prior image/configuration where applicable.

## Running the release workflow

Open **Actions → Production release → Run workflow** and provide:

- `head_sha`: exact current `main` commit SHA to deploy;
- `config_version`: operator-visible production configuration version;
- `migration_plan`: reviewed migration intent and sequencing;
- `database_migration`: exact migration identifier/command or `none`;
- `backup_status`: must be `verified`;
- `security_review`: concise evidence/result, not secrets;
- `smoke_test_status`: must be `passed`;
- `smoke_test_evidence`: durable reference to the candidate smoke-test result;
- `smoke_test_plan`: exact post-deploy production checks to run;
- `verification_results`: concise pre-release verification summary or durable reference;
- `rollback_command`: executable rollback command with no secrets embedded.

Do not place credentials, OAuth codes, raw health data, session cookies, provider tokens or API keys in workflow inputs. Inputs are retained in GitHub workflow history.

Image digests are intentionally not workflow inputs. The release job authenticates to GHCR, resolves the immutable SHA tag for the exact current `main` commit, verifies `latest` points at the same digest, and records the resulting web and agent SHA-256 digests in release evidence.

## Automated gate behaviour

The workflow fails before deployment when any of the following is true:

1. `head_sha` is malformed, cannot be checked out, or is not exactly current `origin/main`;
2. GitHub cannot associate that SHA with a merged pull request to `main`;
3. the PR's exact final head SHA has no independent approved review;
4. the release operator authored the pull request;
5. any latest check run for the deployment SHA is absent, pending or not `success`;
6. the exact-main SHA image is unavailable or `latest` points at a different digest;
7. backup status is not explicitly `verified`;
8. candidate smoke tests are not explicitly `passed` or their evidence reference is missing;
9. migration, configuration, security, verification, post-deploy smoke-test or rollback evidence is missing;
10. the protected `production` environment does not approve the deployment;
11. the `PORTAINER_WEBHOOK` environment secret is unavailable.

There is no `continue-on-error` path around evidence validation or deployment. A failed gate must be corrected and the workflow rerun. Do not weaken the workflow or validator as part of the release being promoted.

## Deployment record

Each successful promotion emits `release-record.json` and uploads it as a 90-day `production-release-record-<sha>` artifact. The record contains:

- deployed current-main SHA, reviewed PR-head SHA and merged PR number;
- change author, approving reviewer and release operator;
- UTC release timestamp and workflow-run URL;
- immutable web and agent image digests resolved from GHCR;
- configuration version;
- database migration identifier/command and migration plan;
- backup status and security review;
- candidate smoke-test status/evidence and the production smoke-test plan;
- pre-release verification results;
- rollback command;
- deterministic check-run names and conclusions used by the gate;
- confirmation that the protected Portainer deployment webhook accepted the request.

Retain release records alongside operational evidence so an incident responder can answer exactly what was promoted, by whom, under which configuration and with which verification evidence.

## Post-deploy verification

After Portainer accepts the webhook, execute the recorded production smoke-test plan. At minimum verify:

- readiness/health endpoints;
- authenticated login/session flow;
- an authenticated dashboard/API read for a dedicated test tenant;
- a negative tenant-isolation check;
- affected background jobs/connectors without duplicate side effects;
- no new error-rate or security alerts.

If a production smoke test fails, stop further promotion and run the recorded rollback command. Preserve the failed release evidence and verification output rather than overwriting it with a later successful run.
