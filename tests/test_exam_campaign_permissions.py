import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'exam_campaigns.py'
_SPEC = importlib.util.spec_from_file_location('exam_campaigns', _MODULE_PATH)
exam_campaigns = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(exam_campaigns)


class ExamCampaignPermissionTest(unittest.TestCase):
    def test_global_campaign_requires_super_admin_or_global_admin_claw(self):
        super_admin = SimpleNamespace(role='super_admin')
        global_admin_claw = SimpleNamespace(role='admin', is_global=True, managed_projects=[])
        project_admin = SimpleNamespace(role='admin', is_global=False, managed_projects=[7])

        self.assertTrue(exam_campaigns.can_launch_campaign(super_admin, 'global'))
        self.assertTrue(exam_campaigns.can_launch_campaign(global_admin_claw, 'global'))
        self.assertFalse(exam_campaigns.can_launch_campaign(project_admin, 'global'))

    def test_project_campaign_requires_managed_project(self):
        project_admin = SimpleNamespace(role='admin', is_global=False, managed_projects=[7])
        member = SimpleNamespace(role='user', managed_projects=[])

        self.assertTrue(exam_campaigns.can_launch_campaign(project_admin, 'project', project_id=7))
        self.assertFalse(exam_campaigns.can_launch_campaign(project_admin, 'project', project_id=8))
        self.assertFalse(exam_campaigns.can_launch_campaign(member, 'project', project_id=7))

    def test_personal_campaign_allowed_for_authenticated_owner_or_agent(self):
        user = SimpleNamespace(role='user', username='alice')
        agent_proxy = SimpleNamespace(role='user', _claw_id=3, _claw_name='AgentA', username='alice')

        self.assertTrue(exam_campaigns.can_launch_campaign(user, 'personal'))
        self.assertTrue(exam_campaigns.can_launch_campaign(agent_proxy, 'personal'))
        self.assertFalse(exam_campaigns.can_launch_campaign(None, 'personal'))

    def test_agent_participation_matches_campaign_scope(self):
        campaign_global = SimpleNamespace(scope='global', project_id=None, target_claw_ids=[])
        campaign_project = SimpleNamespace(scope='project', project_id=7, target_claw_ids=[])
        campaign_personal = SimpleNamespace(scope='personal', project_id=None, target_claw_ids=[3])

        agent = SimpleNamespace(id=3, project_id=7)
        other_agent = SimpleNamespace(id=4, project_id=8)

        self.assertTrue(exam_campaigns.agent_can_participate(campaign_global, other_agent))
        self.assertTrue(exam_campaigns.agent_can_participate(campaign_project, agent))
        self.assertFalse(exam_campaigns.agent_can_participate(campaign_project, other_agent))
        self.assertTrue(exam_campaigns.agent_can_participate(campaign_personal, agent))
        self.assertFalse(exam_campaigns.agent_can_participate(campaign_personal, other_agent))


if __name__ == '__main__':
    unittest.main()
