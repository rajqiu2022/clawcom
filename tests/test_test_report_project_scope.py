import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'test_report_projects.py'
_SPEC = importlib.util.spec_from_file_location('test_report_projects', _MODULE_PATH)
test_report_projects = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(test_report_projects)


class TestReportProjectScopeServiceTest(unittest.TestCase):
    def test_openclaw_report_project_uses_bound_project(self):
        self.assertEqual(
            test_report_projects.resolve_report_project_id(
                {'type': 'openclaw', 'project_id': 6},
                requested_project_id=1,
            ),
            6,
        )

    def test_user_report_project_uses_requested_project(self):
        self.assertEqual(
            test_report_projects.resolve_report_project_id(
                {'type': 'user', 'project_id': None},
                requested_project_id=1,
            ),
            1,
        )

    def test_openclaw_without_project_falls_back_to_request(self):
        self.assertEqual(
            test_report_projects.resolve_report_project_id(
                {'type': 'openclaw', 'project_id': None},
                requested_project_id=1,
            ),
            1,
        )

    def test_openclaw_draft_defaults_hidden(self):
        self.assertTrue(
            test_report_projects.resolve_report_is_hidden(
                {'type': 'openclaw', 'claw_id': 11},
                'draft',
                {},
            )
        )

    def test_openclaw_explicit_is_hidden_false(self):
        self.assertFalse(
            test_report_projects.resolve_report_is_hidden(
                {'type': 'openclaw', 'claw_id': 11},
                'draft',
                {'is_hidden': False},
            )
        )

    def test_user_draft_defaults_visible(self):
        self.assertFalse(
            test_report_projects.resolve_report_is_hidden(
                {'type': 'user', 'user_id': 1},
                'draft',
                {},
            )
        )


if __name__ == '__main__':
    unittest.main()
