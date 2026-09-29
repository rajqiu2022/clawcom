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
    AutomationCapability,
    AutomationCapabilityEvent,
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
    CapabilityGapEvent,
    EntityRelation,
    OpenClawInstance,
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
        db.session.flush()
        self.worker = OpenClawInstance(
            name='RacingGO Worker', claw_tag='claw-capability-worker',
            owner='capability_owner', project_id=self.project.id)
        db.session.add(self.worker)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _available_body(self, key, name, **overrides):
        checked = datetime.now().replace(microsecond=0)
        body = {
            'project_id': self.project.id,
            'key': key,
            'name': name,
            'operations': [],
            'observables': [],
            'reset_hooks': [],
            'platforms': ['unity_editor'],
            'status': 'available',
            'implementation_status': 'implemented',
            'verification_status': 'verified',
            'implementation_version': 'deepflow-v1',
            'producer_claw_id': self.worker.id,
            'release_id': 'deepflow-release-v1',
            'source_commit': 'a' * 40,
            'manifest_sha256': 'b' * 64,
            'health_checked_at': checked.isoformat(),
            'health_expires_at': (checked + timedelta(hours=1)).isoformat(),
            'verification': {
                'release_check': {'status': 'passed'},
                'contract_check': {'status': 'passed'},
                'health_probe': {'status': 'passed'},
                'evidence_refs': [{'type': 'workflow_run', 'id': 700}],
            },
        }
        body.update(overrides)
        return body

    def test_catalog_create_list_update_and_version_conflict(self):
        body = self._available_body(
            'vehicle.upgrade_once', 'Upgrade vehicle once',
            operations=['open_vehicle_upgrade', 'upgrade_once'],
            observables=['vehicle_level', 'currency_balance'],
            reset_hooks=['restore_test_account'],
            implementation_version='3',
        )
        created = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'capability-create-1'})
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
        updated = self.client.post(
            '/api/v1/automation-capabilities', json=update,
            headers={'Idempotency-Key': 'capability-update-1'})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()['version'], 2)
        self.assertEqual(updated.get_json()['status'], 'degraded')

        conflict = self.client.post(
            '/api/v1/automation-capabilities', json=update,
            headers={'Idempotency-Key': 'capability-update-conflict'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()['code'],
            'AUTOMATION_CAPABILITY_VERSION_CONFLICT')
        self.assertEqual(AutomationCapability.query.count(), 1)

    def test_same_capability_can_be_published_by_two_workers_independently(self):
        second = OpenClawInstance(
            name='Second RacingGO Worker', claw_tag='claw-capability-worker-2',
            owner='capability_owner', project_id=self.project.id)
        db.session.add(second)
        db.session.commit()
        key = 'racinggo.runner.control'
        gap = CapabilityGap(
            project_id=self.project.id, gap_key='runner-control-missing',
            title='Runner unavailable', missing_capabilities_json=[key],
            status='open')
        db.session.add(gap)
        db.session.commit()
        first_body = self._available_body(key, 'Runner control')
        second_body = self._available_body(
            key, 'Runner control', producer_claw_id=second.id,
            release_id='deepflow-release-worker-2')
        first = self.client.post(
            '/api/v1/automation-capabilities', json=first_body,
            headers={'Idempotency-Key': 'first-worker-registration'})
        second_result = self.client.post(
            '/api/v1/automation-capabilities', json=second_body,
            headers={'Idempotency-Key': 'second-worker-registration'})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(second_result.status_code, 201,
                         second_result.get_data(as_text=True))
        self.assertNotEqual(first.get_json()['id'], second_result.get_json()['id'])
        self.assertEqual(AutomationCapability.query.count(), 2)

        listed = self.client.get('/api/v1/automation-capabilities',
                                 query_string={'project_id': self.project.id})
        self.assertEqual(listed.get_json()['total'], 2)
        scoped = self.client.get('/api/v1/automation-capabilities',
                                 query_string={
                                     'project_id': self.project.id,
                                     'producer_claw_id': second.id,
                                 })
        self.assertEqual(scoped.get_json()['total'], 1)
        self.assertEqual(scoped.get_json()['items'][0]['producer_claw_id'],
                         second.id)

        updated = self.client.post(
            '/api/v1/automation-capabilities',
            json=dict(first_body, expected_version=1, status='degraded'),
            headers={'Idempotency-Key': 'first-worker-degraded'})
        self.assertEqual(updated.status_code, 200,
                         updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()['status'], 'degraded')
        db.session.refresh(db.session.get(
            AutomationCapability, second_result.get_json()['id']))
        self.assertEqual(db.session.get(
            AutomationCapability, second_result.get_json()['id']).status,
            'available')
        db.session.refresh(gap)
        self.assertEqual(gap.status, 'resolved')
        self.assertEqual(
            [second.id],
            [source['producer_claw_id'] for source in
             gap.resolution_json['capability_sources']],
        )

        ambiguous = self.client.post(
            '/api/v1/automation-capabilities',
            json={'project_id': self.project.id, 'key': key, 'name': 'Runner'},
            headers={'Idempotency-Key': 'ambiguous-worker-update'})
        self.assertEqual(ambiguous.status_code, 400)
        self.assertEqual(ambiguous.get_json()['code'],
                         'PRODUCER_CLAW_ID_REQUIRED')

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
        second_candidate = AutomationCaseCandidate(
            project_id=self.project.id,
            title='Endless reconnect smoke',
            state='WAITING_CAPABILITY',
            capability_gap_id=gap.id,
            qualification_outcome='AUTOMATION_CAPABILITY_GAP',
            qualification_run_id=573,
            qualification_evidence_json={'missing': True},
            dedupe_key='endless-reconnect-smoke-gap',
            version=1,
        )
        db.session.add_all([candidate, second_candidate])
        db.session.commit()

        response = self.client.post(
            '/api/v1/automation-capabilities', json=self._available_body(
            'racinggo.conditional_driving', 'Conditional driving',
            operations=['drive.enter_endless'],
            observables=['ui.gameplay_ready'],
            implementation_version='deepflow-release-667417',
        ), headers={'Idempotency-Key': 'conditional-driving-create'})

        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        self.assertEqual([gap.id], response.get_json()[
            'auto_requalification']['resolved_gap_ids'])
        self.assertEqual([candidate.id, second_candidate.id], response.get_json()[
            'auto_requalification']['requeued_candidate_ids'])
        db.session.refresh(gap)
        db.session.refresh(candidate)
        db.session.refresh(second_candidate)
        self.assertEqual('resolved', gap.status)
        self.assertFalse(gap.candidate_requeue_pending)
        self.assertEqual('READY_FOR_CANARY', candidate.state)
        self.assertEqual('READY_FOR_CANARY', second_candidate.state)
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
        capability_relation = EntityRelation.query.filter_by(
            project_id=self.project.id,
            from_type='automation_capability',
            relation_type='satisfies',
            to_type='capability_gap',
            to_id=str(gap.id),
        ).one()
        self.assertTrue(capability_relation.metadata_json['active'])
        self.assertEqual(
            ['drive.enter_endless', 'ui.gameplay_ready'],
            capability_relation.metadata_json['matched_tokens'])
        replay = self.client.post(
            '/api/v1/automation-capabilities', json=self._available_body(
                'racinggo.conditional_driving', 'Conditional driving',
                operations=['drive.enter_endless'],
                observables=['ui.gameplay_ready'],
                implementation_version='deepflow-release-667417',
            ), headers={'Idempotency-Key': 'conditional-driving-create'})
        self.assertEqual(200, replay.status_code)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(2, AutomationCaseCandidateEvent.query.filter_by(
            event_type='capability_catalog_auto_requeued').count())

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
            'status': 'planned',
            'implementation_status': 'declared',
            'verification_status': 'unverified',
        }, headers={'Idempotency-Key': 'bridge-planned'})
        self.assertEqual(201, response.status_code)
        db.session.refresh(gap)
        self.assertEqual('open', gap.status)
        self.assertEqual([], response.get_json()[
            'auto_requalification']['resolved_gap_ids'])

    def test_available_requires_verified_release_health_and_evidence(self):
        response = self.client.post('/api/v1/automation-capabilities', json={
            'project_id': self.project.id,
            'key': 'bridge.snapshot',
            'name': 'Bridge snapshot',
            'status': 'available',
        }, headers={'Idempotency-Key': 'invalid-available'})
        self.assertEqual(400, response.status_code)
        self.assertEqual(
            'AUTOMATION_CAPABILITY_INVALID', response.get_json()['code'])
        self.assertEqual(0, AutomationCapability.query.count())

    def test_incompatible_contract_cannot_unlock_gap(self):
        gap = CapabilityGap(
            project_id=self.project.id, gap_key='incompatible-bridge-gap',
            title='Bridge contract missing',
            missing_capabilities_json=['bridge.snapshot'], status='open')
        db.session.add(gap)
        db.session.commit()
        body = self._available_body('bridge.snapshot', 'Bridge snapshot')
        body['verification']['contract_check'] = {
            'status': 'failed', 'reason': 'operation_contract_version_mismatch'}
        response = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'bridge-incompatible-contract'})
        self.assertEqual(400, response.status_code)
        self.assertIn('verification.contract_check', response.get_json()['message'])
        db.session.refresh(gap)
        self.assertEqual('open', gap.status)
        self.assertEqual(0, AutomationCapability.query.count())

    def test_publication_idempotency_replays_once_and_rejects_key_reuse(self):
        body = self._available_body('bridge.snapshot', 'Bridge snapshot')
        first = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'bridge-idempotent'})
        replay = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'bridge-idempotent'})
        reused = self.client.post(
            '/api/v1/automation-capabilities',
            json=dict(body, name='Different payload'),
            headers={'Idempotency-Key': 'bridge-idempotent'})
        self.assertEqual(201, first.status_code)
        self.assertEqual(200, replay.status_code)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(1, replay.get_json()['version'])
        self.assertEqual(409, reused.status_code)
        self.assertEqual(1, AutomationCapabilityEvent.query.filter_by(
            capability_id=first.get_json()['id']).count())

    def test_degradation_reopens_gap_and_requires_canary_again(self):
        gap = CapabilityGap(
            project_id=self.project.id, gap_key='bridge-gap',
            title='Bridge snapshot missing',
            missing_capabilities_json=['bridge.snapshot'], status='open')
        db.session.add(gap)
        db.session.flush()
        candidate = AutomationCaseCandidate(
            project_id=self.project.id, title='Bridge smoke',
            state='WAITING_CAPABILITY', capability_gap_id=gap.id,
            required_capabilities_json=['bridge.snapshot'],
            dedupe_key='bridge-smoke', version=1)
        db.session.add(candidate)
        db.session.commit()
        body = self._available_body('bridge.snapshot', 'Bridge snapshot')
        created = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'bridge-up'})
        self.assertEqual(201, created.status_code)
        self.assertEqual('READY_FOR_CANARY', candidate.state)

        candidate.state = 'QUALIFIED'
        candidate.qualification_outcome = 'QUALIFIED'
        candidate.qualification_run_id = 701
        candidate.qualification_evidence_json = {'manifest': 'kept-in-event'}
        db.session.commit()
        down = dict(body, expected_version=1, status='degraded')
        degraded = self.client.post(
            '/api/v1/automation-capabilities', json=down,
            headers={'Idempotency-Key': 'bridge-down'})
        self.assertEqual(200, degraded.status_code)
        db.session.refresh(gap)
        db.session.refresh(candidate)
        self.assertEqual('open', gap.status)
        self.assertEqual('WAITING_CAPABILITY', candidate.state)
        self.assertIn(gap.id, degraded.get_json()[
            'auto_requalification']['reopened_gap_ids'])
        event = AutomationCaseCandidateEvent.query.filter_by(
            candidate_id=candidate.id,
            event_type='capability_catalog_invalidated').one()
        self.assertEqual(
            {'manifest': 'kept-in-event'},
            event.payload_json['previous_qualification']['evidence'])
        capability_relation = EntityRelation.query.filter_by(
            project_id=self.project.id,
            from_type='automation_capability',
            relation_type='satisfies',
            to_type='capability_gap',
            to_id=str(gap.id),
        ).one()
        self.assertFalse(capability_relation.metadata_json['active'])
        self.assertTrue(capability_relation.metadata_json['invalidated_at'])

    def test_expired_health_lease_is_reconciled_on_read(self):
        body = self._available_body('ui.snapshot', 'UI snapshot')
        created = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'ui-snapshot-up'})
        self.assertEqual(201, created.status_code)
        row = db.session.get(AutomationCapability, created.get_json()['id'])
        row.health_expires_at = datetime.now() - timedelta(seconds=1)
        db.session.commit()
        listed = self.client.get(
            '/api/v1/automation-capabilities',
            query_string={'project_id': self.project.id})
        self.assertEqual(200, listed.status_code)
        db.session.refresh(row)
        self.assertEqual('unavailable', row.status)
        self.assertEqual('failed', row.verification_status)
        self.assertEqual(
            [row.id], listed.get_json()['diagnostics'][
                'expired_capability_ids'])

    def test_health_renewal_is_stable_but_release_change_requalifies(self):
        gap = CapabilityGap(
            project_id=self.project.id, gap_key='login-gap',
            title='Login unavailable',
            missing_capabilities_json=['login.execute'], status='open')
        db.session.add(gap)
        db.session.flush()
        candidate = AutomationCaseCandidate(
            project_id=self.project.id, title='Login smoke',
            state='WAITING_CAPABILITY', capability_gap_id=gap.id,
            required_capabilities_json=['login.execute'],
            dedupe_key='login-smoke', version=1)
        db.session.add(candidate)
        db.session.commit()
        body = self._available_body(
            'login.execute', 'Login', operations=['login.execute'])
        created = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'login-v1'})
        self.assertEqual(201, created.status_code)
        candidate.state = 'QUALIFIED'
        candidate.qualification_outcome = 'QUALIFIED'
        candidate.qualification_run_id = 710
        candidate.qualification_evidence_json = {'run': 710}
        db.session.commit()

        checked = datetime.now().replace(microsecond=0)
        renewed_body = dict(
            body, expected_version=1,
            health_checked_at=checked.isoformat(),
            health_expires_at=(checked + timedelta(hours=2)).isoformat())
        renewed = self.client.post(
            '/api/v1/automation-capabilities', json=renewed_body,
            headers={'Idempotency-Key': 'login-health-renewal'})
        self.assertEqual(200, renewed.status_code)
        self.assertEqual([], renewed.get_json()[
            'auto_requalification']['requalified_gap_ids'])
        self.assertEqual('QUALIFIED', candidate.state)

        changed_body = dict(
            renewed_body, expected_version=2,
            implementation_version='deepflow-v2',
            release_id='deepflow-release-v2',
            source_commit='c' * 40,
            manifest_sha256='d' * 64)
        changed = self.client.post(
            '/api/v1/automation-capabilities', json=changed_body,
            headers={'Idempotency-Key': 'login-v2'})
        self.assertEqual(200, changed.status_code)
        db.session.refresh(gap)
        db.session.refresh(candidate)
        self.assertEqual('resolved', gap.status)
        self.assertEqual('READY_FOR_CANARY', candidate.state)
        self.assertEqual([gap.id], changed.get_json()[
            'auto_requalification']['requalified_gap_ids'])
        self.assertEqual(1, AutomationCaseCandidateEvent.query.filter_by(
            candidate_id=candidate.id,
            event_type='capability_catalog_requalification_required').count())

    def test_platform_mismatch_does_not_resolve_gap(self):
        gap = CapabilityGap(
            project_id=self.project.id, gap_key='android-login-gap',
            title='Android login unavailable',
            missing_capabilities_json=['login.execute'],
            evidence_json={'platform': 'android'}, status='open')
        db.session.add(gap)
        db.session.commit()
        body = self._available_body(
            'login.execute', 'Unity login', operations=['login.execute'],
            platforms=['unity_editor'])
        response = self.client.post(
            '/api/v1/automation-capabilities', json=body,
            headers={'Idempotency-Key': 'unity-login-only'})
        self.assertEqual(201, response.status_code)
        db.session.refresh(gap)
        self.assertEqual('open', gap.status)
        self.assertEqual([], response.get_json()[
            'auto_requalification']['resolved_gap_ids'])


if __name__ == '__main__':
    unittest.main()
