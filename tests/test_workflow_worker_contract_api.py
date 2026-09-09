import sys
import inspect
import json
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.api import workflows as workflows_api  # noqa: E402
from app.models import (AgentTask, OpenClawInstance, Project, SystemConfig, TestReport as ReportModel, User,  # noqa: E402
                        WecomSendLog, WorkflowDefinition, ShiftLeftFinding,
                        WorkflowEvidenceManifest,
                        WorkflowRun, WorkflowRunStep, hash_token)


class WorkflowWorkerContractApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        project = Project(name='Alpha')
        db.session.add(project)
        db.session.flush()
        self.token = 'hub_tk_worker_a'
        self.other_token = 'hub_tk_worker_b'
        self.claw = OpenClawInstance(
            name='Worker A',
            safe_name='worker-a',
            claw_tag='worker-a',
            owner='alice',
            project_id=project.id,
            project_name='Alpha',
            api_token_hash=hash_token(self.token),
        )
        self.other_claw = OpenClawInstance(
            name='Worker B',
            safe_name='worker-b',
            claw_tag='worker-b',
            owner='bob',
            project_id=project.id,
            project_name='Alpha',
            api_token_hash=hash_token(self.other_token),
        )
        db.session.add_all([self.claw, self.other_claw])
        db.session.flush()
        self.definition = WorkflowDefinition(
            workflow_key='worker-contract',
            name='Worker Contract',
            project_id=project.id,
            definition_json={'steps': []},
            owner_type='claw',
            owner_id=self.claw.id,
            executor_acl_json={'claw_ids': [self.claw.id, self.other_claw.id]},
        )
        db.session.add(self.definition)
        db.session.flush()
        self.run = WorkflowRun(
            definition_id=self.definition.id,
            run_name='Run',
            status='running',
            current_step_id='worker_step',
            project_id=project.id,
            context_json={'workflow_start': {
                'executor_claw_ids': [self.claw.id, self.other_claw.id],
            }},
        )
        db.session.add(self.run)
        db.session.flush()
        self.worker_step = WorkflowRunStep(
            run_id=self.run.id,
            step_id='worker_step',
            position=1,
            name='Worker Step',
            step_type='worker_task',
            status='running',
            runner='worker.demo',
        )
        self.agent_step = WorkflowRunStep(
            run_id=self.run.id,
            step_id='agent_step',
            position=2,
            name='Agent Step',
            step_type='agent_task',
            status='running',
            target_claw_id=self.claw.id,
            runner='agent.demo',
        )
        db.session.add_all([self.worker_step, self.agent_step])
        db.session.commit()

    def _headers(self, token=None):
        return {'Authorization': f'Bearer {token or self.token}'}

    def _post(self, step_id, action, body, token=None):
        return self.client.post(
            f'/api/v1/workflow-runs/{self.run.id}/steps/{step_id}/{action}',
            json=body,
            headers=self._headers(token),
        )

    def _post_with_headers(self, step_id, action, body, headers):
        return self.client.post(
            f'/api/v1/workflow-runs/{self.run.id}/steps/{step_id}/{action}',
            json=body,
            headers=headers,
        )

    def _worker_step(self):
        return WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='worker_step').first()

    def test_terminal_block_without_business_evidence_is_not_completed(self):
        self.run.status = 'blocked'
        self.run.business_conclusion = 'COMPLETED'
        self.run.automation_conclusion = 'COMPLETED_WITH_AUTOMATION_ERROR'
        self.run.evidence_ingest_status = 'EVIDENCE_INGEST_INCOMPLETE'
        self.worker_step.status = 'blocked'
        self.worker_step.outputs_json = {}
        self.worker_step.contract_result_json = {'code': 'CONTRACT_INVALID'}
        db.session.commit()

        workflows_api._sync_run_conclusions(self.run)

        self.assertEqual(self.run.business_conclusion, 'INCONCLUSIVE')
        self.assertEqual(
            self.run.automation_conclusion, 'AUTOMATION_ENV_BLOCKED')

    def test_explicit_business_pass_remains_completed(self):
        self.run.status = 'succeeded'
        self.worker_step.status = 'passed'
        self.worker_step.outputs_json = {
            'business_passed': True,
            'business_conclusion': 'COMPLETED',
        }
        db.session.commit()

        workflows_api._sync_run_conclusions(self.run)

        self.assertEqual(self.run.business_conclusion, 'COMPLETED')

    def test_success_without_business_semantics_keeps_conclusion_empty(self):
        self.run.status = 'succeeded'
        self.run.business_conclusion = 'COMPLETED'
        self.worker_step.status = 'passed'
        self.worker_step.outputs_json = {}
        db.session.commit()

        workflows_api._sync_run_conclusions(self.run)

        self.assertEqual(self.run.business_conclusion, '')

    def test_blocked_cleanup_dispatches_opted_in_analysis_then_remains_blocked(self):
        self.definition.definition_json = {
            'key': 'cleanup-analysis',
            'name': 'Cleanup analysis',
            'steps': [
                {'id': 'safe_stop', 'name': 'Safe stop'},
                {
                    'id': 'analysis',
                    'name': 'Analysis report',
                    'depends_on': ['safe_stop'],
                    # The current Definition is intentionally strict. The Run
                    # snapshot below is permissive and must remain authoritative.
                    'inputs': {},
                },
            ],
        }
        continuation_run = WorkflowRun(
            definition_id=self.definition.id,
            run_name='Cleanup analysis run',
            status='running',
            current_step_id='safe_stop',
            project_id=self.definition.project_id,
        )
        db.session.add(continuation_run)
        db.session.flush()
        safe_stop = WorkflowRunStep(
            run_id=continuation_run.id,
            step_id='safe_stop',
            position=0,
            name='Safe stop',
            step_type='worker_task',
            status='blocked',
            blocker_json={'type': 'SAFE_STOP_FAILED'},
            step_config_json={'id': 'safe_stop', 'inputs': {}},
        )
        analysis = WorkflowRunStep(
            run_id=continuation_run.id,
            step_id='analysis',
            position=1,
            name='Analysis report',
            step_type='agent_task',
            status='pending',
            depends_on_json=['safe_stop'],
            step_config_json={
                'id': 'analysis',
                'depends_on': ['safe_stop'],
                'inputs': {'run_even_if_upstream_blocked': True},
            },
        )
        db.session.add_all([safe_stop, analysis])
        db.session.flush()

        with patch.object(workflows_api, '_dispatch_step_message'):
            workflows_api._recompute_run_status(continuation_run, 'test')

        self.assertEqual(analysis.status, 'running')
        self.assertEqual(continuation_run.status, 'running')
        self.assertEqual(continuation_run.current_step_id, 'analysis')
        self.assertIsNone(continuation_run.finished_at)

        analysis.status = 'passed'
        analysis.finished_at = datetime.now()
        workflows_api._recompute_run_status(continuation_run, 'test')

        self.assertEqual(continuation_run.status, 'blocked')
        self.assertEqual(continuation_run.current_step_id, 'safe_stop')
        self.assertEqual(
            continuation_run.blocker_json['type'], 'SAFE_STOP_FAILED')

    def test_claw_started_flow_routes_agent_role_to_one_bound_worker(self):
        definition = WorkflowDefinition(
            workflow_key='single-flow-worker',
            name='Single Flow Worker',
            project_id=self.definition.project_id,
            definition_json={
                'key': 'single-flow-worker',
                'name': 'Single Flow Worker',
                'steps': [{
                    'id': 'act_as_other',
                    'name': 'Act As Other Agent',
                    'type': 'agent_task',
                    'runner': 'agent.demo',
                    'target_claw_id': self.other_claw.id,
                    'target_agent': self.other_claw.name,
                }],
            },
            owner_type='claw',
            owner_id=self.claw.id,
            executor_acl_json={'claw_ids': [self.claw.id]},
        )
        db.session.add(definition)
        db.session.commit()

        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': definition.id,
            'start_vars': {'branch': 'dev'},
        }, headers=self._headers())

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        receipt = response.get_json()
        self.assertLess(len(response.data), 4096)
        self.assertEqual(receipt['run_id'], receipt['id'])
        self.assertEqual(
            receipt['readback_url'],
            f"/api/v1/workflow-runs/{receipt['run_id']}")
        readback = self.client.get(
            receipt['readback_url'], headers=self._headers())
        self.assertEqual(readback.status_code, 200, readback.get_data(as_text=True))
        payload = readback.get_json()
        workflow_start = payload['context']['workflow_start']
        self.assertEqual(workflow_start['worker_claw_id'], self.claw.id)
        self.assertEqual(workflow_start['worker_binding_mode'], 'single_flow_worker')

        step = WorkflowRunStep.query.filter_by(
            run_id=payload['id'], step_id='act_as_other').one()
        self.assertEqual(step.target_claw_id, self.other_claw.id)
        task = AgentTask.query.filter_by(task_type='workflow_agent_task').one()
        self.assertEqual(task.claw_id, self.claw.id)
        task_payload = json.loads(task.payload)
        self.assertEqual(task_payload['execution_route'], {
            'mode': 'single_flow_worker',
            'worker_claw_id': self.claw.id,
            'acting_claw_id': self.other_claw.id,
            'acting_agent': self.other_claw.name,
            'acting_post': '',
        })

        worker_tasks = self.client.get(
            '/api/v1/workflow-runs/worker/tasks', headers=self._headers())
        acting_agent_tasks = self.client.get(
            '/api/v1/workflow-runs/worker/tasks',
            headers=self._headers(self.other_token))
        self.assertEqual(worker_tasks.status_code, 200)
        # Agent tasks are delivered through AgentTask/SSE.  The Job Service
        # pull endpoint must never surface them as worker work.
        self.assertNotIn(
            'act_as_other',
            [item['step_id'] for item in worker_tasks.get_json()])
        self.assertEqual(acting_agent_tasks.status_code, 200)
        self.assertNotIn(
            'act_as_other',
            [item['step_id'] for item in acting_agent_tasks.get_json()])

        rejected = self.client.post(
            f"/api/v1/workflow-runs/{payload['id']}/steps/act_as_other/result",
            json={'status': 'passed', 'summary': 'wrong worker'},
            headers=self._headers(self.other_token),
        )
        self.assertEqual(rejected.status_code, 403)

        accepted = self.client.post(
            f"/api/v1/workflow-runs/{payload['id']}/steps/act_as_other/result",
            json={'status': 'passed', 'summary': 'done'},
            headers=self._headers(),
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))

    def test_worker_task_list_excludes_agent_tasks(self):
        listed = self.client.get(
            '/api/v1/workflow-runs/worker/tasks',
            headers=self._headers(),
        )

        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(
            [item['step_id'] for item in listed.get_json()],
            ['worker_step'],
        )
        self.assertEqual(listed.get_json()[0]['step_type'], 'worker_task')

    def test_worker_task_list_does_not_require_database_json_functions(self):
        self.run.context_json = {'workflow_start': {
            'worker_claw_id': self.claw.id,
            'worker_binding_mode': 'single_flow_worker',
        }}
        self.worker_step.target_claw_id = None
        db.session.commit()

        def reject_json_sql(_conn, _cursor, statement, _parameters, _context,
                            _executemany):
            if 'JSON_EXTRACT' in statement.upper():
                raise AssertionError(
                    'worker task polling must support databases without JSON_EXTRACT')

        event.listen(db.engine, 'before_cursor_execute', reject_json_sql)
        try:
            listed = self.client.get(
                '/api/v1/workflow-runs/worker/tasks',
                headers=self._headers(),
            )
        finally:
            event.remove(db.engine, 'before_cursor_execute', reject_json_sql)

        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(
            [item['step_id'] for item in listed.get_json()], ['worker_step'])

    def test_explicit_no_response_auto_block_false_keeps_worker_task_running(self):
        self.worker_step.step_config_json = {
            'auto_block_on_no_response': False,
            'heartbeat_auto_block': False,
        }
        self.worker_step.dispatched_at = datetime.now() - timedelta(minutes=16)
        self.worker_step.started_at = self.worker_step.dispatched_at
        self.worker_step.progress_at = self.worker_step.dispatched_at
        self.worker_step.heartbeat_at = None
        db.session.commit()

        listed = self.client.get(
            '/api/v1/workflow-runs/worker/tasks', headers=self._headers())

        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(self._worker_step().status, 'running')
        self.assertEqual(
            [item['step_id'] for item in listed.get_json()], ['worker_step'])

    def test_bound_worker_can_read_private_run_without_management_access(self):
        owner = User(username='private-flow-owner', role='super_admin')
        owner.set_password('secret')
        db.session.add(owner)
        db.session.flush()
        definition = WorkflowDefinition(
            workflow_key='private-bound-worker',
            name='Private Bound Worker',
            project_id=self.definition.project_id,
            definition_json={
                'key': 'private-bound-worker',
                'name': 'Private Bound Worker',
                'context': {'executor_operation_policy': {
                    'mode': 'workflow_run_executor',
                    'require_worker_binding': True,
                }},
                'steps': [{
                    'id': 'bootstrap',
                    'name': 'Bootstrap',
                    'type': 'worker_task',
                    'runner': 'deepflow.racinggo.runtime_bootstrap_v1',
                }],
            },
            owner_type='user',
            owner_id=owner.id,
            visibility_scope='private',
            executor_acl_json={'user_ids': [owner.id]},
        )
        db.session.add(definition)
        db.session.commit()
        with self.client.session_transaction() as flask_session:
            flask_session['user_id'] = owner.id
        created = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': definition.id,
            'worker_claw_id': self.other_claw.id,
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        with self.client.session_transaction() as flask_session:
            flask_session.pop('user_id', None)

        readback = self.client.get(
            f'/api/v1/workflow-runs/{run_id}',
            headers=self._headers(self.other_token),
        )
        unrelated = self.client.get(
            f'/api/v1/workflow-runs/{run_id}',
            headers=self._headers(),
        )
        forbidden_delete = self.client.delete(
            f'/api/v1/workflow-runs/{run_id}',
            headers=self._headers(self.other_token),
        )

        self.assertEqual(readback.status_code, 200, readback.get_data(as_text=True))
        self.assertEqual(
            readback.get_json()['context']['workflow_start']['worker_claw_id'],
            self.other_claw.id)
        self.assertEqual(unrelated.status_code, 404)
        self.assertEqual(forbidden_delete.status_code, 404)

    def test_worker_claim_conflicts_until_lease_expires(self):
        first = self._post('worker_step', 'claim', {
            'worker_id': 'worker-a',
            'lease_seconds': 300,
        })
        conflict = self._post('worker_step', 'claim', {
            'worker_id': 'worker-b',
            'lease_seconds': 30,
        }, token=self.other_token)

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        first_token = first.get_json()['step']['claim_fencing_token']
        self.assertEqual(first_token, 1)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'claim_conflict')

        step = self._worker_step()
        step.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        reclaimed = self._post('worker_step', 'claim', {
            'worker_id': 'worker-b',
        }, token=self.other_token)
        self.assertEqual(reclaimed.status_code, 200, reclaimed.get_data(as_text=True))
        self.assertEqual(self._worker_step().claimed_claw_id, self.other_claw.id)
        self.assertEqual(
            reclaimed.get_json()['step']['claim_fencing_token'], first_token + 1)

    def test_fencing_is_opt_in_and_required_on_full_worker_writeback_chain(self):
        claimed = self._post('worker_step', 'claim', {
            'worker_id': 'worker-a',
            'lease_seconds': 300,
        })
        token = claimed.get_json()['step']['claim_fencing_token']

        # Existing Workers remain compatible until a Step opts into fencing.
        legacy = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-a',
        })
        self.assertEqual(legacy.status_code, 200, legacy.get_data(as_text=True))

        step = self._worker_step()
        step.step_config_json = {'require_fencing_token': True}
        db.session.commit()
        missing = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-a',
        })
        stale = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-a',
            'fencing_token': token + 1,
        })
        accepted = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-a',
            'fencing_token': token,
        })
        progress_missing = self._post('worker_step', 'progress', {
            'worker_id': 'worker-a',
            'phase': 'probe',
        })
        progress_accepted = self._post('worker_step', 'progress', {
            'worker_id': 'worker-a',
            'fencing_token': token,
            'phase': 'probe',
        })
        result_missing = self._post('worker_step', 'result', {
            'worker_id': 'worker-a',
            'status': 'passed',
        })
        result_accepted = self._post('worker_step', 'result', {
            'worker_id': 'worker-a',
            'fencing_token': token,
            'status': 'passed',
        })

        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.get_json()['code'], 'fencing_token_required')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()['code'], 'fencing_token_stale')
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(progress_missing.status_code, 409)
        self.assertEqual(
            progress_missing.get_json()['code'], 'fencing_token_required')
        self.assertEqual(
            progress_accepted.status_code, 200,
            progress_accepted.get_data(as_text=True))
        self.assertEqual(result_missing.status_code, 409)
        self.assertEqual(
            result_missing.get_json()['code'], 'fencing_token_required')
        self.assertEqual(
            result_accepted.status_code, 200,
            result_accepted.get_data(as_text=True))
        receipt = result_accepted.get_json()
        self.assertTrue(receipt['result_accepted'])
        self.assertEqual('hub.workflow_step_result_receipt@1', receipt['schema'])
        self.assertEqual('worker_step', receipt['step']['step_id'])
        self.assertNotIn('steps', receipt)

    def test_controlled_worker_task_payload_has_operation_and_fencing_contract(self):
        self.run.context_json = {'workflow_start': {
            'worker_claw_id': self.claw.id,
            'worker_binding_mode': 'single_flow_worker',
        }}
        self.worker_step.runner = 'deepflow.racinggo.flow25_worker_v1'
        self.worker_step.step_config_json = {
            'id': 'editor_health',
            'type': 'worker_task',
            'runner': 'deepflow.racinggo.flow25_worker_v1',
            'inputs': {
                'operation': 'editor_health',
                'protected_target_id': 'racinggo-dev2',
            },
            'require_fencing_token': True,
        }
        db.session.commit()

        listed = self.client.get(
            '/api/v1/workflow-runs/worker/tasks', headers=self._headers())

        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        task = listed.get_json()[0]
        self.assertEqual(task['step_type'], 'worker_task')
        self.assertEqual(task['worker_claw_id'], self.claw.id)
        self.assertEqual(task['fencing_token'], 0)
        self.assertEqual(task['worker_contract'], {
            'runner': 'deepflow.racinggo.flow25_worker_v1',
            'operation': 'editor_health',
            'protected_target_id': 'racinggo-dev2',
            'require_fencing_token': True,
        })

    def test_claim_idempotency_replays_same_response_and_conflicts_on_body_change(self):
        headers = dict(self._headers(), **{'Idempotency-Key': 'claim-key-1'})
        body = {'worker_id': 'worker-a', 'lease_seconds': 300}

        first = self._post_with_headers('worker_step', 'claim', body, headers)
        replay = self._post_with_headers('worker_step', 'claim', body, headers)
        changed = self._post_with_headers(
            'worker_step',
            'claim',
            {'worker_id': 'worker-a', 'lease_seconds': 30},
            headers,
        )

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_KEY_REUSED')

    def test_worker_heartbeat_requires_owner_and_extends_lease(self):
        self._post('worker_step', 'claim', {
            'worker_id': 'worker-a',
            'lease_seconds': 300,
        })
        step = self._worker_step()
        old_expiry = step.claim_expires_at

        rejected = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-b',
        })
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.get_json()['code'], 'claim_owner_mismatch')

        ok = self._post('worker_step', 'heartbeat', {
            'worker_id': 'worker-a',
            'message': 'still running',
        })
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertGreater(self._worker_step().claim_expires_at, old_expiry)

    def test_worker_result_requires_claim_and_strict_status(self):
        missing_claim = self._post('worker_step', 'result', {
            'worker_id': 'worker-a',
            'status': 'passed',
            'summary': 'done',
        })
        self.assertEqual(missing_claim.status_code, 409)
        self.assertEqual(missing_claim.get_json()['code'], 'claim_required')

        self._post('worker_step', 'claim', {'worker_id': 'worker-a'})
        invalid = self._post('worker_step', 'result', {
            'worker_id': 'worker-a',
            'status': 'done',
            'summary': 'bad status',
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('passed/failed/blocked/skipped', invalid.get_json()['error'])
        self.assertEqual(self._worker_step().status, 'running')

    def test_worker_result_clears_claim_on_terminal_status(self):
        self._post('worker_step', 'claim', {'worker_id': 'worker-a'})
        response = self._post('worker_step', 'result', {
            'worker_id': 'worker-a',
            'status': 'passed',
            'summary': 'done',
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        step = self._worker_step()
        self.assertEqual(step.status, 'passed')
        self.assertEqual(step.claimed_by, '')
        self.assertIsNone(step.claimed_claw_id)
        self.assertIsNone(step.claim_expires_at)

    def test_result_idempotency_replays_after_terminal_status(self):
        self._post('worker_step', 'claim', {'worker_id': 'worker-a'})
        headers = dict(self._headers(), **{'Idempotency-Key': 'result-key-1'})
        body = {
            'worker_id': 'worker-a',
            'status': 'passed',
            'summary': 'done',
            'outputs': {'artifact': 'ok'},
        }

        first = self._post_with_headers('worker_step', 'result', body, headers)
        replay = self._post_with_headers('worker_step', 'result', body, headers)
        changed = self._post_with_headers(
            'worker_step',
            'result',
            dict(body, summary='different'),
            headers,
        )

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_KEY_REUSED')

    def test_list_after_restart_keeps_running_task_and_expired_claim_is_reclaimable(self):
        self._post('worker_step', 'claim', {
            'worker_id': 'worker-a',
            'lease_seconds': 300,
        })
        listed = self.client.get(
            '/api/v1/workflow-runs/worker/tasks',
            headers=self._headers(),
        )
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        self.assertEqual(listed.get_json()[0]['step_id'], 'worker_step')
        self.assertEqual(listed.get_json()[0]['claimed_by'], 'worker-a')

        step = self._worker_step()
        step.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        reclaimed = self._post('worker_step', 'claim', {
            'worker_id': 'worker-b',
        }, token=self.other_token)
        self.assertEqual(reclaimed.status_code, 200, reclaimed.get_data(as_text=True))
        self.assertEqual(self._worker_step().claimed_claw_id, self.other_claw.id)

    def test_agent_task_keeps_existing_no_claim_result_path(self):
        claim = self._post('agent_step', 'claim', {'worker_id': 'worker-a'})
        response = self._post('agent_step', 'result', {
            'status': 'passed',
            'summary': 'agent done',
        })

        self.assertEqual(claim.status_code, 409)
        self.assertEqual(claim.get_json()['code'], 'INVALID_STEP_TYPE')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        step = WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='agent_step').first()
        self.assertEqual(step.status, 'passed')

    def test_agent_direct_runner_claim_requires_scope_and_fencing(self):
        self.agent_step.step_config_json = {
            'direct_execution_lease': {
                'schema': 1,
                'required': True,
                'scope': 'runner_operation',
                'lease_seconds': 240,
            },
        }
        db.session.commit()

        missing_scope = self._post('agent_step', 'claim', {
            'worker_id': 'direct-worker-a',
        })
        claimed = self._post('agent_step', 'claim', {
            'worker_id': 'direct-worker-a',
            'claim_scope': 'runner_operation',
        })
        self.assertEqual(missing_scope.status_code, 409)
        self.assertEqual(
            'DIRECT_EXECUTION_SCOPE_REQUIRED',
            missing_scope.get_json()['code'],
        )
        self.assertEqual(claimed.status_code, 200, claimed.get_data(as_text=True))
        self.assertEqual('runner_operation', claimed.get_json()['claim_scope'])
        self.assertEqual(240, claimed.get_json()['lease_seconds'])
        fencing_token = claimed.get_json()['step']['claim_fencing_token']

        missing_fence = self._post('agent_step', 'heartbeat', {
            'worker_id': 'direct-worker-a',
        })
        accepted_heartbeat = self._post('agent_step', 'heartbeat', {
            'worker_id': 'direct-worker-a',
            'fencing_token': fencing_token,
        })
        stale_result = self._post('agent_step', 'result', {
            'worker_id': 'direct-worker-a',
            'fencing_token': fencing_token + 1,
            'status': 'passed',
            'summary': 'must be rejected',
        })
        accepted_result = self._post('agent_step', 'result', {
            'worker_id': 'direct-worker-a',
            'fencing_token': fencing_token,
            'status': 'passed',
            'summary': 'runner result accepted',
        })

        self.assertEqual(missing_fence.status_code, 409)
        self.assertEqual(
            'fencing_token_required', missing_fence.get_json()['code'])
        self.assertEqual(
            accepted_heartbeat.status_code, 200,
            accepted_heartbeat.get_data(as_text=True))
        self.assertEqual(stale_result.status_code, 409)
        self.assertEqual(
            'fencing_token_stale', stale_result.get_json()['code'])
        self.assertEqual(
            accepted_result.status_code, 200,
            accepted_result.get_data(as_text=True))
        refreshed = WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='agent_step').first()
        self.assertEqual('passed', refreshed.status)
        self.assertEqual('', refreshed.claimed_by)

    def test_agent_direct_expired_claim_rejects_old_fencing_owner(self):
        self.agent_step.step_config_json = {
            'direct_execution_lease': {
                'schema': 1,
                'required': True,
                'scope': 'runner_operation',
                'lease_seconds': 60,
            },
        }
        db.session.commit()
        first = self._post('agent_step', 'claim', {
            'worker_id': 'direct-worker-a',
            'claim_scope': 'runner_operation',
        })
        first_token = first.get_json()['step']['claim_fencing_token']
        step = WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='agent_step').first()
        step.claim_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        reclaimed = self._post('agent_step', 'claim', {
            'worker_id': 'direct-worker-b',
            'claim_scope': 'runner_operation',
        })
        self.assertEqual(
            reclaimed.status_code, 200, reclaimed.get_data(as_text=True))
        second_token = reclaimed.get_json()['step']['claim_fencing_token']

        late = self._post('agent_step', 'result', {
            'worker_id': 'direct-worker-a',
            'fencing_token': first_token,
            'status': 'passed',
            'summary': 'late old result',
        })
        accepted = self._post('agent_step', 'result', {
            'worker_id': 'direct-worker-b',
            'fencing_token': second_token,
            'status': 'passed',
            'summary': 'current owner result',
        })

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first_token + 1, second_token)
        self.assertEqual(late.status_code, 409)
        self.assertEqual('claim_owner_mismatch', late.get_json()['code'])
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))

    def test_agent_direct_result_ack_loss_replays_same_response(self):
        self.agent_step.step_config_json = {
            'direct_execution_lease': {
                'schema': 1,
                'required': True,
                'scope': 'runner_operation',
                'lease_seconds': 180,
            },
        }
        db.session.commit()
        claimed = self._post('agent_step', 'claim', {
            'worker_id': 'direct-worker-a',
            'claim_scope': 'runner_operation',
        })
        fencing_token = claimed.get_json()['step']['claim_fencing_token']
        headers = dict(
            self._headers(),
            **{'Idempotency-Key': 'direct-result-ack-loss-1'},
        )
        body = {
            'worker_id': 'direct-worker-a',
            'fencing_token': fencing_token,
            'status': 'passed',
            'summary': 'persisted exactly once',
            'outputs': {'result_sha256': 'a' * 64},
        }

        first = self._post_with_headers('agent_step', 'result', body, headers)
        replay = self._post_with_headers('agent_step', 'result', body, headers)
        conflict = self._post_with_headers(
            'agent_step', 'result', dict(body, summary='different'), headers)

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            'IDEMPOTENCY_KEY_REUSED', conflict.get_json()['code'])

    def test_claim_protected_write_routes_lock_step_before_validation(self):
        helper_source = inspect.getsource(
            workflows_api._locked_workflow_step)
        self.assertIn('.with_for_update()', helper_source)
        handlers = (
            workflows_api.claim_workflow_step,
            workflows_api.heartbeat_workflow_step,
            workflows_api.progress_workflow_step,
            workflows_api.update_workflow_step_display_status,
            workflows_api.report_workflow_step_result,
        )
        for handler in handlers:
            with self.subTest(handler=handler.__name__):
                source = inspect.getsource(handler)
                self.assertIn('_locked_workflow_step(', source)
        result_source = inspect.getsource(
            workflows_api.report_workflow_step_result)
        self.assertLess(
            result_source.index('_workflow_idempotency_begin()'),
            result_source.index('_locked_workflow_step('),
        )

    def test_missing_declared_output_is_contract_invalid_and_blocks(self):
        self.agent_step.step_config_json = {'outputs': ['required_output']}
        db.session.commit()
        response = self._post('agent_step', 'result', {
            'status': 'passed', 'summary': 'missing output', 'outputs': {},
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        step = WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='agent_step').first()
        self.assertEqual(step.status, 'blocked')
        self.assertEqual(step.contract_result_json['code'], 'CONTRACT_INVALID')
        self.assertEqual(step.blocker_json['type'], 'result_contract_invalid')

    def test_warn_contract_invalid_keeps_progress_and_is_visible(self):
        self.agent_step.step_config_json = {
            'outputs': ['required_output'],
            'gates': [{'expression': 'metrics.ok == true', 'on_fail': 'warn'}],
        }
        db.session.commit()
        response = self._post('agent_step', 'result', {
            'status': 'passed', 'metrics': {'ok': True}, 'outputs': {},
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()['result_accepted'])
        readback = self.client.get(
            f'/api/v1/workflow-runs/{self.run.id}', headers=self._headers())
        self.assertEqual(200, readback.status_code, readback.get_data(as_text=True))
        step_payload = next(
            row for row in readback.get_json()['steps']
            if row['step_id'] == 'agent_step')
        self.assertEqual(step_payload['status'], 'passed')
        self.assertEqual(step_payload['result_code'], 'CONTRACT_INVALID')

    def test_self_reported_notification_without_hub_audit_is_contract_invalid(self):
        self.agent_step.step_config_json = {
            'required_metrics': [
                'business_failure_confirmed', 'notification_required',
                'report_required', 'notification_skipped', 'wecom_sent',
            ],
            'required_evidence': ['proof'],
            'outputs': ['must_output'],
            'gates': [{'expression': 'metrics.must_exist == true',
                       'on_fail': 'warn'}],
        }
        db.session.commit()

        response = self._post('agent_step', 'result', {
            'status': 'passed',
            'summary': 'agent claims it sent a notification',
            'metrics': {
                'business_failure_confirmed': False,
                'business_passed': True,
                'notification_required': True,
                'report_required': False,
                'notification_skipped': False,
                'wecom_sent': True,
                'must_exist': True,
            },
            'evidence': {'proof': 'isolated-canary'},
            'outputs': {'must_output': 'ok'},
        })

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        step = WorkflowRunStep.query.filter_by(
            run_id=self.run.id, step_id='agent_step').first()
        self.assertEqual(step.status, 'blocked')
        self.assertEqual(step.contract_result_json['code'], 'CONTRACT_INVALID')
        self.assertEqual(
            step.contract_result_json['notification']['code'],
            'NOTIFICATION_CONTRACT_INVALID')
        self.assertIn(
            'wecom_send_audit_missing',
            step.contract_result_json['notification']['violations'])
        self.assertTrue(step.outputs_json['notification_skipped'])
        self.assertFalse(step.outputs_json['wecom_sent'])
        self.assertEqual(
            step.outputs_json['skip_reason'],
            'business_pass_or_automation_only')
        self.assertNotEqual(self.run.status, 'succeeded')

    def test_result_ingests_evidence_manifest_and_flow12_findings(self):
        response = self._post('agent_step', 'result', {
            'status': 'passed',
            'evidence': {
                'screenshots': [{
                    'path': 'local/run123/stage-1.png',
                    'stage': 'login',
                    'snapshot_id': 'snap-1',
                    'ui_tree_id': 'tree-1',
                    'console_id': 'console-1',
                }],
                'ui_snapshot': 'artifact://run123/snapshot-1',
                'console': 'artifact://run123/console-1',
                'case_result': 'artifact://run123/result.json',
            },
            'flow12_bugs': [{
                'finding_key': 'flow12-port-config',
                'title': '端口配置错误',
                'severity': 'high',
                'description': '服务端口与自动化配置不一致',
            }],
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        manifest = WorkflowEvidenceManifest.query.filter_by(
            workflow_run_id=self.run.id).first()
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.completeness_status, 'complete')
        self.assertEqual(len(manifest.artifacts_json), 4)
        self.assertEqual(len(manifest.finding_ids_json), 1)
        finding = ShiftLeftFinding.query.filter_by(
            finding_key='flow12-port-config').first()
        self.assertIsNotNone(finding)
        self.assertEqual(finding.associations_json['evidence_manifest_id'], manifest.id)

    def _notification_fixture(self, business_failure_confirmed):
        report = ReportModel(
            title='Flow report', report_type='workflow', project_id=self.run.project_id,
            content='report', status='published', is_shared=True,
        )
        report.generate_share_token()
        db.session.add(report)
        db.session.flush()
        self.agent_step.outputs_json = {
            'business_failure_confirmed': business_failure_confirmed,
            'notification_required': True,
            'report_required': True,
            'hub_report_id': report.id,
            'share_url': '/r/%s' % report.share_token,
        }
        notification = WorkflowRunStep(
            run_id=self.run.id,
            step_id='notify_group2',
            position=3,
            name='Push group 2',
            step_type='notification',
            status='running',
            runner='deepflow.wecom.release_group',
        )
        db.session.add(notification)
        db.session.commit()
        return report, notification

    def test_workflow_notification_is_refused_and_audited_when_business_passes(self):
        _, notification = self._notification_fixture(False)
        response = self.client.post('/api/v1/wecom/send', headers=self._headers(), json={
            'workflow_run_id': self.run.id,
            'workflow_step_id': notification.step_id,
            'related_type': 'workflow_notification',
            'content': 'must stay silent',
        })
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['skip_reason'],
                         'business_pass_or_automation_only')
        log = WecomSendLog.query.order_by(WecomSendLog.id.desc()).first()
        self.assertEqual(log.status, 'skipped')
        self.assertEqual(log.strategy, 'hub_gate')
        self.assertTrue(log.message_hash)
        self.assertEqual(log.template_version, 'workflow-wecom-v1')

    def test_workflow_notification_send_saves_template_hash_and_receipt(self):
        _, notification = self._notification_fixture(True)
        db.session.add(SystemConfig(
            config_key='registration_pass_webhook', value='https://wecom.invalid/hook'))
        db.session.commit()
        with patch('app.api.wecom._send_via_group_robot', return_value=(
                True, '', {'errcode': 0, 'errmsg': 'ok'})):
            response = self.client.post('/api/v1/wecom/send', headers=self._headers(), json={
                'workflow_run_id': self.run.id,
                'workflow_step_id': notification.step_id,
                'related_type': 'workflow_notification',
                'template_version': 'flow12-v3',
                'title': 'Confirmed issue',
                'content': 'report link',
            })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        log = WecomSendLog.query.order_by(WecomSendLog.id.desc()).first()
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.template_version, 'flow12-v3')
        self.assertEqual(log.receipt_json['errcode'], 0)
        self.assertTrue(log.gate_result_json['allowed'])


if __name__ == '__main__':
    unittest.main()
