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
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
    CapabilityGapEvent,
    EntityRelation,
    Project,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class CapabilityGapLifecycleApiTest(unittest.TestCase):
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
        self.admin = User(username='gap_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        definition = WorkflowDefinition(
            workflow_key='gap-qualification', name='Gap qualification',
            project_id=self.project.id,
            definition_json={'key': 'gap-qualification', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
            executor_acl_json={'user_ids': [self.admin.id], 'claw_ids': []},
            visibility_scope='project')
        db.session.add(definition)
        db.session.flush()
        self.qualification_run = WorkflowRun(
            definition_id=definition.id, run_name='Qualification',
            status='succeeded', project_id=self.project.id)
        db.session.add(self.qualification_run)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _upsert_gap(self, key='gap-upsert-1', **overrides):
        body = {
            'project_id': self.project.id,
            'gap_key': 'racinggo:reward-observability',
            'title': 'Reward state is not observable',
            'missing_capabilities': ['read_reward_state'],
            'required_operations': ['claim_once'],
            'required_observables': ['reward_state', 'currency_balance'],
            'required_reset_hooks': ['restore_test_account'],
            'evidence': {'qualification_run_id': self.qualification_run.id},
            'owner': 'automation-platform',
            'development_requirement': {
                'ref': 'TAPD-STORY-9001',
                'url': 'https://tapd.example/story/9001',
                'status': 'planning',
            },
        }
        body.update(overrides)
        return self.client.post(
            '/api/v1/capability-gaps:upsert', json=body,
            headers={'Idempotency-Key': key})

    def _waiting_candidate(self, suffix):
        created = self.client.post(
            '/api/v1/automation-case-candidates:upsert', json={
                'project_id': self.project.id,
                'title': f'Reward case {suffix}',
                'module_key': 'lobby.reward',
                'case_draft': {
                    'steps': ['claim once'],
                    'expected_results': ['reward state changes exactly once'],
                },
                'required_capabilities': ['read_reward_state'],
                'state': 'DESIGNED',
                'dedupe_key': f'gap-candidate-{suffix}',
            })
        candidate = created.get_json()
        ready = self.client.patch(
            f'/api/v1/automation-case-candidates/{candidate["id"]}',
            json={'expected_version': 1, 'state': 'READY_FOR_CANARY'})
        self.assertEqual(ready.status_code, 200, ready.get_data(as_text=True))
        result = self.client.post(
            f'/api/v1/automation-case-candidates/{candidate["id"]}/qualification-result',
            json={
                'expected_version': 2,
                'qualification_outcome': 'AUTOMATION_CAPABILITY_GAP',
                'qualification_run_id': self.qualification_run.id,
                'capability_gap_key': 'racinggo:reward-observability',
                'capability_gap_title': 'Reward state is not observable',
                'missing_capabilities': ['read_reward_state'],
                'required_observables': ['reward_state', 'currency_balance'],
                'evidence': {'message': 'balance reader is unavailable'},
                'idempotency_key': f'gap-qualification-{suffix}',
            })
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.assertEqual(result.get_json()['state'], 'WAITING_CAPABILITY')
        return result.get_json()

    def test_upsert_is_idempotent_versioned_listable_and_tracks_requirement(self):
        created = self._upsert_gap()
        replay = self._upsert_gap()
        updated = self._upsert_gap(
            key='gap-upsert-2', expected_version=1,
            owner='platform-team-b')

        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(updated.status_code, 200, updated.get_data(as_text=True))
        self.assertEqual(updated.get_json()['version'], 2)
        self.assertEqual(updated.get_json()['owner'], 'platform-team-b')
        self.assertEqual(CapabilityGap.query.count(), 1)
        self.assertEqual(CapabilityGapEvent.query.count(), 2)

        listed = self.client.get(
            '/api/v1/capability-gaps?'
            f'project_id={self.project.id}&status=open&owner=platform-team-b')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['total'], 1)
        self.assertEqual(
            listed.get_json()['items'][0]['development_requirement']['ref'],
            'TAPD-STORY-9001')
        relation = EntityRelation.query.filter_by(
            from_type='capability_gap', relation_type='tracked_by').first()
        self.assertIsNotNone(relation)
        self.assertEqual(relation.to_id, 'TAPD-STORY-9001')

    def test_gap_upsert_atomically_links_evidence_candidate(self):
        candidate = AutomationCaseCandidate(
            project_id=self.project.id,
            title='Generated waiting candidate',
            module_key='story.progress',
            case_draft_json={'steps': ['complete story']},
            required_capabilities_json=['read_reward_state'],
            state='WAITING_CAPABILITY',
            dedupe_key='generated-waiting-candidate',
            version=2,
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(candidate)
        db.session.commit()

        response = self._upsert_gap(
            key='gap-upsert-candidate-link',
            evidence={
                'qualification_run_id': self.qualification_run.id,
                'candidate_id': candidate.id,
            },
        )

        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        db.session.refresh(candidate)
        gap = CapabilityGap.query.one()
        self.assertEqual(gap.id, candidate.capability_gap_id)
        self.assertEqual(3, candidate.version)
        self.assertEqual([candidate.id], response.get_json()['candidate_ids'])
        self.assertEqual(
            1, AutomationCaseCandidateEvent.query.filter_by(
                candidate_id=candidate.id,
                event_type='capability_gap_linked').count())
        self.assertIsNotNone(EntityRelation.query.filter_by(
            from_type='automation_case_candidate',
            from_id=str(candidate.id),
            relation_type='blocked_by',
            to_type='capability_gap',
            to_id=str(gap.id),
        ).first())

    def test_gap_upsert_rejects_candidate_with_unmatched_capabilities(self):
        candidate = AutomationCaseCandidate(
            project_id=self.project.id,
            title='Unrelated waiting candidate',
            required_capabilities_json=['unrelated_capability'],
            state='WAITING_CAPABILITY',
            dedupe_key='unrelated-waiting-candidate',
            version=1,
        )
        db.session.add(candidate)
        db.session.commit()

        response = self._upsert_gap(
            key='gap-upsert-unmatched-candidate',
            evidence={'candidate_id': candidate.id},
        )

        self.assertEqual(409, response.status_code, response.get_data(as_text=True))
        self.assertEqual(
            'CAPABILITY_GAP_CANDIDATE_CONFLICT', response.get_json()['code'])
        self.assertEqual(0, CapabilityGap.query.count())
        db.session.refresh(candidate)
        self.assertIsNone(candidate.capability_gap_id)

    def test_resolve_marks_pending_then_atomic_requeue_is_idempotent(self):
        candidate = self._waiting_candidate('one')
        gap_id = candidate['capability_gap_id']
        gap = self.client.get(f'/api/v1/capability-gaps/{gap_id}').get_json()
        started = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/start', json={
                'expected_version': gap['version'],
                'owner': 'automation-platform',
                'development_requirement': {
                    'ref': 'TAPD-STORY-9001',
                    'url': 'https://tapd.example/story/9001',
                    'status': 'developing',
                },
            }, headers={'Idempotency-Key': 'gap-start-1'})
        self.assertEqual(started.status_code, 200, started.get_data(as_text=True))
        self.assertEqual(started.get_json()['status'], 'in_progress')

        stale = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/resolve', json={
                'expected_version': gap['version'],
                'resolution': {'summary': 'reader delivered'},
            }, headers={'Idempotency-Key': 'gap-resolve-stale'})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()['code'],
                         'CAPABILITY_GAP_VERSION_CONFLICT')

        resolved = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/resolve', json={
                'expected_version': started.get_json()['version'],
                'resolution': {
                    'summary': 'Reward reader and account reset hook delivered',
                    'verification_run_id': self.qualification_run.id,
                },
                'development_requirement': {
                    'ref': 'TAPD-STORY-9001',
                    'url': 'https://tapd.example/story/9001',
                    'status': 'done',
                },
            }, headers={'Idempotency-Key': 'gap-resolve-1'})
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertTrue(resolved.get_json()['candidate_requeue_pending'])

        pending = self.client.get(
            '/api/v1/capability-gaps?'
            f'project_id={self.project.id}&status=resolved'
            '&candidate_requeue_pending=true')
        self.assertEqual(pending.get_json()['total'], 1)

        body = {'expected_version': resolved.get_json()['version']}
        requeued = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/requeue-candidates',
            json=body, headers={'Idempotency-Key': 'gap-requeue-1'})
        replay = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/requeue-candidates',
            json=body, headers={'Idempotency-Key': 'gap-requeue-1'})
        self.assertEqual(requeued.status_code, 200, requeued.get_data(as_text=True))
        self.assertEqual(requeued.get_json()['requeued_candidate_ids'],
                         [candidate['id']])
        self.assertFalse(requeued.get_json()['candidate_requeue_pending'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        refreshed = db.session.get(AutomationCaseCandidate, candidate['id'])
        self.assertEqual(refreshed.state, 'READY_FOR_CANARY')
        self.assertIsNone(refreshed.qualification_outcome)
        self.assertEqual(
            AutomationCaseCandidateEvent.query.filter_by(
                candidate_id=candidate['id'],
                event_type='capability_gap_requeued').count(), 1)
        self.assertIsNotNone(EntityRelation.query.filter_by(
            from_type='automation_case_candidate',
            from_id=str(candidate['id']), relation_type='unblocked_by').first())

    def test_partial_requeue_keeps_pending_until_all_waiting_candidates_move(self):
        first = self._waiting_candidate('first')
        second = self._waiting_candidate('second')
        gap_id = first['capability_gap_id']
        self.assertEqual(gap_id, second['capability_gap_id'])
        gap = db.session.get(CapabilityGap, gap_id)
        resolved = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/resolve', json={
                'expected_version': gap.version,
                'resolution': {'summary': 'capability delivered'},
            }, headers={'Idempotency-Key': 'partial-resolve'})
        first_batch = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/requeue-candidates', json={
                'expected_version': resolved.get_json()['version'],
                'candidate_ids': [first['id']],
            }, headers={'Idempotency-Key': 'partial-requeue-1'})

        self.assertTrue(first_batch.get_json()['candidate_requeue_pending'])
        self.assertEqual(
            db.session.get(AutomationCaseCandidate, second['id']).state,
            'WAITING_CAPABILITY')
        second_batch = self.client.post(
            f'/api/v1/capability-gaps/{gap_id}/requeue-candidates', json={
                'expected_version': first_batch.get_json()['version'],
                'candidate_ids': [second['id']],
            }, headers={'Idempotency-Key': 'partial-requeue-2'})
        self.assertFalse(second_batch.get_json()['candidate_requeue_pending'])

    def test_terminal_candidate_transition_clears_resolved_gap_pending_flag(self):
        candidate = self._waiting_candidate('retired')
        gap = db.session.get(CapabilityGap, candidate['capability_gap_id'])
        resolved = self.client.post(
            f'/api/v1/capability-gaps/{gap.id}/resolve', json={
                'expected_version': gap.version,
                'resolution': {'summary': 'capability delivered'},
            }, headers={'Idempotency-Key': 'retired-resolve'})
        self.assertTrue(resolved.get_json()['candidate_requeue_pending'])

        retired = self.client.patch(
            f'/api/v1/automation-case-candidates/{candidate["id"]}', json={
                'expected_version': candidate['version'],
                'state': 'RETIRED',
            })

        self.assertEqual(retired.status_code, 200, retired.get_data(as_text=True))
        db.session.expire_all()
        refreshed = db.session.get(CapabilityGap, gap.id)
        self.assertFalse(refreshed.candidate_requeue_pending)
        self.assertEqual(
            CapabilityGapEvent.query.filter_by(
                capability_gap_id=gap.id,
                event_type='candidate_requeue_flag_refreshed').count(), 1)

    def test_manual_reopen_is_idempotent(self):
        candidate = self._waiting_candidate('reopen')
        gap = db.session.get(CapabilityGap, candidate['capability_gap_id'])
        resolved = self.client.post(
            f'/api/v1/capability-gaps/{gap.id}/resolve', json={
                'expected_version': gap.version,
                'resolution': {'summary': 'first delivery'},
            }, headers={'Idempotency-Key': 'reopen-resolve'})
        reopened = self.client.post(
            f'/api/v1/capability-gaps/{gap.id}/reopen', json={
                'expected_version': resolved.get_json()['version'],
                'reason': 'regression found in replay',
            }, headers={'Idempotency-Key': 'reopen-manual'})
        replay = self.client.post(
            f'/api/v1/capability-gaps/{gap.id}/reopen', json={
                'expected_version': resolved.get_json()['version'],
                'reason': 'regression found in replay',
            }, headers={'Idempotency-Key': 'reopen-manual'})

        self.assertEqual(reopened.status_code, 200, reopened.get_data(as_text=True))
        self.assertEqual(reopened.get_json()['status'], 'open')
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(
            CapabilityGapEvent.query.filter_by(
                capability_gap_id=gap.id, event_type='reopened').count(), 1)

    def test_new_qualification_automatically_reopens_resolved_gap(self):
        first = self._waiting_candidate('auto-reopen-first')
        gap = db.session.get(CapabilityGap, first['capability_gap_id'])
        resolved = self.client.post(
            f'/api/v1/capability-gaps/{gap.id}/resolve', json={
                'expected_version': gap.version,
                'resolution': {'summary': 'capability delivered'},
            }, headers={'Idempotency-Key': 'auto-reopen-resolve'})
        self.assertEqual(resolved.get_json()['status'], 'resolved')

        second = self._waiting_candidate('auto-reopen-second')
        refreshed = db.session.get(CapabilityGap, gap.id)
        self.assertEqual(second['capability_gap_id'], gap.id)
        self.assertEqual(refreshed.status, 'open')
        self.assertFalse(refreshed.candidate_requeue_pending)
        self.assertEqual(refreshed.resolution_json or {}, {})
        self.assertEqual(
            CapabilityGapEvent.query.filter_by(
                capability_gap_id=gap.id,
                event_type='reopened_by_qualification').count(), 1)


if __name__ == '__main__':
    unittest.main()
