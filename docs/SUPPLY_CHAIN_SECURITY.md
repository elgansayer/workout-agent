# Supply-chain security evidence

Issue #891 is implemented by `.github/workflows/supply-chain-security.yml`. The workflow runs for pull requests targeting `main`, pushes to `main`, a weekly scheduled audit, and manual dispatches. It produces immutable GitHub Actions artifacts whose names include the exact source commit SHA.

## Coverage

The workflow keeps one canonical scanner per target and finding class so reports do not multiply the same advisory across `pip-audit`, `npm audit`, Trivy, and other overlapping tools.

| Target | Evidence / scanner | Coverage |
| --- | --- | --- |
| Repository filesystem | Trivy filesystem scan | Python manifests, Angular `package-lock.json`, dependency vulnerabilities, licenses, Dockerfiles, IaC and GitHub Actions workflow misconfigurations |
| Git history | Gitleaks | Secrets committed anywhere in reachable Git history (`fetch-depth: 0`) |
| Agent image | Trivy image scan | OS/base-image packages, Python packages, licenses and secrets embedded in the built image |
| Web image | Trivy image scan | OS/base-image packages, Python/Angular runtime contents, licenses and secrets embedded in the built image |
| Repository and images | Trivy CycloneDX output | Machine-readable SBOMs retained with the scan reports |

Trivy is deliberately not used for repository secret scanning because Gitleaks owns that source scope. Gitleaks is deliberately not used for container layers. This avoids duplicate source-secret findings while still checking secrets after the build boundary. Source and image vulnerability reports remain separate because the same package has different remediation and exposure context in each target.

## Evidence and traceability

Every source run uploads `supply-chain-source-<commit-sha>`. It contains:

- `provenance.json`, with the repository, exact source SHA/ref, generation time, and SHA-256 hashes of dependency manifests/lockfiles, Docker build inputs, compose files, the exception policy, policy tooling/tests, and the supply-chain workflow itself;
- `repository.cdx.json`, the repository CycloneDX SBOM;
- `trivy-source.json`, the complete repository vulnerability/license/misconfiguration report.

Each image matrix entry uploads `supply-chain-image-<agent|web>-<commit-sha>` containing its CycloneDX SBOM, complete Trivy JSON report, and the locally built image content digest. The SHA in the artifact name plus `provenance.json` ties evidence back to the reviewed source and `frontend/package-lock.json`. Python currently uses version-constrained `backend/requirements*.txt` manifests rather than a generated lock file; those files are hashed into the same provenance record so the exact dependency inputs are still attributable.

Artifacts are retained for 90 days. The workflow actions themselves are pinned to immutable commit SHAs with release comments, and the Trivy binary version is explicitly set rather than following `latest`.

## Severity policy

The JSON reports retain all severities so security triage has complete evidence. CI enforcement is intentionally narrower and predictable:

- **Committed secrets:** any Gitleaks finding fails the job. Do not suppress a real secret; rotate/revoke it and remove it from reachable history where appropriate.
- **Critical vulnerabilities with an available fix:** fail the source or image gate. `ignore-unfixed` applies only to the gating pass so unfixed findings remain visible in the complete JSON evidence.
- **Critical misconfigurations:** fail the source gate.
- **Forbidden/critical licenses:** fail the relevant source or image gate.
- **High findings:** retain in evidence and open/attach a remediation issue. Triage before the next release; promote to a hard gate when the existing backlog is clean enough to do so without hiding findings.
- **Medium/low/unknown:** retain for trend and dependency-review work; they do not block a release by default.

A failed scanner must be fixed or explicitly risk-accepted. Do not change `exit-code`, remove scanner classes, or add broad path exclusions merely to make CI green.

## Time-bounded exceptions

Trivy exceptions live only in the root `.trivyignore`. Permanent entries are rejected by `tools/supply_chain.py`. Every exception must have a metadata line and an expiry no more than 90 days away:

```text
# owner=@github-handle; tracking=#123; reason=Temporary upstream remediation window
CVE-2099-0001 exp:2026-09-01
```

Rules:

1. `owner` is the GitHub handle responsible for removing the exception.
2. `tracking` is the issue/PR or HTTPS URL containing remediation context.
3. `reason` explains why the finding is not currently remediated.
4. `exp:` is mandatory and may be at most 90 days in the future.
5. Duplicate finding IDs are rejected.
6. Gitleaks has no allowlist in this repository. A committed credential must be remediated rather than accepted indefinitely.

The deterministic unit tests cover valid, permanent, expired, over-long and undocumented exception paths, provenance hashing, required scan boundaries, and immutable action pinning.

## Local verification

The policy/provenance checks require only the Python standard library:

```bash
python tools/supply_chain.py validate-exceptions --file .trivyignore
python -m unittest tools.test_supply_chain
python tools/supply_chain.py provenance \
  --root . \
  --output /tmp/workout-agent-provenance.json \
  --commit-sha "$(git rev-parse HEAD)" \
  --repository elgansayer/workout-agent \
  --source-ref "$(git symbolic-ref -q HEAD || git rev-parse HEAD)"
```

With Trivy v0.70.0 and Gitleaks installed, the corresponding local scans are:

```bash
trivy fs --scanners vuln,misconfig,license --ignorefile .trivyignore .
gitleaks detect --source . --log-opts='--all'
docker build -f Dockerfile -t workout-agent-security-agent:local .
docker build -f Dockerfile.web -t workout-agent-security-web:local .
trivy image --scanners vuln,secret,license --ignorefile .trivyignore workout-agent-security-agent:local
trivy image --scanners vuln,secret,license --ignorefile .trivyignore workout-agent-security-web:local
```

CI is the source of truth for the uploaded SBOM and scan artifacts because it records the GitHub event SHA and artifact identity together.
