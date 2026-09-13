import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.api import workflows as workflows_api  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ClawTodo,
    EntityRelation,
    Project,
    ShiftLeftAnalysisFinding,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    User,
    WorkflowArtifact,
    WorkflowApproval,
    WorkflowDefinition,
    WorkflowEvidenceManifest,
    WorkflowRun,
    WorkflowRunStep,
)


class WorkflowRunControlPlaneApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
            SHIFT_LEFT_ENABLED=False,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        self.project = Project(name='RacingGO')
        self.admin = User(username='controller_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.definition = WorkflowDefinition(
            workflow_key='control-plane-flow',
            name='Control Plane Flow',
            project_id=self.project.id,
            definition_json={
                'key': 'control-plane-flow',
                'name': 'Control Plane Flow',
                'steps': [{
                    'id': 'approve',
                    'name': 'Approve',
                    'type': 'approval',
                    'approval_required': True,
                    'depends_on': [],
                }],
                'context': {},
            },
            status='active',
            owner_type='user',
            owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        db.session.add(self.definition)
        db.session.commit()

    def _run_body(self, key='cycle-1', **overrides):
        body = {
            'workflow_definition_id': self.definition.id,
            'idempotency_key': key,
            'controller_run_id': 'codex-cycle-20260812-01',
            'correlation_id': 'racinggo-dev2-abc123',
            'trigger_source': 'codex_controller',
            'start_vars': {'library_id': 33},
        }
        body.update(overrides)
        return body

    def test_at01_create_run_is_idempotent_per_definition(self):
        first = self.client.post('/api/v1/workflow-runs', json=self._run_body())
        replay = self.client.post('/api/v1/workflow-runs', json=self._run_body())

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertFalse(first.get_json()['idempotent_replay'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual('Control Plane Flow', first.get_json()['workflow_name'])
        self.assertEqual(
            self.definition.id,
            first.get_json()['workflow_definition_id'])
        self.assertEqual(
            self.definition.version or 1,
            first.get_json()['workflow_definition_version'])
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_reusing_idempotency_key_with_changed_payload_returns_conflict(self):
        first = self.client.post('/api/v1/workflow-runs', json=self._run_body())
        conflict = self.client.post(
            '/api/v1/workflow-runs',
            json=self._run_body(run_name='Different Run'),
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertTrue(conflict.get_json()['request_id'])
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_legacy_create_without_idempotency_remains_non_idempotent(self):
        body = {'definition_id': self.definition.id, 'start_vars': {}}
        first = self.client.post('/api/v1/workflow-runs', json=body)
        second = self.client.post('/api/v1/workflow-runs', json=body)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.get_json()['id'], second.get_json()['id'])

    def test_missing_required_start_vars_returns_422_without_creating_run(self):
        definition = dict(self.definition.definition_json or {})
        definition['start_vars_schema'] = {
            'hub_url': {'type': 'string', 'required': True},
            'library_id': {'type': 'integer', 'default': 20},
        }
        self.definition.definition_json = definition
        db.session.commit()

        rejected = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {},
        })

        self.assertEqual(422, rejected.status_code, rejected.get_data(as_text=True))
        self.assertEqual(
            'WORKFLOW_START_BINDING_REQUIRED', rejected.get_json()['code'])
        self.assertEqual(
            ['hub_url'], rejected.get_json()['details']['missing_start_vars'])
        self.assertEqual(0, WorkflowRun.query.count())

    def test_start_var_defaults_are_frozen_into_run_context(self):
        definition = dict(self.definition.definition_json or {})
        definition['start_vars_schema'] = {
            'type': 'object',
            'required': ['hub_url'],
            'properties': {
                'hub_url': {'type': 'string'},
                'library_id': {'type': 'integer', 'default': 20},
            },
        }
        self.definition.definition_json = definition
        db.session.commit()

        created = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {'hub_url': 'https://clawteam.woa.com'},
        })

        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        run = WorkflowRun.query.one()
        self.assertEqual(20, run.context_json['start_vars']['library_id'])

    def test_blocked_run_restarts_in_place_idempotently_from_first_step(self):
        self.definition.definition_json = {
            'key': 'control-plane-flow',
            'name': 'Control Plane Flow',
            'steps': [
                {
                    'id': 'approve', 'name': 'Approve', 'type': 'approval',
                    'approval_required': True, 'depends_on': [],
                },
                {
                    'id': 'work', 'name': 'Work', 'type': 'agent_task',
                    'depends_on': ['approve'], 'outputs': ['receipt'],
                },
            ],
            'context': {},
        }
        db.session.commit()
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body('restart-in-place'))
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        run = db.session.get(WorkflowRun, run_id)
        steps = {row.step_id: row for row in WorkflowRunStep.query.filter_by(
            run_id=run_id).all()}
        approval = WorkflowApproval.query.filter_by(
            run_id=run_id, step_id='approve').first()
        approval.status = 'approved'
        approval.approver = self.admin.username
        approve_config = dict(steps['approve'].step_config_json or {})
        approve_config['approval_required'] = False
        steps['approve'].step_config_json = approve_config
        steps['approve'].status = 'passed'
        steps['approve'].outputs_json = {'approved': True}
        steps['work'].status = 'blocked'
        steps['work'].summary = 'old failure'
        steps['work'].outputs_json = {'receipt': 'old'}
        steps['work'].evidence_json = {'log': 'old.log'}
        steps['work'].contract_result_json = {'code': 'CONTRACT_INVALID'}
        run.status = 'blocked'
        run.current_step_id = 'work'
        run.blocker_json = {'message': 'old failure'}
        db.session.add(WorkflowArtifact(
            run_id=run_id, step_id='work', artifact_type='log',
            name='old.log', local_path='F:/old.log'))
        manifest = WorkflowEvidenceManifest(
            project_id=self.project.id, workflow_run_id=run_id,
            coverage_json={'receipt': 'complete'},
            artifacts_json=[{'path': 'F:/old.log'}],
            required_evidence_json=['receipt'],
            completeness_status='complete', missing_required_json=[],
            classification='business_failure', finding_ids_json=[10],
            analysis_summary_json={'summary': 'old'},
            analyzed_by='old-agent', revision=1,
            created_by=self.admin.username, updated_by=self.admin.username,
        )
        db.session.add(manifest)
        db.session.add_all([
            ClawTodo(
                openclaw_id=12,
                title=f'处理 Workflow Run #{run_id} work 节点阻断',
                schedule_type='once', urgency_level='interrupt',
                enabled=True, created_by='workflow-blocked',
            ),
            ClawTodo(
                openclaw_id=12,
                title='structured blocker notice',
                schedule_type='once', urgency_level='interrupt',
                verification_target=(
                    f'workflow_run:{run_id}:step:work:attempt:1'),
                enabled=True, created_by='workflow-blocked',
            ),
        ])
        db.session.commit()

        missing_key = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'receipt fixed'})
        self.assertEqual(missing_key.status_code, 400)
        headers = {'Idempotency-Key': 'restart-run-same-id-1'}
        restarted = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'receipt fixed'}, headers=headers)
        replay = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'receipt fixed'}, headers=headers)

        self.assertEqual(restarted.status_code, 200,
                         restarted.get_data(as_text=True))
        self.assertEqual(restarted.get_json(), replay.get_json())
        self.assertEqual(restarted.get_json()['id'], run_id)
        self.assertTrue(restarted.get_json()['restart']['same_run_id'])
        self.assertEqual(restarted.get_json()['restart_count'], 1)
        self.assertEqual(WorkflowRun.query.count(), 1)
        run = db.session.get(WorkflowRun, run_id)
        self.assertEqual(run.status, 'waiting_approval')
        steps = {row.step_id: row for row in WorkflowRunStep.query.filter_by(
            run_id=run_id).all()}
        self.assertEqual(steps['approve'].status, 'waiting_approval')
        self.assertTrue(steps['approve'].step_config_json['approval_required'])
        self.assertEqual(steps['work'].status, 'pending')
        self.assertEqual(steps['work'].outputs_json, {})
        self.assertEqual(steps['work'].evidence_json, {})
        statuses = [row.status for row in WorkflowApproval.query.filter_by(
            run_id=run_id).all()]
        self.assertIn('superseded', statuses)
        self.assertIn('pending', statuses)
        artifact = WorkflowArtifact.query.filter_by(run_id=run_id).first()
        self.assertTrue(artifact.metadata_json['archived_by_restart'])
        manifest = WorkflowEvidenceManifest.query.filter_by(
            workflow_run_id=run_id).first()
        self.assertEqual(manifest.completeness_status, 'incomplete')
        self.assertEqual(manifest.revision, 2)
        self.assertEqual(manifest.finding_ids_json, [])
        self.assertEqual(AuditLog.query.filter_by(
            resource_type='workflow_run', resource_id=run_id,
            action='restart').count(), 1)
        blocker_todos = ClawTodo.query.filter_by(
            openclaw_id=12, created_by='workflow-blocked').all()
        self.assertEqual(2, len(blocker_todos))
        self.assertTrue(all(not todo.enabled for todo in blocker_todos))

    def test_stale_definition_snapshot_rejects_in_place_restart_idempotently(self):
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body('stale-restart'))
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        run = db.session.get(WorkflowRun, run_id)
        step = WorkflowRunStep.query.filter_by(run_id=run_id).first()
        step.status = 'blocked'
        step.blocker_json = {'message': 'old release failed'}
        run.status = 'blocked'
        run.blocker_json = {'message': 'old release failed'}

        updated = dict(self.definition.definition_json or {})
        updated['description'] = 'new live definition'
        updated['version'] = int(self.definition.version or 1) + 1
        self.definition.definition_json = updated
        self.definition.version = updated['version']
        db.session.commit()

        headers = {'Idempotency-Key': 'stale-restart-1'}
        rejected = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'live definition fixed'}, headers=headers)
        replay = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'live definition fixed'}, headers=headers)

        self.assertEqual(409, rejected.status_code)
        self.assertEqual(rejected.get_json(), replay.get_json())
        body = rejected.get_json()
        self.assertEqual('STALE_DEFINITION_SNAPSHOT', body['code'])
        self.assertTrue(body['replacement_required'])
        self.assertIn('version', body['stale_fields'])
        self.assertIn('sha256', body['stale_fields'])
        self.assertEqual(1, WorkflowRun.query.count())
        self.assertEqual('blocked', db.session.get(WorkflowRun, run_id).status)
        self.assertEqual(1, AuditLog.query.filter_by(
            resource_type='workflow_run', resource_id=run_id,
            action='restart_rejected').count())

    def test_fatal_runtime_result_skips_business_chain_and_runs_cleanup(self):
        definition_json = {
            'key': 'fatal-dependency-flow',
            'name': 'Fatal dependency flow',
            'steps': [
                {
                    'id': 'runtime', 'name': 'Runtime',
                    'type': 'worker_task', 'depends_on': [],
                    'inputs': {'advance_policy': 'advance_on_any_result'},
                },
                {
                    'id': 'precheck', 'name': 'Precheck',
                    'type': 'agent_task', 'depends_on': ['runtime'],
                    'inputs': {'advance_policy': 'advance_on_any_result'},
                },
                {
                    'id': 'business', 'name': 'Business',
                    'type': 'agent_task', 'depends_on': ['precheck'],
                },
                {
                    'id': 'cleanup', 'name': 'Cleanup',
                    'type': 'worker_task',
                    'depends_on': ['runtime', 'business'],
                    'run_even_if_upstream_blocked': True,
                },
            ],
        }
        self.definition.definition_json = definition_json
        run = WorkflowRun(
            definition_id=self.definition.id,
            project_id=self.project.id,
            run_name='Fatal runtime canary',
            status='running',
            context_json={},
        )
        db.session.add(run)
        db.session.flush()
        for position, config in enumerate(definition_json['steps']):
            step = WorkflowRunStep(
                run_id=run.id,
                step_id=config['id'],
                position=position,
                name=config['name'],
                step_type=config['type'],
                status='pending',
                depends_on_json=config.get('depends_on') or [],
                step_config_json=config,
            )
            db.session.add(step)
        db.session.flush()
        runtime = WorkflowRunStep.query.filter_by(
            run_id=run.id, step_id='runtime').one()
        runtime.status = 'blocked'
        runtime.summary = 'Runner 未启动'
        runtime.blocker_json = {
            'code': 'RUNNER_NOT_STARTED',
            'message': 'DeepFlow journal operation key is invalid',
        }
        runtime.outputs_json = {
            'runner_execution': {'state': 'not_started'},
        }

        with patch.object(workflows_api, '_dispatch_step_message') as dispatch:
            workflows_api._recompute_run_status(run, actor='test')

        rows = {row.step_id: row for row in WorkflowRunStep.query.filter_by(
            run_id=run.id).all()}
        self.assertEqual(rows['precheck'].status, 'skipped')
        self.assertEqual(rows['business'].status, 'skipped')
        self.assertEqual(
            rows['precheck'].branch_result_json[
                'dependency_failure_propagation']['code'],
            'UPSTREAM_DEPENDENCY_BLOCKED')
        self.assertEqual(rows['cleanup'].status, 'running')
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.current_step_id, 'cleanup')
        dispatch.assert_called_once_with(rows['cleanup'])

    def test_step_retry_clears_previous_attempt_display_state(self):
        self.definition.definition_json = {
            'key': 'control-plane-flow',
            'name': 'Control Plane Flow',
            'steps': [{
                'id': 'work', 'name': 'Work', 'type': 'agent_task',
                'depends_on': [],
            }],
            'context': {},
        }
        db.session.commit()
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body('clean-retry'))
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        run = db.session.get(WorkflowRun, run_id)
        step = WorkflowRunStep.query.filter_by(run_id=run_id).first()
        previous_attempt = int(step.attempt_no or 0)
        step.status = 'blocked'
        step.summary = 'Attempt 3 通过'
        step.metrics_json = {'passed': True}
        step.evidence_json = {'receipt': 'old'}
        step.logs_json = {'tail': 'old'}
        step.outputs_json = {'result': 'old'}
        step.blocker_json = {'message': 'actual blocker'}
        run.status = 'blocked'
        run.blocker_json = {'message': 'actual blocker'}
        run.business_conclusion = 'INCONCLUSIVE'
        run.automation_conclusion = 'AUTOMATION_ENV_BLOCKED'
        run.outcomes_json = {
            'business': 'INCONCLUSIVE',
            'automation': 'BLOCKED',
        }
        db.session.commit()

        with patch.object(workflows_api, '_dispatch_step_message'):
            retried = self.client.post(
                f'/api/v1/workflow-runs/{run_id}/steps/{step.step_id}/retry')
        self.assertEqual(200, retried.status_code, retried.get_data(as_text=True))
        refreshed = db.session.get(WorkflowRunStep, step.id)
        self.assertEqual(previous_attempt + 1, refreshed.attempt_no)
        self.assertEqual('', refreshed.summary)
        self.assertEqual({}, refreshed.metrics_json)
        self.assertEqual({}, refreshed.evidence_json)
        self.assertEqual({}, refreshed.logs_json)
        self.assertEqual({}, refreshed.outputs_json)
        self.assertNotEqual(
            {'message': 'actual blocker'}, refreshed.blocker_json)
        refreshed_run = db.session.get(WorkflowRun, run_id)
        self.assertEqual('', refreshed_run.business_conclusion)
        self.assertEqual('', refreshed_run.automation_conclusion)
        self.assertNotEqual(
            'BLOCKED', (refreshed_run.outcomes_json or {}).get('automation'))

        refreshed.status = 'passed'
        workflows_api._recompute_run_status(refreshed_run, actor='test')

        self.assertEqual('succeeded', refreshed_run.status)
        self.assertEqual('COMPLETED', refreshed_run.automation_conclusion)

    def test_resume_recomputes_run_outcomes_without_stale_blocked_attempt(self):
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body('clean-resume'))
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        run = db.session.get(WorkflowRun, run_id)
        step = WorkflowRunStep.query.filter_by(run_id=run_id).first()
        step.status = 'blocked'
        step.outputs_json = {'automation_outcome': 'BLOCKED'}
        run.status = 'blocked'
        run.business_conclusion = 'INCONCLUSIVE'
        run.automation_conclusion = 'AUTOMATION_ENV_BLOCKED'
        run.outcomes_json = {
            'business': 'INCONCLUSIVE',
            'automation': 'BLOCKED',
        }
        db.session.commit()

        with patch.object(workflows_api, '_dispatch_step_message'):
            resumed = self.client.post(
                f'/api/v1/workflow-runs/{run_id}/resume',
                json={'from_step_id': step.step_id},
            )

        self.assertEqual(200, resumed.status_code, resumed.get_data(as_text=True))
        refreshed_run = db.session.get(WorkflowRun, run_id)
        self.assertEqual('', refreshed_run.business_conclusion)
        self.assertEqual('', refreshed_run.automation_conclusion)
        self.assertNotEqual(
            'BLOCKED', (refreshed_run.outcomes_json or {}).get('automation'))

    def test_definition_can_require_an_explicit_single_worker_binding(self):
        definition_json = dict(self.definition.definition_json or {})
        definition_context = dict(definition_json.get('context') or {})
        definition_context['executor_operation_policy'] = {
            'mode': 'workflow_run_executor',
            'require_worker_binding': True,
        }
        definition_json['context'] = definition_context
        self.definition.definition_json = definition_json
        db.session.commit()

        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {},
        })

        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['code'], 'WORKER_BINDING_REQUIRED')
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_delete_run_cascades_generated_evidence_and_findings(self):
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body('delete-evidence-run'))
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        run_id = created.get_json()['id']
        analysis = ShiftLeftAnalysisRun(
            project_id=self.project.id,
            workflow_run_id=run_id,
            baseline_fingerprint='delete-run-evidence-fingerprint',
            status='completed_with_findings',
            baseline_json={'workflow_run_id': run_id},
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(analysis)
        db.session.flush()
        finding = ShiftLeftFinding(
            project_id=self.project.id,
            analysis_run_id=analysis.id,
            finding_key='delete-run-generated-finding',
            title='Generated finding for temporary run',
            severity='medium',
            evidence_level='runtime_supported',
            source_type='workflow',
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(finding)
        db.session.flush()
        db.session.add(ShiftLeftAnalysisFinding(
            analysis_run_id=analysis.id,
            finding_id=finding.id,
            snapshot_json={'title': finding.title},
        ))
        manifest = WorkflowEvidenceManifest(
            project_id=self.project.id,
            workflow_run_id=run_id,
            analysis_run_id=analysis.id,
            coverage_json={'case_result': 'complete'},
            artifacts_json=[],
            required_evidence_json=['case_result'],
            completeness_status='complete',
            missing_required_json=[],
            finding_ids_json=[finding.id],
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(manifest)
        db.session.flush()
        db.session.add(EntityRelation(
            project_id=self.project.id,
            from_type='workflow_run', from_id=str(run_id),
            relation_type='has_evidence',
            to_type='evidence_manifest', to_id=str(manifest.id),
            relation_key='delete-run-evidence-relation',
            created_by=self.admin.username,
            updated_by=self.admin.username,
        ))
        finding_id = finding.id
        analysis_id = analysis.id
        manifest_id = manifest.id
        db.session.commit()

        deleted = self.client.delete(f'/api/v1/workflow-runs/{run_id}')

        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        self.assertEqual(deleted.get_json()['cleanup']['manifests'], 1)
        self.assertEqual(deleted.get_json()['cleanup']['findings'], 1)
        self.assertIsNone(db.session.get(WorkflowRun, run_id))
        self.assertIsNone(db.session.get(ShiftLeftAnalysisRun, analysis_id))
        self.assertIsNone(db.session.get(WorkflowEvidenceManifest, manifest_id))
        self.assertIsNone(db.session.get(ShiftLeftFinding, finding_id))
        self.assertEqual(EntityRelation.query.count(), 0)

    def test_list_and_latest_support_controller_filters_and_enriched_payload(self):
        created = self.client.post(
            '/api/v1/workflow-runs', json=self._run_body()).get_json()
        db.session.add(WorkflowArtifact(
            run_id=created['id'],
            step_id='approve',
            artifact_type='test_report',
            name='Run report',
            url='/test-reports/88',
        ))
        db.session.commit()

        query = (
            '/api/v1/workflow-runs?'
            f'workflow_definition_id={self.definition.id}'
            '&status=running,waiting_approval'
            '&controller_run_id=codex-cycle-20260812-01'
            '&created_after=2000-01-01T00:00:00%2B08:00'
            '&page=1&page_size=50'
        )
        listed = self.client.get(query)
        latest = self.client.get(
            '/api/v1/workflow-runs/latest?'
            'correlation_id=racinggo-dev2-abc123')
        with patch.object(
                workflows_api, '_refresh_workflow_step_health') as refresh:
            scoped_latest = self.client.get(
                f'/api/v1/workflow-definitions/{self.definition.id}/runs/latest')

        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        page = listed.get_json()
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['page_size'], 50)
        item = page['items'][0]
        self.assertEqual(item['workflow_definition_id'], self.definition.id)
        self.assertEqual(item['current_step_status'], 'waiting_approval')
        self.assertEqual(item['controller_run_id'], 'codex-cycle-20260812-01')
        self.assertEqual(len(item['output_refs']), 1)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.get_json()['id'], created['id'])
        self.assertEqual(scoped_latest.status_code, 200)
        self.assertEqual(scoped_latest.get_json()['id'], created['id'])
        refresh.assert_called_once()
        self.assertEqual(created['id'], refresh.call_args.kwargs['run'].id)
        self.assertTrue(refresh.call_args.kwargs['commit'])
        self.assertEqual(
            scoped_latest.get_json()['selection'], {
                'mode': 'latest_for_definition',
                'workflow_definition_id': self.definition.id,
            })
        definition_payload = self.client.get(
            f'/api/v1/workflow-definitions/{self.definition.id}').get_json()
        self.assertEqual(
            definition_payload['latest_run_url'],
            f'/api/v1/workflow-definitions/{self.definition.id}/runs/latest')

    def test_definition_api_round_trips_start_schema_and_rejects_bad_gate_path(self):
        good = {
            'key': 'schema-roundtrip',
            'name': 'Schema Roundtrip',
            'start_vars_schema': {
                'type': 'object',
                'properties': {'library_id': {'type': 'integer'}},
            },
            'steps': [{
                'id': 'verify',
                'name': 'Verify',
                'metrics_schema': {'failed': 'integer'},
                'gates': [{'expression': 'metrics.failed == 0'}],
            }],
        }
        created = self.client.post('/api/v1/workflow-definitions', json={
            'key': good['key'],
            'name': good['name'],
            'start_vars_schema': good['start_vars_schema'],
            'definition': {'steps': good['steps']},
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        definition_id = created.get_json()['id']
        readback = self.client.get(
            f'/api/v1/workflow-definitions/{definition_id}').get_json()
        self.assertEqual(
            readback['definition']['start_vars_schema'],
            good['start_vars_schema'])

        bad = dict(good)
        bad['key'] = 'schema-bad-gate'
        bad['steps'] = [{
            'id': 'verify',
            'name': 'Verify',
            'metrics_schema': {'failed': 'integer'},
            'gates': [{'expression': 'metrics.fail_count == 0'}],
        }]
        rejected = self.client.post('/api/v1/workflow-definitions', json=bad)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(
            rejected.get_json()['code'],
            'WORKFLOW_DEFINITION_SCHEMA_INVALID')
        self.assertEqual(
            rejected.get_json()['details']['path'],
            'steps[0].gates[0].expression')
        self.assertIn(
            'steps[0].gates[0].expression', rejected.get_json()['error'])

    def test_definition_list_filters_by_project_and_validates_project_id(self):
        other_project = Project(name='Cartesian')
        db.session.add(other_project)
        db.session.flush()
        other_definition = WorkflowDefinition(
            workflow_key='cartesian-flow',
            name='Cartesian Flow',
            project_id=other_project.id,
            definition_json={
                'key': 'cartesian-flow',
                'name': 'Cartesian Flow',
                'steps': [{'id': 'run', 'name': 'Run'}],
            },
            status='active',
            owner_type='user',
            owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        db.session.add(other_definition)
        db.session.commit()

        filtered = self.client.get(
            f'/api/v1/workflow-definitions?project_id={other_project.id}&page=1&per_page=10')
        invalid = self.client.get(
            '/api/v1/workflow-definitions?project_id=not-a-project')

        self.assertEqual(filtered.status_code, 200, filtered.get_data(as_text=True))
        self.assertEqual(
            [item['id'] for item in filtered.get_json()['items']],
            [other_definition.id])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()['code'], 'INVALID_PROJECT_ID')

    def test_legacy_warn_definition_allows_targeted_patch_without_rewrite(self):
        self.maxDiff = None
        legacy_definition = {
            'key': self.definition.workflow_key,
            'name': self.definition.name,
            'description': 'legacy definition',
            'version': 7,
            'steps': [{
                'id': 'precheck',
                'name': 'Precheck',
                'type': 'worker_task',
                'runner': '',
                'depends_on': [],
                'approval_required': False,
                'metrics_schema': {'env_ready': 'boolean'},
                'gates': [{
                    'expression': 'metrics.env_ready == true',
                    'on_fail': 'warn',
                }],
                'target_agent': '',
                'target_claw_id': None,
                'prompt': '',
                'references': [],
                'inputs': {},
                'input_vars': {},
                'outputs': [],
                'branches': [],
                'retry_max': 0,
            }],
            'context': {'legacy': True},
        }
        self.definition.definition_json = legacy_definition
        self.definition.version = 7
        db.session.commit()

        response = self.client.patch(
            f'/api/v1/workflow-definitions/{self.definition.id}',
            json={'description': 'targeted compatible update'},
            headers={'X-Request-ID': 'legacy-warn-targeted-update'},
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload['description'], 'targeted compatible update')
        self.assertEqual(payload['definition']['version'], 7)
        self.assertEqual(payload['definition']['context'], {'legacy': True})
        expected_definition = dict(legacy_definition)
        expected_definition['description'] = 'targeted compatible update'
        self.assertEqual(payload['definition'], expected_definition)
        self.assertEqual(
            payload['definition']['steps'][0]['gates'][0]['on_fail'],
            'warn')
        db.session.refresh(self.definition)
        self.assertEqual(
            self.definition.definition_json['steps'][0]['gates'][0]['on_fail'],
            'warn')


if __name__ == '__main__':
    unittest.main()
