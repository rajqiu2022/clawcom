import json
import sys
import types
import unittest
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _Noop()


for name, attrs in {
    'flask_cors': {'CORS': _Noop},
    'flask_socketio': {
        'SocketIO': _Noop,
        'emit': _Noop(),
        'join_room': _Noop(),
        'leave_room': _Noop(),
    },
}.items():
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

from app.services.workflows import (  # noqa: E402
    materialize_workflow_run_assignment,
    normalize_workflow_definition,
    normalize_step,
    merge_workflow_run_outcomes,
    composite_workflow_terminal_status,
    resolve_workflow_run_assignment,
    validate_workflow_finalizer_result,
)
from flask import Flask, g  # noqa: E402
from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    OpenClawInstance,
    Project,
    WorkflowDefinition,
    AgentTask,
    WorkflowRun,
    WorkflowRunStep,
    hash_token,
)


class WorkflowRunAssignmentBindingTest(unittest.TestCase):
    def _definition(self):
        return {
            'start_vars_schema': {
                'worker_claw_id': {'type': 'integer', 'required': True},
                'reviewer_claw_id': {'type': 'integer', 'required': True},
            },
            'steps': [
                {'id': 'run_cases', 'name': 'Run', 'type': 'agent_task'},
                {
                    'id': 'peer_review_finalize',
                    'name': 'Review',
                    'type': 'agent_task',
                    'assignment_role': 'reviewer',
                },
                {'id': 'notify', 'name': 'Notify', 'type': 'notification'},
            ],
        }

    def test_resolves_worker_executor_and_independent_reviewer(self):
        assignment = resolve_workflow_run_assignment(
            self._definition(),
            {'worker_claw_id': 11, 'reviewer_claw_id': 7},
            worker_claw_id=11,
        )
        self.assertEqual(11, assignment['executor_claw_id'])
        self.assertEqual(7, assignment['reviewer_claw_id'])
        self.assertEqual('executor', assignment['step_roles']['run_cases'])
        self.assertEqual(
            'reviewer', assignment['step_roles']['peer_review_finalize'])

        definition = materialize_workflow_run_assignment(
            self._definition(), assignment, {11: '小牛', 7: '小安'})
        steps = {step['id']: step for step in definition['steps']}
        self.assertEqual(11, steps['run_cases']['target_claw_id'])
        self.assertEqual([11], steps['run_cases']['executor_claw_ids'])
        self.assertEqual(7, steps['peer_review_finalize']['target_claw_id'])
        self.assertEqual('小安', steps['peer_review_finalize']['target_agent'])
        self.assertNotIn('target_claw_id', steps['notify'])

    def test_rejects_missing_or_same_reviewer_and_worker_mismatch(self):
        with self.assertRaisesRegex(ValueError, 'reviewer_claw_id is required'):
            resolve_workflow_run_assignment(
                self._definition(), {'worker_claw_id': 11},
                worker_claw_id=11)
        with self.assertRaisesRegex(ValueError, 'must differ'):
            resolve_workflow_run_assignment(
                self._definition(),
                {'worker_claw_id': 11, 'reviewer_claw_id': 11},
                worker_claw_id=11)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            resolve_workflow_run_assignment(
                self._definition(),
                {'worker_claw_id': 32, 'reviewer_claw_id': 7},
                worker_claw_id=11)

    def test_legacy_definition_does_not_opt_in_implicitly(self):
        self.assertIsNone(resolve_workflow_run_assignment(
            {'steps': [{'id': 'work', 'type': 'agent_task'}]},
            {}, worker_claw_id=11))

    def test_finalizer_requires_explicit_completion_receipt_only_when_opted_in(self):
        self.assertTrue(validate_workflow_finalizer_result(
            {}, {'outputs': {}})['valid'])
        invalid = validate_workflow_finalizer_result(
            {'finalizer': True}, {'outputs': {}})
        self.assertFalse(invalid['valid'])
        self.assertEqual([
            'outputs.finalizer_complete',
            'outputs.finalizer_receipt',
        ], invalid['missing'])
        self.assertTrue(validate_workflow_finalizer_result(
            {'finalizer': True},
            {'outputs': {
                'finalizer_complete': True,
                'finalizer_receipt': {'completed_at': '2026-08-30T18:00:00+08:00'},
            }},
        )['valid'])

    def test_normalize_step_preserves_assignment_and_finalizer_contract(self):
        normalized = normalize_step({
            'id': 'review',
            'name': 'Review',
            'type': 'agent_task',
            'assignment_role': 'reviewer',
            'finalizer': True,
        }, 0)
        self.assertEqual('reviewer', normalized['assignment_role'])
        self.assertTrue(normalized['finalizer'])

    def test_composite_outcomes_keep_business_result_independent(self):
        outcomes = merge_workflow_run_outcomes({}, {
            'status': 'blocked',
            'outputs': {
                'business_outcome': 'FAILED',
                'automation_outcome': 'PARTIAL',
                'evidence_outcome': 'COMPLETE',
                'report_outcome': 'FAILED',
                'notification_outcome': 'NOT_REQUIRED',
            },
        })
        self.assertEqual('FAILED', outcomes['business'])
        self.assertEqual('PARTIAL', outcomes['automation'])
        self.assertEqual('FAILED', outcomes['report'])
        self.assertEqual(
            'succeeded',
            composite_workflow_terminal_status(
                outcomes, has_blocked=True, has_failed=False),
        )
        self.assertEqual(
            'blocked',
            composite_workflow_terminal_status({
                'business': 'NOT_EXECUTED',
                'automation': 'BLOCKED',
            }, has_blocked=True),
        )

    def test_composite_outcomes_do_not_downgrade_executed_or_published_truth(self):
        outcomes = merge_workflow_run_outcomes({
            'business': 'FAILED',
            'automation': 'PARTIAL',
            'evidence': 'COMPLETE',
            'report': 'PUBLISHED',
            'notification': 'SENT',
            'review': 'COMPLETED',
        }, {
            'outputs': {
                'business_outcome': 'NOT_EXECUTED',
                'automation_outcome': 'SUCCEEDED',
                'evidence_outcome': 'ANALYSIS_INCOMPLETE',
                'report_outcome': 'FAILED',
                'notification_outcome': 'FAILED',
                'review_outcome': 'FAILED',
            },
        })
        self.assertEqual('FAILED', outcomes['business'])
        self.assertEqual('PARTIAL', outcomes['automation'])
        # Evidence completeness is not a sticky success: a later scoped gap
        # must remain visible even when another case was complete.
        self.assertEqual('ANALYSIS_INCOMPLETE', outcomes['evidence'])
        self.assertEqual('PUBLISHED', outcomes['report'])
        self.assertEqual('SENT', outcomes['notification'])
        self.assertEqual('COMPLETED', outcomes['review'])

    def test_definition_normalization_preserves_composite_outcome_mode(self):
        normalized = normalize_workflow_definition({
            'key': 'composite-outcomes',
            'name': 'Composite outcomes',
            'outcome_status_mode': 'composite',
            'composite_outcomes': True,
            'steps': [{
                'id': 'business',
                'name': 'Business',
                'type': 'agent_task',
            }],
        })
        self.assertEqual('composite', normalized['outcome_status_mode'])
        self.assertTrue(normalized['composite_outcomes'])

        with self.assertRaisesRegex(ValueError, 'legacy or composite'):
            normalize_workflow_definition({
                'key': 'invalid-composite-mode',
                'outcome_status_mode': 'automatic',
                'steps': [{'id': 'business', 'type': 'agent_task'}],
            })


class WorkflowRunAssignmentApiTest(unittest.TestCase):
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
        project = Project(name='RacingGO')
        db.session.add(project)
        db.session.flush()
        self.token = 'assignment-claw-11'
        self.reviewer_token = 'assignment-claw-7'
        self.executor = OpenClawInstance(
            id=11,
            name='小牛', safe_name='xiaoniu', claw_tag='xiaoniu',
            owner='rajqiu', project_id=project.id, status='工作',
            api_token_hash=hash_token(self.token),
        )
        self.reviewer = OpenClawInstance(
            id=7,
            name='小安', safe_name='xiaoan', claw_tag='xiaoan',
            owner='rajqiu', project_id=project.id, status='工作',
            api_token_hash=hash_token(self.reviewer_token),
        )
        db.session.add_all([self.executor, self.reviewer])
        db.session.flush()
        self.definition = WorkflowDefinition(
            workflow_key='assignment-flow',
            name='Assignment Flow',
            project_id=project.id,
            status='active',
            version=21,
            owner_type='claw',
            owner_id=self.executor.id,
            visibility_scope='project',
            definition_json={
                'key': 'assignment-flow',
                'name': 'Assignment Flow',
                'require_worker_binding': True,
                'context': {'template_revision': 'flow12-v21-stable'},
                'start_vars_schema': {
                    'worker_claw_id': {
                        'type': 'integer', 'required': True},
                    'reviewer_claw_id': {
                        'type': 'integer', 'required': True},
                },
                'steps': [
                    {
                        'id': 'execute', 'name': 'Execute',
                        'type': 'agent_task', 'runner': 'agent.execute',
                    },
                    {
                        'id': 'peer_review_finalize', 'name': 'Review',
                        'type': 'agent_task', 'runner': 'agent.review',
                        'depends_on': ['execute'],
                        'assignment_role': 'reviewer',
                        'finalizer': True,
                    },
                ],
            },
        )
        db.session.add(self.definition)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _headers(self):
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)
        return {'Authorization': 'Bearer ' + self.token}

    def _reviewer_headers(self):
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)
        return {'Authorization': 'Bearer ' + self.reviewer_token}

    def test_create_materializes_assignment_and_snapshot(self):
        response = self.client.post(
            '/api/v1/workflow-runs',
            json={
                'definition_id': self.definition.id,
                'worker_claw_id': 11,
                'start_vars': {
                    'worker_claw_id': 11,
                    'reviewer_claw_id': 7,
                    'deepflow_release_sha': 'df-release-sha',
                    'baseline_receipt_id': 'baseline-25-1',
                    'baseline_receipt_hash': 'baseline-sha',
                    'runner_policy_version': '1.1.30',
                    'runner_policy_hash': 'runner-policy-sha',
                    'hub_token': 'must-not-be-snapshotted',
                },
            },
            headers=self._headers(),
        )
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        run = WorkflowRun.query.one()
        snapshot = run.context_json['assignment_snapshot']
        self.assertEqual(11, snapshot['executor_claw_id'])
        self.assertEqual(7, snapshot['reviewer_claw_id'])
        self.assertEqual(21, run.context_json[
            'workflow_definition_snapshot']['version'])
        self.assertEqual(
            'flow12-v21-stable',
            run.context_json['workflow_definition_snapshot'][
                'template_revision'
            ],
        )
        execution_snapshot = run.context_json['execution_input_snapshot']
        self.assertEqual(
            'df-release-sha', execution_snapshot['deepflow_release_sha'])
        self.assertEqual(
            'baseline-25-1', execution_snapshot['baseline_receipt_id'])
        self.assertEqual(
            'baseline-sha', execution_snapshot['baseline_receipt_hash'])
        self.assertEqual(
            '1.1.30', execution_snapshot['runner_policy_version'])
        self.assertNotIn('hub_token', execution_snapshot)
        self.assertNotIn(
            'must-not-be-snapshotted', str(execution_snapshot))
        steps = {
            row.step_id: row
            for row in WorkflowRunStep.query.filter_by(run_id=run.id).all()
        }
        self.assertEqual(11, steps['execute'].target_claw_id)
        self.assertEqual(7, steps['peer_review_finalize'].target_claw_id)
        self.assertEqual(
            [11], steps['execute'].step_config_json['executor_claw_ids'])
        self.assertEqual(
            [7], steps['peer_review_finalize'].step_config_json[
                'executor_claw_ids'])
        task = AgentTask.query.filter_by(
            task_id=f'workflow_{run.id}_execute_1', claw_id=11).one()
        task_payload = json.loads(task.payload)
        self.assertEqual(task.task_id, task_payload['task_id'])
        self.assertEqual(1, task_payload['attempt_no'])
        self.assertEqual(21, task_payload['definition_version'])

    def test_assignment_snapshot_keeps_create_idempotency_replay_stable(self):
        body = {
            'definition_id': self.definition.id,
            'worker_claw_id': 11,
            'idempotency_key': 'flow12-assignment-stable-replay',
            'start_vars': {
                'worker_claw_id': 11,
                'reviewer_claw_id': 7,
                'baseline_receipt_id': 'baseline-25-1',
            },
        }
        first = self.client.post(
            '/api/v1/workflow-runs', json=body, headers=self._headers())
        replay = self.client.post(
            '/api/v1/workflow-runs', json=body, headers=self._headers())
        self.assertEqual(201, first.status_code, first.get_data(as_text=True))
        self.assertEqual(200, replay.status_code, replay.get_data(as_text=True))
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(1, WorkflowRun.query.count())

    def test_invalid_assignment_returns_422_without_creating_run(self):
        response = self.client.post(
            '/api/v1/workflow-runs',
            json={
                'definition_id': self.definition.id,
                'worker_claw_id': 11,
                'start_vars': {'worker_claw_id': 11},
            },
            headers=self._headers(),
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual(
            'WORKFLOW_ASSIGNMENT_INVALID', response.get_json()['code'])
        self.assertEqual(0, WorkflowRun.query.count())

    def test_terminal_run_writes_return_stable_non_retryable_conflict(self):
        created = self.client.post(
            '/api/v1/workflow-runs',
            json={
                'definition_id': self.definition.id,
                'worker_claw_id': 11,
                'start_vars': {
                    'worker_claw_id': 11,
                    'reviewer_claw_id': 7,
                },
            },
            headers=self._headers(),
        )
        self.assertEqual(201, created.status_code)
        run = WorkflowRun.query.one()
        run.status = 'succeeded'
        db.session.commit()

        calls = (
            ('heartbeat', {'worker_id': 'claw:11'}),
            ('progress', {'phase': 'late', 'message': 'late write'}),
            ('result', {'status': 'passed', 'summary': 'late result'}),
        )
        for operation, payload in calls:
            response = self.client.post(
                f'/api/v1/workflow-runs/{run.id}/steps/execute/{operation}',
                json=payload,
                headers=self._headers(),
            )
            self.assertEqual(
                409, response.status_code, response.get_data(as_text=True))
            body = response.get_json()
            self.assertEqual('HUB_LIFECYCLE_CONFLICT', body['code'])
            self.assertFalse(body['details']['retryable'])
            self.assertEqual('stop_and_reconcile', body['details']['worker_action'])
            self.assertEqual(operation, body['details']['operation'])

    def test_terminal_step_result_cannot_be_overwritten_while_run_advances(self):
        created = self.client.post(
            '/api/v1/workflow-runs',
            json={
                'definition_id': self.definition.id,
                'worker_claw_id': 11,
                'start_vars': {
                    'worker_claw_id': 11,
                    'reviewer_claw_id': 7,
                },
            },
            headers=self._headers(),
        )
        self.assertEqual(201, created.status_code)
        run = WorkflowRun.query.one()
        step = WorkflowRunStep.query.filter_by(
            run_id=run.id, step_id='execute').one()
        step.status = 'passed'
        step.summary = 'authoritative result'
        step.outputs_json = {'step_result': {'complete': True}}
        run.status = 'running'
        run.current_step_id = 'peer_review_finalize'
        db.session.commit()

        response = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/steps/execute/result',
            json={'status': 'blocked', 'summary': 'late provider epilogue'},
            headers=self._headers(),
        )

        self.assertEqual(409, response.status_code, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual('HUB_LIFECYCLE_CONFLICT', body['code'])
        self.assertEqual('passed', body['details']['step_status'])
        db.session.refresh(step)
        self.assertEqual('passed', step.status)
        self.assertEqual('authoritative result', step.summary)
        self.assertEqual({'step_result': {'complete': True}}, step.outputs_json)

    def test_reviewer_dispatch_and_finalizer_receipt_gate(self):
        created = self.client.post(
            '/api/v1/workflow-runs',
            json={
                'definition_id': self.definition.id,
                'worker_claw_id': 11,
                'start_vars': {
                    'worker_claw_id': 11,
                    'reviewer_claw_id': 7,
                },
            },
            headers=self._headers(),
        )
        self.assertEqual(201, created.status_code)
        run = WorkflowRun.query.one()
        execute = WorkflowRunStep.query.filter_by(
            run_id=run.id, step_id='execute').one()
        completed = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/steps/execute/result',
            json={'status': 'passed', 'summary': 'done', 'outputs': {}},
            headers=self._headers(),
        )
        self.assertEqual(200, completed.status_code, completed.get_data(as_text=True))
        review = WorkflowRunStep.query.filter_by(
            run_id=run.id, step_id='peer_review_finalize').one()
        self.assertEqual('running', review.status)
        self.assertIsNotNone(AgentTask.query.filter_by(
            claw_id=7,
            task_type='workflow_agent_task',
        ).first())

        missing = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/steps/'
            'peer_review_finalize/result',
            json={'status': 'passed', 'summary': 'reviewed', 'outputs': {}},
            headers=self._reviewer_headers(),
        )
        self.assertEqual(409, missing.status_code)
        self.assertEqual('FINALIZER_RECEIPT_REQUIRED', missing.get_json()['code'])
        db.session.refresh(review)
        self.assertEqual('running', review.status)

        accepted = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/steps/'
            'peer_review_finalize/result',
            json={
                'status': 'passed',
                'summary': 'reviewed',
                'outputs': {
                    'finalizer_complete': True,
                    'finalizer_receipt': {
                        'completed_at': '2026-08-30T18:00:00+08:00',
                    },
                },
            },
            headers=self._reviewer_headers(),
        )
        self.assertEqual(200, accepted.status_code, accepted.get_data(as_text=True))
        db.session.refresh(run)
        self.assertEqual('succeeded', run.status)

    def test_composite_mode_does_not_turn_business_failure_into_technical_block(self):
        definition = WorkflowDefinition(
            workflow_key='composite-flow',
            name='Composite Flow',
            project_id=self.definition.project_id,
            status='active',
            owner_type='claw',
            owner_id=self.executor.id,
            visibility_scope='project',
            definition_json={
                'key': 'composite-flow',
                'name': 'Composite Flow',
                'outcome_status_mode': 'composite',
                'steps': [{
                    'id': 'business',
                    'name': 'Business',
                    'type': 'agent_task',
                    'runner': 'agent.business',
                    'outputs': [
                        'business_outcome', 'automation_outcome',
                        'evidence_outcome', 'report_outcome',
                    ],
                }],
            },
        )
        db.session.add(definition)
        db.session.commit()
        created = self.client.post(
            '/api/v1/workflow-runs',
            json={'definition_id': definition.id},
            headers=self._headers(),
        )
        self.assertEqual(201, created.status_code)
        run = WorkflowRun.query.filter_by(definition_id=definition.id).one()
        result = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/steps/business/result',
            json={
                'status': 'blocked',
                'summary': 'business failure confirmed; report transport failed',
                'outputs': {
                    'business_outcome': 'FAILED',
                    'automation_outcome': 'PARTIAL',
                    'evidence_outcome': 'COMPLETE',
                    'report_outcome': 'FAILED',
                },
            },
            headers=self._headers(),
        )
        self.assertEqual(200, result.status_code, result.get_data(as_text=True))
        db.session.refresh(run)
        self.assertEqual('succeeded', run.status)
        self.assertEqual('FAILED', run.outcomes_json['business'])
        self.assertEqual('FAILED', run.outcomes_json['report'])
        self.assertEqual({}, run.blocker_json)


if __name__ == '__main__':
    unittest.main()
