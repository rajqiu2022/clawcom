import importlib.util
import sys
import unittest
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


if __name__ == '__main__':
    unittest.main()
