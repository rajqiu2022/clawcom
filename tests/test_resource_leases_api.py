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
    ResourceLease,
    ResourceLeaseEvent,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class ResourceLeasesApiTest(unittest.TestCase):
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
        self.admin = User(username='lease_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        definition = WorkflowDefinition(
            workflow_key='lease-flow', name='Lease Flow',
            project_id=self.project.id,
            definition_json={'key': 'lease-flow', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
        )
        db.session.add(definition)
        db.session.flush()
        self.flow_run = WorkflowRun(
            definition_id=definition.id,
            run_name='Flow12 protected run',
            status='running',
            project_id=self.project.id,
            controller_run_id='flow12-cycle',
        )
        db.session.add(self.flow_run)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _acquire(self, key, resources, owner='118', priority=100, ttl=900):
        return self.client.post('/api/v1/resource-leases/acquire', json={
            'resource_keys': resources,
            'owner_type': 'workflow_run',
            'owner_id': str(owner),
            'controller_run_id': f'controller-{owner}',
            'priority': priority,
            'ttl_seconds': ttl,
            'idempotency_key': key,
        })

    def test_multi_resource_acquire_is_all_or_nothing(self):
        held = self._acquire(
            'held-unity', ['unity:racinggo:dev2'], owner='run-a')
        blocked = self._acquire(
            'blocked-bundle',
            ['unity:racinggo:dev2', 'device:android-001'],
            owner='run-b')

        self.assertEqual(held.status_code, 201)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['code'], 'RESOURCE_LEASE_CONFLICT')
        self.assertEqual(ResourceLease.query.filter_by(owner_id='run-b').count(), 0)
        self.assertEqual(
            ResourceLease.query.filter_by(
                resource_key='device:android-001').count(), 0)

    def test_at08_flow12_lease_blocks_qualification_without_changing_run(self):
        acquired = self._acquire(
            'flow12-editor', ['unity:racinggo:dev2'],
            owner=self.flow_run.id, priority=100)
        qualification = self.client.post(
            '/api/v1/resource-leases/acquire', json={
                'resource_keys': ['unity:racinggo:dev2'],
                'owner_type': 'automation_candidate',
                'owner_id': 'candidate-1001',
                'priority': 10,
                'ttl_seconds': 300,
                'idempotency_key': 'candidate-editor',
            })

        self.assertEqual(acquired.status_code, 201)
        self.assertEqual(qualification.status_code, 409)
        conflict = qualification.get_json()['details']['conflicts'][0]
        self.assertEqual(conflict['owner_id'], str(self.flow_run.id))
        self.assertEqual(conflict['owner_run_id'], self.flow_run.id)
        self.assertEqual(conflict['owner_run_status'], 'running')
        self.assertEqual(conflict['wait_state'], 'waiting_owner_cleanup')
        self.assertIn(f'Run #{self.flow_run.id}', conflict['wait_message'])
        self.assertGreater(conflict['retry_after_seconds'], 0)
        self.assertEqual(
            qualification.get_json()['details']['state'],
            'WAITING_RESOURCE_CLEANUP')
        self.assertFalse(
            qualification.get_json()['details']['business_started'])
        self.assertEqual(1, ResourceLeaseEvent.query.filter_by(
            event_type='conflict').count())
        db.session.refresh(self.flow_run)
        self.assertEqual(self.flow_run.status, 'running')
        self.assertEqual(self.flow_run.controller_run_id, 'flow12-cycle')

    def test_idempotent_replay_returns_same_group(self):
        first = self._acquire(
            'same-bundle',
            ['bridge:racinggo:dev2:ui', 'device:android-002'])
        replay = self._acquire(
            'same-bundle',
            ['bridge:racinggo:dev2:ui', 'device:android-002'])

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(
            first.get_json()['lease_group_id'], replay.get_json()['lease_group_id'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(ResourceLease.query.count(), 2)

    def test_renew_and_release_apply_to_entire_group(self):
        acquired = self._acquire(
            'renew-bundle',
            ['bridge:racinggo:dev2:mobile', 'device:android-003'], ttl=60)
        leases = acquired.get_json()['leases']
        old_expiry = min(row['expires_at'] for row in leases)

        renewed = self.client.post(
            f'/api/v1/resource-leases/{leases[0]["id"]}/renew',
            json={'ttl_seconds': 600})
        released = self.client.post(
            f'/api/v1/resource-leases/{leases[0]["id"]}/release',
            json={'reason': 'flow completed'})

        self.assertEqual(renewed.status_code, 200)
        self.assertTrue(all(
            row['expires_at'] > old_expiry for row in renewed.get_json()['leases']))
        self.assertEqual(released.status_code, 200)
        self.assertTrue(all(
            row['status'] == 'released' for row in released.get_json()['leases']))
        self.assertTrue(all(row.active_slot is None for row in ResourceLease.query.all()))
        self.assertEqual(
            ResourceLeaseEvent.query.filter_by(event_type='renewed').count(), 2)
        self.assertEqual(
            ResourceLeaseEvent.query.filter_by(event_type='released').count(), 2)

    def test_at09_expired_group_is_reclaimed_and_history_is_preserved(self):
        first = self._acquire(
            'expiring-bundle',
            ['unity:racinggo:dev2', 'device:android-004'], owner='old-run')
        old_group = first.get_json()['lease_group_id']
        ResourceLease.query.filter_by(lease_group_id=old_group).update({
            'expires_at': datetime.now() - timedelta(seconds=1),
        })
        db.session.commit()

        replacement = self._acquire(
            'replacement-bundle',
            ['unity:racinggo:dev2', 'device:android-004'], owner='new-run')

        self.assertEqual(replacement.status_code, 201, replacement.get_data(as_text=True))
        self.assertNotEqual(replacement.get_json()['lease_group_id'], old_group)
        old_rows = ResourceLease.query.filter_by(lease_group_id=old_group).all()
        self.assertTrue(all(row.status == 'expired' for row in old_rows))
        self.assertEqual(
            ResourceLeaseEvent.query.filter_by(
                lease_group_id=old_group, event_type='expired').count(), 2)
        self.assertEqual(ResourceLease.query.filter_by(status='active').count(), 2)

    def test_account_keys_are_rejected_in_favor_of_existing_skill(self):
        response = self._acquire(
            'wrong-account-resource', ['account:7'], owner='run-account')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['code'], 'USE_TEST_ACCOUNT_MANAGER')
        self.assertEqual(ResourceLease.query.count(), 0)

    def test_protected_expiry_quarantines_whole_group_until_admin_stop_receipt(self):
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = True
        first = self._acquire('protected', ['unity:shared', 'device:phone'], owner='old')
        self.assertEqual(first.status_code, 201)
        lease_id = first.get_json()['leases'][0]['id']
        ResourceLease.query.update({'expires_at': datetime.now() - timedelta(seconds=1)})
        db.session.commit()
        replacement = self._acquire('new', ['device:phone'], owner='new')
        self.assertEqual(replacement.status_code, 409)
        conflict = replacement.get_json()['details']['conflicts'][0]
        self.assertEqual(conflict['wait_state'], 'waiting_stop_confirmation')
        self.assertFalse(conflict['retryable'])
        self.assertIsNone(conflict['retry_after_seconds'])
        self.assertTrue(all(row.active_slot and row.status == 'quarantined' for row in ResourceLease.query.all()))
        self.assertEqual(self.client.post('/api/v1/resource-leases/{}/renew'.format(lease_id), json={}).status_code, 409)
        self.assertEqual(self.client.post('/api/v1/resource-leases/{}/release'.format(lease_id), json={}).status_code, 409)
        self.assertEqual(self.client.post('/api/v1/resource-leases/{}/reconcile'.format(lease_id), json={}).status_code, 400)
        receipt = {'stopped': True, 'stop_receipt': 'ops:all-device-and-editor-processes-stopped'}
        self.assertEqual(self.client.post('/api/v1/resource-leases/{}/reconcile'.format(lease_id), json=receipt).status_code, 200)
        replay = self.client.post('/api/v1/resource-leases/{}/reconcile'.format(lease_id), json=receipt)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(self._acquire('new', ['device:phone'], owner='new').status_code, 201)
        self.assertEqual(ResourceLeaseEvent.query.filter_by(event_type='stop_reconciled').count(), 2)

    def test_quarantine_survives_feature_flag_rollback(self):
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = True
        first = self._acquire('protected', ['device:phone'], owner='old')
        ResourceLease.query.update({'expires_at': datetime.now() - timedelta(seconds=1)})
        db.session.commit()
        self.assertEqual(self._acquire('new', ['device:phone'], owner='new').status_code, 409)
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = False
        self.assertEqual(self._acquire('new', ['device:phone'], owner='new').status_code, 409)
        self.assertEqual(first.status_code, 201)

    def test_issued_protected_lease_stays_protected_when_flag_is_disabled_before_expiry(self):
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = True
        self.assertEqual(self._acquire('protected', ['device:phone'], owner='old').status_code, 201)
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = False
        ResourceLease.query.update({'expires_at': datetime.now() - timedelta(seconds=1)})
        db.session.commit()
        self.assertEqual(self._acquire('new', ['device:phone'], owner='new').status_code, 409)
        self.assertEqual(ResourceLease.query.one().status, 'quarantined')

    def test_team_run_protection_cannot_be_disabled_by_request_metadata(self):
        self.flow_run.context_json = {'mission': {'control_mode': 'team_managed'}}
        db.session.commit()
        first = self._acquire('team-protected', ['device:phone'], owner=self.flow_run.id)
        lease_id = first.get_json()['leases'][0]['id']
        denied = self.client.post('/api/v1/resource-leases/{}/release'.format(lease_id), json={'reason': 'finished'})
        self.assertEqual(denied.get_json()['code'], 'RESOURCE_STOP_RECEIPT_REQUIRED')
        ResourceLease.query.update({'expires_at': datetime.now() - timedelta(seconds=1)})
        db.session.commit()
        self.assertEqual(self._acquire('new', ['device:phone'], owner='new').status_code, 409)

    def test_expired_protected_release_cannot_bypass_quarantine_without_a_sweep(self):
        self.app.config['RESOURCE_LEASE_RECONCILIATION_ENABLED'] = True
        first = self._acquire('protected', ['device:phone'], owner='old')
        lease_id = first.get_json()['leases'][0]['id']
        ResourceLease.query.update({'expires_at': datetime.now() - timedelta(seconds=1)})
        db.session.commit()
        release = self.client.post('/api/v1/resource-leases/{}/release'.format(lease_id),
                                   json={'stopped': True, 'stop_receipt': 'late-holder-report'})
        self.assertEqual(release.get_json()['code'], 'RESOURCE_STOP_RECONCILIATION_REQUIRED')
        self.assertEqual(ResourceLease.query.one().status, 'quarantined')


if __name__ == '__main__':
    unittest.main()
