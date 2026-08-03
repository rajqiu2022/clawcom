import unittest
import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'project_access.py'
_SPEC = importlib.util.spec_from_file_location('project_access', _MODULE_PATH)
project_access = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(project_access)

is_project_required_path = project_access.is_project_required_path
tapd_member_matches_username = project_access.tapd_member_matches_username


class ProjectAccessTest(unittest.TestCase):
    def test_tapd_member_matches_username_from_common_fields(self):
        self.assertTrue(tapd_member_matches_username({'User': {'email': 'alice@tencent.com'}}, 'alice'))
        self.assertTrue(tapd_member_matches_username({'WorkspaceUser': {'user': 'bob'}}, 'bob'))
        self.assertTrue(tapd_member_matches_username({'nick': 'charlie'}, 'charlie'))
        self.assertFalse(tapd_member_matches_username({'User': {'email': 'someone@tencent.com'}}, 'alice'))

    def test_project_required_path_excludes_public_and_dashboard(self):
        self.assertFalse(is_project_required_path('/'))
        self.assertFalse(is_project_required_path('/login'))
        self.assertFalse(is_project_required_path('/api/v1/auth/me'))
        self.assertFalse(is_project_required_path('/static/css/style.css'))
        self.assertFalse(is_project_required_path('/test-reports/share/abc'))
        self.assertTrue(is_project_required_path('/hub'))
        self.assertTrue(is_project_required_path('/panorama'))
        self.assertTrue(is_project_required_path('/api/v1/openclaws'))


if __name__ == '__main__':
    unittest.main()
