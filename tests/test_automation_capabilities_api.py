import sys
import types
import unittest
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
    AutomationCapability,
    AutomationCaseCandidate,
    CapabilityGap,
    CapabilityGapEvent,
    EntityRelation,
    Project,
    User,
)


class AutomationCapabilitiesApiTest(unittest.TestCase):
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
        self.admin = User(username='capability_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_catalog_create_list_update_and_version_conflict(self):
        body = {
            'project_id': self.project.id,
            'key': 'vehicle.upgrade_once',
            'name': 'Upgrade vehicle once',
            'operations': ['open_vehicle_upgrade', 'upgrade_once'],
            'observables': ['vehicle_level', 'currency_balance'],
            'reset_hooks': ['restore_test_account'],
            'platforms': ['unity_editor'],
            'status': 'available',
            'implementation_version': '3',
            'health_checked_at': '2026-08-13T10:30:00+08:00',
        }
        created = self.client.post('/api/v1/automation-capabilities', json=body)
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(created.get_json()['version'], 1)

        listed = self.client.get(
            '/api/v1/automation-capabilities', query_string={
                'project_id': self.project.id,
                'platform': 'unity_editor',
            })
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['total'], 1)
        self.assertEqual(listed.get_json()['items'][0]['key'], body['key'])

        update = dict(body, expected_version=1, status='degraded')
        updated = self.client.post('/api/v1/automation-capabilities', json=update)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['version'], 2)
        self.assertEqual(updated.get_json()['status'], 'degraded')

        conflict = self.client.post('/api/v1/automation-capabilities', json=update)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()['code'],
            'AUTOMATION_CAPABILITY_VERSION_CONFLICT')
        self.assertEqual(AutomationCapability.query.count(), 1)

    def test_healthy_versioned_capability_auto_resolves_gap_and_requeues(self):
        gap = CapabilityGap(
            project_id=self.project.id,
            gap_key='conditional-driving',
            title='Conditional driving missing',
            missing_capabilities_json=['drive.enter_endless'],
            required_observables_json=['ui.gameplay_ready'],
            status='in_progress',
            candidate_requeue_pending=True,
            version=4,
        )
        db.session.add(gap)
        db.session.flush()
        candidate = AutomationCaseCandidate(
            project_id=self.project.id,
            title='Endless smoke',
            state='WAITING_CAPABILITY',
            capability_gap_id=gap.id,
            qualification_outcome='AUTOMATION_CAPABILITY_GAP',
            qualification_run_id=572,
            qualification_evidence_json={'missing': True},
            dedupe_key='endless-smoke-gap',
            version=2,
        )
        db.session.add(candidate)
        db.session.commit()

        response = self.client.post('/api/v1/automation-capabilities', json={
            'project_id': self.project.id,
            'key': 'racinggo.conditional_driving',
            'name': 'Conditional driving',
            'operations': ['drive.enter_endless'],
            'observables': ['ui.gameplay_ready'],
            'status': 'available',
            'implementation_version': 'deepflow-release-667417',
            'health_checked_at': '2026-09-10T12:00:00+08:00',
        })

        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        self.assertEqual([gap.id], response.get_json()[
            'auto_requalification']['resolved_gap_ids'])
        self.assertEqual([candidate.id], response.get_json()[
            'auto_requalification']['requeued_candidate_ids'])
        db.session.refresh(gap)
        db.session.refresh(candidate)
        self.assertEqual('resolved', gap.status)
        self.assertFalse(gap.candidate_requeue_pending)
        self.assertEqual('READY_FOR_CANARY', candidate.state)
        self.assertIsNone(candidate.qualification_run_id)
        self.assertEqual({}, candidate.qualification_evidence_json)
        self.assertEqual(1, CapabilityGapEvent.query.filter_by(
            capability_gap_id=gap.id,
            event_type='capability_catalog_auto_resolved').count())
        relation = EntityRelation.query.filter_by(
            project_id=self.project.id,
            from_type='automation_case_candidate',
            from_id=str(candidate.id),
            relation_type='unblocked_by',
            to_type='capability_gap',
            to_id=str(gap.id),
        ).one()
        self.assertEqual(
            'capability_catalog_auto_resolved',
            relation.metadata_json['resolution'])

    def test_unhealthy_or_unversioned_capability_does_not_close_gap(self):
        gap = CapabilityGap(
            project_id=self.project.id,
            gap_key='bridge-snapshot',
            title='Bridge snapshot missing',
            missing_capabilities_json=['bridge.snapshot'],
            status='open',
        )
        db.session.add(gap)
        db.session.commit()
        response = self.client.post('/api/v1/automation-capabilities', json={
            'project_id': self.project.id,
            'key': 'bridge.snapshot',
            'name': 'Bridge snapshot',
            'status': 'available',
        })
        self.assertEqual(201, response.status_code)
        db.session.refresh(gap)
        self.assertEqual('open', gap.status)
        self.assertEqual([], response.get_json()[
            'auto_requalification']['resolved_gap_ids'])


if __name__ == '__main__':
    unittest.main()
