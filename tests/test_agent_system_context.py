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

    def test_primary_team_manager_gets_dedicated_runtime_identity(self):
        team = {
            'team_id': 1,
            'name': 'RacingGO Agent团队',
            'objective': '持续版本质量管理',
            'status': 'active',
            'primary_manager_claw_id': self.claw.id,
            'self': {
                'claw_id': self.claw.id,
                'effective_role_key': 'test_manager',
                'manager_kind': 'primary',
                'has_manager_authority': True,
            },
            'plan_supervision': {
                'manager_runtime': {
                    'required_skill_ids': [244],
                    'critical_rules': ['必须真实执行并回读。'],
                },
            },
        }

        payload = agent_system_context.build_agent_system_context(
            self.claw, 'codex', [], [], agent_teams=[team])

        identity = payload['system_context']['identity']
        runtime = payload['system_context']['policy']['manager_runtime']
        self.assertEqual('test_manager', identity['role'])
        self.assertEqual('专用测试经理 / Owner 代理', identity['title'])
        self.assertEqual('持续版本质量管理', identity['objective'])
        self.assertEqual(1, identity['team_id'])
        self.assertEqual('team-manager:1', runtime['session_scope'])
        self.assertEqual([244], runtime['required_skill_ids'])
        self.assertEqual(
            ['wecom_owner', 'team_chat', 'plan_supervisor',
             'formal_delegation'],
            runtime['serialized_channels'])

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

    def test_editor_acl_is_included_in_workflow_create_grants(self):
        definitions = [
            SimpleNamespace(
                id=36,
                status='active',
                owner_type='claw',
                owner_id=99,
                created_by='owner',
                executor_acl_json={'claw_ids': []},
                editor_acl_json={'claw_ids': [11]},
            ),
            SimpleNamespace(
                id=37,
                status='active',
                owner_type='claw',
                owner_id=99,
                created_by='owner',
                executor_acl_json={'claw_ids': [11]},
                editor_acl_json={'claw_ids': []},
            ),
        ]

        self.assertEqual(
            [36, 37],
            agent_system_context.allowed_workflow_create_definition_ids(
                self.claw, definitions),
        )

    def test_codex_orchestrator_is_intersected_with_workflow_acl(self):
        configured_policy = {
            'allowed_workflow_create_definition_ids': [12, 25, 26],
            'codex_orchestrator': {
                'enabled': True,
                'session_key': 'racinggo:flow-orchestrator',
                'resume_on': ['blocked', 'failed', 'timeout'],
                'allowed_next_flows': [12, 25, 26],
                'max_retries': 2,
                'review_success': True,
            },
        }

        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=[12, 25],
            configured_policy=configured_policy,
        )

        policy = payload['system_context']['policy']
        self.assertEqual(
            [12, 25], policy['allowed_workflow_create_definition_ids'])
        self.assertEqual(
            [12, 25], policy['codex_orchestrator']['allowed_next_flows'])
        self.assertIn('CODEX_ORCHESTRATOR_FLOWS_FILTERED',
                      payload['context_warnings'])

    def test_codex_remote_source_ids_are_canonical_and_downlinked(self):
        configured_policy = {
            'allowed_workflow_create_definition_ids': [1, 41],
            'remote_source_id': 'racinggo_server',
            'remote_source_ids': [
                'racinggo_unity', 'racinggo_server'],
        }

        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=[1, 41],
            configured_policy=configured_policy,
        )

        self.assertEqual(
            ['racinggo_server', 'racinggo_unity'],
            payload['system_context']['policy']['remote_source_ids'],
        )

    def test_flow12_worker_bindings_transitions_and_release_gate_are_downlinked(self):
        configured_policy = {
            'allowed_workflow_create_definition_ids': [12, 25, 39],
            'workflow_start_bindings': {
                '12': {
                    'executor_claw_ids': [11],
                    'start_vars': {'reviewer_claw_id': 7},
                },
            },
            'deepflow_release_required_definition_ids': [12, 25],
            'codex_orchestrator': {
                'enabled': True,
                'session_key': 'racinggo:flow-orchestrator',
                'resume_on': ['blocked', 'failed'],
                'allowed_next_flows': [12, 25, 39],
                'transitions': {'12': [25, 39], '25': [12]},
                'max_retries': 2,
                'review_success': True,
            },
        }

        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=[12, 25, 39],
            configured_policy=configured_policy,
        )

        policy = payload['system_context']['policy']
        self.assertEqual(
            [11],
            policy['workflow_start_bindings']['12']['executor_claw_ids'],
        )
        self.assertEqual(
            7,
            policy['workflow_start_bindings']['12']['start_vars'][
                'reviewer_claw_id'
            ],
        )
        self.assertEqual(
            [12, 25], policy['deepflow_release_required_definition_ids']
        )
        self.assertEqual(
            {'12': [25, 39], '25': [12]},
            policy['codex_orchestrator']['transitions'],
        )

    def test_remote_source_policy_rejects_path_or_secret_fields(self):
        for value in (
            ['../ssh'],
            ['racinggo_unity', ''],
            'racinggo_unity',
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    agent_system_context.validate_system_context_policy({
                        'allowed_workflow_create_definition_ids': [1],
                        'remote_source_ids': value,
                    })

    def test_active_mission_adds_autonomous_dispatch_rule(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=[12],
            workflow_missions=[{
                'id': 91,
                'mission_key': 'mission-autonomous-91',
                'project_id': 6,
                'definitions_api': '/api/v1/workflow-missions/91/definitions',
                'dispatch_api': '/api/v1/workflow-missions/91/dispatch',
            }],
        )

        rules = payload['system_context']['rules']
        mission_rule = next(
            item for item in rules
            if item['name'] == 'codex_workflow_mission_autonomy')
        self.assertIn('下一步选择由你自主决策', mission_rule['content'])
        self.assertIn('/api/v1/workflow-missions/91/dispatch',
                      mission_rule['content'])
        self.assertIn('不需要 verify', mission_rule['content'])
        self.assertIn(
            {'id': 91, 'control_mode': 'agent_autonomous'},
            payload['system_context']['policy']['active_workflow_missions'])

    def test_invalid_codex_policy_fails_closed(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=[12, 25],
            configured_policy={
                'allowed_workflow_create_definition_ids': [12, 25],
                'codex_orchestrator': {
                    'enabled': True,
                    'session_key': 'contains secret token',
                },
            },
        )

        policy = payload['system_context']['policy']
        self.assertEqual([], policy['allowed_workflow_create_definition_ids'])
        self.assertNotIn('codex_orchestrator', policy)
        self.assertIn('CODEX_ORCHESTRATOR_POLICY_INVALID',
                      payload['context_warnings'])

    def test_missing_acl_fails_closed_for_configured_codex_policy(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codex',
            [],
            [],
            workflow_create_definition_ids=None,
            configured_policy={
                'allowed_workflow_create_definition_ids': [12, 25],
                'codex_orchestrator': {
                    'enabled': True,
                    'session_key': 'racinggo:flow-orchestrator',
                    'resume_on': ['blocked'],
                    'allowed_next_flows': [12, 25],
                    'max_retries': 2,
                    'review_success': True,
                },
            },
        )

        policy = payload['system_context']['policy']
        self.assertEqual([], policy['allowed_workflow_create_definition_ids'])
        self.assertNotIn('codex_orchestrator', policy)
        self.assertIn('WORKFLOW_CREATE_GRANTS_UNAVAILABLE',
                      payload['context_warnings'])

    def test_hermes_ignores_codex_orchestrator_override(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'hermes',
            [],
            [],
            workflow_create_definition_ids=[12, 25, 26],
            configured_policy={
                'allowed_workflow_create_definition_ids': [12],
                'codex_orchestrator': {
                    'enabled': True,
                    'session_key': 'racinggo:flow-orchestrator',
                    'resume_on': ['blocked'],
                    'allowed_next_flows': [12],
                    'max_retries': 1,
                    'review_success': False,
                },
            },
        )

        policy = payload['system_context']['policy']
        self.assertEqual(
            [12, 25, 26], policy['allowed_workflow_create_definition_ids'])
        self.assertNotIn('codex_orchestrator', policy)

    def test_codebuddy_receives_tapd_readonly_policy_without_secret_value(self):
        payload = agent_system_context.build_agent_system_context(
            self.claw,
            'codebuddy',
            [],
            [],
            workflow_create_definition_ids=[],
            configured_policy={
                'allowed_workflow_create_definition_ids': [],
                'tapd_mcp': {
                    'enabled': True,
                    'endpoint': 'https://mcp-oa.tapd.woa.com/mcp/',
                    'credential_secret_key': 'tapd-mcp',
                    'server': 'tapd',
                    'read_only': True,
                    'credential_env': 'TAPD_ACCESS_TOKEN',
                    'credential_header': 'X-Tapd-Access-Token',
                    'credential_prefix': '',
                },
            },
        )

        self.assertEqual({
            'enabled': True,
            'endpoint': 'https://mcp-oa.tapd.woa.com/mcp/',
            'credential_secret_key': 'tapd-mcp',
            'server': 'tapd',
            'read_only': True,
            'credential_env': 'TAPD_ACCESS_TOKEN',
            'credential_header': 'X-Tapd-Access-Token',
            'credential_prefix': '',
        }, payload['system_context']['policy']['tapd_mcp'])

    def test_tapd_policy_rejects_inline_credentials(self):
        with self.assertRaises(ValueError):
            agent_system_context.validate_system_context_policy({
                'allowed_workflow_create_definition_ids': [],
                'tapd_mcp': {
                    'enabled': True,
                    'endpoint': 'https://mcp-oa.tapd.woa.com/mcp/',
                    'credential_secret_key': 'tapd-mcp',
                    'server': 'tapd',
                    'read_only': True,
                    'access_token': 'must-not-be-stored',
                },
            })

    def test_tapd_policy_requires_safe_endpoint_contract(self):
        base = {
            'enabled': True,
            'credential_secret_key': 'tapd-mcp',
            'server': 'tapd',
            'read_only': True,
        }
        for endpoint in (
                '', 'ftp://mcp.example.test/mcp',
                'https://user:password@mcp.example.test/mcp',
                'https://mcp.example.test/mcp?token=secret'):
            with (self.subTest(endpoint=endpoint),
                  self.assertRaisesRegex(ValueError, 'tapd_mcp.endpoint')):
                agent_system_context.validate_system_context_policy({
                    'allowed_workflow_create_definition_ids': [],
                    'tapd_mcp': {**base, 'endpoint': endpoint},
                })
        with self.assertRaisesRegex(ValueError, 'tapd_mcp.endpoint'):
            agent_system_context.validate_system_context_policy({
                'allowed_workflow_create_definition_ids': [],
                'tapd_mcp': {
                    **base, 'endpoint': 'http://mcp.example.test/mcp',
                },
            })
        policy = agent_system_context.validate_system_context_policy({
            'allowed_workflow_create_definition_ids': [],
            'tapd_mcp': {
                **base,
                'endpoint': 'http://mcp.example.test/mcp',
                'allow_insecure_http': True,
            },
        })
        self.assertTrue(policy['tapd_mcp']['allow_insecure_http'])

    def test_policy_validator_rejects_unknown_or_secret_fields(self):
        with self.assertRaises(ValueError):
            agent_system_context.validate_system_context_policy({
                'allowed_workflow_create_definition_ids': [12],
                'codex_orchestrator': {
                    'enabled': True,
                    'session_key': 'safe-key',
                    'resume_on': ['blocked'],
                    'allowed_next_flows': [12],
                    'max_retries': 1,
                    'review_success': True,
                    'thread_id': 'must-not-be-stored',
                },
            })
        with self.assertRaises(ValueError):
            agent_system_context.validate_system_context_policy({
                'allowed_workflow_create_definition_ids': [12],
                'workflow_start_bindings': {
                    '12': {
                        'executor_claw_ids': [11],
                        'start_vars': {'hub_token': 'must-not-downlink'},
                    },
                },
            })


if __name__ == '__main__':
    unittest.main()
