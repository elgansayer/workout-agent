# AGENTS.md — Engineering Constitution

This file is the authoritative contribution contract for humans and coding agents working on Workout Agent.

## 1. Before starting work

- Read the complete GitHub issue and acceptance criteria.
- Search open and recently closed pull requests, remote branches, and relevant recent commits before implementing. Continue canonical viable work instead of duplicating it.
- Start from current `main` unless the issue explicitly requires another base.
- Read the relevant guide under `.agents/skills/` before changing a recurring subsystem such as routes, migrations, connectors, programme generation, or verification.
- Do not invent missing infrastructure. Confirm packages, tables, columns, routes, environment variables, and external contracts before wiring callers to them.

## 2. Multi-tenancy and security

Workout Agent is a multi-user product. Every user-owned record, cache, cursor, job, connector, secret, export, notification, programme, and derived result must remain tenant scoped.

- New user-owned tables require an immutable `user_id` foreign key and tenant-aware indexes.
- Reads, writes, deletes, exports, jobs, and caches must filter by authenticated user identity. Never trust a client-supplied owner ID as authorization.
- Schema changes must be additive/idempotent unless a documented migration and rollback path requires otherwise.
- Secrets must come from approved environment or encrypted per-user secret storage. Never log, echo, serialize, cache, or commit raw API keys, OAuth tokens, session secrets, or encryption keys.
- Public/anonymously reachable behaviour must fail closed. Do not weaken authentication, authorization, CSRF, cache, proxy, security-header, or route-inventory guards to make a test pass.
- Synthetic fixtures only. Do not commit real health, workout, account, credential, or provider payload data.

## 3. AI provider boundary

User-facing AI features must resolve the authenticated user's configured provider/key/model through the repository provider abstraction. Do not bypass tenant-scoped provider resolution with a shared hard-coded SDK client.

LLM output may explain, summarize, or format validated deterministic domain output, but it must not bypass structured programme, safety, authorization, connector, or persistence validation.

## 4. Engineering standards

- Python: use type hints on new/changed public functions and `from __future__ import annotations` in new modules.
- Use parameterized SQL; never concatenate user input into SQL.
- Catch meaningful exception classes at external boundaries and degrade safely where the product contract allows it.
- Add dependencies only when they are necessary, pin/record them through the repository's dependency policy, and update the relevant requirements/package manifest.
- New environment variables must be documented in the relevant example/configuration documentation.
- Do not leave orphaned replacement modules. A replacement is complete only when the production caller uses it or the issue intentionally delivers a staged foundation with a documented consumer.
- Keep changes scoped to the selected issue; repair directly exposed defects when necessary, but do not silently widen the task into unrelated refactors.

## 5. Verification gate

Do not claim checks that were not run. The exact required commands depend on the affected area, but a normal backend change should include the relevant subset of:

```bash
python -m compileall backend
ruff check backend tests tools
mypy backend
pytest -q
```

Frontend changes should run the repository's deterministic install/test/build commands. Migration, security, connector, programme, accessibility, and deployment changes must also run their focused suites and policy checks.

GitHub Actions verifies the pushed head. A failing required gate means the task is not complete. Never delete, skip, soften, or mark a deterministic check optional merely to obtain green CI.

## 6. Pull-request contract

Each implementation PR must:

- identify the source issue (`Closes #…` when appropriate);
- explain the user/operator-visible result and important safety properties;
- include migrations, tests, and documentation required by the change;
- record exact verification commands and actual results;
- call out assumptions, external prerequisites, and intentionally deferred scope;
- avoid containing secrets or real personal data.

Review and repair continue on the same branch unless the work is explicitly superseded. Preserve useful commits before closing or replacing a canonical branch.

## 7. Runtime/product principles

- Prefer typed, versioned domain contracts over implicit JSON blobs or prose-only behaviour.
- Keep provider integrations idempotent, provenance-preserving, bounded, observable, and resilient to partial failure.
- Never fabricate fresh data, successful connectivity, a workout prescription, or provider capability when the underlying state is absent/stale/degraded.
- Accessibility, correctness, privacy, and security are product requirements, not optimization trade-offs.
- The target programme architecture is Hevy-first and dynamic; do not introduce new dependencies on retired static Hybrid Powerbuilding behaviour.
