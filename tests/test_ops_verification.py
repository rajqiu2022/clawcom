import importlib.util
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / 'web'
for p in (str(REPO_ROOT), str(WEB_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ov = _load('ops_verification', 'web/app/services/ops_verification.py')


class OpsVerificationTest(unittest.TestCase):
    def test_all_match_verified(self):
        res = ov.compare_expected_actual({'status': 'done'}, {'status': 'done'})
        self.assertTrue(res['verified'])
        self.assertEqual(res['mismatches'], [])

    def test_mismatch_reported(self):
        res = ov.compare_expected_actual(
            {'status': 'done'}, {'status': 'running'})
        self.assertFalse(res['verified'])
        self.assertEqual(res['mismatches'][0]['field'], 'status')
        self.assertEqual(res['mismatches'][0]['actual'], 'running')

    def test_missing_actual_key_is_none(self):
        res = ov.compare_expected_actual({'status': 'done'}, {})
        self.assertFalse(res['verified'])
        self.assertIsNone(res['mismatches'][0]['actual'])

    def test_bool_int_loose_equal(self):
        # True 与 1 归一相等，避免类型差异误判
        res = ov.compare_expected_actual({'enabled': True}, {'enabled': 1})
        self.assertTrue(res['verified'])

    def test_empty_expected_is_verified(self):
        self.assertTrue(ov.compare_expected_actual({}, {'x': 1})['verified'])

    def test_reread_unknown_type_none(self):
        self.assertIsNone(ov.reread_resource('bogus', 1, ['status']))

    def test_requirement_review_verdict_is_supported_resource_type(self):
        self.assertEqual(
            ov.RESOURCE_MODEL_NAMES['requirement_review_verdict'],
            'RequirementReviewVerdict',
        )

    def test_requirement_review_verdict_uses_canonical_payload(self):
        row = SimpleNamespace(
            id=9,
            requirement_item_id=101,
            iteration_id=5,
            review_key='run-7',
            verdict='risk',
            risk_level='high',
            testability='unclear',
            issues_json=[{'field': 'scope', 'message': '不清晰'}],
            summary='需要补充边界',
            reviewer_name='Worker A',
            reviewer_claw_id=3,
            created_at=None,
            updated_at=None,
        )

        actual = ov.resource_to_actual(
            'requirement_review_verdict',
            row,
            ['id', 'issues', 'summary', 'issues_json'],
        )

        self.assertEqual(actual['id'], 9)
        self.assertEqual(actual['issues'][0]['field'], 'scope')
        self.assertEqual(actual['summary'], '需要补充边界')
        self.assertNotIn('issues_json', actual)


if __name__ == '__main__':
    unittest.main()
