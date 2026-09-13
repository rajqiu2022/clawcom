import json
import sys
import types
import unittest
from datetime import timedelta
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


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
from app.api.agent_client import agent_bp  # noqa: E402
from app.models import (  # noqa: E402
    AgentTask,
    AuditLog,
    OpenClawInstance,
    User,
    WorkerRelease,
    _now,
    hash_token,
)
from app.services.agent_tasks import (  # noqa: E402
    expire_stale_ordinary_tasks,
)


HUB_COMMIT = '79d89764f962f289e330247ec9e6b84a1c715120'
WORKER_COMMIT = '84a885d1cfd81ed8b42987c8c46e580edc193227'
MANIFEST_SHA = 'a' * 64
ARTIFACT_SHA = 'b' * 64


class AgentTaskLeaseApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(agent_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.admin = User(username='admin', role='super_admin')
        self.admin.set_password('secret')
        self.token = 'oc_tk_agent_task_lease'
        self.claw = OpenClawInstance(
            name='lease-claw', claw_tag='lease-claw', owner='admin',
            api_token_hash=hash_token(self.token))
        db.session.add_all([self.admin, self.claw])
        db.session.flush()
        self.release = WorkerRelease(
            release_id='worker-' + WORKER_COMMIT,
            source_repository='https://git.woa.com/worker/repository',
            source_ref='release/main',
            source_commit=WORKER_COMMIT,
            release_manifest_sha256=MANIFEST_SHA,
            platform='windows-x86_64',
            platform_manifest_sha256='c' * 64,
            package_manifest_sha256='d' * 64,
            artifact_filename='worker.zip',
            artifact_sha256=ARTIFACT_SHA,
            artifact_size=100,
            artifact_path='C:/private/worker.zip',
            approval_status='approved',
            is_default=True,
        )
        db.session.add(self.release)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _headers(self):
        return {'Authorization': 'Bearer ' + self.token}

    def _dispatch(self, payload=None, retry_max=0):
        return self.client.post(
            '/api/openclaws/%s/dispatch' % self.claw.id,
            json={
                'task_type': 'agent_task',
                'command': 'controlled operation',
                'payload': payload or {},
                'retry_max': retry_max,
            })

    def _claim(self):
        response = self.client.get(
            '/api/openclaws/%s/pending-tasks' % self.claw.id,
            headers=self._headers())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['tasks']

    def test_deployment_shaped_task_requires_explicit_kind(self):
        response = self._dispatch({
            'source_branch': 'codex/long-agent-control-plane-pilot',
            'source_commit': HUB_COMMIT,
        })

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['code'], 'DEPLOYMENT_KIND_REQUIRED')
        self.assertEqual(AgentTask.query.count(), 0)

    def test_hub_application_contract_accepts_only_hub_repository(self):
        accepted = self._dispatch({
            'deployment_kind': 'hub_application',
            'source_repository': 'https://git.woa.com/J1_QA_Group1/clawteam.git',
            'source_branch': 'codex/long-agent-control-plane-pilot',
            'source_commit': HUB_COMMIT,
        })
        rejected = self._dispatch({
            'deployment_kind': 'hub_application',
            'source_repository': 'https://git.woa.com/worker/repository',
            'source_branch': 'main',
            'source_commit': HUB_COMMIT,
        })

        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.get_json()['code'], 'HUB_REPOSITORY_MISMATCH')

    def test_worker_release_contract_binds_approved_catalog_record(self):
        payload = {
            'deployment_kind': 'worker_release',
            'release_id': 'worker-' + WORKER_COMMIT,
            'source_commit': WORKER_COMMIT,
            'release_manifest_sha256': MANIFEST_SHA,
            'platform': 'windows-x86_64',
            'artifact_sha256': ARTIFACT_SHA,
        }
        accepted = self._dispatch(payload)
        payload['worker_release_record_id'] = self.release.id + 1
        rejected = self._dispatch(payload)

        self.assertEqual(accepted.status_code, 201, accepted.get_data(as_text=True))
        stored = AgentTask.query.first()
        self.assertEqual(
            json.loads(stored.payload)['worker_release_record_id'],
            self.release.id)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(
            rejected.get_json()['code'], 'WORKER_RELEASE_RECORD_MISMATCH')

    def test_hub_commit_cannot_be_submitted_as_worker_release(self):
        response = self._dispatch({
            'deployment_kind': 'worker_release',
            'release_id': 'worker-' + HUB_COMMIT,
            'source_commit': HUB_COMMIT,
            'release_manifest_sha256': MANIFEST_SHA,
            'platform': 'windows-x86_64',
            'artifact_sha256': ARTIFACT_SHA,
            'source_repository': 'https://git.woa.com/J1_QA_Group1/clawteam',
        })

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()['code'], 'WORKER_RELEASE_NOT_APPROVED')
        self.assertEqual(AgentTask.query.count(), 0)

    def test_claim_is_single_and_requires_fencing_for_heartbeat_and_result(self):
        created = self._dispatch({'required_outputs': ['work_completed']})
        self.assertEqual(created.status_code, 201)
        first = self._claim()
        second = self._claim()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        claim = first[0]
        self.assertEqual(claim['attempt_no'], 1)
        self.assertEqual(claim['fencing_token'], 1)
        self.assertTrue(claim['claim_token'])
        public = self.client.get(
            '/api/openclaws/%s/tasks/%s' % (
                self.claw.id, claim['task_id'])).get_json()
        self.assertNotIn('claim_token', public)

        missing = self.client.post(
            '/api/openclaws/%s/report' % self.claw.id,
            headers=self._headers(),
            json={'task_id': claim['task_id'], 'status': 'completed'})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.get_json()['code'], 'AGENT_TASK_CLAIM_REQUIRED')

        stale_attempt = self.client.post(
            '/api/openclaws/%s/tasks/%s/heartbeat' % (
                self.claw.id, claim['task_id']),
            headers=self._headers(),
            json={
                'claim_token': claim['claim_token'],
                'attempt_no': claim['attempt_no'] + 1,
                'fencing_token': claim['fencing_token'],
            })
        self.assertEqual(stale_attempt.status_code, 409)
        self.assertEqual(
            stale_attempt.get_json()['code'], 'AGENT_TASK_FENCING_STALE')

        binding = {
            'claim_token': claim['claim_token'],
            'attempt_no': claim['attempt_no'],
            'fencing_token': claim['fencing_token'],
        }
        heartbeat = self.client.post(
            '/api/openclaws/%s/tasks/%s/heartbeat' % (
                self.claw.id, claim['task_id']),
            headers=self._headers(),
            json=dict(binding, progress={'phase': 'checking'}))
        duplicate = self.client.post(
            '/api/openclaws/%s/tasks/%s/heartbeat' % (
                self.claw.id, claim['task_id']),
            headers=self._headers(),
            json=dict(binding, progress={'phase': 'checking'}))
        self.assertEqual(heartbeat.status_code, 200)
        self.assertFalse(heartbeat.get_json()['duplicate_progress'])
        self.assertTrue(duplicate.get_json()['duplicate_progress'])

        completed = self.client.post(
            '/api/openclaws/%s/report' % self.claw.id,
            headers=self._headers(),
            json=dict(binding, task_id=claim['task_id'], status='blocked',
                      error_code='external_gate', reason='waiting for owner',
                      retryable=False, evidence={'ticket': 7}))
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()['task']['status'], 'blocked')
        self.assertEqual(
            completed.get_json()['task']['terminal_reason'], 'external_gate')
        self.assertEqual(
            completed.get_json()['task']['payload']['required_outputs'],
            ['work_completed'])

    def test_stale_result_after_admin_cancel_is_rejected(self):
        self._dispatch()
        claim = self._claim()[0]
        cancelled = self.client.post(
            '/api/openclaws/%s/tasks/%s/cancel' % (
                self.claw.id, claim['task_id']),
            json={'reason': 'superseded deployment'})
        late = self.client.post(
            '/api/openclaws/%s/report' % self.claw.id,
            headers=self._headers(),
            json={
                'task_id': claim['task_id'],
                'status': 'completed',
                'claim_token': claim['claim_token'],
                'attempt_no': claim['attempt_no'],
                'fencing_token': claim['fencing_token'],
            })

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.get_json()['code'], 'AGENT_TASK_TERMINAL')
        db.session.expire_all()
        self.assertEqual(AgentTask.query.one().status, 'cancelled')
        self.assertEqual(
            AuditLog.query.filter_by(
                resource_type='agent_task', action='cancel').count(), 1)

    def test_admin_retry_requeues_terminal_task_and_rotates_fencing(self):
        self._dispatch()
        claim = self._claim()[0]
        self.client.post(
            '/api/openclaws/%s/tasks/%s/cancel' % (
                self.claw.id, claim['task_id']),
            json={'reason': 'test cancellation'})

        retried = self.client.post(
            '/api/openclaws/%s/tasks/%s/retry' % (
                self.claw.id, claim['task_id']), json={})

        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.get_json()['task']['status'], 'pending')
        self.assertGreater(
            retried.get_json()['task']['fencing_token'],
            claim['fencing_token'])
        self.assertEqual(
            AuditLog.query.filter_by(
                resource_type='agent_task', action='retry').count(), 1)

    def test_workflow_agent_task_cannot_use_generic_admin_operations(self):
        task = AgentTask(
            task_id='workflow-task', claw_id=self.claw.id,
            task_type='workflow_agent_task', status='running')
        db.session.add(task)
        db.session.commit()

        cancelled = self.client.post(
            '/api/openclaws/%s/tasks/%s/cancel' % (
                self.claw.id, task.task_id), json={})
        retried = self.client.post(
            '/api/openclaws/%s/tasks/%s/retry' % (
                self.claw.id, task.task_id), json={})

        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(
            cancelled.get_json()['code'], 'WORKFLOW_TASK_CANCEL_SEPARATE')
        self.assertEqual(retried.status_code, 409)
        self.assertEqual(
            retried.get_json()['code'], 'WORKFLOW_TASK_RETRY_SEPARATE')

    def test_expired_lease_retries_once_then_fails(self):
        self._dispatch(retry_max=1)
        self._claim()
        task = AgentTask.query.one()
        task.lease_expires_at = _now() - timedelta(seconds=1)
        db.session.commit()

        self.assertEqual(expire_stale_ordinary_tasks(), 1)
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.retry_count, 1)
        second = self._claim()[0]
        self.assertEqual(second['attempt_no'], 2)
        task.lease_expires_at = _now() - timedelta(seconds=1)
        db.session.commit()

        self.assertEqual(expire_stale_ordinary_tasks(), 1)
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.terminal_reason, 'agent_task_lease_expired')
        self.assertEqual(
            AuditLog.query.filter_by(
                resource_type='agent_task', action='lease_expired').count(), 1)

    def test_legacy_invalid_deployment_task_is_closed_by_watcher(self):
        task = AgentTask(
            task_id='legacy-invalid-deploy',
            claw_id=self.claw.id,
            task_type='agent_task',
            status='running',
            assigned_at=_now() - timedelta(hours=1),
            payload=json.dumps({
                'source_branch': 'codex/long-agent-control-plane-pilot',
                'source_commit': HUB_COMMIT,
            }))
        db.session.add(task)
        db.session.commit()

        self.assertEqual(expire_stale_ordinary_tasks(), 1)
        self.assertEqual(task.status, 'failed')
        self.assertEqual(task.terminal_reason, 'invalid_deployment_contract')
        self.assertEqual(
            AuditLog.query.filter_by(
                resource_type='agent_task', action='contract_rejected').count(), 1)


if __name__ == '__main__':
    unittest.main()
