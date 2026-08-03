import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'test_report_visibility.py'
_SPEC = importlib.util.spec_from_file_location('test_report_visibility', _MODULE_PATH)
test_report_visibility = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(test_report_visibility)


class TestReportVisibilityServiceTest(unittest.TestCase):
    def test_non_hidden_reports_are_not_restricted_by_hidden_rule(self):
        self.assertTrue(test_report_visibility.can_view_hidden_report(
            {'type': 'user', 'user_id': 1, 'username': 'alice'},
            {'is_hidden': False, 'submitter_type': 'openclaw', 'submitter_claw_id': 2},
            owned_claw_ids=set(),
        ))

    def test_user_can_view_hidden_report_from_owned_agent(self):
        self.assertTrue(test_report_visibility.can_view_hidden_report(
            {'type': 'user', 'user_id': 1, 'username': 'alice'},
            {'is_hidden': True, 'submitter_type': 'openclaw', 'submitter_claw_id': 2},
            owned_claw_ids={2},
        ))

    def test_user_cannot_view_hidden_report_from_other_agent(self):
        self.assertFalse(test_report_visibility.can_view_hidden_report(
            {'type': 'user', 'user_id': 1, 'username': 'alice'},
            {'is_hidden': True, 'submitter_type': 'openclaw', 'submitter_claw_id': 3},
            owned_claw_ids={2},
        ))

    def test_agent_can_only_view_its_own_hidden_report(self):
        self.assertTrue(test_report_visibility.can_view_hidden_report(
            {'type': 'openclaw', 'claw_id': 7},
            {'is_hidden': True, 'submitter_type': 'openclaw', 'submitter_claw_id': 7},
            owned_claw_ids=set(),
        ))
        self.assertFalse(test_report_visibility.can_view_hidden_report(
            {'type': 'openclaw', 'claw_id': 7},
            {'is_hidden': True, 'submitter_type': 'openclaw', 'submitter_claw_id': 8},
            owned_claw_ids=set(),
        ))

    def test_hidden_workflow_report_is_excluded_from_report_list(self):
        caller = {'type': 'user', 'user_id': 1, 'username': 'alice'}
        report = {
            'is_hidden': True,
            'report_type': 'workflow',
            'submitter_type': 'user',
            'submitter_user_id': 1,
        }

        self.assertFalse(test_report_visibility.should_include_in_report_list(
            caller, report, include_hidden=True, owned_claw_ids=set()))

    def test_hidden_non_workflow_report_can_be_included_when_allowed(self):
        caller = {'type': 'user', 'user_id': 1, 'username': 'alice'}
        report = {
            'is_hidden': True,
            'report_type': 'feature_test',
            'submitter_type': 'user',
            'submitter_user_id': 1,
        }

        self.assertTrue(test_report_visibility.should_include_in_report_list(
            caller, report, include_hidden=True, owned_claw_ids=set()))


if __name__ == '__main__':
    unittest.main()
