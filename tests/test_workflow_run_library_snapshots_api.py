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
from app.models import (  # noqa: E402
    Project,
    TestCase as CaseModel,
    TestCaseLibrary as LibraryModel,
    User,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowRunLibrarySnapshot as RunLibrarySnapshot,
)
from app.api.workflows import _recompute_run_status  # noqa: E402
from app.services.testcase_library_versioning import (  # noqa: E402
    ensure_library_revision,
)
from app.services.workflows import build_workflow_agent_task_payload  # noqa: E402


class WorkflowRunLibrarySnapshotsApiTest(unittest.TestCase):
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
        self.project = Project(name='RacingGO')
        self.admin = User(username='snapshot_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.definition = WorkflowDefinition(
            workflow_key='flow12-snapshot-test',
            name='Flow 12 snapshot test',
            project_id=self.project.id,
            definition_json={
                'key': 'flow12-snapshot-test',
                'name': 'Flow 12 snapshot test',
                'steps': [{
                    'id': 'execute_cases',
                    'name': 'Execute frozen cases',
                    'type': 'approval',
                    'approval_required': True,
                    'depends_on': [],
                }],
            },
            status='active',
            owner_type='user',
            owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project',
        )
        self.library = LibraryModel(
            id=33,
            name='RacingGO production actions',
            project_name=self.project.name,
            owner=self.admin.username,
            mindmap={'id': 'root', 'title': 'Actions'},
        )
        db.session.add_all([self.definition, self.library])
        db.session.flush()
        self.first_case = CaseModel(
            library_id=self.library.id,
            case_id='ACTION-001',
            title='Claim one reward',
            priority='P1',
            type='functional',
            content={
                'steps': ['claim once'],
                'expected_results': ['balance changes exactly once'],
            },
            module_path='lobby/sign_in',
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(self.first_case)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _create_run(self, key, **overrides):
        body = {
            'workflow_definition_id': self.definition.id,
            'idempotency_key': key,
            'start_vars': {'test_case_library_id': self.library.id},
        }
        body.update(overrides)
        return self.client.post('/api/v1/workflow-runs', json=body)

    def test_at06_run_keeps_frozen_revision_after_library_upgrade(self):
        first_run_response = self._create_run('flow12-before-upgrade')
        self.assertEqual(
            first_run_response.status_code, 201,
            first_run_response.get_data(as_text=True))
        first_run = first_run_response.get_json()
        first_meta = first_run['testcase_library_snapshot']
        self.assertEqual(first_meta['revision'], 0)
        self.assertEqual(first_meta['case_ids'], [self.first_case.id])
        self.assertEqual(first_meta['case_count'], 1)
        self.assertEqual(
            first_run['context']['testcase_library_snapshot']['revision'], 0)

        first_case_id = self.first_case.id
        self.first_case.title = 'Claim one reward and verify final state'
        second_case = CaseModel(
            library_id=self.library.id,
            case_id='ACTION-002',
            title='Upgrade one vehicle',
            priority='P1',
            type='functional',
            content={
                'steps': ['upgrade exactly once'],
                'expected_results': ['level and currency both change'],
            },
            module_path='garage/upgrade',
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(second_case)
        ensure_library_revision(self.library, self.admin.username)
        db.session.commit()
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 1)

        second_run_response = self._create_run('flow12-after-upgrade')
        self.assertEqual(second_run_response.status_code, 201)
        second_run = second_run_response.get_json()
        self.assertEqual(second_run['testcase_library_snapshot']['revision'], 1)
        self.assertEqual(second_run['testcase_library_snapshot']['case_count'], 2)
        self.assertNotEqual(
            second_run['testcase_library_snapshot']['content_hash'],
            first_meta['content_hash'])

        frozen_first = self.client.get(
            f'/api/v1/workflow-runs/{first_run["id"]}/'
            'testcase-library-snapshot')
        frozen_second = self.client.get(
            f'/api/v1/workflow-runs/{second_run["id"]}/'
            'testcase-library-snapshot')
        self.assertEqual(frozen_first.status_code, 200)
        self.assertEqual(frozen_second.status_code, 200)
        first_snapshot = frozen_first.get_json()
        second_snapshot = frozen_second.get_json()
        self.assertEqual(first_snapshot['revision'], 0)
        self.assertEqual(first_snapshot['case_count'], 1)
        self.assertEqual(first_snapshot['cases'][0]['id'], first_case_id)
        self.assertEqual(first_snapshot['cases'][0]['title'], 'Claim one reward')
        self.assertEqual(second_snapshot['revision'], 1)
        self.assertEqual(second_snapshot['case_count'], 2)
        self.assertEqual(
            {case['case_id'] for case in second_snapshot['cases']},
            {'ACTION-001', 'ACTION-002'})

        # Sidecar/Agent payload receives the same frozen contract and cases API.
        agent_payload = build_workflow_agent_task_payload(
            first_run,
            {
                'step_id': 'execute_cases',
                'name': 'Execute frozen cases',
                'status': 'running',
                'config': {},
            },
        )
        self.assertEqual(
            agent_payload['testcase_library_snapshot']['revision'], 0)
        self.assertIn(
            f'/workflow-runs/{first_run["id"]}/testcase-library-snapshot',
            agent_payload['testcase_library_snapshot']['cases_api'])
        self.assertEqual(RunLibrarySnapshot.query.count(), 2)

    def test_idempotent_run_replay_reuses_one_snapshot(self):
        first = self._create_run('same-run')
        replay = self._create_run('same-run')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertEqual(
            first.get_json()['testcase_library_snapshot']['id'],
            replay.get_json()['testcase_library_snapshot']['id'])
        self.assertEqual(RunLibrarySnapshot.query.count(), 1)

    def test_run_deletion_removes_its_snapshot(self):
        created = self._create_run('delete-run').get_json()
        self.assertEqual(RunLibrarySnapshot.query.count(), 1)

        deleted = self.client.delete(f'/api/v1/workflow-runs/{created["id"]}')

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(WorkflowRun.query.count(), 0)
        self.assertEqual(RunLibrarySnapshot.query.count(), 0)

    def test_snapshot_failure_warns_but_does_not_block_flow(self):
        with patch(
                'app.api.workflows.freeze_workflow_run_library_snapshot',
                side_effect=RuntimeError('simulated snapshot outage')):
            response = self._create_run('snapshot-outage')

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(WorkflowRun.query.count(), 1)
        self.assertEqual(RunLibrarySnapshot.query.count(), 0)
        warnings = payload['context']['warnings']
        self.assertEqual(warnings[0]['code'], 'TESTCASE_LIBRARY_SNAPSHOT_FAILED')
        self.assertEqual(warnings[0]['scope'], 'testcase_library_snapshot')
        self.assertEqual(warnings[0]['details']['library_id'], self.library.id)

    def test_missing_snapshot_table_still_does_not_block_flow(self):
        RunLibrarySnapshot.__table__.drop(db.engine)

        response = self._create_run('migration-not-ready')

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(WorkflowRun.query.count(), 1)
        warning = response.get_json()['context']['warnings'][0]
        self.assertEqual(warning['code'], 'TESTCASE_LIBRARY_SNAPSHOT_FAILED')
        self.assertEqual(warning['details']['error_type'], 'OperationalError')
        deleted = self.client.delete(
            f'/api/v1/workflow-runs/{response.get_json()["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(WorkflowRun.query.count(), 0)

    def test_run_without_library_binding_remains_legacy_compatible(self):
        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {'build_branch': 'dev'},
        })

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertNotIn('testcase_library_snapshot', payload)
        self.assertNotIn('warnings', payload['context'])
        self.assertEqual(RunLibrarySnapshot.query.count(), 0)

    def test_flow12_uses_compatible_default_library_binding(self):
        self.definition.workflow_key = 'racinggo_editor_smoke_report_only'
        db.session.commit()

        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {'build_branch': 'dev'},
        })

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        snapshot = response.get_json()['testcase_library_snapshot']
        self.assertEqual(snapshot['library_id'], 33)
        self.assertEqual(snapshot['revision'], 0)
        self.assertEqual(snapshot['case_ids'], [self.first_case.id])

    def test_blocked_run_records_finished_at_and_resume_clears_it(self):
        created = self._create_run('blocked-finished-at').get_json()
        run = db.session.get(WorkflowRun, created['id'])
        step = WorkflowRunStep.query.filter_by(run_id=run.id).one()
        step.status = 'blocked'
        step.blocker_json = {'type': 'test', 'message': 'blocked for test'}
        _recompute_run_status(run, self.admin.username)
        db.session.commit()

        self.assertEqual(run.status, 'blocked')
        self.assertIsNotNone(run.finished_at)

        resumed = self.client.post(
            f'/api/v1/workflow-runs/{run.id}/resume', json={})

        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        db.session.refresh(run)
        self.assertEqual(run.status, 'waiting_approval')
        self.assertIsNone(run.finished_at)

    def test_invalid_library_binding_is_a_non_blocking_warning(self):
        response = self.client.post('/api/v1/workflow-runs', json={
            'workflow_definition_id': self.definition.id,
            'start_vars': {'testcase_library_id': 'not-an-id'},
        })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.get_json()['context']['warnings'][0]['code'],
            'INVALID_TESTCASE_LIBRARY_ID')
        self.assertEqual(RunLibrarySnapshot.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
