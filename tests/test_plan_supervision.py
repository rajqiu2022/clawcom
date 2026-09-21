"""Plan wake/lease/receipt contract, including actual Mission dispatch fencing."""
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_workflow_missions_api as fixtures
from app import db
from app.models import (TestPlan, TestTask, ClawMessage, WorkflowRun, WorkflowRunStep,
                        WorkflowMission, WorkflowMissionDispatch, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt
from app.services import plan_supervision as svc


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
        self.assertEqual(WorkflowRun.query.count(), 0)
        self.assertEqual(self.plan.status, 'active')
        self.assertEqual(self.post('start', {'command_key': 'start-2',
            'orchestrator_claw_id': self.other_claw.id}).status_code, 409)

    def scoped_team(self):
        from app.models import AgentTeam
        team = AgentTeam(project_id=self.project.id, name='First team', objective='Scoped test',
                         primary_manager_claw_id=self.main_claw.id, status='active',
                         policy_json={'allowed_definition_ids': [], 'max_child_runs': 1})
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
        self.assertEqual(svc.sweep(now=expires + timedelta(seconds=1)), 1)
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

    def test_three_expired_turns_block_instead_of_infinite_model_retries(self):
        self.start()
        for index in range(3):
            self.claim('attempt-%s' % index)
            svc.sweep(now=self.sup().lease_expires_at + timedelta(seconds=1))
        self.assertEqual(self.sup().status, 'blocked')
        self.assertIsNone(self.sup().wake_message_id)
        self.assertEqual(svc.sweep(now=_now() + timedelta(hours=2)), 0)

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
        self.assertEqual(self.client.post(self.base + '/resume', json={'command_key': 'resume'}).status_code, 200)

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
