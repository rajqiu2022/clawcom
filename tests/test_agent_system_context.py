import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / 'web' / 'app' / 'services' / 'agent_system_context.py'
)
SPEC = importlib.util.spec_from_file_location('agent_system_context', MODULE_PATH)
agent_system_context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_system_context)


def profile(profile_id=1, *, status='active'):
    value = {
        'id': profile_id,
        'profile_key': 'automation_test_engineer',
        'name': '自动化测试工程师',
        'system_prompt': '负责自动化测试分析与执行。',
        'workflow_config': '先验证，再给结论。',
        'required_skills': [],
        'contract': {},
        'version': 1,
        'status': status,
    }
    return SimpleNamespace(
        status=status,
        to_dict=lambda: dict(value),
    )


def assignment(assignment_id=1, *, primary=False, profile_status='active'):
    post = SimpleNamespace(
        post_key='automation_test_engineer',
        name='自动化测试工程师',
        status='active',
        required_profile_version=1,
        profile=profile(assignment_id, status=profile_status),
    )
    return SimpleNamespace(
        id=assignment_id,
        status='active',
        is_primary=primary,
        profile_version=1,
        post=post,
    )


def rule_link(rule_id, *, enabled=True, applied=True, deleted=False,
              review_status='approved'):
    rule = SimpleNamespace(
        id=rule_id,
        name=f'rule-{rule_id}',
        display_name=f'规则 {rule_id}',
        description='规则说明',
        category='standard',
        scope='global',
        content_template=f'完整规则正文 {rule_id}',
        is_deleted=deleted,
        review_status=review_status,
        updated_at=datetime(2026, 8, 15, 12, 0, 0),
    )
    return SimpleNamespace(enabled=enabled, applied=applied, rule=rule)


class AgentSystemContextTests(unittest.TestCase):
    def setUp(self):
        self.claw = SimpleNamespace(id=11, name='小牛-自动化测试工程师')

    def test_missing_profile_is_warning_not_execution_gate(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [rule_link(19)],
        )

        self.assertIsNone(payload['active_agent_profile'])
        self.assertIn('PROFILE_UNASSIGNED', payload['context_warnings'])
        self.assertFalse(
            payload['system_context']['policy']['profile_required_for_execution'])
        self.assertEqual(
            'continue_with_identity_and_rules',
            payload['system_context']['policy']['missing_profile_behavior'],
        )
        self.assertEqual('完整规则正文 19', payload['rules'][0]['content'])
        self.assertEqual('codex', payload['system_context']['identity']['provider'])

    def test_primary_active_profile_is_selected_deterministically(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [assignment(2), assignment(3, primary=True)],
            [],
        )

        self.assertEqual(3, payload['active_agent_profile']['profile']['id'])
        self.assertTrue(payload['active_agent_profile']['is_primary'])
        self.assertNotIn('PROFILE_UNASSIGNED', payload['context_warnings'])

    def test_disabled_unreviewed_and_deleted_rules_are_not_injected(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [
                rule_link(1),
                rule_link(2, enabled=False),
                rule_link(3, deleted=True),
                rule_link(4, review_status='pending'),
                rule_link(5, applied=False),
            ],
        )

        self.assertEqual([1, 5], [item['id'] for item in payload['rules']])
        self.assertFalse(payload['rules'][1]['applied'])

    def test_inactive_profile_is_treated_as_optional_unassigned(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'hermes',
            [assignment(profile_status='disabled')],
            [],
        )

        self.assertEqual([], payload['agent_profiles'])
        self.assertIn('PROFILE_UNASSIGNED', payload['context_warnings'])

    def test_digest_is_stable_for_the_same_context(self):
        first = agent_system_context.build_agent_system_context(
            self.claw, 'codex', [assignment(primary=True)], [rule_link(2)])
        second = agent_system_context.build_agent_system_context(
            self.claw, 'codex', [assignment(primary=True)], [rule_link(2)])

        self.assertEqual(
            first['system_context_digest'], second['system_context_digest'])
        self.assertEqual(64, len(first['system_context_digest']))


if __name__ == '__main__':
    unittest.main()
