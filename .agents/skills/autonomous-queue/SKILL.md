---
name: autonomous-queue
description: 'Work on the autonomous implementation queue that the OpenHands VPS scheduler uses to select GitHub issues. Use when changing eligible labels, claim leases, heartbeats, cancellation, branch naming, pull-request creation, review handling, or the rule that only the VPS scheduler selects work.'
---

# Autonomous Implementation Queue

## Why This Exists

The OpenHands Agent Canvas on the VPS is the **single autonomous scheduler** for this
repository. It picks GitHub issues to implement, claims them under a lease, opens
branches and pull requests, and reacts to review outcomes. GitHub Actions stay
deterministic verification and never select work.

The contract has one executable implementation (`tools/agent_queue.py`) and one
normative document (`docs/AUTONOMOUS_QUEUE_CONTRACT.md`). The
`autonomous-scheduler-ownership` CI workflow fails when those two drift apart or
when a workflow could select implementation work.

## When to Use

- Changing eligible labels, the claim lease, the heartbeat, cancellation, branch
  naming, pull-request creation, or review handling.
- Adding a queue command or a new scheduler capability.
- Debugging why an issue was or was not selected.

## Procedure

1. **Change the contract in one place.** Update `tools/agent_queue.py` first. The
   constants (`CONTRACT_LABELS`, `BRANCH_PREFIX`, `DEFAULT_LEASE_SECONDS`,
   `DEFAULT_HEARTBEAT_SECONDS`, `DEFAULT_MAX_ATTEMPTS`,
   `QUEUE_ENVIRONMENT_VARIABLES`) are the executable contract.
2. **Update the document.** `docs/AUTONOMOUS_QUEUE_CONTRACT.md` must describe the
   new behaviour, and `tools/check_scheduler_ownership.py` verifies the labels, the
   branch namespace, the environment variables and the lease values are all still
   documented. Update both in the same change.
3. **Keep claims atomic and expirable.** A claim runs inside an immediate SQLite
   transaction. A lease that expires must release the item back to the queue, and
   the stale worker must be fenced by the lease token — never let a stalled worker
   resurrect a claim another worker holds.
4. **Never select work from CI.** `guard_not_github_actions()` makes every
   state-changing command fail closed when `GITHUB_ACTIONS` is set. Keep that
   guard on every command that changes queue state, including `heartbeat`.
5. **Never let Actions touch the queue.** The ownership check rejects workflows that
   trigger on `issues`/`issue_comment`, reference a contract label, reference the
   `agent/issue-` branch namespace, or run a queue command. Both invocation forms
   (`python tools/agent_queue.py <command>` and `python -m tools.agent_queue
   <command>`) are recognised. Do not weaken the check to accommodate a workflow.
6. **Do not widen the ledger.** The ledger stores repository metadata only —
   issue numbers, titles, worker ids, lease bookkeeping, pull-request numbers. It
   must never hold product, tenant, health or credential data.
7. **Bind reviews to the recorded pull request.** A review outcome applies only to
   the pull request the scheduler opened for the issue, and the verification
   conclusion is read from GitHub rather than from a caller-supplied value. An
   unknown or failing check holds the item; it never makes it merge-ready.
8. **Add deterministic tests.** `tools/test_agent_queue.py` covers eligibility,
   exclusivity, expiry, heartbeat fencing, cancellation and review handling.
   `tools/test_scheduler_ownership.py` covers the ownership boundary. No test may
   reach the network: inject a transport instead.

## Verification

Run the focused checks, then the repository gate:

```bash
python -m unittest tools.test_agent_queue tools.test_scheduler_ownership -v
python tools/check_scheduler_ownership.py --root .
python -m pytest -q
```

## Gotchas

- The label `agent:queued` is a human decision. The scheduler never applies it, and
  an issue that is cancelled or blocked is never revived by a later registration.
- `APPROVED` means *await a human merge*. The scheduler never merges or approves
  its own pull request.
- A change request requeues the item only while attempts remain. Once the attempt
  budget is spent the item is blocked for a human instead of looping.
- The GitHub token comes from `GITHUB_TOKEN` or `GH_TOKEN` and must never be logged,
  echoed or committed. It is read straight from the process environment, never
  through the queue settings mapping, so it cannot be serialised by accident.
