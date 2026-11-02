from __future__ import annotations

import unittest

from tools.check_auto_release import evaluate


class AutoReleaseChecks(unittest.TestCase):
    def test_missing_empty_pending_and_wrong_app_never_pass(self) -> None:
        self.assertEqual(evaluate([], [], [])[0], 'failed')
        rule = [{'context': 'CI / required', 'integration_id': 15368}]
        self.assertEqual(evaluate(rule, [], [])[0], 'pending')
        check = {'id': 1, 'name': 'CI / required', 'status': 'completed', 'conclusion': 'success', 'app': {'id': 1}}
        self.assertEqual(evaluate(rule, [check], [])[0], 'pending')
        check['app']['id'] = 15368
        check['status'] = 'in_progress'
        self.assertEqual(evaluate(rule, [check], [])[0], 'pending')

    def test_only_literal_success_from_required_app_passes(self) -> None:
        rule = [{'context': 'tests', 'integration_id': 15368}]
        check = {'id': 1, 'name': 'tests', 'status': 'completed', 'conclusion': 'success', 'app': {'id': 15368}}
        self.assertEqual(evaluate(rule, [check], [])[0], 'success')
        for conclusion in ['failure', 'skipped', 'neutral', 'cancelled', 'timed_out', None]:
            check['conclusion'] = conclusion
            self.assertEqual(evaluate(rule, [check], [])[0], 'failed')

    def test_ci_without_an_explicit_app_cannot_use_legacy_statuses(self) -> None:
        status = {'id': 1, 'context': 'CI / required', 'state': 'success'}
        check = {'id': 2, 'name': 'CI / required', 'status': 'completed', 'conclusion': 'success', 'app': {'id': 15368}}
        for value in [None, 0, -1, True, '15368']:
            self.assertEqual(evaluate([{'context': 'CI / required', 'integration_id': value}], [check], [status])[0], 'failed')
        self.assertEqual(evaluate([{'context': 'CI / required'}], [check], [status])[0], 'failed')

    def test_latest_independent_review_status_is_required(self) -> None:
        rule = [{'context': 'factory/independent-review'}]
        good = {'id': 1, 'context': 'factory/independent-review', 'state': 'success'}
        bad = dict(good, id=2, state='failure')
        self.assertEqual(evaluate(rule, [], [good])[0], 'success')
        self.assertEqual(evaluate(rule, [], [good, bad])[0], 'failed')


if __name__ == '__main__':
    unittest.main()
