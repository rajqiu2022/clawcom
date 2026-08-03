import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


_MODULE_PATH = (
    Path(__file__).resolve().parents[1] /
    'web' / 'app' / 'services' / 'agent_templates.py'
)
_SPEC = importlib.util.spec_from_file_location('agent_templates_service', _MODULE_PATH)
agent_templates = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_templates)


class AgentTemplatePermissionTest(unittest.TestCase):
    def test_admin_user_can_manage_any_template(self):
        actor = SimpleNamespace(role='admin', id=99)
        self.assertTrue(agent_templates.can_edit_agent_template(
            actor, 'user', template_owner_claw_id=7))

    def test_regular_agent_can_edit_own_template(self):
        actor = SimpleNamespace(role='user', id=7)
        self.assertTrue(agent_templates.can_edit_agent_template(
            actor, 'claw', template_owner_claw_id=7))

    def test_regular_agent_cannot_edit_other_template(self):
        actor = SimpleNamespace(role='user', id=8)
        self.assertFalse(agent_templates.can_edit_agent_template(
            actor, 'claw', template_owner_claw_id=7))

    def test_agent_without_owner_match_cannot_edit(self):
        actor = SimpleNamespace(role='user', id=7)
        self.assertFalse(agent_templates.can_edit_agent_template(
            actor, 'claw', template_owner_claw_id=None))

    def test_regular_agent_can_apply_approved_template_to_self(self):
        actor = SimpleNamespace(role='user', id=11)
        self.assertTrue(agent_templates.can_apply_agent_template(
            actor, 'claw', template_owner_claw_id=7,
            template_status='approved', target_claw_id=11))

    def test_regular_agent_cannot_apply_template_to_other_agent(self):
        actor = SimpleNamespace(role='user', id=11)
        self.assertFalse(agent_templates.can_apply_agent_template(
            actor, 'claw', template_owner_claw_id=7,
            template_status='approved', target_claw_id=12))

    def test_regular_agent_cannot_apply_unapproved_template(self):
        actor = SimpleNamespace(role='user', id=11)
        self.assertFalse(agent_templates.can_apply_agent_template(
            actor, 'claw', template_owner_claw_id=11,
            template_status='pending_review', target_claw_id=11))

    def test_admin_user_can_apply_any_template(self):
        actor = SimpleNamespace(role='admin', id=99)
        self.assertTrue(agent_templates.can_apply_agent_template(
            actor, 'user', template_owner_claw_id=7,
            template_status='draft', target_claw_id=11))

    def test_actor_display_name_supports_user_and_claw(self):
        user = SimpleNamespace(username='admin1', display_name='管理员')
        claw = SimpleNamespace(name='小牛')

        self.assertEqual(agent_templates.agent_template_actor_name(user, 'user'), 'admin1')
        self.assertEqual(agent_templates.agent_template_actor_name(claw, 'claw'), '小牛')


if __name__ == '__main__':
    unittest.main()
