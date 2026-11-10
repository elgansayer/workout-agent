# Autonomous implementation queue contract

This document is the canonical contract between the OpenHands Agent Canvas on the VPS
and GitHub. It defines which issues the scheduler may select, how a claim is leased
and fenced, how work is cancelled, how branches and pull requests are named and
created, and how review outcomes return work to the queue.

The contract has one executable implementation: `tools/agent_queue.py`. The
scheduler-ownership check `tools/check_scheduler_ownership.py` fails CI when this
document and the executable contract drift apart, or when a GitHub Actions workflow
tries to select implementation work.

## Roles

| Role | Responsibility | Never does |
| --- | --- | --- |
| OpenHands Agent Canvas on the VPS | Selects eligible issues, claims them with a lease, opens branches and pull requests, repairs the branch after review, reacts to review outcomes | Merges its own pull request, approves its own pull request, changes verification results |
| GitHub Actions | Deterministic verification: lint, type-check, unit tests, builds, policy checks | Selects, claims, implements, branches or opens implementation pull requests |
| Human maintainer | Reviews and merges pull requests, decides on blocked work, cancels work | — |

The VPS scheduler is the **single autonomous scheduler**. There is exactly one
selection path, and it lives on the VPS. The retired GitHub-hosted swarm is not
revived: no workflow reacts to issue labels or comments to start implementation.

## Eligible labels

An issue is eligible for autonomous implementation when **all** of the following hold:

1. The issue is open (`state == "open"`).
2. The issue is not a pull request.
3. The issue carries the eligible label `agent:queued`.
4. The issue carries neither `agent:blocked` nor `agent:cancelled`.

The scheduler never applies `agent:queued` itself. Applying it is an explicit human
decision that an issue is ready for autonomous work.

The remaining contract labels track state and are owned by the scheduler:

| Label | Meaning |
| --- | --- |
| `agent:queued` | The issue is eligible and may be selected. |
| `agent:in-progress` | The issue is claimed by a worker under a live lease. |
| `agent:review` | A pull request is open for the issue and awaits review. |
| `agent:blocked` | The issue needs a human decision. It is never selected again. |
| `agent:cancelled` | The issue was cancelled. It is never selected again. |

Ordering is deterministic. Issues are selected by priority label first
(`priority:high`, then `priority:medium`, then `priority:low`, then unlabelled) and
by ascending issue number within the same priority.

Selection and other state-changing commands fail closed when `GITHUB_ACTIONS` is set.
A CI runner can read the queue status but can never select work.

## Claim lease and heartbeat

Every claim is a **lease**, not a lock:

- The default lease is **1800** seconds (30 minutes).
- The default heartbeat interval is **300** seconds (5 minutes).
- The lease must be at least three heartbeat intervals, and the heartbeat interval
  must be shorter than the lease. A policy that violates either rule is rejected
  before any work starts.

A claim is **atomic**. Selection runs inside an immediate SQLite transaction against
the queue ledger, so two workers polling the same ledger can never take the same
issue. The claim records the issue number, title, worker identity, priority, attempt
count, a random lease token and the lease deadline.

A claim **expires safely** after worker failure. When a lease deadline passes, the
item is released back to the queue and becomes selectable again. The stale worker is
fenced: its heartbeat fails once its lease has expired, and it cannot resurrect a
claim that another worker may already hold.

A heartbeat renews the lease of the current holder. It succeeds only when the
issue number, worker identity and lease token all match **and** the lease has not
yet expired. A heartbeat from a different worker, with a stale token, or after
expiry returns no new deadline.

Attempts are bounded by **3** attempts per issue. An item that returns to the queue
with no attempts left is moved to the blocked state instead of being requeued, so a
poisoned issue stops consuming worker time and waits for a human.

The ledger stores repository metadata only: issue numbers, titles, worker
identities, lease bookkeeping and pull-request numbers. It never stores product,
tenant, health or credential data.

## Cancellation

Cancellation is terminal and releases any live lease:

- A cancelled issue leaves the queue immediately and is never selected again.
- A later registration of the same issue does **not** revive it.
- The scheduler mirrors the cancellation with the `agent:cancelled` label and removes
  `agent:queued` and `agent:in-progress`.

## Branch naming

Implementation branches live in a reserved namespace that no other automation may
write to:

```text
agent/issue-<issue-number>-<slug>
```

The slug is the issue title lowercased, reduced to alphanumeric words joined by
hyphens, and truncated to 48 characters. The branch name is a pure function of the
issue number and title, so the same issue always yields the same branch.

## Pull request creation

The scheduler opens a pull request from the reserved branch into `main`:

- The title is `<type>: <summary> (#<issue-number>)`, where the type is one of
  `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`.
- The body contains the `## Summary`, `## Related Issue`, `## Changes` and
  `## Testing` sections, and links the issue with `Fixes #<issue-number>`.
- The pull request is created from the branch that belongs to the current lease.
- The issue moves to the review state and the scheduler applies `agent:review`.
- Only the holder of a live lease may move the item to the review state, so a
  worker whose lease expired cannot hand the item over.

The scheduler does not merge and does not approve its own pull request. Verification
runs in GitHub Actions, and the merge is a human decision. An item is closed only
from the review state, so a queued or claimed issue can never be silently dropped.

## Review handling

A review outcome is only applied to the pull request the scheduler opened for the
issue. The ledger records that pull-request number when the pull request is
created, and the `review` command fails closed when the supplied pull request does
not match it. An issue without a recorded pull request has no review to apply.

The verification conclusion is read from the pull request's own check rollup on
GitHub. It is never taken from a caller-supplied value, and an unknown or
still-running check is not a success.

Review outcomes map to exactly one queue action:

| Review outcome | Condition | Action |
| --- | --- | --- |
| `CHANGES_REQUESTED` | Attempts remain | Requeue the issue for another attempt. |
| `CHANGES_REQUESTED` | Attempts exhausted | Block the issue for a human. |
| `CLOSED` | Attempts remain | Requeue the issue for another attempt. |
| `CLOSED` | Attempts exhausted | Block the issue for a human. |
| `APPROVED` | Checks succeeded | Await a human merge. The scheduler takes no further action. |
| `APPROVED` | Checks failed or unknown | Hold the issue and re-check. |
| `COMMENTED`, `PENDING`, unknown | Any | Hold the item. |

A blocked or cancelled issue is terminal: it is never selected again, and a later
registration does not revive it.

## Repair

Repair belongs to the same VPS scheduler that claimed the issue:

- A `CHANGES_REQUESTED` or `CLOSED` review requeues the item, and the scheduler
  repairs the existing branch under a fresh lease before reopening the pull
  request.
- Repair never starts a second planner, resolver, reviewer or repair agent. There
  is no GitHub-hosted fallback owner for any of claim, branch, pull request,
  review or repair.
- When the attempt budget is spent the item is blocked for a human decision
  instead of being handed to another automation.

## Scheduler ownership

Selection belongs to the OpenHands Agent Canvas on the VPS. The boundary is enforced
mechanically:

- `tools/agent_queue.py` refuses to run any state-changing command when
  `GITHUB_ACTIONS` is set, so the queue can never be driven from a CI runner.
- `tools/check_scheduler_ownership.py` rejects any workflow that triggers on
  `issues` or `issue_comment`, references a contract label, references the
  reserved branch namespace, or runs a queue command. Both invocation forms are
  recognised — `python tools/agent_queue.py claim` and
  `python -m tools.agent_queue claim` — so the check cannot be side-stepped by
  changing how the CLI is started.
- GitHub Actions stay deterministic verification. They never select an issue, never
  claim work, never create an implementation branch and never open an
  implementation pull request.
- The retired `auto-dispatcher`, planner, resolver, reviewer, hourly, daily, weekly
  and failure-monitor workflow filenames are rejected if restored. Every retained
  workflow declares explicit permissions and concurrency, external actions are
  full-SHA pinned, and actionlint validates workflow syntax and action metadata.

### Queue commands

| Command | Effect | Allowed in GitHub Actions |
| --- | --- | --- |
| `sync` | Registers the eligible issues found on GitHub. | No |
| `claim` | Takes the next eligible issue under a lease. | No |
| `heartbeat` | Renews the lease of the current holder. | No |
| `pr` | Opens the pull request for the current lease. | No |
| `review` | Applies a pull-request review outcome. | No |
| `cancel` | Cancels an item and releases its lease. | No |
| `requeue` | Returns an item to the queue. | No |
| `done` | Closes a reviewed item. | No |
| `status` | Reads the ledger. | Yes |

Run the checks locally with:

```bash
python -m unittest tools.test_agent_queue tools.test_scheduler_ownership -v
python tools/check_scheduler_ownership.py --root .
```

The scheduler runs the queue commands from the repository root:

```bash
# Register eligible issues from GitHub.
python tools/agent_queue.py sync --repo owner/name

# Take the next eligible issue under a lease.
python tools/agent_queue.py claim --worker openhands-canvas --apply-labels

# Renew the lease while working.
python tools/agent_queue.py heartbeat --worker openhands-canvas --issue 846 --token <token>

# Open the pull request for the current lease.
python tools/agent_queue.py pr --issue 846 --worker openhands-canvas --token <token> \
  --summary "Define the OpenHands VPS as the single autonomous scheduler" --type docs

# Apply a review outcome. The pull request must be the one the scheduler
# opened for the issue, and the checks conclusion is read from GitHub.
python tools/agent_queue.py review --issue 846 --pull-request 512 --apply-labels

# Cancel work.
python tools/agent_queue.py cancel --issue 846 --reason "superseded"

# Close a reviewed item after a human merged it.
python tools/agent_queue.py done --issue 846 --apply-labels

# Inspect the queue.
python tools/agent_queue.py status
```

The ledger location is set with `AGENT_QUEUE_DB` and defaults to
`data/agent_queue.db`. The lease, heartbeat and attempt values are set with
`AGENT_QUEUE_LEASE_SECONDS`, `AGENT_QUEUE_HEARTBEAT_SECONDS` and
`AGENT_QUEUE_MAX_ATTEMPTS`.

### Queue environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_QUEUE_DB` | `data/agent_queue.db` | Path to the queue ledger. |
| `AGENT_QUEUE_LEASE_SECONDS` | `1800` | Claim lease duration in seconds. |
| `AGENT_QUEUE_HEARTBEAT_SECONDS` | `300` | Heartbeat interval in seconds. |
| `AGENT_QUEUE_MAX_ATTEMPTS` | `3` | Attempts before an issue is blocked. |
| `AGENT_QUEUE_ELIGIBLE_LABEL` | `agent:queued` | Label that makes an issue eligible. |
| `GITHUB_REPOSITORY` | — | `owner/name` of the repository to work in. |
| `GITHUB_TOKEN` or `GH_TOKEN` | — | Token used to read issues and open pull requests. It is read straight from the environment and is never logged or serialised. |
| `GITHUB_ACTIONS` | — | Set by CI. Its presence disables every state-changing command. |

