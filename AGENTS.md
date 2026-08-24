# AGENTS.md — Engineering Constitution

This file is the authoritative contribution contract for humans and coding agents working on Workout Agent.

## 1. Autonomous control plane

**OpenHands Agent Canvas on the VPS is the single autonomous engineering control plane for this repository.** GitHub Issues are the work queue and GitHub pull requests are the review/merge boundary. GitHub Actions are deterministic verification, packaging, deployment, smoke-test, and status-reporting infrastructure only.

The retired GitHub-hosted swarm, auto-dispatcher, architect, resolver, reviewer, hourly/daily/weekly coding agents, and self-healing issue generator must never be re-enabled as autonomous schedulers or code authors. Their inert workflow stubs may remain only as explicit retirement records.

The canonical lifecycle is:

1. The VPS scheduler chooses an eligible GitHub issue after checking open/recent PRs and branches for overlapping work.
2. OpenHands claims one issue, creates one dedicated branch from current `main`, and keeps the issue/branch/PR relationship unambiguous.
3. The implementing agent changes code, migrations, tests, and docs on that branch.
4. Deterministic local checks and GitHub Actions verify the exact head SHA. Verification workflows never make product decisions or author repairs.
5. OpenHands handles review feedback and repairs on the same canonical branch.
6. A PR is merged only when required checks and review policy permit it. GitHub-hosted AI workflows do not approve or merge their own work.
7. Failure recovery resumes the same issue/branch when viable; it does not spawn a competing swarm task.

See `docs/AUTONOMOUS_ENGINEERING.md` for ownership, failure recovery, and workflow boundaries. The policy is enforced by `tools/check_openhands_control_plane.py`.

## 2. Before starting work

- Read the complete GitHub issue and acceptance criteria.
- Search open and recently closed pull requests, remote branches, and relevant recent commits before implementing. Continue canonical viable work instead of duplicating it.
- Start from current `main` unless the issue explicitly requires another base.
- Read the relevant guide under `.agents/skills/` before changing a recurring subsystem such as routes, migrations, connectors, programme generation, or verification.
- Do not invent missing infrastructure. Confirm packages, tables, columns, routes, environment variables, and external contracts before wiring callers to them.

## 3. Multi-tenancy and security

Workout Agent is a multi-user product. Every user-owned record, cache, cursor, job, connector, secret, export, notification, programme, and derived result must remain tenant scoped.

- New user-owned tables require an immutable `user_id` foreign key and tenant-aware indexes.
- Reads, writes, deletes, exports, jobs, and caches must filter by authenticated user identity. Never trust a client-supplied owner ID as authorization.
- Schema changes must be additive/idempotent unless a documented migration and rollback path requires otherwise.
- Secrets must come from approved environment or encrypted per-user secret storage. Never log, echo, serialize, cache, or commit raw API keys, OAuth tokens, session secrets, or encryption keys.
- Public/anonymously reachable behaviour must fail closed. Do not weaken authentication, authorization, CSRF, cache, proxy, security-header, or route-inventory guards to make a test pass.
- Synthetic fixtures only. Do not commit real health, workout, account, credential, or provider payload data.

## 4. AI provider boundary

User-facing AI features must resolve the authenticated user's configured provider/key/model through the repository provider abstraction. Do not bypass tenant-scoped provider resolution with a shared hard-coded SDK client.

LLM output may explain, summarize, or format validated deterministic domain output, but it must not bypass structured programme, safety, authorization, connector, or persistence validation.

## 5. Engineering standards

- Python: use type hints on new/changed public functions and `from __future__ import annotations` in new modules.
- Use parameterized SQL; never concatenate user input into SQL.
- Catch meaningful exception classes at external boundaries and degrade safely where the product contract allows it.
- Add dependencies only when they are necessary, pin/record them through the repository's dependency policy, and update the relevant requirements/package manifest.
- New environment variables must be documented in the relevant example/configuration documentation.
- Do not leave orphaned replacement modules. A replacement is complete only when the production caller uses it or the issue intentionally delivers a staged foundation with a documented consumer.
- Keep changes scoped to the selected issue; repair directly exposed defects when necessary, but do not silently widen the task into unrelated refactors.

## 6. Verification gate

Do not claim checks that were not run. The exact required commands depend on the affected area, but a normal backend change should include the relevant subset of:

```bash
python -m compileall backend
ruff check backend tests tools
mypy backend
pytest -q
```

Frontend changes should run the repository's deterministic install/test/build commands. Migration, security, connector, programme, accessibility, and deployment changes must also run their focused suites and policy checks.

For autonomous-control-plane documentation or workflow changes, run:

```bash
python tools/check_openhands_control_plane.py
python -m unittest tools.test_openhands_control_plane -v
```

GitHub Actions verifies the pushed head. A failing required gate means the task is not complete. Never delete, skip, soften, or mark a deterministic check optional merely to obtain green CI.

## 7. Pull-request contract

Each implementation PR must:

- identify the source issue (`Closes #…` when appropriate);
- explain the user/operator-visible result and important safety properties;
- include migrations, tests, and documentation required by the change;
- record exact verification commands and actual results;
- call out assumptions, external prerequisites, and intentionally deferred scope;
- avoid containing secrets or real personal data.

Review and repair continue on the same branch unless the work is explicitly superseded. Preserve useful commits before closing or replacing a canonical branch.

## 8. Runtime/product principles

- Prefer typed, versioned domain contracts over implicit JSON blobs or prose-only behaviour.
- Keep provider integrations idempotent, provenance-preserving, bounded, observable, and resilient to partial failure.
- Never fabricate fresh data, successful connectivity, a workout prescription, or provider capability when the underlying state is absent/stale/degraded.
- Accessibility, correctness, privacy, and security are product requirements, not optimization trade-offs.
- The target programme architecture is Hevy-first and dynamic; do not introduce new dependencies on retired static Hybrid Powerbuilding behaviour.

## 9. Ownership summary

| Responsibility | Owner |
| --- | --- |
| Issue eligibility and scheduling | OpenHands Agent Canvas on VPS |
| Issue claim / branch creation | OpenHands Agent Canvas on VPS |
| Implementation and repair | OpenHands Agent Canvas on VPS |
| Deterministic build/test/security checks | GitHub Actions + local tooling |
| Human/product decisions requiring approval | Repository maintainers |
| PR review response | OpenHands on the canonical branch |
| Merge | Repository merge policy / authorized maintainer automation only after gates |
| Failure recovery | Resume/requeue through the VPS control plane; never spawn the retired swarm |

Any document or workflow that contradicts this table is stale and must be corrected rather than followed.