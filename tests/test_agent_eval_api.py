import hashlib
import json
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

    def __getattr__(self, _name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    AgentArtifact, AgentEvalCase, AgentEvalDataset, AgentEvalRun,
    AgentEvalScore, OpenClawInstance, Project, User, WorkflowMission,
    hash_token,
)


class AgentEvalApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='agent-eval-test', TESTING=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.project = Project(name='Eval Project')
        self.other_project = Project(name='Other Eval Project')
        db.session.add_all([self.project, self.other_project])
        db.session.flush()
        self.admin = self._user(
            'eval_admin', 'admin', [self.project.id])
        self.second_admin = self._user(
            'eval_second_admin', 'admin', [self.project.id])
        self.member = self._user(
            'eval_member', 'user', [self.project.id])
        self.outsider = self._user(
            'eval_outsider', 'user', [self.other_project.id])
        self.token = 'eval-claw-token'
        self.claw = OpenClawInstance(
            name='需求评测 Agent', safe_name='eval-claw',
            claw_tag='eval-claw', owner=self.member.username,
            project_id=self.project.id, api_token_hash=hash_token(self.token),
            status='工作', role='specialist', llm_provider='timiai',
            llm_model='deepseek-v4-pro-r1')
        db.session.add(self.claw)
        db.session.flush()
        self.mission = WorkflowMission(
            mission_key='eval-mission', project_id=self.project.id,
            main_claw_id=self.claw.id, objective='评测需求分析 Agent',
            status='active', control_mode='agent_autonomous',
            created_by_type='user', created_by_id=self.admin.id,
            created_by_name=self.admin.username,
            expires_at=datetime.now() + timedelta(hours=8))
        db.session.add(self.mission)
        db.session.flush()
        self.artifact = AgentArtifact(
            project_id=self.project.id, mission_id=self.mission.id,
            stage_id='requirement-analysis',
            artifact_type='RequirementArtifact', schema_version=1,
            artifact_version=1, producer_type='claw', producer_id=self.claw.id,
            producer_name=self.claw.name, producer_claw_id=self.claw.id,
            producer_role_key='specialist', producer_provider='timiai',
            producer_profile_version=1, input_baseline_sha256='',
            content_json={'summary': '完成'},
            content_sha256='sha256:' + ('a' * 64), status='accepted',
            idempotency_key='eval-artifact', request_sha256='a' * 64)
        db.session.add(self.artifact)
        db.session.commit()
        self.client = self.app.test_client()
        self._login(self.admin)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _user(self, username, role, project_ids):
        row = User(
            username=username, role=role, managed_projects=project_ids)
        row.set_password('secret')
        db.session.add(row)
        db.session.flush()
        return row

    def _login(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user.id

    def _logout(self):
        with self.client.session_transaction() as session:
            session.clear()

    def _headers(self, key=None, claw=False):
        headers = {}
        if key:
            headers['Idempotency-Key'] = key
        if claw:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    def _create_dataset(self, key='dataset-create-1', **overrides):
        body = {
            'project_id': self.project.id,
            'dataset_key': 'requirement-golden',
            'role_key': 'specialist',
            'version': 1,
            'split': 'golden',
            'rubric_version': 1,
        }
        body.update(overrides)
        return self.client.post(
            '/api/v1/agent-eval/datasets', json=body,
            headers=self._headers(key))

    def test_dataset_with_dual_review_policy_cannot_freeze_before_two_humans(self):
        dataset = self._create_dataset(
            key='dual-review-dataset',
            dataset_key='requirement-dual-review',
            review_policy={'required_reviewers': 2}).get_json()
        case = self._add_case(
            dataset['id'], key='dual-review-case').get_json()
        early = self._freeze(dataset['id'], key='dual-review-early-freeze')
        self.assertEqual(early.status_code, 409)
        self.assertEqual(
            early.get_json()['code'], 'DATASET_REVIEW_INCOMPLETE')

        self._login(self.member)
        denied = self.client.post(
            f"/api/v1/agent-eval/datasets/{dataset['id']}/human-review",
            json={
                'decision': 'approved', 'reviewed_case_keys': [case['case_key']],
                'case_scores': {case['case_key']: {
                    'contract_quality': 90, 'evidence_quality': 90,
                    'difficulty_calibration': 90}},
                'comment': 'member cannot approve',
                'idempotency_key': 'dual-review-member',
            })
        self.assertEqual(denied.status_code, 403)

        self._login(self.admin)
        first = self.client.post(
            f"/api/v1/agent-eval/datasets/{dataset['id']}/human-review",
            json={
                'decision': 'approved', 'reviewed_case_keys': [case['case_key']],
                'case_scores': {case['case_key']: {
                    'contract_quality': 90, 'evidence_quality': 90,
                    'difficulty_calibration': 90}},
                'comment': '第一位评审通过',
                'idempotency_key': 'dual-review-first',
            })
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(first.get_json()['review_status'], 'pending')
        self.assertEqual(first.get_json()['approved_reviewer_count'], 1)

        self._login(self.second_admin)
        second = self.client.post(
            f"/api/v1/agent-eval/datasets/{dataset['id']}/human-review",
            json={
                'decision': 'approved', 'reviewed_case_keys': [case['case_key']],
                'case_scores': {case['case_key']: {
                    'contract_quality': 85, 'evidence_quality': 85,
                    'difficulty_calibration': 85}},
                'comment': '第二位独立评审通过',
                'idempotency_key': 'dual-review-second',
            })
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.get_json()['review_status'], 'approved')
        self.assertEqual(second.get_json()['approved_reviewer_count'], 2)
        frozen = self._freeze(dataset['id'], key='dual-review-freeze')
        self.assertEqual(frozen.status_code, 200, frozen.get_data(as_text=True))

    def test_dual_review_variance_above_threshold_requires_calibration(self):
        dataset = self._create_dataset(
            key='variance-dataset', dataset_key='variance-review',
            review_policy={
                'required_reviewers': 2,
                'max_dimension_variance_percent': 20}).get_json()
        case = self._add_case(dataset['id'], key='variance-case').get_json()

        def review(user, score, key):
            self._login(user)
            return self.client.post(
                f"/api/v1/agent-eval/datasets/{dataset['id']}/human-review",
                json={
                    'decision': 'approved',
                    'reviewed_case_keys': [case['case_key']],
                    'case_scores': {case['case_key']: {
                        'contract_quality': score,
                        'evidence_quality': score,
                        'difficulty_calibration': score}},
                    'comment': '独立评分', 'idempotency_key': key,
                })

        self.assertEqual(review(self.admin, 90, 'variance-first').status_code, 201)
        second = review(self.second_admin, 60, 'variance-second')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(
            second.get_json()['review_status'], 'calibration_required')
        blocked = self._freeze(dataset['id'], key='variance-freeze')
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(
            blocked.get_json()['code'], 'DATASET_REVIEW_INCOMPLETE')

    def _add_case(self, dataset_id, key='case-create-1', **overrides):
        body = {
            'case_key': 'golden-001',
            'input_snapshot': {
                'requirement': {'knowledge_id': 334},
                'baseline_sha256': 'sha256:' + ('b' * 64),
            },
            'expected_contract': {'artifact_type': 'RequirementArtifact'},
            'required_evidence': [{'type': 'knowledge', 'id': 334}],
            'deterministic_checks': [{'check': 'schema_valid'}],
            'allowed_variance': {'wording': True},
            'hidden_tags': ['boundary'],
        }
        body.update(overrides)
        return self.client.post(
            f'/api/v1/agent-eval/datasets/{dataset_id}/cases', json=body,
            headers=self._headers(key))

    def _freeze(self, dataset_id, key='dataset-freeze-1'):
        return self.client.post(
            f'/api/v1/agent-eval/datasets/{dataset_id}/freeze',
            json={'idempotency_key': key})

    def _frozen_case(self):
        dataset = self._create_dataset().get_json()
        case = self._add_case(dataset['id']).get_json()
        frozen = self._freeze(dataset['id'])
        self.assertEqual(frozen.status_code, 200, frozen.get_data(as_text=True))
        return dataset, case

    def test_only_project_admin_can_create_dataset_and_replay_is_idempotent(self):
        first = self._create_dataset(key='same-dataset')
        replay = self._create_dataset(key='same-dataset')
        changed = self._create_dataset(
            key='same-dataset', split='challenge')
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_CONFLICT')

        self._login(self.member)
        denied = self._create_dataset(key='member-dataset')
        self.assertEqual(denied.status_code, 403)

    def test_dataset_list_detail_and_case_hide_challenge_tags_from_members(self):
        dataset = self._create_dataset(
            key='list-dataset', dataset_key='list-challenge',
            split='challenge').get_json()
        case = self._add_case(dataset['id'], key='list-case').get_json()
        admin_detail = self.client.get(
            f"/api/v1/agent-eval/datasets/{dataset['id']}")
        self.assertEqual(admin_detail.status_code, 200)
        self.assertIn('hidden_tags', admin_detail.get_json()['cases'][0])

        self._login(self.member)
        listing = self.client.get(
            f'/api/v1/agent-eval/datasets?project_id={self.project.id}'
            '&role_key=specialist&split=challenge')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()['count'], 1)
        self.assertNotIn('review_records', listing.get_json()['items'][0])
        detail = self.client.get(
            f"/api/v1/agent-eval/datasets/{dataset['id']}")
        case_detail = self.client.get(
            f"/api/v1/agent-eval/cases/{case['id']}")
        self.assertNotIn('hidden_tags', detail.get_json()['cases'][0])
        self.assertNotIn('review_records', detail.get_json())
        self.assertNotIn('hidden_tags', case_detail.get_json())
        self.assertFalse(detail.get_json()['can_review'])

        self._login(self.outsider)
        hidden = self.client.get(
            f"/api/v1/agent-eval/datasets/{dataset['id']}")
        self.assertEqual(hidden.status_code, 404)

    def test_case_input_hash_and_frozen_dataset_are_immutable(self):
        dataset = self._create_dataset().get_json()
        case_response = self._add_case(dataset['id'])
        self.assertEqual(
            case_response.status_code, 201,
            case_response.get_data(as_text=True))
        case = case_response.get_json()
        canonical = json.dumps(
            self._add_case_input(), ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        self.assertEqual(
            case['input_sha256'],
            'sha256:' + hashlib.sha256(canonical).hexdigest())

        frozen = self._freeze(dataset['id'])
        self.assertEqual(frozen.status_code, 200)
        self.assertEqual(frozen.get_json()['status'], 'frozen')
        self.assertTrue(frozen.get_json()['dataset_sha256'].startswith('sha256:'))
        denied = self._add_case(
            dataset['id'], key='case-after-freeze', case_key='golden-002')
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.get_json()['code'], 'DATASET_IMMUTABLE')

    def _add_case_input(self):
        return {
            'requirement': {'knowledge_id': 334},
            'baseline_sha256': 'sha256:' + ('b' * 64),
        }

    def test_new_dataset_version_preserves_frozen_history(self):
        dataset, _case = self._frozen_case()
        second = self._create_dataset(
            key='dataset-version-2', version=2)
        self.assertEqual(second.status_code, 201, second.get_data(as_text=True))
        self.assertEqual(second.get_json()['version'], 2)
        self.assertEqual(
            db.session.get(AgentEvalDataset, dataset['id']).status, 'frozen')
        duplicate = self._create_dataset(
            key='dataset-duplicate-v2', version=2)
        self.assertEqual(duplicate.status_code, 409)

    def test_eval_run_requires_frozen_dataset_and_uses_server_claw_identity(self):
        dataset = self._create_dataset().get_json()
        case = self._add_case(dataset['id']).get_json()
        self._logout()
        body = {
            'case_id': case['id'],
            'claw_id': self.claw.id,
            'profile_version': 1,
            'worker_release': 'worker-test-sha',
            'mission_id': self.mission.id,
            'artifact_id': self.artifact.id,
            'status': 'completed',
            'usage': {'input_tokens': 100, 'output_tokens': 30},
            'timing': {'duration_ms': 2500},
        }
        denied = self.client.post(
            '/api/v1/agent-eval/runs', json=body,
            headers=self._headers('run-before-freeze', claw=True))
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.get_json()['code'], 'DATASET_NOT_FROZEN')

        self._login(self.admin)
        self._freeze(dataset['id'])
        self._logout()
        created = self.client.post(
            '/api/v1/agent-eval/runs', json=body,
            headers=self._headers('run-after-freeze', claw=True))
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        payload = created.get_json()
        self.assertEqual(payload['provider'], 'timiai')
        self.assertEqual(payload['model'], 'deepseek-v4-pro-r1')
        self.assertEqual(payload['dataset_version'], 1)
        self.assertEqual(payload['case_input_sha256'], case['input_sha256'])

        self.claw.llm_model = 'changed-after-run'
        db.session.commit()
        replay = self.client.post(
            '/api/v1/agent-eval/runs', json=body,
            headers=self._headers('run-after-freeze', claw=True))
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['id'], payload['id'])
        self.assertEqual(replay.get_json()['model'], 'deepseek-v4-pro-r1')

    def test_human_score_is_admin_only_versioned_and_fatal_forces_zero(self):
        _dataset, case = self._frozen_case()
        self._logout()
        run = self.client.post(
            '/api/v1/agent-eval/runs',
            json={
                'case_id': case['id'], 'claw_id': self.claw.id,
                'profile_version': 1, 'worker_release': 'worker-test-sha',
                'mission_id': self.mission.id,
                'artifact_id': self.artifact.id, 'status': 'completed',
            }, headers=self._headers('score-run', claw=True)).get_json()

        self._login(self.member)
        denied = self.client.post(
            f"/api/v1/agent-eval/runs/{run['id']}/human-score",
            json={'scorer_version': 1,
                  'dimension_scores': {'factual_correctness': 20},
                  'fatal_violations': [], 'review_comment': 'ok'})
        self.assertEqual(denied.status_code, 403)

        self._login(self.admin)
        scored = self.client.post(
            f"/api/v1/agent-eval/runs/{run['id']}/human-score",
            json={
                'scorer_version': 1,
                'dimension_scores': {
                    'schema': 15, 'baseline': 15,
                    'factual_correctness': 25, 'evidence': 20,
                    'role_judgment': 15, 'efficiency': 5,
                    'experience': 5,
                },
                'fatal_violations': ['CREDENTIAL_LEAK'],
                'review_comment': '发现凭据泄露',
                'idempotency_key': 'human-score-v1',
            })
        self.assertEqual(scored.status_code, 201, scored.get_data(as_text=True))
        self.assertEqual(scored.get_json()['total_score'], 0)
        self.assertEqual(scored.get_json()['scorer_version'], 1)

        rescored = self.client.post(
            f"/api/v1/agent-eval/runs/{run['id']}/human-score",
            json={
                'scorer_version': 2,
                'dimension_scores': {'factual_correctness': 20},
                'fatal_violations': [],
                'review_comment': 'Rubric v2',
                'idempotency_key': 'human-score-v2',
            })
        self.assertEqual(rescored.status_code, 201)
        self.assertEqual(AgentEvalScore.query.count(), 2)
        overview = self.client.get(
            '/api/v1/agent-eval/overview?role_key=specialist')
        self.assertEqual(overview.get_json()['score_count'], 1)
        self.assertEqual(overview.get_json()['historical_score_count'], 2)
        self.assertEqual(overview.get_json()['average_score'], 20.0)

    def test_overview_and_cross_project_visibility(self):
        _dataset, case = self._frozen_case()
        self._logout()
        run = self.client.post(
            '/api/v1/agent-eval/runs',
            json={
                'case_id': case['id'], 'claw_id': self.claw.id,
                'profile_version': 1, 'worker_release': 'worker-test-sha',
                'artifact_id': self.artifact.id, 'status': 'completed',
            }, headers=self._headers('overview-run', claw=True)).get_json()
        self._login(self.admin)
        self.client.post(
            f"/api/v1/agent-eval/runs/{run['id']}/human-score",
            json={
                'scorer_version': 1,
                'dimension_scores': {'factual_correctness': 20},
                'fatal_violations': [], 'review_comment': '通过',
                'idempotency_key': 'overview-score',
            })
        overview = self.client.get(
            '/api/v1/agent-eval/overview?role_key=specialist')
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.get_json()['run_count'], 1)
        self.assertEqual(overview.get_json()['score_count'], 1)
        self.assertEqual(overview.get_json()['average_score'], 20.0)
        run_list = self.client.get(
            f'/api/v1/agent-eval/runs?project_id={self.project.id}'
            '&role_key=specialist&status=completed')
        self.assertEqual(run_list.status_code, 200)
        self.assertEqual(run_list.get_json()['count'], 1)
        self.assertNotIn('scores', run_list.get_json()['items'][0])

        self._login(self.outsider)
        hidden = self.client.get(f"/api/v1/agent-eval/runs/{run['id']}")
        self.assertEqual(hidden.status_code, 404)


if __name__ == '__main__':
    unittest.main()
