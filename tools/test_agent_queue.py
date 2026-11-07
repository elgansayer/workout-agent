from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from tools.agent_queue import (
    ACTION_AWAIT_MERGE,
    ACTION_BLOCK,
    ACTION_HOLD,
    ACTION_REQUEUE,
    BLOCKED_LABEL,
    BRANCH_PREFIX,
    CANCELLED_LABEL,
    CLAIMED_LABEL,
    ELIGIBLE_LABEL,
    REVIEW_LABEL,
    STATE_BLOCKED,
    STATE_CANCELLED,
    STATE_CLAIMED,
    STATE_DONE,
    STATE_PENDING,
    STATE_REVIEW,
    Claim,
    GitHubClient,
    QueueError,
    QueueLedger,
    QueuePolicy,
    branch_name,
    checks_conclusion,
    github_token,
    is_eligible_issue,
    label_transitions,
    main,
    priority_of,
    pull_request_body,
    pull_request_title,
    queue_environment,
    review_action,
    slugify,
)


def _issue(
    number: int = 846,
    title: str = "Define the OpenHands VPS as the single autonomous scheduler",
    labels: tuple[str, ...] = (ELIGIBLE_LABEL,),
    state: str = "open",
    pull_request: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": number,
        "title": title,
        "state": state,
        "labels": [{"name": label} for label in labels],
    }
    if pull_request:
        payload["pull_request"] = {"url": "https://example.invalid/pull/1"}
    return payload


class EligibilityTests(unittest.TestCase):
    def test_open_issue_with_eligible_label_is_eligible(self) -> None:
        self.assertTrue(is_eligible_issue(_issue()))

    def test_issue_without_eligible_label_is_not_eligible(self) -> None:
        self.assertFalse(is_eligible_issue(_issue(labels=("enhancement",))))

    def test_closed_issue_is_not_eligible(self) -> None:
        self.assertFalse(is_eligible_issue(_issue(state="closed")))

    def test_pull_request_is_not_eligible(self) -> None:
        self.assertFalse(is_eligible_issue(_issue(pull_request=True)))

    def test_blocked_and_cancelled_issues_are_not_eligible(self) -> None:
        self.assertFalse(
            is_eligible_issue(_issue(labels=(ELIGIBLE_LABEL, BLOCKED_LABEL)))
        )
        self.assertFalse(
            is_eligible_issue(_issue(labels=(ELIGIBLE_LABEL, CANCELLED_LABEL)))
        )

    def test_priority_orders_high_before_medium_before_low(self) -> None:
        self.assertEqual(priority_of(["priority:high"]), 0)
        self.assertEqual(priority_of(["priority:medium"]), 1)
        self.assertEqual(priority_of(["priority:low"]), 2)
        self.assertEqual(priority_of([]), 3)


class BranchAndPullRequestTests(unittest.TestCase):
    def test_branch_name_is_reserved_deterministic_and_git_safe(self) -> None:
        name = branch_name(846, "Define the OpenHands VPS as the single scheduler!")
        self.assertTrue(name.startswith(f"{BRANCH_PREFIX}846-"))
        self.assertEqual(name, branch_name(846, "Define the OpenHands VPS as the single scheduler!"))
        self.assertNotIn(" ", name)
        self.assertNotIn("!", name)

    def test_branch_name_falls_back_for_untitled_issues(self) -> None:
        self.assertTrue(branch_name(1, "???").startswith(f"{BRANCH_PREFIX}1-"))

    def test_slug_is_bounded(self) -> None:
        self.assertLessEqual(len(slugify("word " * 200)), 48)

    def test_pull_request_title_uses_issue_link(self) -> None:
        self.assertEqual(
            pull_request_title("Add the queue contract", 846, "feat"),
            "feat: Add the queue contract (#846)",
        )

    def test_pull_request_title_rejects_unknown_change_type(self) -> None:
        with self.assertRaises(QueueError):
            pull_request_title("Add the queue contract", 846, "wibble")

    def test_pull_request_body_links_the_issue_and_records_sections(self) -> None:
        body = pull_request_body(
            "Add the queue contract",
            846,
            changes=["Added tools/agent_queue.py"],
            testing=["python -m pytest -q tools/test_agent_queue.py"],
        )
        self.assertIn("Fixes #846", body)
        self.assertIn("## Summary", body)
        self.assertIn("## Changes", body)
        self.assertIn("## Testing", body)
        self.assertIn("tools/agent_queue.py", body)

    def test_label_transitions_move_the_issue_through_the_contract_states(self) -> None:
        claimed_add, claimed_remove = label_transitions(STATE_CLAIMED)
        self.assertIn(CLAIMED_LABEL, claimed_add)
        self.assertIn(ELIGIBLE_LABEL, claimed_remove)

        review_add, review_remove = label_transitions(STATE_REVIEW)
        self.assertIn(REVIEW_LABEL, review_add)
        self.assertIn(CLAIMED_LABEL, review_remove)

        cancelled_add, cancelled_remove = label_transitions(STATE_CANCELLED)
        self.assertIn(CANCELLED_LABEL, cancelled_add)
        self.assertIn(ELIGIBLE_LABEL, cancelled_remove)

    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaises(QueueError):
            label_transitions("teleported")


class ReviewHandlingTests(unittest.TestCase):
    def test_change_request_requeues_until_attempts_are_exhausted(self) -> None:
        self.assertEqual(
            review_action("CHANGES_REQUESTED", attempts=1, max_attempts=3), ACTION_REQUEUE
        )
        self.assertEqual(
            review_action("CHANGES_REQUESTED", attempts=3, max_attempts=3), ACTION_BLOCK
        )

    def test_closed_pull_request_requeues_the_item(self) -> None:
        self.assertEqual(review_action("closed", attempts=1), ACTION_REQUEUE)

    def test_approval_with_successful_checks_waits_for_a_human_merge(self) -> None:
        self.assertEqual(
            review_action("APPROVED", checks_conclusion="success"), ACTION_AWAIT_MERGE
        )

    def test_approval_with_failing_checks_is_held(self) -> None:
        self.assertEqual(
            review_action("APPROVED", checks_conclusion="failure"), ACTION_HOLD
        )

    def test_unknown_review_state_is_held(self) -> None:
        self.assertEqual(review_action("COMMENTED"), ACTION_HOLD)
        self.assertEqual(review_action(""), ACTION_HOLD)

    def test_approval_with_unknown_checks_is_held(self) -> None:
        self.assertEqual(review_action("APPROVED"), ACTION_HOLD)
        self.assertEqual(
            review_action("APPROVED", checks_conclusion=None), ACTION_HOLD
        )


class ChecksConclusionTests(unittest.TestCase):
    def test_successful_checks_are_reported_as_success(self) -> None:
        pull_request = {
            "status_check_rollup": [
                {"status": "completed", "conclusion": "success", "name": "ci"},
                {"state": "success", "context": "verify"},
            ]
        }

        self.assertEqual(checks_conclusion(pull_request), "success")

    def test_a_failing_check_is_reported_as_failure(self) -> None:
        pull_request = {
            "status_check_rollup": [
                {"status": "completed", "conclusion": "success", "name": "ci"},
                {"status": "completed", "conclusion": "failure", "name": "lint"},
            ]
        }

        self.assertEqual(checks_conclusion(pull_request), "failure")

    def test_a_running_check_is_not_a_success(self) -> None:
        pull_request = {
            "status_check_rollup": [
                {"status": "in_progress", "conclusion": None, "name": "ci"}
            ]
        }

        self.assertIsNone(checks_conclusion(pull_request))

    def test_missing_rollup_is_unknown(self) -> None:
        self.assertIsNone(checks_conclusion({}))
        self.assertIsNone(checks_conclusion({"status_check_rollup": []}))
        self.assertIsNone(checks_conclusion({"status_check_rollup": "nonsense"}))


class PolicyTests(unittest.TestCase):
    def test_default_policy_is_valid(self) -> None:
        self.assertEqual(QueuePolicy().validate(), [])

    def test_heartbeat_must_be_shorter_than_the_lease(self) -> None:
        problems = QueuePolicy(lease_seconds=300, heartbeat_seconds=300).validate()
        self.assertTrue(any("shorter than lease_seconds" in problem for problem in problems))

    def test_lease_must_cover_three_heartbeats(self) -> None:
        problems = QueuePolicy(lease_seconds=600, heartbeat_seconds=300).validate()
        self.assertTrue(any("three heartbeat intervals" in problem for problem in problems))

    def test_invalid_policy_is_rejected_by_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(QueueError):
            QueueLedger(
                Path(directory) / "queue.db", QueuePolicy(lease_seconds=0)
            )


class QueueLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tempdir.name) / "queue.db"
        self.ledger = QueueLedger(self.path)
        self.addCleanup(self._tempdir.cleanup)

    def _claim(self, worker_id: str, *, now: float | None = None) -> Claim:
        claim = self.ledger.claim(worker_id, now=now)
        self.assertIsNotNone(claim)
        assert claim is not None
        return claim

    def test_claim_is_exclusive_between_workers(self) -> None:
        self.ledger.register(846, "First issue")
        self.ledger.register(847, "Second issue")

        first = self._claim("worker-a")
        second = self._claim("worker-b")

        self.assertNotEqual(first.issue_number, second.issue_number)

    def test_concurrent_claims_never_hand_out_the_same_issue(self) -> None:
        for number in range(900, 905):
            self.ledger.register(number, f"Issue {number}")

        results: list[int] = []
        lock = threading.Lock()

        def _claim(worker: str) -> None:
            ledger = QueueLedger(self.path)
            claim = ledger.claim(worker)
            if claim is not None:
                with lock:
                    results.append(claim.issue_number)

        threads = [threading.Thread(target=_claim, args=(f"worker-{index}",)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 5)
        self.assertEqual(len(set(results)), 5)

    def test_claim_returns_none_when_the_queue_is_empty(self) -> None:
        self.assertIsNone(self.ledger.claim("worker-a"))

    def test_claim_requires_a_worker_identity(self) -> None:
        with self.assertRaises(QueueError):
            self.ledger.claim("  ")

    def test_claim_uses_priority_then_issue_number(self) -> None:
        self.ledger.register(900, "Low", priority=2)
        self.ledger.register(901, "High", priority=0)
        self.ledger.register(902, "High too", priority=0)

        first = self._claim("worker-a")
        second = self._claim("worker-b")
        third = self._claim("worker-c")

        self.assertEqual(first.issue_number, 901)
        self.assertEqual(second.issue_number, 902)
        self.assertEqual(third.issue_number, 900)

    def test_expired_lease_is_released_after_worker_failure(self) -> None:
        self.ledger.register(846, "Worker dies mid-flight")
        claim = self._claim("worker-a")

        after_expiry = claim.lease_expires_at + 1
        recovered = self._claim("worker-b", now=after_expiry)

        self.assertEqual(recovered.issue_number, 846)
        self.assertNotEqual(recovered.lease_token, claim.lease_token)

    def test_heartbeat_renews_a_held_lease(self) -> None:
        self.ledger.register(846, "Long running work")
        claim = self._claim("worker-a")

        renewed = self.ledger.heartbeat(
            846, "worker-a", claim.lease_token, now=claim.lease_expires_at - 60
        )

        self.assertIsNotNone(renewed)
        assert renewed is not None
        self.assertGreater(renewed, claim.lease_expires_at - 60)

    def test_heartbeat_is_fenced_after_the_lease_expired(self) -> None:
        self.ledger.register(846, "Stalled worker wakes up")
        claim = self._claim("worker-a")

        renewed = self.ledger.heartbeat(
            846, "worker-a", claim.lease_token, now=claim.lease_expires_at + 1
        )

        self.assertIsNone(renewed)

    def test_heartbeat_is_fenced_for_a_different_worker(self) -> None:
        self.ledger.register(846, "Impostor worker")
        claim = self._claim("worker-a")

        self.assertIsNone(self.ledger.heartbeat(846, "worker-b", claim.lease_token))
        self.assertIsNone(self.ledger.heartbeat(846, "worker-a", "not-the-token"))

    def test_verify_lease_returns_the_held_claim(self) -> None:
        self.ledger.register(846, "Lease holder")
        claim = self._claim("worker-a")

        verified = self.ledger.verify_lease(846, "worker-a", claim.lease_token)

        self.assertEqual(verified.issue_number, 846)
        self.assertEqual(verified.branch, claim.branch)

    def test_verify_lease_fails_closed_for_a_fenced_worker(self) -> None:
        self.ledger.register(846, "Fenced worker")
        claim = self._claim("worker-a")

        with self.assertRaises(QueueError):
            self.ledger.verify_lease(846, "worker-b", claim.lease_token)

        with self.assertRaises(QueueError):
            self.ledger.verify_lease(846, "worker-a", "not-the-token")

    def test_verify_lease_fails_closed_after_expiry(self) -> None:
        self.ledger.register(846, "Expired lease")
        claim = self._claim("worker-a")

        with self.assertRaises(QueueError):
            self.ledger.verify_lease(
                846, "worker-a", claim.lease_token, now=claim.lease_expires_at + 1
            )

    def test_cancellation_is_terminal_and_releases_the_lease(self) -> None:
        self.ledger.register(846, "Cancelled by the operator")
        claim = self._claim("worker-a")
        self.assertIsNotNone(claim)

        self.assertTrue(self.ledger.cancel(846, reason="superseded"))
        self.assertFalse(self.ledger.cancel(846, reason="again"))

        state = {item["issue_number"]: item["state"] for item in self.ledger.status()}
        self.assertEqual(state[846], STATE_CANCELLED)
        self.assertIsNone(self.ledger.claim("worker-b"))

    def test_cancelled_items_are_not_revived_by_a_later_registration(self) -> None:
        self.ledger.register(846, "Cancelled work")
        self.ledger.cancel(846)

        state = self.ledger.register(846, "Cancelled work")

        self.assertEqual(state, STATE_CANCELLED)

    def test_requeue_returns_an_item_to_the_queue(self) -> None:
        self.ledger.register(846, "Needs another attempt")
        claim = self._claim("worker-a")

        state = self.ledger.requeue(846)

        self.assertEqual(state, STATE_PENDING)
        self.assertEqual(self._claim("worker-b").issue_number, 846)
        self.assertIsNotNone(claim)

    def test_requeue_blocks_the_item_when_attempts_are_exhausted(self) -> None:
        self.ledger.register(846, "Repeatedly fails")
        self._claim("worker-a")
        self.ledger.requeue(846)
        self._claim("worker-a")
        self.ledger.requeue(846)
        self._claim("worker-a")

        state = self.ledger.requeue(846)

        self.assertEqual(state, STATE_BLOCKED)
        self.assertIsNone(self.ledger.claim("worker-b"))

    def test_requeue_of_an_unknown_item_fails_closed(self) -> None:
        with self.assertRaises(QueueError):
            self.ledger.requeue(4242)

    def test_review_state_requires_the_matching_lease_token(self) -> None:
        self.ledger.register(846, "Ready for review")
        claim = self._claim("worker-a")

        self.assertFalse(self.ledger.mark_review(846, "worker-b", claim.lease_token))
        self.assertTrue(self.ledger.mark_review(846, "worker-a", claim.lease_token, 512))
        state = {item["issue_number"]: item["state"] for item in self.ledger.status()}
        self.assertEqual(state[846], STATE_REVIEW)

    def test_review_state_is_fenced_after_the_lease_expired(self) -> None:
        self.ledger.register(846, "Stalled worker hands over late")
        claim = self._claim("worker-a")

        moved = self.ledger.mark_review(
            846,
            "worker-a",
            claim.lease_token,
            512,
            now=claim.lease_expires_at + 1,
        )

        self.assertFalse(moved)
        state = {item["issue_number"]: item["state"] for item in self.ledger.status()}
        self.assertEqual(state[846], STATE_CLAIMED)

    def test_done_closes_the_item(self) -> None:
        self.ledger.register(846, "Merged")
        claim = self._claim("worker-a")
        self.ledger.mark_review(846, "worker-a", claim.lease_token, 512)

        self.assertTrue(self.ledger.mark_done(846))

        state = {item["issue_number"]: item["state"] for item in self.ledger.status()}
        self.assertEqual(state[846], STATE_DONE)

    def test_done_requires_the_review_state(self) -> None:
        self.ledger.register(846, "Still in progress")

        self.assertFalse(self.ledger.mark_done(846))

        state = {item["issue_number"]: item["state"] for item in self.ledger.status()}
        self.assertEqual(state[846], STATE_PENDING)

    def test_registration_of_only_eligible_issues(self) -> None:
        states = self.ledger.register_issues(
            [
                _issue(846, labels=(ELIGIBLE_LABEL,)),
                _issue(847, labels=("enhancement",)),
                _issue(848, labels=(ELIGIBLE_LABEL,), state="closed"),
                _issue(849, labels=(ELIGIBLE_LABEL,), pull_request=True),
                _issue(850, labels=(ELIGIBLE_LABEL, BLOCKED_LABEL)),
            ]
        )

        self.assertEqual(set(states), {846})

    def test_status_reports_the_ledger_without_leaking_a_token(self) -> None:
        self.ledger.register(846, "Tracked item")
        claim = self._claim("worker-a")

        status = self.ledger.status()

        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["issue_number"], 846)
        self.assertNotIn("lease_token", status[0])
        self.assertNotEqual(status[0].get("lease_token"), claim.lease_token)


class GitHubClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []
        self.responses: dict[tuple[str, str], tuple[int, bytes]] = {}

        def _transport(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
            self.calls.append((method, url, body))
            key = (method, url)
            if key in self.responses:
                return self.responses[key]
            if method == "GET" and "/issues?" in url:
                return 200, json.dumps([_issue(846), _issue(847)]).encode("utf-8")
            if method == "GET" and "/reviews" in url:
                return 200, json.dumps(
                    [{"state": "COMMENTED"}, {"state": "APPROVED"}]
                ).encode("utf-8")
            if method == "POST" and url.endswith("/pulls"):
                return 201, json.dumps({"number": 512}).encode("utf-8")
            return 200, b"{}"

        self.transport = _transport
        self.client = GitHubClient("token", "owner/name", transport=_transport)

    def test_open_issues_are_read_from_the_repository(self) -> None:
        issues = self.client.list_open_issues()

        self.assertEqual([issue["number"] for issue in issues], [846, 847])
        method, url, _ = self.calls[0]
        self.assertEqual(method, "GET")
        self.assertIn("/repos/owner/name/issues", url)

    def test_labels_are_added_and_removed(self) -> None:
        self.client.apply_labels(846, add=(CLAIMED_LABEL,), remove=(ELIGIBLE_LABEL,))

        methods = [method for method, _, _ in self.calls]
        self.assertIn("POST", methods)
        self.assertIn("DELETE", methods)

    def test_pull_request_creation_returns_the_number(self) -> None:
        number = self.client.create_pull_request(
            title="feat: Add the queue contract (#846)",
            body="Fixes #846",
            head="agent/issue-846-queue-contract",
        )

        self.assertEqual(number, 512)

    def test_latest_review_state_is_the_most_recent_submitted_review(self) -> None:
        self.assertEqual(self.client.latest_review_state(512), "APPROVED")

    def test_repository_must_be_owner_and_name(self) -> None:
        with self.assertRaises(QueueError):
            GitHubClient("token", "owner", transport=self.transport)

    def test_token_is_required(self) -> None:
        with self.assertRaises(QueueError):
            GitHubClient("  ", "owner/name", transport=self.transport)

    def test_unexpected_status_fails_closed(self) -> None:
        def _transport(method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
            return 500, b"boom"

        client = GitHubClient("token", "owner/name", transport=_transport)

        with self.assertRaises(QueueError):
            client.list_open_issues()


class _FakeGitHubClient:
    """A GitHub client stand-in that never reaches the network."""

    def __init__(self, review_state: str = "APPROVED", conclusion: str | None = "success") -> None:
        self.review_state = review_state
        self.conclusion = conclusion
        self.opened: list[str] = []
        self.applied: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> int:
        self.opened.append(head)
        return 512

    def latest_review_state(self, pull_request_number: int) -> str:
        return self.review_state

    def pull_request_checks_conclusion(self, pull_request_number: int) -> str | None:
        return self.conclusion

    def apply_labels(
        self,
        issue_number: int,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> None:
        self.applied.append((issue_number, tuple(add), tuple(remove)))


class SecretHandlingTests(unittest.TestCase):
    def test_queue_settings_never_expose_the_github_token(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "super-secret-token"}):
            settings = queue_environment()

        self.assertNotIn("super-secret-token", json.dumps(settings))
        self.assertNotIn("GITHUB_TOKEN", settings)
        self.assertNotIn("GH_TOKEN", settings)

    def test_github_token_is_read_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "token-value"}):
            self.assertEqual(github_token(), "token-value")

    def test_github_token_falls_back_to_gh_token(self) -> None:
        environ = {"GH_TOKEN": "fallback-token"}
        self.assertEqual(github_token(environ), "fallback-token")

    def test_github_token_is_required(self) -> None:
        with self.assertRaises(QueueError):
            github_token({})


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tempdir.name) / "queue.db"
        self.addCleanup(self._tempdir.cleanup)
        self._environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        # GitHub Actions sets GITHUB_ACTIONS for every job, and the queue
        # refuses to change state while it is set. The harness must therefore
        # drop the inherited flag so the commands under test run the way the
        # OpenHands VPS scheduler runs them. The guard itself is exercised
        # explicitly by the tests that opt into it.
        os.environ.pop("GITHUB_ACTIONS", None)

    def _restore_environ(self) -> None:
        os.environ.clear()
        os.environ.update(self._environ)

    def _run(self, *arguments: str) -> tuple[int, dict[str, Any]]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["--db", str(self.path), *arguments])
        payload = stdout.getvalue().strip()
        return code, json.loads(payload) if payload else {}

    def _run_capturing_stderr(self, *arguments: str) -> tuple[int, dict[str, Any], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.path), *arguments])
        payload = stdout.getvalue().strip()
        return code, (json.loads(payload) if payload else {}), stderr.getvalue()

    def _item_in_review(self, pull_request_number: int) -> None:
        ledger = QueueLedger(self.path)
        ledger.register(846, "Contract work")
        claim = ledger.claim("worker-a")
        assert claim is not None
        ledger.mark_review(846, "worker-a", claim.lease_token, pull_request_number)

    def test_the_harness_runs_the_scheduler_outside_github_actions(self) -> None:
        """The suite must not inherit a CI runner that disables the scheduler.

        GitHub Actions sets ``GITHUB_ACTIONS`` for every job and the queue then
        fails closed, so the harness drops the inherited flag. Without that, the
        suite could only ever pass outside CI.
        """

        self.assertNotIn("GITHUB_ACTIONS", os.environ)
        QueueLedger(self.path).register(846, "Contract work")

        code, payload = self._run("claim", "--worker", "worker-a")

        self.assertEqual(code, 0)
        claimed = payload["claimed"]
        assert isinstance(claimed, dict)
        self.assertEqual(claimed["issue_number"], 846)

    def test_claim_command_reports_the_claim(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")

        code, payload = self._run("claim", "--worker", "worker-a")

        self.assertEqual(code, 0)
        claimed = payload["claimed"]
        assert isinstance(claimed, dict)
        self.assertEqual(claimed["issue_number"], 846)
        self.assertTrue(str(claimed["branch"]).startswith(BRANCH_PREFIX))

    def test_claim_command_reports_an_empty_queue(self) -> None:
        code, payload = self._run("claim", "--worker", "worker-a")

        self.assertEqual(code, 0)
        self.assertIsNone(payload["claimed"])

    def test_heartbeat_command_reports_the_renewed_lease(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")
        _, payload = self._run("claim", "--worker", "worker-a")
        claimed = payload["claimed"]
        assert isinstance(claimed, dict)
        token = str(claimed["lease_token"])

        code, heartbeat = self._run("heartbeat", "--worker", "worker-a", "--issue", "846", "--token", token)

        self.assertEqual(code, 0)
        self.assertIsNotNone(heartbeat["lease_expires_at"])

    def test_heartbeat_command_fails_for_an_unknown_lease(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")
        self._run("claim", "--worker", "worker-a")

        code, heartbeat = self._run("heartbeat", "--worker", "worker-a", "--issue", "846", "--token", "wrong")

        self.assertEqual(code, 1)
        self.assertIsNone(heartbeat["lease_expires_at"])

    def test_cancel_command_cancels_the_item(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")
        self._run("claim", "--worker", "worker-a")

        code, payload = self._run("cancel", "--issue", "846", "--reason", "superseded")

        self.assertEqual(code, 0)
        self.assertTrue(payload["cancelled"])

    def test_status_command_lists_the_ledger(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")

        code = main(["--db", str(self.path), "status"])

        self.assertEqual(code, 0)

    def test_selection_commands_fail_closed_inside_github_actions(self) -> None:
        os.environ["GITHUB_ACTIONS"] = "true"
        QueueLedger(self.path).register(846, "Contract work")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--db", str(self.path), "claim", "--worker", "worker-a"])

        self.assertEqual(code, 2)
        self.assertIn("single autonomous scheduler", stderr.getvalue())

    def test_status_command_still_works_inside_github_actions(self) -> None:
        os.environ["GITHUB_ACTIONS"] = "true"

        code = main(["--db", str(self.path), "status"])

        self.assertEqual(code,  0)

    def test_heartbeat_command_fails_closed_inside_github_actions(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")
        _, payload = self._run("claim", "--worker", "worker-a")
        claimed = payload["claimed"]
        assert isinstance(claimed, dict)
        token = str(claimed["lease_token"])
        os.environ["GITHUB_ACTIONS"] = "true"

        code, _, stderr = self._run_capturing_stderr(
            "heartbeat", "--worker", "worker-a", "--issue", "846", "--token", token
        )

        self.assertEqual(code, 2)
        self.assertIn("single autonomous scheduler", stderr)

    def test_review_command_rejects_a_pull_request_of_another_issue(self) -> None:
        self._item_in_review(512)

        code, _, stderr = self._run_capturing_stderr(
            "review", "--issue", "846", "--pull-request", "513", "--repo", "owner/name"
        )

        self.assertEqual(code, 2)
        self.assertIn("does not belong to issue #846", stderr)

    def test_review_command_rejects_an_item_without_a_pull_request(self) -> None:
        QueueLedger(self.path).register(846, "Contract work")

        code, _, stderr = self._run_capturing_stderr(
            "review", "--issue", "846", "--pull-request", "512", "--repo", "owner/name"
        )

        self.assertEqual(code, 2)
        self.assertIn("no pull request yet", stderr)

    def test_review_command_holds_when_the_checks_are_unknown(self) -> None:
        self._item_in_review(512)

        with mock.patch(
            "tools.agent_queue.github_client",
            return_value=_FakeGitHubClient(conclusion=None),
        ):
            code, payload = self._run(
                "review", "--issue", "846", "--pull-request", "512", "--repo", "owner/name"
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["action"], ACTION_HOLD)
        self.assertIsNone(payload["checks_conclusion"])

    def test_review_command_awaits_a_human_merge_when_the_checks_succeeded(self) -> None:
        self._item_in_review(512)

        with mock.patch(
            "tools.agent_queue.github_client",
            return_value=_FakeGitHubClient(conclusion="success"),
        ):
            code, payload = self._run(
                "review", "--issue", "846", "--pull-request", "512", "--repo", "owner/name"
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["action"], ACTION_AWAIT_MERGE)
        self.assertEqual(payload["checks_conclusion"], "success")

    def test_invalid_policy_is_reported_without_a_traceback(self) -> None:
        os.environ["AGENT_QUEUE_LEASE_SECONDS"] = "0"

        code, _, stderr = self._run_capturing_stderr("status")

        self.assertEqual(code, 2)
        self.assertIn("agent queue error", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_a_fenced_worker_cannot_open_a_pull_request(self) -> None:
        ledger = QueueLedger(self.path)
        ledger.register(846, "Contract work")
        claim = ledger.claim("worker-a")
        assert claim is not None
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE queue_items SET lease_expires_at = ? WHERE issue_number = ?",
            (1, 846),
        )
        connection.commit()
        connection.close()
        client = _FakeGitHubClient()

        with mock.patch("tools.agent_queue.github_client", return_value=client):
            code, _, stderr = self._run_capturing_stderr(
                "pr",
                "--issue",
                "846",
                "--worker",
                "worker-a",
                "--token",
                claim.lease_token,
                "--summary",
                "Contract work",
                "--repo",
                "owner/name",
            )

        self.assertEqual(code, 2)
        self.assertIn("lease expired", stderr)
        self.assertEqual(client.opened, [])


if __name__ == "__main__":
    unittest.main()
