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
    TestAccount as AccountModel,
    TestAccountUsageLog as AccountUsageLog,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class TestAccountLeasesApiTest(unittest.TestCase):
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
        self.owner = User(username='account_owner', role='super_admin')
        self.owner.set_password('secret')
        self.other = User(username='other_operator', role='super_admin')
        self.other.set_password('secret')
        db.session.add_all([self.project, self.owner, self.other])
        db.session.flush()
        definition = WorkflowDefinition(
            workflow_key='account-lease-flow', name='Account Lease Flow',
            project_id=self.project.id,
            definition_json={'key': 'account-lease-flow', 'steps': []},
            owner_type='user', owner_id=self.owner.id,
        )
        db.session.add(definition)
        db.session.flush()
        self.run = WorkflowRun(
            definition_id=definition.id, run_name='Account login smoke',
            status='running', project_id=self.project.id,
            controller_run_id='codex-account-cycle')
        self.account = AccountModel(
            platform='qq', account='10000001', password='internal-secret',
            status='idle', notes='shared smoke account',
            created_by=self.owner.username)
        db.session.add_all([self.run, self.account])
        db.session.commit()
        self.client = self.app.test_client()
        self._login(self.client, self.owner.id)
        self.other_client = self.app.test_client()
        self._login(self.other_client, self.other.id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _login(client, user_id):
        with client.session_transaction() as session:
            session['user_id'] = user_id

    def _acquire(self, client=None, **overrides):
        body = {'purpose': '登录链路冒烟'}
        body.update(overrides)
        return (client or self.client).post(
            f'/api/v1/test-accounts/{self.account.id}/acquire', json=body)

    def test_existing_skill_acquire_release_contract_remains_compatible(self):
        acquired = self._acquire()

        self.assertEqual(acquired.status_code, 200, acquired.get_data(as_text=True))
        payload = acquired.get_json()
        self.assertEqual(payload['password'], 'internal-secret')
        self.assertEqual(payload['status'], 'in_use')
        self.assertEqual(payload['lease_ttl_seconds'], 1800)
        self.assertIsNotNone(payload['lease_expires_at'])

        released = self.client.post(
            f'/api/v1/test-accounts/{self.account.id}/release',
            json={'summary': '登录冒烟通过'})
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json()['account']['status'], 'idle')
        self.assertIsNone(released.get_json()['account']['lease_expires_at'])
        self.assertEqual(
            AccountUsageLog.query.filter_by(action='acquire').count(), 1)
        self.assertEqual(
            AccountUsageLog.query.filter_by(action='release').count(), 1)

    def test_account_row_is_exclusive_and_second_acquire_conflicts(self):
        first = self._acquire()
        second = self._acquire(self.other_client)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()['current_user'], self.owner.username)
        self.assertEqual(AccountUsageLog.query.filter_by(
            action='acquire').count(), 1)

    def test_keepalive_extends_ttl_and_preserves_workflow_lineage(self):
        acquired = self._acquire(
            ttl_seconds=60,
            workflow_run_id=self.run.id,
            controller_run_id='codex-account-cycle')
        self.assertEqual(acquired.status_code, 200)
        old_expiry = acquired.get_json()['lease_expires_at']

        renewed = self.client.post(
            f'/api/v1/test-accounts/{self.account.id}/keepalive',
            json={'ttl_seconds': 600})

        self.assertEqual(renewed.status_code, 200)
        account = renewed.get_json()['account']
        self.assertGreater(account['lease_expires_at'], old_expiry)
        self.assertEqual(account['workflow_run_id'], self.run.id)
        self.assertEqual(account['controller_run_id'], 'codex-account-cycle')
        self.assertEqual(account['lease_ttl_seconds'], 600)
        self.assertEqual(AccountUsageLog.query.filter_by(
            action='renew').count(), 1)

    def test_expired_account_is_reclaimed_before_next_acquire(self):
        first = self._acquire(ttl_seconds=60)
        self.assertEqual(first.status_code, 200)
        self.account.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()

        replacement = self._acquire(
            self.other_client,
            ttl_seconds=120,
            purpose='另一个登录验证')

        self.assertEqual(
            replacement.status_code, 200,
            replacement.get_data(as_text=True))
        self.assertEqual(replacement.get_json()['current_user'], self.other.username)
        actions = [row.action for row in AccountUsageLog.query.order_by(
            AccountUsageLog.id.asc()).all()]
        self.assertEqual(actions, ['acquire', 'expire', 'acquire'])
        expire_log = AccountUsageLog.query.filter_by(action='expire').first()
        self.assertEqual(expire_log.actor_type, 'system')
        self.assertEqual(expire_log.extra['holder_name'], self.owner.username)

    def test_expired_lease_cannot_be_revived_by_keepalive(self):
        self._acquire(ttl_seconds=60)
        self.account.lease_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()

        response = self.client.post(
            f'/api/v1/test-accounts/{self.account.id}/keepalive', json={})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'TEST_ACCOUNT_LEASE_EXPIRED')
        db.session.refresh(self.account)
        self.assertEqual(self.account.status, 'idle')
        self.assertIsNone(self.account.lease_expires_at)

    def test_invalid_workflow_reference_is_rejected_without_acquiring(self):
        response = self._acquire(workflow_run_id=999999)

        self.assertEqual(response.status_code, 404)
        db.session.refresh(self.account)
        self.assertEqual(self.account.status, 'idle')
        self.assertEqual(AccountUsageLog.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
