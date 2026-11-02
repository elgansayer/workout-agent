#!/usr/bin/env python3
"""Verify automatic production eligibility against live GitHub evidence."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any


def gh(path: str) -> Any:
    return json.loads(subprocess.check_output(['gh', 'api', path], text=True))


def evaluate(required: list[dict[str, Any]], checks: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> tuple[str, str]:
    if not required:
        return 'failed', 'No required checks are configured'
    for rule in required:
        name = rule['context']
        matches = [c for c in checks if c['name'] == name and (not rule.get('integration_id') or c.get('app', {}).get('id') == rule['integration_id'])]
        status_matches = [s for s in statuses if s['context'] == name] if not rule.get('integration_id') else []
        if matches:
            check = max(matches, key=lambda c: c['id'])
            if check['status'] != 'completed':
                return 'pending', name
            if check['conclusion'] != 'success':
                return 'failed', name
        elif status_matches:
            status = max(status_matches, key=lambda s: s['id'])
            if status['state'] == 'pending':
                return 'pending', name
            if status['state'] != 'success':
                return 'failed', name
        else:
            return 'pending', 'Missing ' + name
    return 'success', 'All required checks passed'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--build-run', required=True, type=int)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    repo = args.repository
    build = gh(f'repos/{repo}/actions/runs/{args.build_run}')
    if (build['head_repository']['full_name'] != repo or build['head_branch'] != 'main'
            or build['head_sha'] != args.sha or build['conclusion'] != 'success'
            or build['path'] != '.github/workflows/build-images.yml'):
        raise SystemExit('Release rejected: image-build identity or result mismatch')
    prs = gh(f'repos/{repo}/commits/{args.sha}/pulls?per_page=100')
    candidates = [p for p in prs if p.get('merged_at') and p['base']['ref'] == 'main' and p['merge_commit_sha'] == args.sha]
    if not candidates:
        raise SystemExit('Release rejected: commit must be a merged PR on main')
    pr = max(candidates, key=lambda p: p['merged_at'])
    reviewed = pr['head']['sha']
    rules = gh(f'repos/{repo}/rules/branches/main')
    required = [check for rule in rules if rule['type'] == 'required_status_checks' for check in rule['parameters']['required_status_checks']]
    # Deterministic CI must succeed on the merge commit. The factory's independent
    # review status belongs to the exact PR head, which branch rules enforced at merge.
    ci_required = [r for r in required if r['context'] != 'factory/independent-review']
    review_required = [r for r in required if r['context'] == 'factory/independent-review']
    if not ci_required or not review_required:
        raise SystemExit('Release rejected: required CI or independent factory review rule is missing')
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if gh(f'repos/{repo}/git/ref/heads/main')['object']['sha'] != args.sha:
            raise SystemExit('Release rejected: a newer main commit superseded this build')
        checks = gh(f'repos/{repo}/commits/{args.sha}/check-runs?filter=latest&per_page=100')['check_runs']
        if len(checks) == 100:
            raise SystemExit('Release rejected: check pagination requires explicit handling')
        statuses = gh(f'repos/{repo}/commits/{args.sha}/status')['statuses']
        review_statuses = gh(f'repos/{repo}/commits/{reviewed}/status')['statuses']
        results = [evaluate(ci_required, checks, statuses), evaluate(review_required, [], review_statuses)]
        for state, reason in results:
            if state == 'failed':
                raise SystemExit('Release rejected: ' + reason)
        if all(state == 'success' for state, _ in results):
            args.output.write_text(json.dumps({'sha': args.sha, 'reviewed_head_sha': reviewed, 'pull_request': pr['number'], 'build_run': args.build_run, 'required_checks': required, 'decision': 'eligible'}, indent=2) + '\n')
            print('Automatic release eligible: current main, successful image build, required CI and independent factory review verified.')
            return
        print('Waiting for ' + ', '.join(reason for state, reason in results if state == 'pending'), flush=True)
        time.sleep(15)
    raise SystemExit('Release rejected: required verification did not complete in time')


if __name__ == '__main__':
    main()
