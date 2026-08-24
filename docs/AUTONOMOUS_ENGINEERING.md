# Autonomous Engineering Architecture and Runbook

## Status

This document is the canonical architecture and operating runbook for autonomous software-engineering work in Workout Agent.

**Decision:** OpenHands Agent Canvas running on the VPS is the single autonomous engineering control plane. GitHub is the durable work/review system. GitHub Actions are deterministic verification and delivery infrastructure, not an autonomous software-engineering scheduler.

This decision supersedes the retired GitHub-hosted swarm, including the former auto-dispatcher, AI architect/resolver/reviewer, recurring coding agents, and self-healing issue-generation loops.

## System boundaries

### OpenHands Agent Canvas on the VPS

Owns:

- selecting an eligible GitHub issue;
- checking for overlapping branches and pull requests;
- claiming/resuming work;
- branch creation and implementation;
- code, migration, test, and documentation changes;
- responding to deterministic CI failures;
- responding to review feedback;
- preparing the pull request and updating the same canonical branch;
- deciding when a failed/stalled claim is safe to resume or release.

The VPS control plane may use configured model/provider fallbacks, but those are implementation details of the VPS runtime. They do not move scheduling authority into GitHub Actions.

### GitHub Issues

Issues are the durable work queue and scope contract. An issue is eligible only when its acceptance criteria and dependencies permit implementation. Before work starts, search open and recently closed pull requests and branches. Existing viable work takes precedence over duplicate implementation.

### Git branches and pull requests

One issue has one canonical implementation branch and one canonical pull request unless the original is explicitly superseded. Useful commits must be preserved before superseding a branch.

Pull requests are the review and merge boundary. They must identify the issue, explain material behaviour/safety changes, and record verification actually run.

### GitHub Actions

Allowed responsibilities:

- build and package;
- deterministic unit/integration/E2E tests;
- lint/type/static analysis;
- security and policy checks;
- artifact/SBOM generation;
- deterministic deployment and post-deployment smoke checks;
- status/reporting that does not make open-ended product decisions.

GitHub Actions must not:

- choose the next product issue to implement;
- prompt an LLM to author arbitrary repository changes;
- create broad product backlogs autonomously;
- review/approve its own generated code;
- spawn repair agents in response to CI failure;
- merge its own autonomous implementation by bypassing normal gates;
- recursively trigger a competing agent from bot-created commits/comments.

Retired workflow stubs are permitted only when they are manual-only (`workflow_dispatch`), read-only, and perform no mutation. They exist as explicit retirement records and must not regain schedules, issue/PR triggers, write permissions, secrets, or code-authoring steps.

## Canonical issue lifecycle

1. **Select:** VPS scheduler evaluates the backlog and dependencies.
2. **Deduplicate:** search open/recent PRs and branches for issue number, fingerprint, title, and overlapping implementation.
3. **Claim:** associate the issue with one active VPS task/lease according to scheduler policy.
4. **Branch:** create a dedicated branch from current `main` unless a documented dependency requires otherwise.
5. **Implement:** complete the issue acceptance criteria, including migrations/tests/docs where required.
6. **Verify locally:** run focused tests and the relevant repository verification gate.
7. **Push and verify:** GitHub Actions verifies the exact pushed SHA deterministically.
8. **Open/update PR:** include `Closes #<issue>` when appropriate and record commands/results honestly.
9. **Review/repair:** OpenHands applies review and CI repairs on the same canonical branch.
10. **Merge:** only after required checks and repository review/merge policy permit it.
11. **Recover/close:** release the claim after merge or explicitly supersede the branch while preserving useful work.

## Ownership matrix

| Activity | Responsible system | Must not be delegated to |
| --- | --- | --- |
| Backlog prioritisation and issue selection | VPS OpenHands scheduler | GitHub-hosted swarm |
| Duplicate/overlap check | VPS OpenHands scheduler | recurring agent workflow |
| Branch/implementation | VPS OpenHands Agent Canvas | GitHub Actions LLM job |
| Deterministic tests/security checks | local tooling + GitHub Actions | open-ended agent |
| Review feedback repairs | VPS OpenHands Agent Canvas | self-healing workflow |
| Product/security decision needing human approval | repository maintainer | autonomous merge loop |
| Merge | repository merge policy / authorised maintainer mechanism after gates | code-authoring workflow |
| Failure recovery | VPS task resume/requeue | recursive bot trigger |

## Failure recovery runbook

### VPS worker/process failure

1. Keep GitHub issue, branch, and existing PR as durable state.
2. Allow the scheduler lease/claim to expire or explicitly release it.
3. A replacement VPS worker inspects the existing branch/PR before doing any work.
4. Resume the existing canonical branch if viable.
5. Create a replacement branch only when the original is demonstrably unusable; preserve unique commits and record the supersession in the PR/issue.

### CI failure

1. GitHub Actions records deterministic failure evidence only.
2. The VPS control plane reads the failure and reproduces/diagnoses it.
3. Repair the same branch and push a new SHA.
4. Do not create a new issue for every failed run and do not trigger a GitHub-hosted repair agent.

### Review changes requested

1. Treat the review as input to the existing task.
2. Modify the same canonical branch.
3. Re-run focused verification and required CI.
4. Resolve or respond to review comments with concrete evidence.

### Duplicate work discovered after start

1. Stop adding divergent implementation.
2. Compare the branches/PRs and identify the canonical viable path.
3. Preserve unique useful commits by applying them to the canonical branch where appropriate.
4. Close/supersede the duplicate with an explicit cross-reference.

### GitHub/API outage

Do not create a second coordination channel that can race GitHub state. Retain local/VPS task state and resume against GitHub once authoritative issue/branch/PR state is available.

## Merge and branch policy

- A task does not merge merely because the implementing agent believes it is complete.
- Required deterministic checks must pass on the reviewed head SHA.
- Migrations, security-sensitive changes, provider assumptions, and external prerequisites must be stated explicitly.
- Autonomous code-authoring workflows must never grant themselves a bypass around branch protection/review requirements.
- Branch cleanup occurs only after merge/supersession and after preserving unique work.

## Verification policy

The repository policy checker validates the canonical documentation and retired autonomous workflow boundary:

```bash
python tools/check_openhands_control_plane.py
python -m unittest tools.test_openhands_control_plane -v
```

The corresponding GitHub Actions workflow runs the same deterministic checks for pull requests and `main` changes that affect control-plane policy.

The checker intentionally fails when:

- canonical docs stop declaring the VPS OpenHands control plane;
- ownership of issue selection/implementation becomes ambiguous;
- a retired autonomous workflow gains a non-manual trigger;
- a retired autonomous workflow gains write permissions;
- a retired autonomous workflow stops being explicitly identified as retired.

## Updating this contract

Changes to autonomous-engineering ownership must update all three canonical surfaces together:

- `AGENTS.md` — contributor constitution;
- `.openhands_instructions` — OpenHands bootstrap instructions;
- `docs/AUTONOMOUS_ENGINEERING.md` — architecture and runbook.

A deliberate future architecture change should modify the policy checker and tests in the same reviewed pull request. Do not bypass the checker with a broad exclusion.