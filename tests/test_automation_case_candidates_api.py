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
    Project,
    TestCase,
    TestCaseLibrary,
    User,
    WorkflowDefinition,
    WorkflowRun,
)


class AutomationCaseCandidatesApiTest(unittest.TestCase):
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
        self._seed()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        self.project = Project(name='RacingGO')
        self.admin = User(username='candidate_owner', role='super_admin')
        self.admin.set_password('secret')
        db.session.add_all([self.project, self.admin])
        db.session.flush()
        self.library = TestCaseLibrary(
            name='RacingGO production cases',
            project_name=self.project.name,
            owner=self.admin.username,
        )
        self.definition = WorkflowDefinition(
            workflow_key='candidate-qualification',
            name='Candidate Qualification',
            project_id=self.project.id,
            definition_json={'key': 'candidate-qualification', 'steps': []},
            owner_type='user', owner_id=self.admin.id,
        )
        db.session.add_all([self.library, self.definition])
        db.session.flush()
        self.qualification_run = WorkflowRun(
            definition_id=self.definition.id,
            run_name='Qualification Run',
            status='succeeded',
            project_id=self.project.id,
        )
        db.session.add(self.qualification_run)
        db.session.commit()

    def _candidate_body(self, key='candidate-sign-in'):
        return {
            'project_id': self.project.id,
            'title': 'Claim one sign-in reward and verify business state',
            'module_key': 'lobby.sign_in',
            'source_type': 'content_scan',
            'source_refs': [
                {'type': 'commit', 'value': 'abc123'},
                {'type': 'finding', 'value': 'FND-789'},
            ],
            'case_draft': {
                'preconditions': ['account is eligible for sign-in reward'],
                'steps': ['open sign-in panel', 'claim one reward'],
                'expected_results': [
                    'claimed state becomes true',
                    'reward balance changes exactly once',
                ],
                'automation': {'close_to_lobby': True},
            },
            'required_capabilities': [
                'open_sign_in', 'claim_once', 'read_reward_state'],
            'state': 'DESIGNED',
            'production_library_id': self.library.id,
            'dedupe_key': key,
            'idempotency_key': key,
        }

    def _create_candidate(self, key='candidate-sign-in'):
        response = self.client.post(
            '/api/v1/automation-case-candidates:upsert',
            json=self._candidate_body(key),
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def _ready_candidate(self, key='candidate-sign-in'):
        created = self._create_candidate(key)
        response = self.client.patch(
            f'/api/v1/automation-case-candidates/{created["id"]}',
            json={'expected_version': 1, 'state': 'READY_FOR_CANARY'},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_upsert_replay_does_not_create_duplicate_candidate(self):
        first = self.client.post(
            '/api/v1/automation-case-candidates:upsert',
            json=self._candidate_body(),
        )
        replay = self.client.post(
            '/api/v1/automation-case-candidates:upsert',
            json=self._candidate_body(),
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(AutomationCaseCandidate.query.count(), 1)
        self.assertEqual(AutomationCaseCandidateEvent.query.count(), 1)

    def test_at02_stale_expected_version_is_rejected(self):
        created = self._create_candidate()
        first = self.client.patch(
            f'/api/v1/automation-case-candidates/{created["id"]}',
            json={
                'expected_version': 1,
                'title': 'Updated by writer one',
            },
        )
        stale = self.client.patch(
            f'/api/v1/automation-case-candidates/{created["id"]}',
            json={
                'expected_version': 1,
                'title': 'Overwritten by writer two',
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()['version'], 2)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()['code'], 'CANDIDATE_VERSION_CONFLICT')
        self.assertEqual(stale.get_json()['details']['current_version'], 2)
        current = db.session.get(AutomationCaseCandidate, created['id'])
        self.assertEqual(current.title, 'Updated by writer one')

    def test_server_rejects_direct_designed_to_active_jump(self):
        created = self._create_candidate()
        rejected = self.client.patch(
            f'/api/v1/automation-case-candidates/{created["id"]}',
            json={'expected_version': 1, 'state': 'ACTIVE'},
        )

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()['code'], 'CANDIDATE_VALIDATION_FAILED')
        self.assertEqual(
            db.session.get(AutomationCaseCandidate, created['id']).state,
            'DESIGNED')

    def test_at03_capability_gap_moves_candidate_and_creates_link(self):
        ready = self._ready_candidate()
        result = self.client.post(
            f'/api/v1/automation-case-candidates/{ready["id"]}/qualification-result',
            json={
                'expected_version': ready['version'],
                'qualification_outcome': 'AUTOMATION_CAPABILITY_GAP',
                'qualification_run_id': self.qualification_run.id,
                'missing_capabilities': ['read_reward_state'],
                'evidence': {'message': 'reward balance is not observable'},
                'idempotency_key': 'qualification-gap-1',
            },
        )

        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        payload = result.get_json()
        self.assertEqual(payload['state'], 'WAITING_CAPABILITY')
        self.assertEqual(
            payload['qualification_outcome'], 'AUTOMATION_CAPABILITY_GAP')
        self.assertIsNotNone(payload['capability_gap_id'])
        self.assertEqual(payload['capability_gap']['status'], 'open')
        self.assertEqual(CapabilityGap.query.count(), 1)
        self.assertEqual(TestCase.query.count(), 0)
        self.assertIsNone(payload['production_case_id'])

        gap = self.client.get(
            f'/api/v1/capability-gaps/{payload["capability_gap_id"]}')
        self.assertEqual(gap.status_code, 200)
        self.assertEqual(gap.get_json()['candidate_ids'], [ready['id']])

    def test_qualification_result_is_idempotent(self):
        ready = self._ready_candidate('candidate-vehicle-upgrade')
        body = {
            'expected_version': ready['version'],
            'qualification_outcome': 'QUALIFIED',
            'qualification_run_id': self.qualification_run.id,
            'evidence': {'editor_run': 'passed'},
            'idempotency_key': 'qualification-pass-1',
        }
        first = self.client.post(
            f'/api/v1/automation-case-candidates/{ready["id"]}/qualification-result',
            json=body,
        )
        replay = self.client.post(
            f'/api/v1/automation-case-candidates/{ready["id"]}/qualification-result',
            json=body,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(first.get_json()['idempotent_replay'])
        self.assertTrue(replay.get_json()['idempotent_replay'])
        self.assertEqual(first.get_json()['version'], replay.get_json()['version'])
        self.assertEqual(
            AutomationCaseCandidateEvent.query.filter_by(
                candidate_id=ready['id'],
                event_type='qualification_result').count(), 1)

    def test_list_filters_and_detail_returns_event_timeline(self):
        candidate = self._ready_candidate()
        listed = self.client.get(
            '/api/v1/automation-case-candidates?'
            f'project_id={self.project.id}&state=READY_FOR_CANARY'
            '&page=1&page_size=20')
        detail = self.client.get(
            f'/api/v1/automation-case-candidates/{candidate["id"]}')

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()['total'], 1)
        self.assertEqual(listed.get_json()['items'][0]['state'], 'READY_FOR_CANARY')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.get_json()['events']), 2)
        self.assertEqual(
            detail.get_json()['source_lineage'][0]['type'], 'commit')


if __name__ == '__main__':
    unittest.main()
