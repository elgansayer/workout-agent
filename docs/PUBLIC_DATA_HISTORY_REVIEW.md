# Public data history review

This review records the remediation decision for issue #768 after personal training and health constraints were found in public source documentation.

## Current-tree finding

The public `README.md` directly attributed a detailed training profile to a named athlete and included joint/toe constraints, body-composition goals, nutrition targets, and other personal training preferences. `current-workout.md` also presented a personalised programme without declaring that the profile was synthetic.

The current-tree remediation is to remove the named/personal profile from `README.md`, convert `current-workout.md` into an explicitly synthetic example, and enforce a repository rule that rejects common forms of real-user identity/health data in public documentation and fixtures.

## History implications

Deleting or replacing a file on `main` does not delete earlier blobs from Git history, forks, caches, or existing clones. The identified personal profile was committed to the repository, so **a coordinated Git-history rewrite is required if the remediation goal includes purging that material from the repository's reachable public history**. This pull request deliberately sanitizes the current tree but does not rewrite shared history.

The connector used for this remediation can inspect the current repository and individual GitHub resources but cannot perform an exhaustive `git log -S`/blob scan across every historical object. A local full-history scan is therefore a required gate before any destructive rewrite so the complete affected commit/path set is known. Do not claim historical erasure until that scan and rewrite have actually completed.

Recommended pre-rewrite review from a full clone:

```bash
git log --all -- README.md current-workout.md
git log -S'Elgan' --all --oneline
git log -S'bad toes' --all --oneline
git rev-list --objects --all > /tmp/workout-agent-objects.txt
```

Also run the repository's approved secret scanner across all refs before executing the rewrite.

## Rewrite and credential decision

- **Current tree:** sanitize immediately. This pull request does that.
- **Historical personal health/training data:** a coordinated rewrite is required to purge the already-committed profile from reachable public Git history. Before rewriting, run the full-history scan above to identify every affected commit/path and use the narrowest `git filter-repo` replacement that removes the material. A rewrite is disruptive and must be announced before execution.
- **Credentials:** the identified incident contains personal health/training information, not credential material. Credential rotation is therefore **not required by this finding**. If the all-ref secret scan discovers an API key, OAuth token, cookie secret, encryption key, webhook secret, or similar credential, revoke/rotate that credential immediately regardless of whether history is rewritten.

## Coordinated rewrite procedure

If the project owner proceeds with public-history purging:

1. Freeze merges and automated branch writers.
2. Capture the exact affected commits/paths from a full-history scan.
3. Back up refs and document the rewrite window.
4. Use `git filter-repo` with the narrowest path/content replacement that removes the sensitive blobs.
5. Force-push rewritten protected refs in a controlled maintenance window.
6. Rebase or recreate open pull requests against rewritten `main`.
7. Invalidate/rebuild deployment artifacts that embed repository content.
8. Ask collaborators to reclone or hard-reset; document that forks/caches may retain old objects.
9. Re-run the all-ref secret scan and the current-tree data policy checker.
10. Rotate/revoke every credential discovered by the secret scan. Do not rotate unrelated credentials merely because personal training data was present.

## Ongoing prevention

`backend/scripts/check_data_policy.py` scans public documentation and fixture/sample paths for high-confidence identity leaks, non-example email addresses, first-person sensitive-profile content, and unmarked sensitive fixtures. The `Data classification policy` workflow runs this check when public documentation, fixtures, or the checker itself changes.

This static check is intentionally conservative. Reviewers must still reject screenshots, exports, issue attachments, copied provider payloads, or other real-user data that a text heuristic cannot reliably identify.
