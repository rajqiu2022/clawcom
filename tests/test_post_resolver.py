import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'post_resolver.py'
_SPEC = importlib.util.spec_from_file_location('post_resolver', _MODULE_PATH)
post_resolver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(post_resolver)


class PostResolverTest(unittest.TestCase):
    def test_selects_online_certified_lowest_load_agent(self):
        now = datetime(2026, 8, 1, 10, 0, 0)
        candidates = [
            {
                'claw_id': 1,
                'post_key': 'case_designer',
                'project_id': 9,
                'profile_version': 2,
                'required_profile_version': 2,
                'exam_passed': True,
                'status': 'online',
                'last_activity': now - timedelta(seconds=30),
                'active_steps': 3,
            },
            {
                'claw_id': 2,
                'post_key': 'case_designer',
                'project_id': 9,
                'profile_version': 3,
                'required_profile_version': 2,
                'exam_passed': True,
                'status': 'working',
                'last_activity': now - timedelta(seconds=20),
                'active_steps': 1,
            },
        ]
        result = post_resolver.resolve_post_candidate(
            candidates, post_key='case_designer', project_id=9, now=now)
        self.assertEqual(result['claw_id'], 2)
        self.assertEqual(result['reason'], 'matched')

    def test_filters_wrong_project_old_profile_exam_and_offline_agents(self):
        now = datetime(2026, 8, 1, 10, 0, 0)
        candidates = [
            {'claw_id': 1, 'post_key': 'case_designer', 'project_id': 8,
             'profile_version': 3, 'required_profile_version': 2,
             'exam_passed': True, 'status': 'online', 'last_activity': now},
            {'claw_id': 2, 'post_key': 'case_designer', 'project_id': 9,
             'profile_version': 1, 'required_profile_version': 2,
             'exam_passed': True, 'status': 'online', 'last_activity': now},
            {'claw_id': 3, 'post_key': 'case_designer', 'project_id': 9,
             'profile_version': 3, 'required_profile_version': 2,
             'exam_passed': False, 'status': 'online', 'last_activity': now},
            {'claw_id': 4, 'post_key': 'case_designer', 'project_id': 9,
             'profile_version': 3, 'required_profile_version': 2,
             'exam_passed': True, 'status': 'offline', 'last_activity': now},
        ]
        result = post_resolver.resolve_post_candidate(
            candidates, post_key='case_designer', project_id=9, now=now)
        self.assertIsNone(result['claw_id'])
        self.assertEqual(result['reason'], 'no_available_agent')

    def test_reviewer_excludes_agents_that_produced_reviewed_steps(self):
        now = datetime(2026, 8, 1, 10, 0, 0)
        candidates = [
            {'claw_id': 7, 'post_key': 'reviewer', 'project_id': 9,
             'profile_version': 1, 'required_profile_version': 1,
             'exam_passed': True, 'status': 'online', 'last_activity': now,
             'active_steps': 0},
            {'claw_id': 8, 'post_key': 'reviewer', 'project_id': 9,
             'profile_version': 1, 'required_profile_version': 1,
             'exam_passed': True, 'status': 'online', 'last_activity': now,
             'active_steps': 2},
        ]
        result = post_resolver.resolve_post_candidate(
            candidates,
            post_key='reviewer',
            project_id=9,
            now=now,
            exclude_claw_ids={7},
        )
        self.assertEqual(result['claw_id'], 8)


if __name__ == '__main__':
    unittest.main()
