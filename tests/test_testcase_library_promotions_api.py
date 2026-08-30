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
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    Project,
    TestCase,
    TestCaseLibrary,
    TestCaseLibraryPromotion,
    TestCaseLibraryRevision,
    User,
)
from app.services.testcase_library_versioning import (  # noqa: E402
    canonical_hash,
    canonical_library_snapshot,
    ensure_library_revision,
)


class TestcaseLibraryPromotionsApiTest(unittest.TestCase):
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
        self.admin = User(username='promotion_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.library = TestCaseLibrary(
            name='RacingGO production cases',
            project_name=self.project.name,
            owner=self.admin.username,
            mindmap={'id': 'root', 'title': 'RacingGO'},
        )
        db.session.add(self.library)
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _candidate(self, suffix, state='QUALIFIED', outcome='QUALIFIED'):
        row = AutomationCaseCandidate(
            project_id=self.project.id,
            title=f'Run business assertion {suffix}',
            module_key='lobby.sign_in',
            source_type='content_scan',
            source_refs_json=[{'type': 'commit', 'value': f'commit-{suffix}'}],
            case_draft_json={
                'case_id': f'AUTO-{suffix}',
                'priority': 'P1',
                'type': 'functional',
                'preconditions': ['account is eligible'],
                'steps': ['claim once'],
                'expected_results': ['claimed state and balance change exactly once'],
                'automation': {'close_to_lobby': True},
                'tags': ['closed-loop'],
            },
            required_capabilities_json=['claim_once', 'read_reward_state'],
            state=state,
            qualification_outcome=outcome,
            production_library_id=self.library.id,
            dedupe_key=f'candidate-{suffix}',
            version=4,
            created_by=self.admin.username,
            updated_by=self.admin.username,
        )
        db.session.add(row)
        db.session.commit()
        return row

    def _promote(self, candidates, key, expected=0, dry_run=False):
        return self.client.post(
            f'/api/v1/testcase-libraries/{self.library.id}/promotions',
            json={
                'candidate_ids': [candidate.id for candidate in candidates],
                'expected_library_revision': expected,
                'idempotency_key': key,
                'dry_run': dry_run,
                'message': f'publish {key}',
            },
        )

    def test_at03_capability_gap_candidate_cannot_enter_library(self):
        candidate = self._candidate(
            'gap', state='WAITING_CAPABILITY',
            outcome='AUTOMATION_CAPABILITY_GAP')

        response = self._promote([candidate], 'gap-publish')

        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['code'], 'PROMOTION_CONFLICT')
        self.assertEqual(TestCase.query.count(), 0)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 0)
        self.assertEqual(TestCaseLibraryRevision.query.count(), 0)
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 0)

    def test_dry_run_returns_diff_without_publishing(self):
        candidate = self._candidate('dry')

        response = self._promote([candidate], 'dry-publish', dry_run=True)

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload['dry_run'])
        self.assertEqual(payload['expected_after_revision'], 1)
        self.assertEqual(payload['diff']['create_count'], 1)
        self.assertEqual(TestCase.query.count(), 0)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 0)
        self.assertEqual(TestCaseLibraryRevision.query.count(), 0)
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 0)
        self.assertEqual(self.library.content_hash, '')
        db.session.refresh(candidate)
        self.assertEqual(candidate.state, 'QUALIFIED')

    def test_at04_idempotent_replay_publishes_once(self):
        candidate = self._candidate('once')

        first = self._promote([candidate], 'publish-once')
        replay = self._promote([candidate], 'publish-once')

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(
            first.get_json()['promotion_id'], replay.get_json()['promotion_id'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(TestCase.query.count(), 1)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 1)
        self.assertEqual(TestCaseLibraryRevision.query.count(), 2)  # baseline + r1
        self.assertEqual(AutomationCaseCandidateEvent.query.filter_by(
            event_type='promoted').count(), 1)
        db.session.refresh(candidate)
        self.assertEqual(candidate.state, 'ACTIVE')
        self.assertIsNotNone(candidate.production_case_id)
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 1)
        self.assertEqual(self.library.content_hash, first.get_json()['content_hash'])
        db.session.expire_all()
        live_library = db.session.get(TestCaseLibrary, self.library.id)
        self.assertEqual(
            canonical_hash(canonical_library_snapshot(live_library)),
            first.get_json()['content_hash'])

    def test_at05_stale_publisher_loses_and_can_dry_run_again(self):
        winner = self._candidate('winner')
        stale = self._candidate('stale')

        success = self._promote([winner], 'winner-key', expected=0)
        conflict = self._promote([stale], 'stale-key', expected=0)
        retry_dry_run = self._promote(
            [stale], 'stale-dry-key', expected=1, dry_run=True)

        self.assertEqual(success.status_code, 201)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'], 'LIBRARY_REVISION_CONFLICT')
        self.assertEqual(retry_dry_run.status_code, 200)
        self.assertEqual(retry_dry_run.get_json()['before_revision'], 1)
        self.assertEqual(TestCase.query.count(), 1)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 1)
        db.session.refresh(stale)
        self.assertEqual(stale.state, 'QUALIFIED')

    def test_conflict_rolls_back_implicit_legacy_sync_revision(self):
        winner = self._candidate('baseline-winner')
        promoted = self._promote([winner], 'baseline-winner-key', expected=0)
        self.assertEqual(promoted.status_code, 201)
        published_case = TestCase.query.one()
        published_case.title = 'Changed through the legacy editor'
        db.session.commit()
        revision_count_before = TestCaseLibraryRevision.query.count()
        stale = self._candidate('legacy-stale')

        response = self._promote(
            [stale], 'legacy-stale-key', expected=1)

        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['code'], 'LIBRARY_REVISION_CONFLICT')
        self.assertEqual(TestCaseLibraryRevision.query.count(), revision_count_before)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 1)
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 1)
        db.session.refresh(published_case)
        self.assertEqual(published_case.title, 'Changed through the legacy editor')
        db.session.refresh(stale)
        self.assertEqual(stale.state, 'QUALIFIED')

    def test_revision_list_is_read_only_and_reports_live_drift(self):
        candidate = self._candidate('read-only-history')
        promoted = self._promote(
            [candidate], 'read-only-history-key', expected=0)
        self.assertEqual(promoted.status_code, 201)
        case = TestCase.query.one()
        case.title = 'Changed by legacy editor after formal publish'
        db.session.commit()
        before_count = TestCaseLibraryRevision.query.count()

        response = self.client.get(
            f'/api/v1/testcase-libraries/{self.library.id}/revisions')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['content_drift'])
        self.assertNotEqual(
            response.get_json()['live_content_hash'],
            response.get_json()['content_hash'])
        self.assertEqual(TestCaseLibraryRevision.query.count(), before_count)
        db.session.refresh(self.library)
        self.assertEqual(self.library.revision, 1)

    def test_revision_insert_race_is_a_stable_409_without_side_effects(self):
        candidate = self._candidate('revision-race')
        duplicate = IntegrityError(
            'duplicate revision', {}, Exception('duplicate key'))

        with patch(
                'app.api.testcase_promotions.ensure_library_revision',
                side_effect=duplicate):
            response = self._promote(
                [candidate], 'revision-race-key', expected=0)

        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['code'], 'LIBRARY_REVISION_CONFLICT')
        self.assertTrue(response.get_json()['details']['retryable'])
        self.assertEqual(TestCase.query.count(), 0)
        self.assertEqual(TestCaseLibraryRevision.query.count(), 0)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 0)
        db.session.refresh(candidate)
        self.assertEqual(candidate.state, 'QUALIFIED')

    def test_revision_can_be_read_back_by_exact_number_and_hash(self):
        candidate = self._candidate('readback')
        promoted = self._promote([candidate], 'readback-key')
        self.assertEqual(promoted.status_code, 201)

        response = self.client.get(
            f'/api/v1/testcase-libraries/{self.library.id}/revisions/1')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['revision'], 1)
        self.assertEqual(payload['content_hash'], promoted.get_json()['content_hash'])
        self.assertEqual(payload['snapshot']['cases'][0]['case_id'], 'AUTO-readback')
        self.assertNotIn(
            '.', payload['snapshot']['cases'][0]['created_at'])
        self.assertIn('ai_prompt', payload['snapshot']['cases'][0])
        self.assertIn('tapd_story_url', payload['snapshot']['cases'][0])

    def test_at12_rollback_creates_new_revision_and_preserves_history(self):
        first_candidate = self._candidate('first')
        first = self._promote([first_candidate], 'first-key', expected=0)
        self.assertEqual(first.status_code, 201)
        second_candidate = self._candidate('second')
        second = self._promote([second_candidate], 'second-key', expected=1)
        self.assertEqual(second.status_code, 201)
        historical_before = TestCaseLibraryRevision.query.filter_by(
            library_id=self.library.id, revision=2).first()
        historical_snapshot = dict(historical_before.snapshot_json)
        historical_hash = historical_before.content_hash

        rollback = self.client.post(
            f'/api/v1/testcase-libraries/{self.library.id}/revisions/1/rollback',
            json={
                'expected_library_revision': 2,
                'idempotency_key': 'rollback-to-one',
                'message': 'revert failed expansion',
            },
        )

        self.assertEqual(rollback.status_code, 201, rollback.get_data(as_text=True))
        payload = rollback.get_json()
        self.assertEqual(payload['after_revision'], 3)
        self.assertEqual(payload['rollback_from_revision'], 1)
        self.assertEqual(TestCase.query.count(), 1)
        self.assertEqual(TestCase.query.first().case_id, 'AUTO-first')
        db.session.refresh(second_candidate)
        self.assertEqual(second_candidate.state, 'PENDING_PUBLISH')
        self.assertIsNone(second_candidate.production_case_id)
        self.assertEqual(AutomationCaseCandidateEvent.query.filter_by(
            candidate_id=second_candidate.id,
            event_type='promotion_rolled_back').count(), 1)
        revision_three = TestCaseLibraryRevision.query.filter_by(
            library_id=self.library.id, revision=3).first()
        revision_one = TestCaseLibraryRevision.query.filter_by(
            library_id=self.library.id, revision=1).first()
        self.assertEqual(revision_three.source_type, 'rollback')
        self.assertEqual(revision_three.content_hash, revision_one.content_hash)
        db.session.expire_all()
        historical_after = TestCaseLibraryRevision.query.filter_by(
            library_id=self.library.id, revision=2).first()
        self.assertEqual(historical_after.content_hash, historical_hash)
        self.assertEqual(historical_after.snapshot_json, historical_snapshot)
        db.session.expire_all()
        current_library = db.session.get(TestCaseLibrary, self.library.id)
        current_revision, drifted = ensure_library_revision(
            current_library, self.admin.username)
        self.assertFalse(drifted)
        self.assertEqual(current_revision.revision, 3)
        db.session.commit()
        self.assertEqual(TestCaseLibraryRevision.query.count(), 4)

    def test_rollback_compat_route_uses_same_append_only_operation(self):
        candidate = self._candidate('compat-rollback')
        promoted = self._promote([candidate], 'compat-promote', expected=0)
        self.assertEqual(promoted.status_code, 201)

        response = self.client.post(
            f'/api/v1/testcase-libraries/{self.library.id}/rollback',
            json={
                'target_revision': 0,
                'expected_library_revision': 1,
                'idempotency_key': 'compat-rollback-key',
            },
        )

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        self.assertEqual(response.get_json()['rollback_from_revision'], 0)
        self.assertEqual(response.get_json()['after_revision'], 2)
        self.assertEqual(TestCase.query.count(), 0)

    def test_library_delete_cleans_promotion_revision_and_candidate_links(self):
        candidate = self._candidate('delete-library')
        promoted = self._promote([candidate], 'delete-library-key', expected=0)
        self.assertEqual(promoted.status_code, 201)

        response = self.client.delete(
            f'/api/v1/testcase-libraries/{self.library.id}')

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIsNone(db.session.get(TestCaseLibrary, self.library.id))
        self.assertEqual(TestCase.query.count(), 0)
        self.assertEqual(TestCaseLibraryPromotion.query.count(), 0)
        self.assertEqual(TestCaseLibraryRevision.query.count(), 0)
        db.session.refresh(candidate)
        self.assertIsNone(candidate.production_library_id)
        self.assertIsNone(candidate.production_case_id)


if __name__ == '__main__':
    unittest.main()
