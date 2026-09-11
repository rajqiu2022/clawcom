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


from flask import Flask, g  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    OpenClawInstance,
    Project,
    TestReport as ReportModel,
    WorkflowArtifact,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunStep,
    hash_token,
)


class TestReportShareContractApiTest(unittest.TestCase):
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

        self.project = Project(name='RacingGO')
        db.session.add(self.project)
        db.session.flush()
        self.tokens = {
            11: 'share-contract-worker',
            32: 'share-contract-author',
            52: 'share-contract-outsider',
        }
        claws = [
            OpenClawInstance(
                id=claw_id,
                name=name,
                safe_name='claw-%s' % claw_id,
                claw_tag='claw-%s' % claw_id,
                owner='owner-%s' % claw_id,
                project_id=self.project.id,
                status='工作',
                api_token_hash=hash_token(self.tokens[claw_id]),
            )
            for claw_id, name in ((11, '小牛'), (32, '小刺'), (52, '小窗'))
        ]
        db.session.add_all(claws)
        definition = WorkflowDefinition(
            workflow_key='share-contract-flow',
            name='Share contract Flow',
            project_id=self.project.id,
            status='active',
            owner_type='claw',
            owner_id=32,
            definition_json={
                'key': 'share-contract-flow',
                'name': 'Share contract Flow',
                'steps': [{'id': 'publish', 'name': 'Publish'}],
            },
        )
        db.session.add(definition)
        db.session.flush()
        self.run = WorkflowRun(
            definition_id=definition.id,
            project_id=self.project.id,
            run_name='Run with publisher report',
            status='running',
            context_json={
                'assignment_snapshot': {
                    'worker_claw_id': 11,
                    'executor_claw_id': 11,
                    'reviewer_claw_id': 32,
                },
                'workflow_start': {'worker_claw_id': 11},
            },
        )
        db.session.add(self.run)
        db.session.flush()
        db.session.add(WorkflowRunStep(
            run_id=self.run.id,
            step_id='publish',
            position=0,
            name='Publish',
            step_type='agent_task',
            status='passed',
            target_claw_id=11,
            step_config_json={'id': 'publish', 'type': 'agent_task'},
        ))
        self.report = ReportModel(
            title='Run report',
            report_type='feature_test',
            project_id=self.project.id,
            content='evidence',
            status='published',
            submitter_type='openclaw',
            submitter_claw_id=32,
            submitter_name='小刺',
            source_ref_type='workflow',
            source_ref_id=self.run.id,
        )
        db.session.add(self.report)
        db.session.flush()
        db.session.add(WorkflowArtifact(
            run_id=self.run.id,
            step_id='publish',
            artifact_type='workflow_report',
            name='Run report',
            test_report_id=self.report.id,
        ))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _headers(self, claw_id):
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)
        return {'Authorization': 'Bearer ' + self.tokens[claw_id]}

    def _share_path(self, suffix=''):
        return '/api/v1/test-reports/%s/share%s' % (self.report.id, suffix)

    def test_bound_worker_can_create_reuse_and_read_back_share(self):
        suffix = '?workflow_run_id=%s' % self.run.id
        first = self.client.post(
            self._share_path(suffix), headers=self._headers(11), json={})
        second = self.client.post(
            self._share_path(suffix), headers=self._headers(11), json={})
        readback = self.client.get(
            self._share_path(suffix), headers=self._headers(11))

        self.assertEqual(200, first.status_code, first.get_data(as_text=True))
        self.assertEqual(200, second.status_code, second.get_data(as_text=True))
        self.assertEqual(200, readback.status_code, readback.get_data(as_text=True))
        first_body = first.get_json()
        second_body = second.get_json()
        self.assertEqual('hub.test_report_share@1', first_body['schema'])
        self.assertFalse(first_body['reused'])
        self.assertTrue(second_body['reused'])
        self.assertEqual(first_body['share_token'], second_body['share_token'])
        self.assertEqual(first_body['shared_at'], second_body['shared_at'])
        self.assertEqual(self.run.id, readback.get_json()['workflow_run_id'])
        self.assertEqual('authenticated_only', first_body['attachment_policy'])
        self.assertTrue(first_body['share_url'].endswith(
            '/r/' + first_body['share_token']))

    def test_workflow_share_permission_is_run_scoped_and_cannot_rotate(self):
        suffix = '?workflow_run_id=%s' % self.run.id
        self.assertEqual(403, self.client.post(
            self._share_path(), headers=self._headers(11), json={}).status_code)
        self.assertEqual(403, self.client.post(
            self._share_path(suffix), headers=self._headers(52), json={}).status_code)

        enabled = self.client.post(
            self._share_path(suffix), headers=self._headers(11), json={})
        rotated = self.client.post(
            self._share_path(suffix + '&refresh=1'),
            headers=self._headers(11), json={})
        self.assertEqual(200, enabled.status_code)
        self.assertEqual(403, rotated.status_code)


if __name__ == '__main__':
    unittest.main()
