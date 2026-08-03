import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = (Path(__file__).resolve().parents[1]
                / 'web' / 'app' / 'services' / 'review_visibility.py')
_SPEC = importlib.util.spec_from_file_location('review_visibility', _MODULE_PATH)
review_visibility = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(review_visibility)

can_view_topic = review_visibility.can_view_topic
can_mark_review_nodes = review_visibility.can_mark_review_nodes
normalize_visibility = review_visibility.normalize_visibility
is_project_exempt_review_path = review_visibility.is_project_exempt_review_path


class FakeTopic(object):
    def __init__(self, visibility, author_user_id=None, author_claw_id=None):
        self.visibility = visibility
        self.author_user_id = author_user_id
        self.author_claw_id = author_claw_id


def user_caller(user_id=7, project_ids=(), role='user'):
    return {
        'username': 'alice',
        'user_id': user_id,
        'claw_id': None,
        'role': role,
        'is_admin': role in ('super_admin', 'admin'),
        'project_ids': list(project_ids),
    }


def claw_caller(claw_id=11, project_ids=(3,)):
    return {
        'username': 'xiaoniu',
        'user_id': None,
        'claw_id': claw_id,
        'role': 'user',
        'is_admin': False,
        'project_ids': list(project_ids),
    }


class NormalizeVisibilityTest(unittest.TestCase):
    def test_known_values_pass_through(self):
        for value in ('public_all', 'public', 'project', 'assigned'):
            self.assertEqual(normalize_visibility(value), value)

    def test_unknown_and_empty_fall_back_to_public(self):
        for value in ('', None, 'hub', 'anyone', 'PUBLIC_ALL'):
            self.assertEqual(normalize_visibility(value), 'public')


class ProjectExemptPathTest(unittest.TestCase):
    """项目门禁豁免是本次改造最敏感的面，逐条锁定。"""

    def test_exempts_topic_read_and_reply(self):
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics', 'GET'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/boards', 'GET'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/42', 'GET'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/42/replies', 'POST'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/42/review-mindmap', 'GET'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/42/review-marks', 'GET'))
        self.assertTrue(is_project_exempt_review_path('/api/v1/topics/42/review-marks', 'PUT'))

    def test_exempts_review_case_detail_read(self):
        """脑图就地展开用例步骤，外部评审人必须能读"""
        self.assertTrue(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases/7', 'GET'))

    def test_review_case_prefix_does_not_leak_other_paths(self):
        """前缀豁免必须限定尾段是数字，不能顺带放开别的子路径"""
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases', 'GET'))
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases/7/delete', 'GET'))
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases/abc', 'GET'))
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases/7', 'DELETE'))
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/review-cases/7', 'PUT'))

    def test_does_not_exempt_topic_creation(self):
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics', 'POST'))

    def test_does_not_exempt_destructive_or_admin_actions(self):
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/42', 'DELETE'))
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/42/close', 'POST'))
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/42/grants', 'POST'))
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/42/review-status', 'POST'))

    def test_does_not_exempt_testcase_library_apis(self):
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/testcase-libraries/5/directory-mindmap', 'GET'))
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/testcase-libraries/5/cases', 'GET'))

    def test_rejects_non_numeric_topic_segment(self):
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/abc', 'GET'))
        self.assertFalse(is_project_exempt_review_path('/api/v1/topics/42x/replies', 'POST'))

    def test_reply_delete_is_not_exempt(self):
        self.assertFalse(is_project_exempt_review_path(
            '/api/v1/topics/42/replies/9', 'DELETE'))


class PublicAllVisibilityTest(unittest.TestCase):
    def test_visible_to_user_without_any_project(self):
        topic = FakeTopic('public_all')
        caller = user_caller(project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, caller_has_project_access=False))

    def test_visible_to_agent_without_project(self):
        topic = FakeTopic('public_all')
        caller = claw_caller(project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, caller_has_project_access=False))

    def test_invisible_to_anonymous(self):
        topic = FakeTopic('public_all')
        self.assertFalse(can_view_topic(None, topic))


class HubVisibilityTest(unittest.TestCase):
    def test_public_requires_project_access(self):
        topic = FakeTopic('public')
        caller = user_caller(project_ids=())
        self.assertFalse(can_view_topic(
            caller, topic, caller_has_project_access=False))
        self.assertTrue(can_view_topic(
            caller, topic, caller_has_project_access=True))


class ProjectVisibilityTest(unittest.TestCase):
    def test_same_project_can_view(self):
        topic = FakeTopic('project')
        caller = user_caller(project_ids=(3, 4))
        self.assertTrue(can_view_topic(caller, topic, topic_project_id=3))

    def test_other_project_cannot_view(self):
        topic = FakeTopic('project')
        caller = user_caller(project_ids=(3, 4))
        self.assertFalse(can_view_topic(caller, topic, topic_project_id=9))

    def test_no_project_user_cannot_view(self):
        topic = FakeTopic('project')
        caller = user_caller(project_ids=())
        self.assertFalse(can_view_topic(
            caller, topic, topic_project_id=9, caller_has_project_access=False))

    def test_topic_without_project_degrades_to_hub_scope(self):
        topic = FakeTopic('project')
        caller = user_caller(project_ids=(3,))
        self.assertTrue(can_view_topic(caller, topic, topic_project_id=None))
        self.assertFalse(can_view_topic(
            caller, topic, topic_project_id=None, caller_has_project_access=False))


class AssignedVisibilityTest(unittest.TestCase):
    def test_granted_caller_can_view_even_without_project(self):
        topic = FakeTopic('assigned')
        caller = user_caller(project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, granted=True, caller_has_project_access=False))

    def test_ungranted_caller_cannot_view_even_with_project(self):
        topic = FakeTopic('assigned')
        caller = user_caller(project_ids=(3,))
        self.assertFalse(can_view_topic(
            caller, topic, granted=False, caller_has_project_access=True))

    def test_author_can_always_view_own_assigned_topic(self):
        topic = FakeTopic('assigned', author_user_id=7)
        caller = user_caller(user_id=7, project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, granted=False, caller_has_project_access=False))

    def test_author_agent_can_view_own_assigned_topic(self):
        topic = FakeTopic('assigned', author_claw_id=11)
        caller = claw_caller(claw_id=11, project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, granted=False, caller_has_project_access=False))

    def test_super_admin_can_view_anything(self):
        topic = FakeTopic('assigned')
        caller = user_caller(role='super_admin', project_ids=())
        self.assertTrue(can_view_topic(
            caller, topic, granted=False, caller_has_project_access=False))


class MarkPermissionTest(unittest.TestCase):
    def test_marking_follows_comment_permission(self):
        topic = FakeTopic('public_all')
        caller = user_caller(project_ids=())
        self.assertTrue(can_mark_review_nodes(
            caller, topic, caller_has_project_access=False))

    def test_unauthorized_viewer_cannot_mark(self):
        topic = FakeTopic('assigned')
        caller = user_caller(project_ids=(3,))
        self.assertFalse(can_mark_review_nodes(caller, topic, granted=False))


if __name__ == '__main__':
    unittest.main()
