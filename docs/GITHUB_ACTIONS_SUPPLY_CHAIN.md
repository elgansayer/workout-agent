# GitHub Actions supply-chain policy

## Policy

Every external GitHub Action and reusable workflow referenced by `uses:` must be pinned to a reviewed, immutable 40-character commit SHA. The same line must retain a human-readable upstream version comment, for example:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

Version tags, branches, short SHAs and floating refs such as `@main`, `@v6` or `@latest` are not allowed. An external `docker://` action is also rejected because it is not tied to a reviewed Git commit by this policy.

Repository-relative actions beginning with `./` are the only approved exception. They execute action code from the same reviewed repository revision as the workflow, so they do not introduce an independently mutable external dependency.

The policy applies to workflow files under `.github/workflows/` and composite action definitions under `.github/actions/`.

## Enforcement

`tools/check_actions_pinning.py` performs a fail-closed scan of all workflow and composite-action `uses:` references. It rejects:

- external refs that are not full commit SHAs;
- pinned external refs without an adjacent `# v<version>` comment;
- malformed external refs; and
- external Docker action refs.

`.github/workflows/actions-pinning-policy.yml` runs the deterministic unit tests and repository scan whenever workflow/action policy files change, and on `main` after merge.

Run the same checks locally with:

```bash
python -m unittest tools.test_actions_pinning -v
python tools/check_actions_pinning.py --root .
```

## Updating actions

Dependabot is configured in `.github/dependabot.yml` with the `github-actions` ecosystem and creates weekly update pull requests. Those pull requests carry Dependabot's dependency metadata and upstream release/change information while changing the pinned digest rather than replacing it with a floating tag.

GitHub Actions dependency PRs are deliberately excluded from the repository's automatic Dependabot merge path. The `Auto-Merge Dependabot PRs` workflow records the action name, old/new versions and Dependabot dependency metadata in the workflow summary, then leaves the pull request open for review.

Before merging an action update:

1. Read the Dependabot release notes and changelog for the proposed version.
2. Open the upstream action repository and verify that the proposed full commit SHA belongs to the expected release/tag or release branch.
3. Review relevant upstream security/provenance information and any material runtime changes.
4. Confirm the workflow still has an adjacent version comment matching the reviewed release.
5. Require the `GitHub Actions pinning policy` check and the repository's other applicable CI checks to pass.

This keeps human-readable versions for maintainability while making execution depend only on immutable reviewed commits.
