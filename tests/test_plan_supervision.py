"""Plan wake/lease/receipt contract, including actual Mission dispatch fencing."""
import json
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import patch

import test_workflow_missions_api as fixtures
from app import db
from app.models import (AgentTask, AgentTeam, AgentTeamMember, AgentTeamMission, MissionStage,
                        OpenClawInstance, TestPlan, TestTask, TestTaskOccurrence, ClawMessage,
                        WorkflowRun, WorkflowRunStep,
                        WorkflowMission, WorkflowMissionDispatch, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt
from app.services import plan_supervision as svc
from app.services.agent_tasks import (claim_pending_tasks, complete_task,
                                      expire_stale_ordinary_tasks)


class PlanSupervisionTest(unittest.TestCase):
    tearDown = fixtures.WorkflowMissionsApiTest.tearDown
    _definition = fixtures.WorkflowMissionsApiTest._definition
    _login_admin = fixtures.WorkflowMissionsApiTest._login_admin
    _headers = fixtures.WorkflowMissionsApiTest._headers
    _create_mission = fixtures.WorkflowMissionsApiTest._create_mission

    def setUp(self):
        fixtures.WorkflowMissionsApiTest.setUp(self)
        self.app.config['PLAN_SUPERVISION_ENABLED'] = True
        self.app.config['PLAN_SUPERVISION_TEAM_IDS'] = '*'
        from app.api.agent_client import agent_bp
        self.app.register_blueprint(agent_bp, url_prefix='/api/openclaws')
        self.plan = TestPlan(name='Supervised plan', project_id=self.project.id,
            start_date=_now().date(), end_date=(_now() + timedelta(days=7)).date(),
            status='draft', created_by=self.admin.username)
        db.session.add(self.plan)
        db.session.commit()
        self.base = '/api/v1/test-plans/%s/supervision' % self.plan.id
        self.patcher = patch('app.services.plan_supervision.wake')
        self.wake = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def agent(self):
        with self.client.session_transaction() as session:
            session.clear()

    def post(self, action, data, token=None):
        return self.client.post(self.base + '/' + action, json=data, headers=self._headers(token))

    def sup(self):
        return db.session.get(PlanSupervisor, self.plan.id)

    def start(self):
        response = self.post('start', {'command_key': 'start-1', 'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(response.status_code, 200, response.json)
        self.agent()
        return response.json

    def claim(self, key='claim-1'):
        response = self.post('claim', {'command_key': key, 'worker_id': 'worker-A',
                                     'wake_message_id': self.sup().wake_message_id})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def credentials(self):
        return {'worker_id': 'worker-A', 'fencing_token': self.sup().fencing_token}

    def wait_body(self, **changes):
        body = {'command_key': 'decision-1', **self.credentials(), 'cursor': self.sup().lease_cursor,
                'outcome': 'wait', 'summary': '明早继续；无人工通知',
                'next_check_at': (_now() + timedelta(hours=12)).isoformat() + '+08:00',
                'resume_condition': 'timer_or_event'}
        body.update(changes)
        return body

    def test_start_is_atomic_idempotent_and_does_not_start_runs(self):
        first = self.start()
        again = self.post('start', {'command_key': 'start-1', 'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(again.status_code, 200, again.json)
        self.assertEqual(first['receipt_id'], again.json['receipt_id'])
        self.assertEqual(PlanSupervisor.query.count(), 1)
        self.assertEqual(ClawMessage.query.filter_by(msg_type='plan_supervision').count(), 1)
        wake = ClawMessage.query.filter_by(msg_type='plan_supervision').one()
        self.assertEqual(set(json.loads(wake.content)), {
            'contract', 'plan_id', 'team_id', 'supervisor_api', 'cursor',
            'instruction',
        })
        self.assertEqual(WorkflowRun.query.count(), 0)
        self.assertEqual(self.plan.status, 'active')
        self.assertEqual(self.post('start', {'command_key': 'start-2',
            'orchestrator_claw_id': self.other_claw.id}).status_code, 409)

    def scoped_team(self):
        self.app.config.update(
            AGENT_TEAMS_ENABLED=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
            AGENT_TEAMS_PROJECT_IDS=[self.project.id],
        )
        team = AgentTeam(project_id=self.project.id, name='First team', objective='Scoped test',
                         primary_manager_claw_id=self.main_claw.id, status='active',
                         policy_json={'allowed_definition_ids': [self.flow_a.id], 'max_child_runs': 3})
        db.session.add(team)
        db.session.commit()
        self.app.config['PLAN_SUPERVISION_TEAM_IDS'] = str(team.id)
        return team

    def test_team_scope_rejects_missing_foreign_or_wrong_manager(self):
        team = self.scoped_team()
        base = {'command_key': 'scoped', 'orchestrator_claw_id': self.main_claw.id}
        for changes in ({}, {'team_id': team.id + 100},
                        {'team_id': team.id, 'orchestrator_claw_id': self.other_claw.id}):
            self.assertEqual(self.post('start', dict(base, **changes)).status_code, 403)
        self.assertEqual(PlanSupervisor.query.count(), 0)
        response = self.post('start', dict(base, team_id=team.id))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['supervision']['team_id'], team.id)
        self.assertIsNotNone(response.json['supervision']['mission_id'])
        self.assertTrue(response.json['supervision']['manager_lease']['active'])
        self.assertEqual(WorkflowMission.query.count(), 1)
        self.assertEqual(WorkflowRun.query.count(), 0)
        self.agent()
        self.claim()
        for field, value in [('status', 'paused'), ('primary_manager_claw_id', self.other_claw.id)]:
            old = getattr(team, field)
            setattr(team, field, value)
            db.session.commit()
            self.assertEqual(self.post('heartbeat', self.credentials()).status_code, 409)
            setattr(team, field, old)
            db.session.commit()
        self.app.config['PLAN_SUPERVISION_TEAM_IDS'] = ''
        self.assertEqual(self.post('heartbeat', self.credentials()).status_code, 409)

    def test_team_bootstrap_creates_member_stages_and_claim_receipt(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='执行 Flow', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='automation')
        manager_task = TestTask(
            plan_id=self.plan.id, name='历史错误经理执行项', status='assigned',
            assignee_claw_id=self.main_claw.id, task_type='automation')
        # Use a claimable worker task so the test covers the authoritative
        # Worker claim -> TestTask in_progress transition.
        definition = dict(self.flow_a.definition_json)
        definition['steps'] = [dict(definition['steps'][0], type='worker_task')]
        self.flow_a.definition_json = definition
        db.session.add_all([task, manager_task])
        db.session.commit()

        started = self.post('start', {
            'command_key': 'team-bootstrap', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        supervisor = self.sup()
        mission = db.session.get(WorkflowMission, supervisor.mission_id)
        binding = db.session.get(AgentTeamMission, mission.id)
        stage = MissionStage.query.filter_by(mission_id=mission.id).one()
        self.assertEqual(mission.control_mode, 'team_managed')
        self.assertEqual(mission.allowed_worker_claw_ids_json, [self.other_claw.id])
        self.assertEqual(binding.team_id, team.id)
        self.assertEqual(stage.assigned_claw_id, self.other_claw.id)
        self.assertEqual(stage.input_snapshot_json['test_task_id'], task.id)
        self.assertEqual(task.status, 'assigned')
        contract = self.client.get(
            '/api/v1/workflow-missions/%s' % mission.id,
            headers=self._headers()).json['dispatch_contract']
        self.assertIn('plan_supervision', contract['required_fields'])
        self.assertEqual(
            contract['plan_supervision']['source'],
            'POST /api/v1/test-plans/%s/supervision/claim' % self.plan.id)
        self.assertTrue(
            contract['plan_supervision']['legacy_flat_shape_accepted'])
        gap = PlanSupervisorEvent.query.filter_by(
            kind='task_assignment_gap').one()
        self.assertEqual(gap.payload_json['task_id'], manager_task.id)
        self.assertEqual(gap.payload_json['reason'], 'manager_cannot_execute')

        self.agent()
        claim = self.claim()
        manager = claim['supervision']['manager_lease']
        dispatched = self.client.post(
            '/api/v1/workflow-missions/%s/dispatch' % mission.id,
            headers=self._headers(), json={
                'workflow_definition_id': self.flow_a.id,
                'stage_key': stage.stage_key,
                'decision_key': 'dispatch-task-%s' % task.id,
                'manager_epoch': manager['epoch'],
                'manager_session_id': manager['session_id'],
                **self.credentials(),
            })
        self.assertEqual(dispatched.status_code, 201, dispatched.json)
        db.session.refresh(task)
        self.assertEqual(task.status, 'assigned')
        premature = self.client.put(
            '/api/v1/test-plans/%s/tasks/%s' % (self.plan.id, task.id),
            headers=self._headers(), json={'status': 'in_progress'})
        self.assertEqual(premature.status_code, 409, premature.json)
        self.assertEqual(
            premature.json['code'], 'TEST_TASK_DISPATCH_RECEIPT_REQUIRED')

        run_id = dispatched.json['workflow_run_id']
        run = db.session.get(WorkflowRun, run_id)
        step = WorkflowRunStep.query.filter_by(run_id=run_id).one()
        run.status = 'running'
        step.status = 'running'
        db.session.commit()
        claimed = self.client.post(
            '/api/v1/workflow-runs/%s/steps/%s/claim' % (run_id, step.step_id),
            headers=self._headers(self.other_token),
            json={'worker_id': 'worker-other', 'lease_seconds': 120})
        self.assertEqual(claimed.status_code, 200, claimed.json)
        db.session.refresh(task)
        self.assertEqual(task.status, 'in_progress')
        self.assertGreaterEqual(task.progress, 1)
        event = PlanSupervisorEvent.query.filter_by(kind='task_claimed').one()
        self.assertEqual(event.payload_json['task_id'], task.id)
        run.status = 'succeeded'
        run.summary = 'Flow 验证通过'
        db.session.commit()
        svc.sweep()
        db.session.refresh(task)
        db.session.refresh(stage)
        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.progress, 100)
        self.assertEqual(stage.state, 'completed')
        self.assertEqual(task.result_summary, 'Flow 验证通过')

    def test_active_team_plan_without_supervisor_is_repaired_by_watchdog(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        self.plan.status = 'active'
        db.session.add(TestTask(
            plan_id=self.plan.id, name='watchdog task', status='pending',
            assignee_claw_id=self.other_claw.id, task_type='automation'))
        db.session.commit()

        self.assertIsNone(self.sup())
        self.assertEqual(svc.sweep(), 1)
        self.assertIsNotNone(self.sup())
        self.assertIsNotNone(self.sup().mission_id)
        self.assertEqual(WorkflowMission.query.count(), 1)
        self.assertEqual(MissionStage.query.count(), 1)
        self.assertEqual(ClawMessage.query.filter_by(msg_type='plan_supervision').count(), 1)

    def test_empty_team_allowlist_fails_closed_and_capability_is_scoped(self):
        team = self.scoped_team()
        self.assertTrue(svc.team_capability(team.id)['enabled'])
        self.assertFalse(svc.team_capability(team.id + 100)['enabled'])
        self.app.config['PLAN_SUPERVISION_TEAM_IDS'] = ''
        self.assertEqual(self.post('start', {'command_key': 'empty', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id}).status_code, 403)
        self.assertEqual(ClawMessage.query.filter_by(msg_type='plan_supervision').count(), 0)

    def test_single_lease_wrong_identity_and_fenced_dispatch(self):
        self.start()
        self.claim()
        self.assertEqual(self.post('claim', {'command_key': 'another', 'worker_id': 'worker-B',
            'wake_message_id': self.sup().wake_message_id}).status_code, 409)
        self.assertEqual(self.post('heartbeat', self.credentials(), self.other_token).status_code, 403)
        self.assertEqual(self.post('heartbeat', {**self.credentials(), 'fencing_token': 0}).status_code, 409)
        self.assertEqual(self.post('heartbeat', self.credentials()).status_code, 200)

    def test_wait_requires_time_condition_cursor_and_persistent_receipt(self):
        self.start()
        self.claim()
        for changes in ({'next_check_at': '明早'}, {'resume_condition': None}, {'cursor': 9999},
                        {'next_check_at': (_now() - timedelta(hours=1)).isoformat() + '+08:00'}):
            with self.subTest(changes=changes):
                self.assertIn(self.post('decision', self.wait_body(**changes)).status_code, (400, 409))
        body = self.wait_body()
        result = self.post('decision', body)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['scheduled'])
        self.assertEqual(self.sup().status, 'waiting')
        self.assertIsNotNone(self.sup().next_check_at)
        self.assertEqual(self.post('decision', body).json['receipt_id'], result.json['receipt_id'])
        readback = self.client.get(self.base + '/receipts/%s' % result.json['receipt_id'], headers=self._headers())
        self.assertTrue(readback.json['scheduled'])
        self.assertEqual(self.post('decision', dict(body, summary='changed')).status_code, 409)
        self.assertEqual(self.post('heartbeat', self.credentials()).status_code, 409)

    def test_retryable_decision_has_bounded_automatic_retry(self):
        self.start()
        self.claim()
        response = self.post('decision', {
            'command_key': 'provider-timeout', **self.credentials(),
            'cursor': self.sup().lease_cursor, 'outcome': 'retryable',
            'summary': 'Provider timeout; Hub will retry automatically',
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['supervisor_status'], 'retryable')
        self.assertEqual(self.sup().status, 'retryable')
        self.assertIsNotNone(self.sup().next_check_at)
        self.assertEqual(self.sup().resume_condition, 'timer_or_event')

    def test_hub_schedule_and_portfolio_are_in_readback(self):
        team = self.scoped_team()
        team.policy_json = dict(team.policy_json or {}, supervision_schedule={
            'timezone': 'Asia/Shanghai', 'morning_check': '08:30',
            'progress_summaries': ['12:30', '16:30'], 'day_close': '19:00',
        })
        self.plan.team_id = team.id
        db.session.commit()
        started = self.post('start', {
            'command_key': 'scheduled-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        readback = self.client.get(self.base, headers=self._headers()).json
        self.assertEqual(
            readback['project_portfolio']['schedule']['timezone'],
            'Asia/Shanghai')
        self.assertEqual(
            readback['project_portfolio']['schedule']['day_close'], '19:00')
        self.assertIn('independent_stage_status', readback['supervision'])
        self.assertIn('allowed_actions', readback['supervision'])

    def test_manager_goal_brief_and_commitment_audit_are_durable(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='等待构建后执行冒烟', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='automation')
        db.session.add(task)
        db.session.commit()
        started = self.post('start', {
            'command_key': 'manager-goal-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        self.claim('manager-goal-claim')
        readback = self.client.get(self.base, headers=self._headers()).json
        self.assertEqual(
            'hub.manager_goal.v1', readback['manager_goal']['contract'])
        self.assertEqual(
            readback['manager_goal'],
            readback['supervision']['manager_goal'])
        stage_id = readback['manager_brief']['ready_stages'][0]['stage_id']
        decided = self.post('decision', {
            'command_key': 'manager-goal-defer', **self.credentials(),
            'cursor': self.sup().lease_cursor,
            'outcome': 'wait', 'summary': '构建尚未发布，保留任务并定时复查',
            'next_check_at': (_now() + timedelta(minutes=15)).isoformat() + '+08:00',
            'resume_condition': 'timer_or_event',
            'stage_decisions': [{
                'stage_id': stage_id, 'disposition': 'deferred',
                'reason': '等待可观察的 build_changed 事件',
            }],
            'action_receipts': [],
        })
        self.assertEqual(decided.status_code, 200, decided.json)
        self.assertEqual(
            stage_id,
            decided.json['supervision']['last_decision'][
                'stage_decisions'][0]['stage_id'])
        pending = svc.manager_goal_snapshot(self.sup())['pending_commitments']
        self.assertEqual(stage_id, pending[-1]['stage_id'])

    def test_manager_contract_can_atomically_dispatch_fixed_stage_executor(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='经理自主派发冒烟', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='automation')
        db.session.add(task)
        db.session.commit()
        started = self.post('start', {
            'command_key': 'manager-dispatch-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        self.claim('manager-dispatch-claim')
        stage_key = 'test_task_%s' % task.id
        decided = self.post('decision', {
            'command_key': 'manager-dispatch-decision', **self.credentials(),
            'cursor': self.sup().lease_cursor,
            'manager_contract': 'hub.manager_goal.v1',
            'outcome': 'wait', 'summary': '执行条件满足，直接派发固定执行者',
            'next_check_at': (_now() + timedelta(minutes=5)).isoformat() + '+08:00',
            'resume_condition': 'timer_or_event',
            'stage_actions': [{
                'stage_key': stage_key, 'action': 'dispatch',
                'reason': '执行 Agent 在线且无资源冲突',
            }],
        })
        self.assertEqual(decided.status_code, 200, decided.json)
        self.assertEqual('dispatched', decided.json['stage_action_receipts'][0]['status'])
        agent_task = AgentTask.query.filter_by(claw_id=self.other_claw.id).one()
        self.assertEqual('pending', agent_task.status)
        self.assertEqual(
            'dispatched',
            MissionStage.query.filter_by(
                mission_id=self.sup().mission_id, stage_key=stage_key).one().state)
        self.wake.assert_any_call(self.other_claw.id)

    def test_key_decision_delegates_owner_notice_to_agent_and_quiet_wait_stays_silent(self):
        self.start()
        self.claim()
        blocked = {
            'command_key': 'blocked-notice', **self.credentials(),
            'cursor': self.sup().lease_cursor,
            'outcome': 'blocked', 'summary': '派发合同不匹配，需要人工处理',
        }
        with patch('app.api.wecom.WecomDispatcher.send') as send:
            response = self.post('decision', blocked)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(
                response.json['owner_notification']['status'],
                'agent_action_required')
            self.assertEqual(
                response.json['owner_notification']['reasons'], ['blocked'])
            self.assertEqual(
                response.json['owner_notification']['delivery'], 'agent_wecom')
            send.assert_not_called()
            replay = self.post('decision', blocked)
            self.assertEqual(replay.status_code, 200, replay.json)
            send.assert_not_called()

        self._login_admin()
        resumed = self.client.post(
            self.base + '/resume', json={
                'command_key': 'resume-after-notice',
                'reason': '管理员确认恢复'})
        self.assertEqual(resumed.status_code, 200, resumed.json)
        self.agent()
        self.claim('quiet-claim')
        quiet = self.wait_body(command_key='quiet-wait')
        with patch('app.api.wecom.WecomDispatcher.send') as send:
            response = self.post('decision', quiet)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(
                response.json['owner_notification']['status'], 'not_required')
            send.assert_not_called()

    def test_stage_block_keeps_supervisor_running_while_undispatched_stages_remain(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        db.session.add_all([
            TestTask(plan_id=self.plan.id, name='先执行的冒烟', status='assigned',
                     assignee_claw_id=self.other_claw.id, task_type='automation'),
            TestTask(plan_id=self.plan.id, name='可继续派发的性能测试', status='assigned',
                     assignee_claw_id=self.other_claw.id, task_type='performance'),
        ])
        db.session.commit()
        started = self.post('start', {
            'command_key': 'stage-block-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        self.claim('stage-block-claim')

        blocked = self.post('decision', {
            'command_key': 'stage-block-decision', **self.credentials(),
            'cursor': self.sup().lease_cursor, 'outcome': 'blocked',
            'summary': '首个执行项阻断，但其他任务仍可由经理判断后派发',
        })
        self.assertEqual(blocked.status_code, 200, blocked.json)
        self.assertEqual(blocked.json['requested_outcome'], 'blocked')
        self.assertEqual(blocked.json['effective_outcome'], 'wait')
        self.assertEqual(blocked.json['block_scope'], 'stage')
        self.assertTrue(blocked.json['continue_supervision'])
        self.assertEqual(len(blocked.json['undispatched_stages']), 2)
        self.assertNotEqual(self.sup().status, 'blocked')
        self.assertIsNotNone(self.sup().wake_message_id)

    def test_explicit_plan_scope_can_block_with_undispatched_stages(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        db.session.add(TestTask(
            plan_id=self.plan.id, name='尚未派发的任务', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='automation'))
        db.session.commit()
        started = self.post('start', {
            'command_key': 'plan-block-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        self.claim('plan-block-claim')

        blocked = self.post('decision', {
            'command_key': 'plan-block-decision', **self.credentials(),
            'cursor': self.sup().lease_cursor, 'outcome': 'blocked',
            'block_scope': 'plan',
            'plan_block_reason_code': 'global_security_or_compliance',
            'plan_block_evidence': [{'ref': 'security://incident/1'}],
            'summary': '团队级安全问题，明确暂停整个计划',
        })
        self.assertEqual(blocked.status_code, 200, blocked.json)
        self.assertEqual(blocked.json['effective_outcome'], 'blocked')
        self.assertEqual(blocked.json['block_scope'], 'plan')
        self.assertFalse(blocked.json['continue_supervision'])
        self.assertEqual(self.sup().status, 'blocked_owner_gate')

    def test_plan_scope_rejects_local_summary_and_missing_global_evidence(self):
        team = self.scoped_team()
        self.plan.team_id = team.id
        db.session.commit()
        self.post('start', {
            'command_key': 'scope-guard-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.agent()
        self.claim('scope-guard-claim')
        credentials = self.credentials()
        cursor = self.sup().lease_cursor
        missing = self.post('decision', {
            'command_key': 'scope-guard-missing', **credentials,
            'cursor': cursor, 'outcome': 'blocked',
            'block_scope': 'plan', 'summary': '整个团队环境不可用',
        })
        self.assertEqual(missing.status_code, 400, missing.json)
        self.assertEqual(missing.json['code'], 'PLAN_BLOCK_REASON_REQUIRED')
        conflict = self.post('decision', {
            'command_key': 'scope-guard-conflict', **credentials,
            'cursor': cursor, 'outcome': 'blocked',
            'block_scope': 'plan',
            'plan_block_reason_code': 'global_environment_unavailable',
            'plan_block_evidence': [{'ref': 'probe://global-env'}],
            'summary': '这只是阶段级阻断，其他独立任务可继续',
        })
        self.assertEqual(conflict.status_code, 409, conflict.json)
        self.assertEqual(conflict.json['code'], 'PLAN_BLOCK_SCOPE_CONFLICT')
        self.assertEqual(self.sup().status, 'leased')

    def test_watchdog_auto_recovers_legacy_local_owner_gate_for_team_manager(self):
        team = self.scoped_team()
        self.plan.team_id = team.id
        db.session.commit()
        started = self.post('start', {
            'command_key': 'legacy-gate-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        supervisor = self.sup()
        supervisor.status = 'blocked_owner_gate'
        supervisor.resume_condition = 'manual'
        supervisor.next_check_at = None
        supervisor.last_decision_json = {
            'outcome': 'blocked', 'requested_outcome': 'blocked',
            'block_scope': 'plan',
            'summary': '任务局部阻断，不构成全计划 Owner gate；其他任务继续',
            'plan_block_reason_code': None,
            'plan_block_evidence': [],
        }
        db.session.commit()

        self.assertTrue(svc.manager_auto_resume_allowed(supervisor))
        svc.sweep(now=_now())
        supervisor = self.sup()
        self.assertNotEqual(supervisor.status, 'blocked_owner_gate')
        self.assertEqual(supervisor.resume_condition, 'timer_or_event')
        self.assertIsNotNone(supervisor.next_check_at)
        repaired = PlanSupervisorEvent.query.filter_by(
            plan_id=self.plan.id, kind='supervisor_auto_repaired').one()
        self.assertEqual(
            repaired.payload_json['authority'], 'team_test_manager')

    def test_watchdog_preserves_evidenced_global_owner_gate(self):
        team = self.scoped_team()
        self.plan.team_id = team.id
        db.session.commit()
        self.post('start', {
            'command_key': 'global-gate-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        supervisor = self.sup()
        supervisor.status = 'blocked_owner_gate'
        supervisor.resume_condition = 'manual'
        supervisor.next_check_at = None
        supervisor.last_decision_json = {
            'outcome': 'blocked', 'requested_outcome': 'blocked',
            'block_scope': 'plan',
            'summary': '全团队运行环境不可用，暂停整个计划',
            'plan_block_reason_code': 'global_environment_unavailable',
            'plan_block_evidence': [{'ref': 'probe://all-workers'}],
        }
        db.session.commit()

        self.assertFalse(svc.manager_auto_resume_allowed(supervisor))
        svc.sweep(now=_now())
        self.assertEqual(self.sup().status, 'blocked_owner_gate')
        self.assertIsNone(self.sup().next_check_at)

    def test_manager_can_dispatch_non_flow_agent_task_while_plan_is_blocked(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='每日 Bug 回归', status='pending',
            assignee_claw_id=self.other_claw.id, task_type='functional')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'direct-task-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.agent()
        self.claim('direct-task-claim')
        blocked = self.post('decision', {
            'command_key': 'direct-task-block-plan', **self.credentials(),
            'cursor': self.sup().lease_cursor, 'outcome': 'blocked',
            'block_scope': 'plan',
            'plan_block_reason_code': 'global_environment_unavailable',
            'plan_block_evidence': [{'ref': 'environment://all-workers'}],
            'summary': '全团队运行环境不可用，须人工处理',
        })
        self.assertEqual(blocked.status_code, 200, blocked.json)
        self.assertEqual(self.sup().status, 'blocked_owner_gate')

        path = self.base + '/agent-tasks'
        body = {
            'command_key': 'plan-task-%s-direct-v1' % task.id,
            'test_task_id': task.id,
            'instruction': '独立执行 Bug 回归，不启动 Flow',
            'retry_max': 1,
        }
        dispatched = self.client.post(
            path, json=body, headers=self._headers(self.main_token))
        self.assertEqual(dispatched.status_code, 201, dispatched.json)
        self.assertEqual(dispatched.json['execution_mode'], 'ordinary_agent_task')
        agent_task = AgentTask.query.one()
        self.assertEqual(agent_task.claw_id, self.other_claw.id)
        self.assertEqual(agent_task.status, 'pending')
        stage = MissionStage.query.filter_by(
            mission_id=self.sup().mission_id,
            stage_key='test_task_%s' % task.id).one()
        self.assertEqual(stage.state, 'dispatched')

        replay = self.client.post(
            path, json=body, headers=self._headers(self.main_token))
        self.assertEqual(replay.status_code, 200, replay.json)
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(AgentTask.query.count(), 1)

        claimed = claim_pending_tasks(self.other_claw.id)
        self.assertEqual(len(claimed), 1)
        agent_task = claimed[0]
        self.assertEqual(db.session.get(TestTask, task.id).status, 'in_progress')
        self.assertEqual(db.session.get(MissionStage, stage.id).state, 'running')
        complete_task(agent_task, {
            'claim_token': agent_task.claim_token,
            'attempt_no': agent_task.attempt_no,
            'fencing_token': agent_task.fencing_token,
            'status': 'completed',
            'result': {
                'status': 'passed', 'summary': '回归通过',
                'outputs': {'checked': 8}, 'evidence': {'report_id': 9},
            },
        })
        completed = db.session.get(TestTask, task.id)
        self.assertEqual(completed.status, 'completed')
        self.assertEqual(completed.progress, 100)
        self.assertEqual(completed.result_summary, '回归通过')
        self.assertEqual(db.session.get(MissionStage, stage.id).state, 'completed')
        receipt = svc.task_dispatch_receipt(completed)
        self.assertEqual(receipt['execution_mode'], 'ordinary_agent_task')
        self.assertTrue(receipt['claimed'])

    def test_direct_agent_task_timeout_projects_retry_and_terminal_state(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['ios']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='iOS 客户端性能测试', status='pending',
            assignee_claw_id=self.other_claw.id, task_type='performance')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'timeout-task-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.agent()
        dispatched = self.client.post(self.base + '/agent-tasks', json={
            'command_key': 'timeout-task-%s' % task.id,
            'test_task_id': task.id,
            'retry_max': 1,
        }, headers=self._headers(self.main_token))
        self.assertEqual(dispatched.status_code, 201, dispatched.json)
        stage = MissionStage.query.filter_by(
            mission_id=self.sup().mission_id,
            stage_key='test_task_%s' % task.id).one()

        agent_task = claim_pending_tasks(self.other_claw.id)[0]
        self.assertEqual(task.status, 'in_progress')
        agent_task.lease_expires_at = _now() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(expire_stale_ordinary_tasks(), 1)
        self.assertEqual(agent_task.status, 'pending')
        self.assertEqual(task.status, 'pending')
        self.assertEqual(stage.state, 'dispatched')
        self.assertEqual(stage.last_reason_code, 'ordinary_agent_task_retry_pending')

        agent_task = claim_pending_tasks(self.other_claw.id)[0]
        agent_task.lease_expires_at = _now() - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(expire_stale_ordinary_tasks(), 1)
        self.assertEqual(agent_task.status, 'failed')
        self.assertEqual(task.status, 'blocked')
        self.assertEqual(stage.state, 'failed')
        self.assertEqual(stage.last_reason_code, 'agent_task_lease_expired')

        # A process restart or an older Hub may leave terminal AgentTask truth
        # unprojected.  The reconciliation path must repair it idempotently.
        task.status, stage.state = 'in_progress', 'running'
        db.session.commit()
        self.assertEqual(svc.reconcile_ordinary_task_truth(self.sup()), 1)
        self.assertEqual(task.status, 'blocked')
        self.assertEqual(stage.state, 'failed')
        self.assertEqual(svc.reconcile_ordinary_task_truth(self.sup()), 0)

    def test_daily_occurrences_dispatch_at_time_and_retry_result_invalid(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['ios']))
        self.plan.team_id = team.id
        db.session.commit()
        self.post('start', {
            'command_key': 'daily-task-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})

        task = TestTask(
            plan_id=self.plan.id, name='每日客户端性能检查', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='performance',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='10:00', due_time='18:00', auto_dispatch=True,
            execution_role='member_work')
        db.session.add(task)
        db.session.flush()
        first_day = self.plan.start_date
        before = datetime.combine(first_day, time(9, 59))
        svc.sync_plan_stages(self.sup(), now=before)
        db.session.commit()
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id, occurrence_date=first_day).one()
        self.assertEqual(occurrence.status, 'scheduled')
        self.assertEqual(AgentTask.query.count(), 0)
        self.assertEqual(
            svc.promote_and_dispatch_due_occurrences(self.sup(), now=before), [])

        due = datetime.combine(first_day, time(10, 0))
        self.assertEqual(
            svc.promote_and_dispatch_due_occurrences(self.sup(), now=due),
            [self.other_claw.id])
        db.session.commit()
        agent_task = AgentTask.query.one()
        initial_payload = json.loads(agent_task.payload)
        self.assertNotIn('resume_contract', initial_payload)
        self.assertNotIn('checkpoint', initial_payload)
        db.session.refresh(occurrence)
        self.assertEqual(occurrence.status, 'dispatched')
        self.assertEqual(occurrence.agent_task_id, agent_task.id)
        self.assertEqual(task.status, 'assigned')

        agent_task = claim_pending_tasks(self.other_claw.id, now=due)[0]
        self.assertEqual(occurrence.status, 'running')
        outcome = complete_task(agent_task, {
            'claim_token': agent_task.claim_token,
            'attempt_no': agent_task.attempt_no,
            'fencing_token': agent_task.fencing_token,
            'status': 'failed',
            'result': {
                'status': 'failed', 'error_code': 'result_invalid',
                'reason': '结构化结果暂不可解析',
            },
        }, now=due + timedelta(minutes=1))
        self.assertEqual(outcome, 'retried')
        self.assertEqual(agent_task.status, 'pending')
        self.assertEqual(occurrence.status, 'dispatched')
        self.assertEqual(task.status, 'assigned')

        agent_task = claim_pending_tasks(
            self.other_claw.id, now=due + timedelta(minutes=2))[0]
        complete_task(agent_task, {
            'claim_token': agent_task.claim_token,
            'attempt_no': agent_task.attempt_no,
            'fencing_token': agent_task.fencing_token,
            'status': 'completed',
            'result': {'status': 'passed', 'summary': '今日检查通过'},
        }, now=due + timedelta(minutes=3))
        self.assertEqual(occurrence.status, 'completed')
        self.assertEqual(occurrence.result_summary, '今日检查通过')
        self.assertEqual(task.status, 'assigned')

        second_day = first_day + timedelta(days=1)
        svc.sync_plan_stages(
            self.sup(), now=datetime.combine(second_day, time(9, 0)))
        wake_ids = svc.promote_and_dispatch_due_occurrences(
            self.sup(), now=datetime.combine(second_day, time(10, 0)))
        db.session.commit()
        self.assertEqual(wake_ids, [self.other_claw.id])
        self.assertEqual(TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).count(), 2)
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 2)
        self.assertEqual(TestTaskOccurrence.query.filter_by(
            test_task_id=task.id, occurrence_date=first_day).one().status,
            'completed')

    def test_waiting_condition_probe_resumes_same_occurrence_with_new_fence(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['ios']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='当日 iOS 性能测试', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='performance',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='00:00', due_time='23:59', auto_dispatch=True,
            execution_role='member_work')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'condition-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        svc.sync_plan_stages(self.sup(), now=now)
        self.assertEqual(
            svc.promote_and_dispatch_due_occurrences(self.sup(), now=now),
            [self.other_claw.id])
        db.session.commit()
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        first = claim_pending_tasks(self.other_claw.id, now=now)[0]
        disposition = complete_task(first, {
            'claim_token': first.claim_token,
            'attempt_no': first.attempt_no,
            'fencing_token': first.fencing_token,
            'status': 'waiting_condition',
            'result': {
                'summary': 'WDA 已就绪，等待微信登录',
                'checkpoint': {
                    'completed_scope': ['device_ready', 'wda_ready'],
                    'remaining_steps': ['collect_performance'],
                    'side_effect_receipts': [],
                },
                'resume_contract': {
                    'conditions': [{
                        'condition_type': 'login_session_ready',
                        'condition_scope': {'device_id': 'ios-1'},
                        'probe_operation': 'ios.wecom.login_ready_v1',
                    }],
                    'probe_interval_seconds': 120,
                    'resume_from_checkpoint': 'collect_performance',
                    'owner_gate': True,
                    'human_action': '在设备上完成登录',
                },
            },
        }, now=now + timedelta(minutes=1))
        self.assertEqual(disposition, 'waiting_condition')
        db.session.refresh(occurrence)
        self.assertEqual(first.status, 'waiting_condition')
        self.assertEqual(occurrence.status, 'waiting_condition')
        self.assertEqual(occurrence.condition_state, 'waiting_condition')
        self.assertTrue(occurrence.owner_gate)
        self.assertEqual(occurrence.checkpoint_json['completed_scope'], [
            'device_ready', 'wda_ready'])
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        self.assertEqual(stage.state, 'waiting_condition')

        body = {
            'command_key': 'condition-ready-1',
            'condition_type': 'login_session_ready',
            'condition_ready': True,
            'expected_resume_fencing_token': 0,
            'facts': {'page': 'home'},
        }
        result = svc.record_condition_probe(
            self.sup(), self.other_claw.id, occurrence.id, body,
            now=now + timedelta(minutes=30))
        db.session.commit()
        self.assertTrue(result['resumed'])
        self.assertEqual(occurrence.resume_fencing_token, 1)
        self.assertEqual(occurrence.condition_state, 'resuming')
        self.assertFalse(occurrence.owner_gate)
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 2)
        resumed = AgentTask.query.order_by(AgentTask.id.desc()).first()
        payload = json.loads(resumed.payload)
        self.assertEqual(payload['resume_fencing_token'], 1)
        self.assertIn('resume_contract', payload)
        self.assertEqual(payload['checkpoint']['remaining_steps'], [
            'collect_performance'])
        replay = svc.record_condition_probe(
            self.sup(), self.other_claw.id, occurrence.id, body,
            now=now + timedelta(minutes=31))
        self.assertTrue(replay['replayed'])
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 2)

    def test_one_off_task_waiting_condition_resumes_with_checkpoint_and_new_fence(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['ios']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='一次性 iOS 性能续采', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='performance',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=False, execution_role='member_work')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'one-off-condition-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        dispatched = svc.dispatch_test_task(
            self.sup(), self.main_claw.id, task.id, {
                'command_key': 'one-off-condition-dispatch',
                'test_task_id': task.id,
                'instruction': '启动 PerfDogService 并采集剩余性能指标',
                'retry_max': 1,
            })
        db.session.commit()
        self.assertEqual(dispatched['wake_claw_id'], self.other_claw.id)
        first = claim_pending_tasks(self.other_claw.id, now=now)[0]
        disposition = complete_task(first, {
            'claim_token': first.claim_token,
            'attempt_no': first.attempt_no,
            'fencing_token': first.fencing_token,
            'status': 'waiting_condition',
            'result': {
                'summary': '设备与 WDA 已就绪，等待 PerfDogService 登录启动',
                'checkpoint': {
                    'completed_scope': ['device_ready', 'wda_ready'],
                    'remaining_steps': ['collect_cpu_jank_memory'],
                    'side_effect_receipts': [],
                },
                'resume_contract': {
                    'conditions': [{
                        'condition_type': 'custom_ready',
                        'condition_scope': {
                            'service': 'PerfDogService', 'port': 23456,
                        },
                        'probe_operation': 'macos.perfdog.ready_v1',
                    }],
                    'probe_interval_seconds': 120,
                    'resume_from_checkpoint': 'collect_cpu_jank_memory',
                    'owner_gate': True,
                    'human_action': '在普通 macOS Terminal 启动并登录 PerfDogService',
                },
            },
        }, now=now + timedelta(minutes=1))
        self.assertEqual(disposition, 'waiting_condition')
        db.session.refresh(task)
        self.assertEqual(task.status, 'waiting_condition')
        self.assertEqual(task.condition_state, 'waiting_condition')
        self.assertTrue(task.owner_gate)
        self.assertEqual(task.checkpoint_json['remaining_steps'], [
            'collect_cpu_jank_memory'])

        body = {
            'command_key': 'one-off-condition-ready-1',
            'condition_type': 'custom_ready',
            'condition_ready': True,
            'expected_resume_fencing_token': 0,
            'facts': {'port': 23456, 'listening': True},
        }
        self.agent()
        response = self.client.post(
            self.base + '/tasks/%s/condition-events' % task.id,
            headers=self._headers(self.other_token), json=body)
        self.assertEqual(response.status_code, 201, response.json)
        result = response.json
        self.assertTrue(result['resumed'])
        self.assertEqual(task.resume_fencing_token, 1)
        self.assertEqual(task.condition_state, 'resuming')
        self.assertEqual(task.status, 'pending')
        resumed = AgentTask.query.order_by(AgentTask.id.desc()).first()
        payload = json.loads(resumed.payload)
        self.assertEqual(payload['resume_fencing_token'], 1)
        self.assertEqual(payload['checkpoint']['remaining_steps'], [
            'collect_cpu_jank_memory'])
        self.assertIn('resume_contract', payload)
        replay = self.client.post(
            self.base + '/tasks/%s/condition-events' % task.id,
            headers=self._headers(self.other_token), json=body)
        self.assertEqual(replay.status_code, 200, replay.json)
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 2)
        stale = self.client.post(
            self.base + '/tasks/%s/condition-events' % task.id,
            headers=self._headers(self.other_token), json=dict(
                body, command_key='one-off-condition-stale-1'))
        self.assertEqual(stale.status_code, 409, stale.json)
        self.assertEqual(stale.json['code'], 'RESUME_CONDITION_FENCED')

    def test_schedule_migration_is_idempotent_and_preserves_task_history(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['ios']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='历史每日任务', status='completed',
            result_summary='昨日已完成', progress=100,
            assignee_claw_id=self.other_claw.id, task_type='performance',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=False, recurrence_type='once')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'migration-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        effective = self.plan.start_date + timedelta(days=1)
        body = {
            'command_key': 'migrate-daily-v1',
            'effective_from': effective.isoformat(),
            'templates': [{
                'test_task_id': task.id,
                'recurrence_type': 'daily',
                'not_before_time': '10:00',
                'due_time': '18:00',
                'auto_dispatch': True,
                'execution_role': 'member_work',
            }],
        }
        first = svc.migrate_schedule_templates(self.sup(), body)
        db.session.commit()
        self.assertFalse(first['replayed'])
        self.assertTrue(task.schedule_enabled)
        self.assertEqual(task.schedule_effective_from, effective)
        self.assertEqual(task.status, 'completed')
        self.assertEqual(task.result_summary, '昨日已完成')
        replay = svc.migrate_schedule_templates(self.sup(), body)
        self.assertTrue(replay['replayed'])
        self.assertEqual(PlanSupervisorReceipt.query.filter_by(
            action='migrate_schedule_templates').count(), 1)

    def test_reassignment_skips_same_role_without_frozen_capabilities(self):
        team = self.scoped_team()
        editor = OpenClawInstance(
            name='Editor only', safe_name='editor-only',
            claw_tag='editor-only', owner='editor',
            project_id=self.project.id, status='工作')
        equivalent = OpenClawInstance(
            name='Mobile performance', safe_name='mobile-performance',
            claw_tag='mobile-performance', owner='performance',
            project_id=self.project.id, status='工作')
        db.session.add_all([editor, equivalent])
        db.session.flush()
        db.session.add_all([
            AgentTeamMember(
                team_id=team.id, claw_id=self.other_claw.id,
                role_key='test_executor',
                specialties_json=['client_performance', 'mobile_package']),
            AgentTeamMember(
                team_id=team.id, claw_id=editor.id,
                role_key='test_executor', specialties_json=['editor']),
            AgentTeamMember(
                team_id=team.id, claw_id=equivalent.id,
                role_key='test_executor',
                specialties_json=['client_performance', 'mobile_package']),
        ])
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='微信小游戏每日性能测试',
            status='assigned', assignee_claw_id=self.other_claw.id,
            task_type='performance', start_date=self.plan.start_date,
            end_date=self.plan.end_date, schedule_enabled=True,
            recurrence_type='daily', not_before_time='00:00',
            due_time='23:59', auto_dispatch=True,
            execution_role='member_work',
            required_capabilities_json=[
                'client_performance', 'mobile_package'])
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'capability-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        svc.sync_plan_stages(self.sup(), now=now)
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        occurrence.status = 'failed'
        occurrence.next_action = 'reassign_stage'
        occurrence.next_check_at = now
        stage.state = 'blocked'
        db.session.commit()

        self.assertEqual(
            svc.advance_occurrence_actions(self.sup(), now=now),
            [equivalent.id])
        db.session.commit()
        self.assertEqual(occurrence.assignee_claw_id, equivalent.id)
        self.assertNotEqual(occurrence.assignee_claw_id, editor.id)
        event = PlanSupervisorEvent.query.filter_by(
            kind='occurrence_reassigned').one()
        self.assertEqual(event.payload_json['from_claw_id'],
                         self.other_claw.id)
        self.assertEqual(event.payload_json['to_claw_id'], equivalent.id)
        self.assertEqual(event.payload_json['capability_match'][
            'required_capabilities'], [
                'client_performance', 'mobile_package'])

    def test_manager_review_creates_control_task_and_fenced_receipt(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='需要经理复核的局部结果',
            status='assigned', assignee_claw_id=self.other_claw.id,
            task_type='automation', start_date=self.plan.start_date,
            end_date=self.plan.end_date, schedule_enabled=True,
            recurrence_type='daily', not_before_time='00:00',
            due_time='23:59', auto_dispatch=False,
            execution_role='member_work')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'manager-review-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        svc.sync_plan_stages(self.sup(), now=now)
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        original = db.session.get(MissionStage, occurrence.mission_stage_id)
        occurrence.status = 'blocked'
        occurrence.recommended_action = 'manager_review'
        occurrence.next_action = 'manager_review'
        occurrence.next_check_at = now
        original.state = 'blocked'
        db.session.commit()

        self.assertEqual(svc.advance_occurrence_actions(
            self.sup(), now=now), [self.main_claw.id])
        db.session.commit()
        control = AgentTask.query.filter_by(
            task_type='plan_control_action', claw_id=self.main_claw.id).one()
        self.assertEqual(occurrence.next_action, 'await_manager_review')
        truth = svc.stage_truth_snapshot(self.sup())
        self.assertEqual(truth['pending_control_actions'][0][
            'action_kind'], 'manager_review')
        claimed = claim_pending_tasks(self.main_claw.id, now=now)[0]
        complete_task(claimed, {
            'claim_token': claimed.claim_token,
            'attempt_no': claimed.attempt_no,
            'fencing_token': claimed.fencing_token,
            'status': 'completed',
            'result': {
                'decision': 'publish_partial_closeout',
                'evidence': [{'ref': 'report://partial'}],
                'summary': '已复核，发布局部结论',
            },
        }, now=now + timedelta(minutes=1))
        db.session.commit()
        self.assertEqual(occurrence.status, 'analysis_incomplete')
        self.assertEqual(occurrence.next_action, 'none')
        self.assertEqual(occurrence.action_metadata_json[
            'manager_review_receipt']['agent_task_id'], control.task_id)
        self.assertEqual(PlanSupervisorEvent.query.filter_by(
            kind='manager_review_decided').count(), 1)

    def test_environment_repair_is_read_only_idempotent_and_owner_gated(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='共享环境异常', status='assigned',
            assignee_claw_id=self.other_claw.id, task_type='automation',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='00:00', due_time='23:59',
            auto_dispatch=False, execution_role='member_work')
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'environment-repair-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        svc.sync_plan_stages(self.sup(), now=now)
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        original = db.session.get(MissionStage, occurrence.mission_stage_id)
        occurrence.status = 'blocked'
        occurrence.recommended_action = 'create_environment_repair'
        occurrence.next_action = 'create_environment_repair'
        occurrence.next_check_at = now
        original.state = 'blocked'
        db.session.commit()

        self.assertEqual(
            svc.advance_occurrence_actions(self.sup(), now=now),
            [self.other_claw.id],
        )
        db.session.commit()
        control = AgentTask.query.filter_by(
            task_type='plan_control_action').one()
        payload = json.loads(control.payload)
        self.assertEqual(payload['allowed_operations'], [
            'inspect_environment', 'diagnose', 'propose_patch'])
        self.assertEqual(payload['allowed_paths'], [])
        self.assertEqual(payload['allowed_repositories'], [])
        self.assertEqual(payload['side_effect_policy']['mode'], 'read_only')
        self.assertFalse(payload['side_effect_policy'][
            'external_mutations_allowed'])
        self.assertFalse(payload['side_effect_policy']['git_push_allowed'])

        # A stale supervisor snapshot must not create another task or consume
        # another action attempt while the first task is active.
        attempts = occurrence.action_attempt_count
        occurrence.next_action = 'create_environment_repair'
        occurrence.next_check_at = now
        db.session.commit()
        svc.advance_occurrence_actions(self.sup(), now=now)
        db.session.commit()
        self.assertEqual(AgentTask.query.filter_by(
            task_type='plan_control_action').count(), 1)
        self.assertEqual(occurrence.action_attempt_count, attempts)

        claimed = claim_pending_tasks(self.other_claw.id, now=now)[0]
        complete_task(claimed, {
            'claim_token': claimed.claim_token,
            'attempt_no': claimed.attempt_no,
            'fencing_token': claimed.fencing_token,
            'status': 'completed',
            'result': {
                'status': 'passed',
                'summary': '只读诊断完成，等待Owner批准候选diff',
                'evidence': [{'ref': 'proposal://world-124'}],
            },
        }, now=now + timedelta(minutes=1))
        db.session.commit()
        self.assertTrue(occurrence.owner_gate)
        self.assertEqual(
            occurrence.condition_state, 'environment_repair_proposed')
        self.assertEqual(occurrence.next_action, 'owner_action_required')
        self.assertEqual(original.state, 'blocked')
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 0)

    def test_occurrence_keeps_flow_binding_after_template_changes(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        task = TestTask(
            plan_id=self.plan.id, name='固定 Flow 每日任务', status='blocked',
            assignee_claw_id=self.other_claw.id, task_type='automation',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='00:00', due_time='23:59',
            auto_dispatch=True, execution_role='member_work',
            execution_mode='workflow',
            workflow_definition_id=self.flow_a.id,
            workflow_start_vars_json={'frozen': 'yes'})
        db.session.add(task)
        db.session.commit()
        self.post('start', {
            'command_key': 'frozen-flow-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        now = datetime.combine(self.plan.start_date, time(10, 0))
        svc.sync_plan_stages(self.sup(), now=now)
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        task.execution_mode = 'ordinary_agent_task'
        task.workflow_definition_id = None
        task.workflow_start_vars_json = {}
        db.session.commit()

        self.assertEqual(svc.promote_and_dispatch_due_occurrences(
            self.sup(), now=now), [self.other_claw.id])
        db.session.commit()
        self.assertEqual(occurrence.execution_mode, 'workflow')
        self.assertEqual(occurrence.workflow_definition_id, self.flow_a.id)
        self.assertEqual(occurrence.workflow_start_vars_json,
                         {'frozen': 'yes'})
        self.assertIsNotNone(occurrence.workflow_run_id)
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_admin_resume_atomically_dispatches_due_agent_task_and_one_flow(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        ordinary = TestTask(
            plan_id=self.plan.id, name='到点普通回归', status='completed',
            assignee_claw_id=self.other_claw.id, task_type='functional',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='00:00', due_time='23:59',
            auto_dispatch=True, execution_role='member_work')
        flow_task = TestTask(
            plan_id=self.plan.id, name='到点 Flow 冒烟', status='blocked',
            assignee_claw_id=self.other_claw.id, task_type='automation',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='00:00', due_time='23:59',
            auto_dispatch=True, execution_role='member_work',
            workflow_definition_id=self.flow_a.id,
            workflow_start_vars_json={})
        local_block = TestTask(
            plan_id=self.plan.id, name='局部阻断项', status='blocked',
            assignee_claw_id=self.other_claw.id, task_type='performance')
        db.session.add_all([ordinary, flow_task, local_block])
        db.session.commit()
        started = self.post('start', {
            'command_key': 'atomic-resume-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        self.assertEqual(started.status_code, 200, started.json)
        self.agent()
        self.claim('atomic-resume-claim')
        blocked = self.post('decision', {
            'command_key': 'atomic-resume-block', **self.credentials(),
            'cursor': self.sup().lease_cursor, 'outcome': 'blocked',
            'block_scope': 'plan',
            'plan_block_reason_code': 'global_environment_unavailable',
            'plan_block_evidence': [{'ref': 'probe://global'}],
            'summary': '全团队环境不可用，暂停整个计划',
        })
        self.assertEqual(blocked.status_code, 200, blocked.json)
        self._login_admin()
        body = {'command_key': 'atomic-resume-v1',
                'reason': '全局环境恢复，补派到点任务'}
        resumed = self.client.post(self.base + '/resume', json=body)
        self.assertEqual(resumed.status_code, 200, resumed.json)
        ordinary_occ = TestTaskOccurrence.query.filter_by(
            test_task_id=ordinary.id).one()
        flow_occ = TestTaskOccurrence.query.filter_by(
            test_task_id=flow_task.id).one()
        self.assertEqual(ordinary_occ.status, 'dispatched')
        self.assertIsNotNone(ordinary_occ.agent_task_id)
        self.assertEqual(flow_occ.status, 'dispatched')
        self.assertIsNotNone(flow_occ.workflow_run_id)
        self.assertEqual(WorkflowRun.query.count(), 1)
        flow_stage = db.session.get(MissionStage, flow_occ.mission_stage_id)
        self.assertEqual(flow_stage.workflow_run_id, flow_occ.workflow_run_id)
        self.assertEqual(local_block.status, 'blocked')
        replay = self.client.post(self.base + '/resume', json=body)
        self.assertEqual(replay.status_code, 200, replay.json)
        self.assertTrue(replay.json['replayed'])
        self.assertEqual(WorkflowRun.query.count(), 1)
        self.assertEqual(AgentTask.query.filter_by(
            task_type='test_plan_agent_task').count(), 1)

    def test_reconciliation_stage_becomes_claimable_control_agent_task(self):
        team = self.scoped_team()
        db.session.add(AgentTeamMember(
            team_id=team.id, claw_id=self.other_claw.id,
            role_key='test_executor', specialties_json=['editor']))
        self.plan.team_id = team.id
        db.session.commit()
        self.post('start', {
            'command_key': 'control-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        stage = MissionStage(
            mission_id=self.sup().mission_id,
            stage_key='recover_run_844_test_a1', stage_version=1,
            role_key='test_executor', assigned_claw_id=self.other_claw.id,
            state='ready', input_snapshot_json={
                'kind': 'workflow_execution_reconciliation',
                'workflow_run_id': 844,
                'resolve_api': '/resolve', 'recover_api': '/recover',
            }, evidence_refs_json=[])
        db.session.add(stage)
        db.session.commit()
        self.assertEqual(svc.dispatch_ready_control_stages(self.sup()), [
            self.other_claw.id])
        db.session.commit()
        control = AgentTask.query.filter_by(
            task_type='plan_control_action').one()
        self.assertEqual(stage.state, 'dispatched')
        self.assertEqual(svc.task_dispatch_receipts(self.sup())[-1][
            'execution_mode'], 'plan_control_action')
        claimed = claim_pending_tasks(self.other_claw.id)[0]
        self.assertEqual(claimed.id, control.id)
        self.assertEqual(stage.state, 'running')
        # The dedicated execution-reconciliation API verifies the receipt and
        # marks the stage completed before the AgentTask closes.
        stage.state = 'completed'
        db.session.commit()
        complete_task(claimed, {
            'claim_token': claimed.claim_token,
            'attempt_no': claimed.attempt_no,
            'fencing_token': claimed.fencing_token,
            'status': 'completed',
            'result': {
                'execution_stopped': True,
                'side_effects_reconciled': True,
                'receipt_ref': 'receipt://844/a1',
            },
        })
        self.assertEqual(stage.state, 'completed')

    def test_manager_work_occurrence_is_explicitly_assignable(self):
        team = self.scoped_team()
        self.plan.team_id = team.id
        db.session.commit()
        self.post('start', {
            'command_key': 'manager-task-start', 'team_id': team.id,
            'orchestrator_claw_id': self.main_claw.id})
        task = TestTask(
            plan_id=self.plan.id, name='每日风险复盘', status='assigned',
            assignee_claw_id=self.main_claw.id, task_type='other',
            start_date=self.plan.start_date, end_date=self.plan.end_date,
            schedule_enabled=True, recurrence_type='daily',
            not_before_time='09:00', auto_dispatch=False,
            execution_role='manager_work')
        db.session.add(task)
        db.session.flush()
        svc.sync_plan_stages(
            self.sup(), now=datetime.combine(self.plan.start_date, time(9, 0)))
        db.session.commit()
        occurrence = TestTaskOccurrence.query.filter_by(
            test_task_id=task.id).one()
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        self.assertEqual(stage.assigned_claw_id, self.main_claw.id)
        self.assertEqual(stage.role_key, 'test_manager')
        self.assertEqual(stage.state, 'ready')

    def test_timer_restarts_and_unchanged_watchdog_stays_quiet(self):
        self.start()
        self.claim()
        self.post('decision', self.wait_body())
        due = self.sup().next_check_at
        self.assertEqual(svc.sweep(now=due - timedelta(seconds=1)), 0)
        self.assertEqual(svc.sweep(now=due), 1)
        msg = self.sup().wake_message_id
        db.session.remove()  # process-restart equivalent: state is entirely in DB
        self.assertEqual(svc.sweep(now=due + timedelta(seconds=1)), 0)
        self.assertEqual(self.sup().wake_message_id, msg)

    def test_expired_lease_can_recover_but_old_turn_cannot_write(self):
        self.start()
        first = self.claim()
        old = self.credentials()
        expires = self.sup().lease_expires_at
        self.assertEqual(svc.sweep(now=expires + timedelta(seconds=1)), 0)
        retry_at = self.sup().next_check_at
        self.assertGreater(retry_at, expires)
        self.assertEqual(svc.sweep(now=retry_at), 1)
        self.assertEqual(self.post('heartbeat', old).status_code, 409)
        self.claim('claim-2')
        self.assertGreater(self.sup().fencing_token, first['supervision']['fencing_token'])

    def test_event_during_turn_is_not_lost_when_timer_is_scheduled(self):
        self.start()
        self.claim()
        svc.add_event(self.plan.id, 'run_terminal', 'finished-1', {'run_id': 9})
        db.session.commit()
        result = self.post('decision', self.wait_body())
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.sup().status, 'pending')
        self.assertGreater(self.sup().cursor, self.sup().acknowledged_cursor)

    def test_late_lower_event_id_is_sequenced_not_skipped(self):
        self.start()
        self.claim()
        self.post('decision', self.wait_body())
        low = PlanSupervisorEvent.query.order_by(PlanSupervisorEvent.id).first()
        low.sequence = None  # Simulates a lower allocated ID becoming visible later.
        previous_cursor = self.sup().cursor
        db.session.commit()
        svc.ingest(self.sup())
        db.session.commit()
        self.assertGreater(low.sequence, previous_cursor)

    def test_end_date_fences_turn_and_preserves_runs(self):
        self.start()
        self.claim()
        count = WorkflowRun.query.count()
        svc.sweep(now=svc.ends_at(self.plan))
        self.assertEqual(self.sup().status, 'expired')
        self.assertIsNone(self.sup().next_check_at)
        self.assertIsNone(self.sup().wake_message_id)
        self.assertEqual(WorkflowRun.query.count(), count)
        self.assertEqual(self.post('heartbeat', self.credentials()).status_code, 409)

    def test_mission_creation_binding_and_dispatch_require_plan_lease(self):
        self.flow_a.executor_acl_json = {'claw_ids': [self.main_claw.id]}
        db.session.commit()
        self.start()
        self.claim()
        mission_body = {'project_id': self.project.id, 'main_claw_id': self.main_claw.id,
            'objective': 'supervised work', 'test_plan_id': self.plan.id, 'max_child_runs': 2,
            'plan_supervision': self.credentials()}
        response = self.client.post('/api/v1/workflow-missions', json=mission_body, headers=self._headers())
        self.assertEqual(response.status_code, 201, response.json)
        mission_id = response.json['id']
        self.assertEqual(self.sup().mission_id, mission_id)
        repeated = self.client.post('/api/v1/workflow-missions', json=mission_body, headers=self._headers())
        self.assertEqual(repeated.status_code, 409, repeated.json)
        self.assertEqual(WorkflowMission.query.count(), 1)
        path = '/api/v1/workflow-missions/%s/dispatch' % mission_id
        body = {'workflow_definition_id': self.flow_a.id, 'decision_key': 'first-run'}
        self.assertEqual(self.client.post(path, json=body, headers=self._headers()).status_code, 409)
        conflict = dict(body, plan_supervision=self.credentials(),
                        worker_id='other-worker',
                        fencing_token=self.sup().fencing_token)
        invalid = self.client.post(path, json=conflict, headers=self._headers())
        self.assertEqual(invalid.status_code, 400, invalid.json)
        self.assertEqual(invalid.json['code'], 'PLAN_LEASE_CONFLICT')
        body['plan_supervision'] = self.credentials()
        dispatched = self.client.post(path, json=body, headers=self._headers())
        self.assertEqual(dispatched.status_code, 201, dispatched.json)
        self.assertEqual(WorkflowRun.query.count(), 1)
        self.assertEqual(self.client.post(path, json=body, headers=self._headers()).status_code, 200)
        self.assertEqual(WorkflowRun.query.count(), 1)
        run = WorkflowRun.query.first()
        run.status = 'failed'
        db.session.commit()
        self.assertGreater(PlanSupervisorEvent.query.filter_by(kind='run_terminal').count(), 0)
        heartbeat = self.client.post('/api/v1/workflow-runs/%s/steps/analyze/heartbeat' % run.id,
                                     json={'worker_id': 'exec'}, headers=self._headers())
        self.assertEqual(heartbeat.status_code, 409)
        self.assertGreater(PlanSupervisorEvent.query.filter_by(kind='heartbeat_anomaly').count(), 0)

    def test_chat_done_cannot_claim_future_work_is_scheduled(self):
        self.start()
        message_id = self.sup().wake_message_id
        path = '/api/openclaws/%s/messages/%s/done' % (self.main_claw.id, message_id)
        response = self.client.put(path, json={'llm_response': '明早继续'}, headers=self._headers())
        self.assertEqual(response.status_code, 409, response.json)
        self.claim()
        self.post('decision', self.wait_body())
        self.assertEqual(self.client.put(path, json={}, headers=self._headers()).status_code, 200)

    def test_expired_turns_back_off_without_permanent_plan_block(self):
        self.start()
        for index in range(3):
            self.claim('attempt-%s' % index)
            self.assertEqual(
                svc.sweep(now=self.sup().lease_expires_at + timedelta(seconds=1)),
                0)
            self.assertEqual(svc.sweep(now=self.sup().next_check_at), 1)
        self.assertEqual(self.sup().status, 'pending')
        self.assertIsNotNone(self.sup().next_check_at)
        self.assertNotEqual(self.sup().resume_condition, 'manual')

    def test_future_start_is_persisted_without_early_wake(self):
        self.plan.start_date = (_now() + timedelta(days=1)).date()
        db.session.commit()
        self.start()
        self.assertIsNone(self.sup().wake_message_id)
        self.assertEqual(svc.sweep(now=self.sup().next_check_at - timedelta(seconds=1)), 0)
        self.assertEqual(svc.sweep(now=self.sup().next_check_at), 1)

    def test_task_changes_are_durable_and_rollback_does_not_wake(self):
        self.start()
        initial = PlanSupervisorEvent.query.count()
        task = TestTask(plan_id=self.plan.id, name='task', status='assigned', assignee_claw_id=self.main_claw.id)
        db.session.add(task)
        db.session.flush()
        self.assertGreater(PlanSupervisorEvent.query.count(), initial)
        db.session.rollback()
        self.assertEqual(PlanSupervisorEvent.query.count(), initial)

    def test_feature_off_denies_start_and_skips_watchdog(self):
        self.app.config['PLAN_SUPERVISION_ENABLED'] = False
        self.assertEqual(self.post('start', {'command_key': 'x', 'orchestrator_claw_id': self.main_claw.id}).status_code, 503)
        self.assertEqual(svc.sweep(), 0)

    def test_stop_needs_fence_and_blocked_resume_needs_human(self):
        self.start()
        self.claim()
        self.assertEqual(self.post('stop', {'command_key': 'stop'}).status_code, 409)
        self.assertEqual(self.post('stop', {'command_key': 'stop', **self.credentials()}).status_code, 200)
        self.assertEqual(self.post('resume', {'command_key': 'resume'}).status_code, 403)
        self._login_admin()
        self.assertEqual(self.client.post(self.base + '/resume', json={
            'command_key': 'resume', 'reason': '管理员恢复监督'}).status_code, 200)

    def test_progress_repetition_cannot_hide_stall_and_heartbeat_is_quiet(self):
        mission_data = self._create_mission()
        self.start()
        self.sup().mission_id = mission_data['id']
        now = _now()
        run = WorkflowRun(definition_id=self.flow_a.id, project_id=self.project.id, status='running', created_at=now)
        db.session.add(run)
        db.session.flush()
        db.session.add(WorkflowMissionDispatch(mission_id=mission_data['id'], definition_id=self.flow_a.id,
            workflow_run_id=run.id, decision_key='x', idempotency_key='x', request_hash='0' * 64,
            created_by_claw_id=self.main_claw.id))
        step = WorkflowRunStep(run_id=run.id, step_id='work', name='work', step_type='agent_task',
            status='running', started_at=now-timedelta(minutes=15), progress_at=now-timedelta(minutes=11),
            heartbeat_at=now, progress_percent=10, health_status='healthy')
        db.session.add(step)
        db.session.commit()
        svc.sweep(now=now)
        stale = PlanSupervisorEvent.query.filter_by(kind='stale').count()
        self.assertEqual(stale, 1)
        total = PlanSupervisorEvent.query.count()
        step.progress_at = now
        step.heartbeat_at = now + timedelta(seconds=30)
        db.session.commit()
        svc.sweep(now=now + timedelta(seconds=30))
        self.assertEqual(PlanSupervisorEvent.query.count(), total)
        self.assertEqual(PlanSupervisorEvent.query.filter_by(kind='stale').count(), stale)

    def test_event_pagination_uses_claimed_cursor(self):
        self.start()
        for index in range(250):
            svc.add_event(self.plan.id, 'task_changed', ['page', index], {'task_id': index})
        svc.ingest(self.sup())
        svc.ingest(self.sup())
        db.session.commit()
        self.claim()
        first = self.client.get(self.base, headers=self._headers()).json
        self.assertEqual(len(first['events']), 200)
        self.assertTrue(first['has_more'])
        second = self.client.get(self.base + '?after=%s' % first['next_cursor'], headers=self._headers()).json
        self.assertFalse(second['has_more'])
        self.assertEqual(second['next_cursor'], self.sup().lease_cursor)
        self.assertEqual(len(first['events']) + len(second['events']), self.sup().lease_cursor)

    def test_foreign_project_cannot_read_or_start_supervision(self):
        self.start()
        self.other_claw.project_id = self.other_project.id
        db.session.commit()
        self.assertEqual(self.client.get(self.base, headers=self._headers(self.other_token)).status_code, 403)
        self.assertEqual(self.post('start', {'command_key':'foreign',
            'orchestrator_claw_id': self.other_claw.id}, self.other_token).status_code, 403)
