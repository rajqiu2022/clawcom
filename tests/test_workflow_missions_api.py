import sys
import types
import unittest
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

    def __getattr__(self, _name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    ClawSidecarConfig,
    OpenClawInstance,
    Project,
    User,
    WorkflowDefinition,
    WorkflowMission,
    WorkflowMissionDispatch,
    WorkflowRun,
    WorkflowRunStep,
    hash_token,
)


class WorkflowMissionsApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='workflow-mission-test',
            TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.project = Project(name='Mission Project')
        self.other_project = Project(name='Other Mission Project')
        self.admin = User(username='mission_admin', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.other_project, self.admin])
        db.session.flush()
        self.main_token = 'mission-main-token'
        self.other_token = 'mission-other-token'
        self.main_claw = OpenClawInstance(
            name='主Codex', safe_name='mission-main', claw_tag='mission-main',
            owner=self.admin.username, project_id=self.project.id,
            api_token_hash=hash_token(self.main_token), status='工作')
        self.other_claw = OpenClawInstance(
            name='其他Agent', safe_name='mission-other', claw_tag='mission-other',
            owner='other', project_id=self.project.id,
            api_token_hash=hash_token(self.other_token), status='工作')
        db.session.add_all([self.main_claw, self.other_claw])
        db.session.flush()
        db.session.add(ClawSidecarConfig(
            claw_id=self.other_claw.id,
            agent_type='codebuddy',
            config_owner='worker',
            runtime_config_json={
                'schema': 1,
                'kind': 'claw_worker',
                'provider': 'codebuddy',
                'runtime_mode': 'agent_direct',
                'platform': 'windows',
                'provider_version': 'codebuddy-cli',
                'auth_mode': '',
                'llm_provider': '',
                'llm_model': '',
                'timiai_project': '',
                'release_id': '',
                'source_commit': '',
                'artifact_sha256': '',
                'config_digest': '',
                'source': 'worker',
            },
        ))
        self.flow_a = self._definition('mission-flow-a', self.project.id)
        self.flow_b = self._definition('mission-flow-b', self.project.id)
        self.foreign_flow = self._definition(
            'mission-foreign-flow', self.other_project.id)
        db.session.commit()

        self.client = self.app.test_client()
        self._login_admin()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _definition(self, key, project_id):
        row = WorkflowDefinition(
            workflow_key=key,
            name=key,
            project_id=project_id,
            definition_json={
                'key': key,
                'name': key,
                'steps': [{
                    'id': 'analyze',
                    'name': 'Analyze',
                    'type': 'agent_task',
                    'target_claw_id': self.main_claw.id,
                    'outputs': ['summary'],
                }],
            },
            status='active',
            owner_type='user',
            owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        db.session.add(row)
        db.session.flush()
        return row

    def _login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = self.admin.id

    def _headers(self, token=None):
        return {'Authorization': f'Bearer {token or self.main_token}'}

    def _create_mission(self, **overrides):
        body = {
            'project_id': self.project.id,
            'main_claw_id': self.main_claw.id,
            'objective': '自主完成分析、用例设计和验证',
            'max_child_runs': 2,
            'expires_in_hours': 8,
        }
        body.update(overrides)
        response = self.client.post('/api/v1/workflow-missions', json=body)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def test_mission_defaults_to_all_active_project_flows(self):
        mission = self._create_mission()
        self.assertFalse(mission['allow_external_mutations'])
        with self.client.session_transaction() as session:
            session.clear()

        response = self.client.get(
            f"/api/v1/workflow-missions/{mission['id']}/definitions",
            headers=self._headers())

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        ids = {item['id'] for item in response.get_json()['items']}
        self.assertEqual(ids, {self.flow_a.id, self.flow_b.id})
        self.assertNotIn(self.foreign_flow.id, ids)
        self.assertEqual(response.get_json()['control_mode'], 'agent_autonomous')

    def test_external_mutation_authority_requires_logged_in_admin(self):
        approved = self._create_mission(
            mission_key='admin-approved-external-mutations',
            allow_external_mutations=True,
        )
        self.assertTrue(approved['allow_external_mutations'])
        self.assertTrue(db.session.get(
            WorkflowMission, approved['id']).allow_external_mutations)

        with self.client.session_transaction() as session:
            session.clear()
        denied = self.client.post(
            '/api/v1/workflow-missions',
            headers=self._headers(),
            json={
                'project_id': self.project.id,
                'main_claw_id': self.main_claw.id,
                'objective': 'Agent不得自行扩大外部写权限',
                'allow_external_mutations': True,
            },
        )
        self.assertEqual(denied.status_code, 403, denied.get_data(as_text=True))
        self.assertEqual(
            denied.get_json()['code'],
            'MISSION_EXTERNAL_MUTATION_APPROVAL_REQUIRED',
        )

    def test_main_agent_dispatches_idempotently_without_executor_override(self):
        mission = self._create_mission()
        with self.client.session_transaction() as session:
            session.clear()
        body = {
            'workflow_definition_id': self.flow_a.id,
            'start_vars': {'baseline': 'abc123'},
            'reason': '先运行代码分析',
            'decision_key': 'analysis-stage-1',
        }

        first = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json=body, headers=self._headers())
        replay = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json=body, headers=self._headers())

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json()['workflow_run_id'],
                         replay.get_json()['workflow_run_id'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        run = db.session.get(WorkflowRun, first.get_json()['workflow_run_id'])
        self.assertEqual(
            run.context_json['workflow_start']['worker_claw_id'],
            self.main_claw.id)
        self.assertEqual(run.context_json['mission']['id'], mission['id'])
        self.assertEqual(WorkflowRun.query.count(), 1)
        self.assertEqual(WorkflowMissionDispatch.query.count(), 1)

        forbidden = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={
                'workflow_definition_id': self.flow_b.id,
                'executor_claw_ids': [self.other_claw.id],
            }, headers=self._headers())
        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(forbidden.get_json()['code'], 'MISSION_EXECUTOR_OVERRIDE_FORBIDDEN')

    def test_main_agent_can_delegate_to_allowlisted_codebuddy_worker(self):
        self.flow_a.editor_acl_json = {
            'claw_ids': [self.other_claw.id], 'user_ids': []}
        db.session.commit()
        mission = self._create_mission(
            allowed_worker_claw_ids=[self.other_claw.id])
        self.assertEqual(
            mission['allowed_worker_claw_ids'], [self.other_claw.id])
        self.assertIn(
            'worker_claw_id',
            mission['dispatch_contract']['optional_fields'])
        with self.client.session_transaction() as session:
            session.clear()

        dispatched = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={
                'workflow_definition_id': self.flow_a.id,
                'worker_claw_id': self.other_claw.id,
                'decision_key': 'delegate-to-codebuddy',
            },
            headers=self._headers(),
        )

        self.assertEqual(dispatched.status_code, 201,
                         dispatched.get_data(as_text=True))
        self.assertEqual(dispatched.get_json()['worker_claw_id'],
                         self.other_claw.id)
        run = db.session.get(
            WorkflowRun, dispatched.get_json()['workflow_run_id'])
        self.assertEqual(
            run.context_json['workflow_start']['worker_claw_id'],
            self.other_claw.id)
        self.assertEqual(run.context_json['mission']['worker_claw_id'],
                         self.other_claw.id)

    def test_mission_rejects_unlisted_or_unauthorized_delegated_worker(self):
        mission = self._create_mission()
        with self.client.session_transaction() as session:
            session.clear()
        unlisted = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={
                'workflow_definition_id': self.flow_a.id,
                'worker_claw_id': self.other_claw.id,
            }, headers=self._headers())
        self.assertEqual(unlisted.status_code, 403)
        self.assertEqual(unlisted.get_json()['code'],
                         'MISSION_WORKER_NOT_ALLOWED')

        self._login_admin()
        allowed = self._create_mission(
            allowed_worker_claw_ids=[self.other_claw.id],
            mission_key='mission-worker-without-flow-acl')
        with self.client.session_transaction() as session:
            session.clear()
        forbidden = self.client.post(
            f"/api/v1/workflow-missions/{allowed['id']}/dispatch",
            json={
                'workflow_definition_id': self.flow_a.id,
                'worker_claw_id': self.other_claw.id,
            }, headers=self._headers())
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.get_json()['code'],
                         'MISSION_WORKER_EXECUTE_FORBIDDEN')

    def test_main_agent_restarts_blocked_child_without_new_dispatch(self):
        mission = self._create_mission()
        with self.client.session_transaction() as session:
            session.clear()
        dispatched = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={
                'workflow_definition_id': self.flow_a.id,
                'decision_key': 'analysis-stage-1',
            }, headers=self._headers())
        self.assertEqual(dispatched.status_code, 201,
                         dispatched.get_data(as_text=True))
        run_id = dispatched.get_json()['workflow_run_id']
        run = db.session.get(WorkflowRun, run_id)
        step = WorkflowRunStep.query.filter_by(run_id=run_id).first()
        step.status = 'blocked'
        step.blocker_json = {'message': 'receipt missing'}
        run.status = 'blocked'
        run.current_step_id = step.step_id
        run.blocker_json = {'message': 'receipt missing'}
        db.session.commit()

        restarted = self.client.post(
            f'/api/v1/workflow-runs/{run_id}/restart',
            json={'reason': 'receipt fixed'},
            headers=dict(self._headers(), **{
                'Idempotency-Key': 'mission-child-restart-1'}))

        self.assertEqual(restarted.status_code, 200,
                         restarted.get_data(as_text=True))
        self.assertEqual(restarted.get_json()['id'], run_id)
        self.assertEqual(restarted.get_json()['restart_count'], 1)
        self.assertEqual(WorkflowRun.query.count(), 1)
        self.assertEqual(WorkflowMissionDispatch.query.count(), 1)
        row = db.session.get(WorkflowMission, mission['id'])
        self.assertEqual(row.child_run_count, 1)

    def test_budget_and_identity_are_the_only_normal_dispatch_gates(self):
        mission = self._create_mission(max_child_runs=1)
        with self.client.session_transaction() as session:
            session.clear()
        first = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={'workflow_definition_id': self.flow_a.id,
                  'decision_key': 'first'},
            headers=self._headers())
        exhausted = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={'workflow_definition_id': self.flow_b.id,
                  'decision_key': 'second'},
            headers=self._headers())
        wrong_agent = self.client.get(
            f"/api/v1/workflow-missions/{mission['id']}",
            headers=self._headers(self.other_token))

        self.assertEqual(first.status_code, 201)
        self.assertEqual(exhausted.status_code, 409)
        self.assertEqual(exhausted.get_json()['code'], 'MISSION_CHILD_RUN_BUDGET_EXHAUSTED')
        self.assertEqual(wrong_agent.status_code, 403)

    def test_expired_mission_blocks_new_dispatch_but_keeps_readback(self):
        mission = self._create_mission()
        row = db.session.get(WorkflowMission, mission['id'])
        row.expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()

        readback = self.client.get(
            f"/api/v1/workflow-missions/{mission['id']}",
            headers=self._headers())
        dispatch = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={'workflow_definition_id': self.flow_a.id},
            headers=self._headers())

        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.get_json()['effective_status'], 'expired')
        self.assertEqual(dispatch.status_code, 409)
        self.assertEqual(dispatch.get_json()['code'], 'MISSION_EXPIRED')

    def test_main_agent_can_complete_mission_without_touching_child_run(self):
        mission = self._create_mission()
        with self.client.session_transaction() as session:
            session.clear()
        dispatched = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={'workflow_definition_id': self.flow_a.id},
            headers=self._headers())
        completed = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/complete",
            json={}, headers=self._headers())
        blocked = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json={'workflow_definition_id': self.flow_b.id},
            headers=self._headers())

        self.assertEqual(dispatched.status_code, 201)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['status'], 'completed')
        self.assertFalse(completed.get_json()['child_runs_cancelled'])
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['code'], 'MISSION_NOT_ACTIVE')
        self.assertEqual(WorkflowRun.query.count(), 1)

    def test_dispatch_audit_survives_child_run_deletion(self):
        mission = self._create_mission()
        with self.client.session_transaction() as session:
            session.clear()
        body = {
            'workflow_definition_id': self.flow_a.id,
            'decision_key': 'deletable-run',
        }
        dispatched = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json=body, headers=self._headers())
        run_id = dispatched.get_json()['workflow_run_id']
        self._login_admin()
        deleted = self.client.delete(f'/api/v1/workflow-runs/{run_id}')
        with self.client.session_transaction() as session:
            session.clear()
        replay = self.client.post(
            f"/api/v1/workflow-missions/{mission['id']}/dispatch",
            json=body, headers=self._headers())

        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertTrue(replay.get_json()['run_deleted'])
        self.assertIsNone(replay.get_json()['run'])
        self.assertEqual(WorkflowMissionDispatch.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
