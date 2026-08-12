import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'workflows.py'
_SPEC = importlib.util.spec_from_file_location('workflows', _MODULE_PATH)
workflows = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflows)


class WorkflowServiceTest(unittest.TestCase):
    def test_compute_step_health_for_running_heartbeat_states(self):
        now = datetime(2026, 7, 2, 19, 0, 0)
        healthy = workflows.compute_step_health(
            'running', now - timedelta(seconds=30), now)
        stale = workflows.compute_step_health(
            'running', now - timedelta(seconds=90), now)
        overdue = workflows.compute_step_health(
            'running', now - timedelta(seconds=91), now)
        failed = workflows.compute_step_health(
            'running', now - timedelta(seconds=91), now,
            auto_fail_on_missed=True)
        self.assertEqual(healthy['status'], 'healthy')
        self.assertEqual(healthy['missed_count'], 0)
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['missed_count'], 2)
        self.assertEqual(overdue['status'], 'stale')
        self.assertEqual(overdue['missed_count'], 3)
        self.assertFalse(overdue['failed'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['missed_count'], 3)
        self.assertTrue(failed['failed'])

    def test_compute_step_health_ignores_non_running_steps(self):
        now = datetime(2026, 7, 2, 19, 0, 0)
        health = workflows.compute_step_health(
            'passed', now - timedelta(seconds=120), now)
        self.assertEqual(health['status'], 'idle')
        self.assertEqual(health['missed_count'], 0)

    def test_compute_step_health_reports_stale_progress_without_failing(self):
        now = datetime(2026, 7, 2, 19, 0, 0)
        health = workflows.compute_step_health(
            'running',
            now - timedelta(seconds=10),
            now,
            progress_at=now - timedelta(minutes=12),
            progress_timeout_sec=300,
        )
        self.assertEqual(health['status'], 'healthy')
        self.assertEqual(health['progress_status'], 'stale')
        self.assertFalse(health['failed'])

    def test_compute_step_health_treats_fresh_progress_signal_as_healthy(self):
        now = datetime(2026, 7, 2, 19, 0, 0)
        progress_at = now - timedelta(seconds=10)
        health = workflows.compute_step_health(
            'running',
            progress_at,
            now,
            progress_at=progress_at,
            progress_timeout_sec=300,
        )
        self.assertEqual(health['status'], 'healthy')
        self.assertEqual(health['progress_status'], 'fresh')
        self.assertFalse(health['failed'])

    def test_build_heartbeat_fallback_notice_mentions_chat_and_todo_context(self):
        notice = workflows.build_heartbeat_fallback_notice(
            run_id=15,
            step_id='action_install',
            step_name='覆盖安装包体',
            blocker={'last_heartbeat_at': '2026-07-02 21:11:22'},
        )
        self.assertIn('Workflow Run #15', notice['title'])
        self.assertIn('action_install', notice['content'])
        self.assertIn('未响应', notice['content'])

    def test_build_step_blocked_notice_mentions_agent_action(self):
        notice = workflows.build_step_blocked_notice(
            run_id=45,
            step_id='action_download',
            step_name='拉取蓝盾最新构建',
            status='blocked',
            summary='Workflow agent task failed before producing result',
            blocker={'type': 'agent_invocation_failed', 'message': 'No module named fcntl'},
        )
        self.assertIn('Workflow Run #45', notice['title'])
        self.assertIn('action_download', notice['content'])
        self.assertIn('No module named fcntl', notice['content'])
        self.assertIn('请处理该节点阻断', notice['content'])

    def test_resolve_start_var_claw_ids_supports_single_and_multi_values(self):
        start_vars = {
            'agent_id': '7',
            'agent_ids': ['8', 9, 'bad'],
            'nested': {'owners': ['10', 11]},
        }
        self.assertEqual(workflows.resolve_start_var_claw_ids(start_vars, 'agent_id'), [7])
        self.assertEqual(workflows.resolve_start_var_claw_ids(start_vars, 'agent_ids'), [8, 9])
        self.assertEqual(workflows.resolve_start_var_claw_ids(start_vars, 'nested.owners'), [10, 11])

    def test_resolve_start_var_claw_ids_strict_rejects_partial_invalid_values(self):
        with self.assertRaises(ValueError):
            workflows.resolve_start_var_claw_ids(
                {'agent_ids': ['bad', 12]},
                'agent_ids',
                strict=True,
            )

    def test_can_dispatch_workflow_agent_task_requires_live_run_step_and_target(self):
        self.assertTrue(workflows.can_dispatch_workflow_agent_task(
            'running', 'running', 7, target_claw_id=7))
        self.assertTrue(workflows.can_dispatch_workflow_agent_task(
            'retrying', 'retrying', 7, executor_claw_ids=[7, 11]))
        self.assertFalse(workflows.can_dispatch_workflow_agent_task(
            'cancelled', 'running', 7, target_claw_id=7))
        self.assertFalse(workflows.can_dispatch_workflow_agent_task(
            'running', 'passed', 7, target_claw_id=7))
        self.assertFalse(workflows.can_dispatch_workflow_agent_task(
            'running', 'running', 11, executor_claw_ids=[7]))

    def test_worker_task_inherits_run_executor_claw_ids(self):
        self.assertEqual(
            workflows.resolve_workflow_step_claw_ids(
                'worker_task',
                run_executor_claw_ids=['11', 7, 'bad', 11],
            ),
            [7, 11],
        )

    def test_explicit_worker_target_overrides_run_executors(self):
        self.assertEqual(
            workflows.resolve_workflow_step_claw_ids(
                'worker_task',
                target_claw_id=9,
                step_executor_claw_ids=[8],
                run_executor_claw_ids=[7, 11],
            ),
            [9],
        )

    def test_explicit_worker_executors_override_run_executors(self):
        self.assertEqual(
            workflows.resolve_workflow_step_claw_ids(
                'worker_task',
                step_executor_claw_ids=['8', 9],
                run_executor_claw_ids=[7, 11],
            ),
            [8, 9],
        )

    def test_agent_task_does_not_inherit_worker_run_executors(self):
        self.assertEqual(
            workflows.resolve_workflow_step_claw_ids(
                'agent_task',
                run_executor_claw_ids=[7, 11],
            ),
            [],
        )

    def test_worker_task_with_target_post_does_not_inherit_run_executors(self):
        self.assertEqual(
            workflows.resolve_workflow_step_claw_ids(
                'worker_task',
                target_post='windows_builder',
                run_executor_claw_ids=[7, 11],
            ),
            [],
        )

    def test_workflow_step_display_state_maps_technical_statuses(self):
        cases = {
            'pending': 'todo',
            'running': 'running',
            'retrying': 'running',
            'waiting_approval': 'running',
            'blocked': 'blocked',
            'failed': 'blocked',
            'passed': 'done',
            'skipped': 'done',
            'succeeded': 'done',
        }
        for technical, display in cases.items():
            self.assertEqual(
                workflows.workflow_step_display_state(technical),
                display,
                technical,
            )

    def test_workflow_step_display_state_defaults_to_todo(self):
        self.assertEqual(workflows.workflow_step_display_state(''), 'todo')
        self.assertEqual(workflows.workflow_step_display_state(None), 'todo')

    def test_heartbeat_lost_result_requires_force_recover(self):
        blocker = {'type': 'workflow_step_heartbeat_lost'}
        rejected = workflows.can_accept_step_result_after_blocker(
            'blocked', blocker, force_recover=False, can_manage=False)
        self.assertFalse(rejected['accepted'])
        forced_by_non_owner = workflows.can_accept_step_result_after_blocker(
            'blocked', blocker, force_recover=True, can_manage=False)
        self.assertFalse(forced_by_non_owner['accepted'])
        forced_by_owner = workflows.can_accept_step_result_after_blocker(
            'blocked', blocker, force_recover=True, can_manage=True)
        self.assertTrue(forced_by_owner['accepted'])

    def test_paginate_items_defaults_to_ten_and_clamps_page_size(self):
        items = list(range(25))
        page = workflows.paginate_items(items, page=1, per_page=999)
        self.assertEqual(page['items'], items[:10])
        self.assertEqual(page['pagination']['page'], 1)
        self.assertEqual(page['pagination']['per_page'], 10)
        self.assertEqual(page['pagination']['total'], 25)
        self.assertEqual(page['pagination']['pages'], 3)
        self.assertTrue(page['pagination']['has_next'])

    def test_paginate_items_supports_allowed_page_sizes(self):
        items = list(range(25))
        page = workflows.paginate_items(items, page=2, per_page=20)
        self.assertEqual(page['items'], items[20:25])
        self.assertEqual(page['pagination']['per_page'], 20)
        self.assertFalse(page['pagination']['has_next'])
        self.assertTrue(page['pagination']['has_prev'])

    def test_normalize_definition_supports_mvp_node_types(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'racinggo_qa',
            'name': 'RacingGO QA',
            'steps': [
                {'id': 'precheck', 'name': '前置检查', 'type': 'worker_task',
                 'runner': 'deepflow.racinggo.precheck',
                 'input_vars': {'branch': 'context.branch'},
                 'outputs': ['env_ready'],
                 'auto_block_on_heartbeat_loss': True},
                {'id': 'approve_push', 'name': '批准推送', 'type': 'approval',
                 'depends_on': ['precheck']},
                {'id': 'notify_agent', 'name': '通知小安', 'type': 'agent_task',
                 'target_agent': '小安', 'depends_on': ['approve_push'],
                 'target_claw_id_var': 'agent_id',
                 'executor_claw_ids_var': 'agent_ids',
                 'notify_agent_on_start': True},
            ],
        })
        self.assertEqual(definition['key'], 'racinggo_qa')
        self.assertEqual([s['id'] for s in definition['steps']],
                         ['precheck', 'approve_push', 'notify_agent'])
        self.assertEqual(definition['steps'][1]['approval_required'], True)
        self.assertEqual(definition['steps'][0]['input_vars']['branch'], 'context.branch')
        self.assertEqual(definition['steps'][0]['outputs'], ['env_ready'])
        self.assertTrue(definition['steps'][0]['auto_block_on_heartbeat_loss'])
        self.assertEqual(definition['steps'][2]['target_claw_id_var'], 'agent_id')
        self.assertEqual(definition['steps'][2]['executor_claw_ids_var'], 'agent_ids')
        self.assertTrue(definition['steps'][2]['notify_agent_on_start'])

    def test_normalize_definition_preserves_step_references(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'demo_refs',
            'name': 'Demo References',
            'steps': [{
                'id': 'review',
                'name': '审查报告',
                'type': 'agent_task',
                'references': [
                    {'type': 'knowledge', 'id': 12, 'title': '登录坑点'},
                    {'type': 'test_report', 'id': 34, 'title': '冒烟报告'},
                    {'type': 'skill', 'path': 'openclaw-agent/skills/hub-connect/SKILL.md'},
                    {'type': 'url', 'url': 'https://example.com/spec', 'title': '外部规范'},
                    {'type': 'url', 'url': 'javascript:alert(1)', 'title': '危险链接'},
                    {'type': 'url', 'url': '//evil.example/path', 'title': '协议相对链接'},
                    'bad',
                    {'type': 'unknown', 'id': 1},
                ],
            }],
        })
        refs = definition['steps'][0]['references']
        self.assertEqual(len(refs), 4)
        self.assertEqual(refs[0]['type'], 'knowledge')
        self.assertEqual(refs[0]['id'], 12)
        self.assertEqual(refs[2]['path'], 'openclaw-agent/skills/hub-connect/SKILL.md')

    def test_normalize_definition_preserves_target_post(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'agent_team_loop',
            'name': 'Agent Team Loop',
            'steps': [{
                'id': 'req_review',
                'name': '需求评审',
                'type': 'agent_task',
                'target_post': 'requirement_analyst',
            }],
        })
        self.assertEqual(
            definition['steps'][0]['target_post'],
            'requirement_analyst')

    def test_normalize_definition_preserves_safe_code_analysis_config(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'analysis_flow',
            'name': 'Analysis Flow',
            'steps': [{
                'id': 'code_analysis',
                'name': '代码分析',
                'type': 'agent_task',
                'analysis': {
                    'enabled': True,
                    'profile': 'requirement_code_joint',
                    'report_format': 'pdf',
                    'baseline_vars': {
                        'client_target_sha': 'client_target_sha',
                        'server_target_sha': 'server_target_sha',
                        'unexpected_secret': 'must-not-pass',
                    },
                },
            }],
        })
        analysis = definition['steps'][0]['analysis']
        self.assertTrue(analysis['enabled'])
        self.assertEqual(analysis['report_format'], 'html')
        self.assertEqual(analysis['baseline_vars']['client_target_sha'],
                         'client_target_sha')
        self.assertNotIn('unexpected_secret', analysis['baseline_vars'])

    def test_ready_steps_only_include_dependency_satisfied_pending_steps(self):
        definition = workflows.normalize_workflow_definition({
            'key': 'demo',
            'name': 'Demo',
            'steps': [
                {'id': 'a', 'name': 'A', 'runner': 'runner.a'},
                {'id': 'b', 'name': 'B', 'runner': 'runner.b',
                 'depends_on': ['a']},
                {'id': 'c', 'name': 'C', 'runner': 'runner.c',
                 'depends_on': ['b']},
            ],
        })
        states = {'a': 'passed', 'b': 'pending', 'c': 'pending'}
        ready = workflows.ready_step_ids(definition, states)
        self.assertEqual(ready, ['b'])

    def test_update_definition_patch_preserves_key_and_replaces_steps(self):
        existing = workflows.normalize_workflow_definition({
            'key': 'demo',
            'name': 'Demo',
            'description': 'old',
            'steps': [
                {'id': 'a', 'name': 'A', 'runner': 'runner.a'},
            ],
            'context': {'branch': 'dev'},
        })
        updated = workflows.merge_workflow_definition_update(existing, {
            'name': 'Demo v2',
            'description': 'new',
            'definition': {
                'key': 'ignored_key',
                'steps': [
                    {'id': 'b', 'name': 'B', 'runner': 'runner.b'},
                    {'id': 'c', 'name': 'C', 'runner': 'runner.c', 'depends_on': ['b']},
                ],
                'context': {'branch': 'qa_auto_test'},
            },
        })
        self.assertEqual(updated['key'], 'demo')
        self.assertEqual(updated['name'], 'Demo v2')
        self.assertEqual(updated['description'], 'new')
        self.assertEqual([s['id'] for s in updated['steps']], ['b', 'c'])
        self.assertEqual(updated['context']['branch'], 'qa_auto_test')

    def test_build_agent_task_payload_contains_workflow_context(self):
        payload = workflows.build_workflow_agent_task_payload(
            run={
                'id': 7,
                'run_name': 'Demo Run',
                'context': {'branch': 'qa_auto_test'},
            },
            step={
                'step_id': 'analyze_report',
                'name': '分析报告',
                'runner': 'agent.skill.test-report-manager',
                'config': {
                    'prompt': '判断是否可以继续',
                    'inputs': {'report_type': 'editor'},
                    'input_vars': {'report_id': 'outputs.editor_report.report_id'},
                },
            },
            outputs={'editor_report': {'report_id': 123}},
        )
        self.assertEqual(payload['run_id'], 7)
        self.assertEqual(payload['step_id'], 'analyze_report')
        self.assertEqual(payload['runner'], 'agent.skill.test-report-manager')
        self.assertEqual(payload['outputs']['editor_report']['report_id'], 123)
        self.assertIn('/api/v1/workflow-runs/7/steps/analyze_report/result',
                      payload['result_api'])

    def test_build_agent_task_payload_includes_shift_left_api_contract(self):
        payload = workflows.build_workflow_agent_task_payload(
            run={'id': 19, 'run_name': '需求闭环', 'context': {}},
            step={
                'step_id': 'engineering_analysis',
                'name': '工程分析',
                'config': {
                    'analysis': workflows.normalize_analysis_config({
                        'enabled': True,
                        'create_report': True,
                        'report_id_var': 'analysis_report_id',
                    }),
                },
            },
            outputs={},
        )
        self.assertTrue(payload['analysis']['enabled'])
        self.assertEqual(
            payload['analysis']['api_contract']['upsert_finding']['method'],
            'PUT')
        self.assertIn('/api/v1/shift-left/analysis-runs',
                      payload['analysis']['api_contract']['create_run']['path'])
        self.assertEqual(
            payload['analysis']['report_binding']['result_field'],
            'analysis_report_id')

    def test_build_agent_task_payload_contains_references_and_display_state(self):
        payload = workflows.build_workflow_agent_task_payload(
            run={'id': 8, 'run_name': 'Demo', 'context': {}},
            step={
                'step_id': 'review',
                'name': '审查报告',
                'status': 'running',
                'config': {
                    'prompt': '请审查报告',
                    'references': [{'type': 'knowledge', 'id': 12, 'title': '登录坑点'}],
                },
            },
            outputs={},
        )
        self.assertEqual(payload['display_state'], 'running')
        self.assertEqual(payload['references'][0]['type'], 'knowledge')
        self.assertIn('请审查报告', payload['prompt'])

    def test_build_start_context_exposes_start_vars_globally(self):
        context = workflows.build_workflow_start_context(
            {'branch': 'qa_auto_test', 'device_pool': 'android_smoke'},
            {'env': 'staging', 'start_vars': {'branch': 'old'}},
            start_mode='scheduled',
            schedule_cron='0 10 * * 1-5',
        )
        self.assertEqual(context['env'], 'staging')
        self.assertEqual(context['start_vars']['branch'], 'qa_auto_test')
        self.assertEqual(context['workflow_start']['variables']['device_pool'], 'android_smoke')
        self.assertEqual(context['workflow_start']['mode'], 'scheduled')

    def test_agent_task_payload_exposes_start_vars_directly(self):
        payload = workflows.build_workflow_agent_task_payload(
            run={
                'id': 8,
                'run_name': 'Demo Run',
                'context': {
                    'start_vars': {'branch': 'qa_auto_test'},
                    'workflow_start': {'variables': {'branch': 'qa_auto_test'}},
                },
            },
            step={'step_id': 'agent_step', 'name': 'Agent Step'},
            outputs={},
        )
        self.assertEqual(payload['start_vars']['branch'], 'qa_auto_test')
        self.assertEqual(payload['context']['start_vars']['branch'], 'qa_auto_test')

    def test_gate_failure_blocks_step(self):
        step = {'id': 'lib21', 'gates': [
            {'expression': 'metrics.failed == 0', 'on_fail': 'blocked'},
        ]}
        result = {'status': 'passed', 'metrics': {'failed': 2}}
        gate = workflows.evaluate_step_gates(step, result)
        self.assertFalse(gate['passed'])
        self.assertEqual(gate['status'], 'blocked')
        self.assertIn('metrics.failed == 0', gate['failed_gates'][0]['expression'])

    def test_gate_supports_comparing_two_metric_paths(self):
        step = {'id': 'push_and_build', 'gates': [
            {'expression': 'metrics.remote_head == metrics.local_head', 'on_fail': 'blocked'},
        ]}
        passed = workflows.evaluate_step_gates(step, {
            'status': 'passed',
            'metrics': {'remote_head': 'abc123', 'local_head': 'abc123'},
        })
        blocked = workflows.evaluate_step_gates(step, {
            'status': 'passed',
            'metrics': {'remote_head': 'abc123', 'local_head': 'def456'},
        })
        self.assertTrue(passed['passed'])
        self.assertFalse(blocked['passed'])
        self.assertEqual(blocked['status'], 'blocked')

    def test_approval_step_waits_for_approval(self):
        step = workflows.normalize_step({
            'id': 'push_and_build',
            'name': 'Push 并触发蓝盾构包',
            'type': 'worker_task',
            'runner': 'deepflow.bkci.trigger_qa_build',
            'approval_required': True,
        }, 0)
        self.assertEqual(workflows.initial_step_status(step), 'waiting_approval')

    def test_workflow_execution_acl_defaults_to_owner(self):
        acl = workflows.normalize_executor_acl(None)
        self.assertTrue(workflows.can_execute_workflow(
            'claw', 12, acl, 'claw', 12))
        self.assertFalse(workflows.can_execute_workflow(
            'claw', 12, acl, 'claw', 13))

    def test_workflow_execution_acl_allows_added_users_and_agents(self):
        acl = workflows.normalize_executor_acl({
            'claw_ids': [13],
            'user_ids': [7],
        })
        self.assertTrue(workflows.can_execute_workflow(
            'claw', 12, acl, 'claw', 13))
        self.assertTrue(workflows.can_execute_workflow(
            'claw', 12, acl, 'user', 7))
        self.assertFalse(workflows.can_execute_workflow(
            'claw', 12, acl, 'user', 8))

    def test_workflow_manage_acl_requires_owner_or_admin(self):
        acl = workflows.normalize_executor_acl({})
        self.assertTrue(workflows.can_manage_workflow(
            'user', 3, acl, 'user', 3))
        self.assertFalse(workflows.can_manage_workflow(
            'user', 3, acl, 'claw', 3))
        self.assertTrue(workflows.can_manage_workflow(
            'user', 3, acl, 'claw', 9, is_admin=True))

    def test_project_visible_workflow_can_be_seen_by_project_member(self):
        self.assertTrue(workflows.can_view_workflow(
            'project', 'claw', 12, 9, 'claw', 13, [9]))
        self.assertFalse(workflows.can_manage_workflow(
            'claw', 12, {}, 'claw', 13))
        self.assertFalse(workflows.can_execute_workflow(
            'claw', 12, {}, 'claw', 13))

    def test_private_workflow_only_visible_to_owner_or_admin(self):
        self.assertTrue(workflows.can_view_workflow(
            'private', 'user', 7, 9, 'user', 7, [9]))
        self.assertFalse(workflows.can_view_workflow(
            'private', 'user', 7, 9, 'user', 8, [9]))
        self.assertTrue(workflows.can_view_workflow(
            'private', 'user', 7, 9, 'user', 8, [9], is_admin=True))

    def test_system_workflow_is_visible_to_all(self):
        self.assertTrue(workflows.can_view_workflow(
            'project', 'system', None, None, 'claw', 13, []))

    def test_workflow_step_claim_allows_unclaimed_and_same_worker(self):
        now = datetime(2026, 7, 2, 14, 0, 0)
        self.assertTrue(workflows.can_claim_step('', None, 'worker-a', now, 120))
        self.assertTrue(workflows.can_claim_step(
            'worker-a', now - timedelta(seconds=10), 'worker-a', now, 120))

    def test_workflow_step_claim_blocks_other_worker_until_lease_expires(self):
        now = datetime(2026, 7, 2, 14, 0, 0)
        self.assertFalse(workflows.can_claim_step(
            'worker-a', now - timedelta(seconds=30), 'worker-b', now, 120))
        self.assertTrue(workflows.can_claim_step(
            'worker-a', now - timedelta(seconds=121), 'worker-b', now, 120))

    def test_branch_selects_then_and_skips_else_from_outputs(self):
        step = workflows.normalize_step({
            'id': 'downloadandinstall',
            'name': '下载安装',
            'runner': 'deepflow.bkci.download_install',
            'branches': [{
                'if': 'outputs.downloadandinstall.installed == true',
                'then': ['prepare_env'],
                'else': ['download_failed'],
            }],
        }, 0)
        decision = workflows.evaluate_step_branches(step, {
            'outputs': {'downloadandinstall': {'installed': True}},
        })
        self.assertEqual(decision['selected'], ['prepare_env'])
        self.assertEqual(decision['skipped'], ['download_failed'])

    def test_branch_selects_else_when_expression_false(self):
        step = workflows.normalize_step({
            'id': 'prepare_env',
            'name': '环境准备',
            'runner': 'deepflow.racinggo.prepare_env',
            'branches': [{
                'if': 'outputs.prepare_env.ready == true',
                'then': ['mobile_smoke'],
                'else': ['env_blocked'],
            }],
        }, 0)
        decision = workflows.evaluate_step_branches(step, {
            'outputs': {'prepare_env': {'ready': False}},
        })
        self.assertEqual(decision['selected'], ['env_blocked'])
        self.assertEqual(decision['skipped'], ['mobile_smoke'])


if __name__ == '__main__':
    unittest.main()
