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
    AgentArtifact, MissionHandoff, MissionStage, OpenClawInstance,
    Project, User, WorkflowMission, hash_token,
)


class MissionHandoffsApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='mission-handoff-test', TESTING=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.project = Project(name='Handoff Project')
        self.other_project = Project(name='Other Handoff Project')
        self.admin = User(
            username='handoff_admin', role='admin', managed_projects=[])
        self.admin.set_password('secret')
        self.outsider = User(
            username='handoff_outsider', role='user', managed_projects=[])
        self.outsider.set_password('secret')
        db.session.add_all([
            self.project, self.other_project, self.admin, self.outsider])
        db.session.flush()
        self.outsider.managed_projects = [self.other_project.id]
        self.source_token = 'handoff-source-token'
        self.target_token = 'handoff-target-token'
        self.source_claw = self._claw(
            '需求 Agent', 'handoff-source', self.source_token, 'specialist')
        self.target_claw = self._claw(
            '工程 Agent', 'handoff-target', self.target_token, 'test_executor')
        db.session.flush()
        self.mission = WorkflowMission(
            mission_key='handoff-mission-1', project_id=self.project.id,
            main_claw_id=self.source_claw.id, objective='串联需求与工程分析',
            status='active', control_mode='agent_autonomous',
            created_by_type='user', created_by_id=self.admin.id,
            created_by_name=self.admin.username,
            expires_at=datetime.now() + timedelta(hours=8), version=3)
        db.session.add(self.mission)
        db.session.flush()
        self.source_stage = MissionStage(
            mission_id=self.mission.id, stage_key='requirement-analysis',
            stage_version=1, role_key='specialist',
            assigned_claw_id=self.source_claw.id, state='accepted',
            input_snapshot_json={}, evidence_refs_json=[],
            fencing_token=7, version=4)
        self.target_stage = MissionStage(
            mission_id=self.mission.id, stage_key='engineering-analysis',
            stage_version=1, role_key='test_executor', state='ready',
            input_snapshot_json={'requires_handoff': True},
            evidence_refs_json=[], fencing_token=0, version=1)
        db.session.add_all([self.source_stage, self.target_stage])
        db.session.flush()
        self.artifact = AgentArtifact(
            project_id=self.project.id, mission_id=self.mission.id,
            stage_id=self.source_stage.stage_key,
            artifact_type='RequirementArtifact', schema_version=1,
            artifact_version=1, producer_type='claw',
            producer_id=self.source_claw.id,
            producer_name=self.source_claw.name,
            producer_claw_id=self.source_claw.id,
            producer_role_key='specialist', producer_provider='timiai',
            producer_profile_version=1, input_baseline_sha256='',
            content_json={'summary': '需求分析完成'},
            content_sha256='sha256:' + ('a' * 64), status='accepted',
            idempotency_key='fixture-artifact', request_sha256='a' * 64)
        db.session.add(self.artifact)
        db.session.flush()
        self.source_stage.output_artifact_id = self.artifact.id
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _claw(self, name, tag, token, role):
        row = OpenClawInstance(
            name=name, safe_name=tag, claw_tag=tag,
            owner=self.admin.username, project_id=self.project.id,
            api_token_hash=hash_token(token), status='工作', role=role,
            llm_provider='timiai')
        db.session.add(row)
        return row

    def _headers(self, token, key=None):
        headers = {'Authorization': f'Bearer {token}'}
        if key:
            headers['Idempotency-Key'] = key
        return headers

    def _logout(self):
        with self.client.session_transaction() as session:
            session.clear()

    def _login(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user.id

    @property
    def _base_url(self):
        return f'/api/v1/missions/{self.mission.id}/handoffs'

    def _body(self, **overrides):
        body = {
            'handoff_version': 1,
            'source_stage_id': self.source_stage.stage_key,
            'target_stage_id': self.target_stage.stage_key,
            'objective': '基于已确认需求完成工程影响分析',
            'input_artifact_refs': [{'artifact_id': self.artifact.id}],
            'accepted_findings': ['需求边界已确认'],
            'open_questions': ['旧版本兼容范围待确认'],
            'constraints': ['只读分析，不修改工程'],
            'acceptance_criteria': ['输出模块与风险清单'],
            'known_pitfalls': [],
            'recommended_next_actions': ['读取源码基线'],
            'evidence_refs': [],
        }
        body.update(overrides)
        return body

    def _create(self, key='handoff-create-1', **overrides):
        self._logout()
        return self.client.post(
            self._base_url, json=self._body(**overrides),
            headers=self._headers(self.source_token, key))

    def test_create_resolves_artifact_refs_and_server_producer(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload['status'], 'draft')
        self.assertEqual(payload['mission_version'], 3)
        self.assertEqual(payload['from_role'], 'specialist')
        self.assertEqual(payload['to_role'], 'test_executor')
        self.assertEqual(payload['producer']['claw_id'], self.source_claw.id)
        self.assertEqual(
            payload['input_artifact_refs'][0]['content_sha256'],
            self.artifact.content_sha256)
        self.assertTrue(payload['content_sha256'].startswith('sha256:'))

    def test_create_is_idempotent_and_changed_request_conflicts(self):
        first = self._create(key='same-handoff')
        self.artifact.status = 'draft'
        db.session.commit()
        replay = self._create(key='same-handoff')
        changed = self._create(
            key='same-handoff', objective='different objective')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(MissionHandoff.query.count(), 1)

    def test_operation_replay_rechecks_current_project_acl(self):
        created = self._create().get_json()
        submitted_body = {
            'expected_version': 1,
            'idempotency_key': 'acl-submit-replay',
        }
        submitted = self.client.post(
            f"{self._base_url}/{created['id']}/submit",
            json=submitted_body, headers=self._headers(self.source_token))
        self.assertEqual(submitted.status_code, 200)

        self.source_claw.project_id = self.other_project.id
        db.session.commit()
        replay = self.client.post(
            f"{self._base_url}/{created['id']}/submit",
            json=submitted_body, headers=self._headers(self.source_token))
        self.assertEqual(replay.status_code, 404)

    def test_submit_requires_non_draft_artifacts(self):
        self.artifact.status = 'draft'
        db.session.commit()
        created = self._create().get_json()
        submitted = self.client.post(
            f"{self._base_url}/{created['id']}/submit",
            json={'expected_version': 1, 'idempotency_key': 'submit-draft'},
            headers=self._headers(self.source_token))
        self.assertEqual(submitted.status_code, 409)
        self.assertEqual(
            submitted.get_json()['code'], 'HANDOFF_ARTIFACT_NOT_SUBMITTED')

    def test_accept_completes_source_and_unlocks_target_claim(self):
        created = self._create().get_json()
        submitted = self.client.post(
            f"{self._base_url}/{created['id']}/submit",
            json={'expected_version': 1, 'idempotency_key': 'handoff-submit'},
            headers=self._headers(self.source_token))
        self.assertEqual(submitted.status_code, 200, submitted.get_data(as_text=True))

        blocked_claim = self.client.post(
            f'/api/v1/missions/{self.mission.id}/stages/'
            'engineering-analysis/claim',
            json={'expected_version': 1, 'idempotency_key': 'early-claim'},
            headers=self._headers(self.target_token))
        self.assertEqual(blocked_claim.status_code, 409)
        self.assertEqual(
            blocked_claim.get_json()['code'], 'HANDOFF_NOT_ACCEPTED')

        accepted = self.client.post(
            f"{self._base_url}/{created['id']}/accept",
            json={
                'expected_version': submitted.get_json()['version'],
                'source_stage_expected_version': 4,
                'source_stage_fencing_token': 7,
                'target_stage_expected_version': 1,
                'idempotency_key': 'handoff-accept',
            }, headers=self._headers(self.target_token))
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.get_json()['status'], 'accepted')
        db.session.expire_all()
        self.assertEqual(db.session.get(MissionStage, self.source_stage.id).state,
                         'completed')
        target = db.session.get(MissionStage, self.target_stage.id)
        self.assertEqual(target.active_handoff_id, created['id'])
        self.assertEqual(target.version, 2)

        claimed = self.client.post(
            f'/api/v1/missions/{self.mission.id}/stages/'
            'engineering-analysis/claim',
            json={'expected_version': 2, 'idempotency_key': 'accepted-claim'},
            headers=self._headers(self.target_token))
        self.assertEqual(claimed.status_code, 200, claimed.get_data(as_text=True))

    def test_reject_requires_reason_and_new_version_supersedes_rejected(self):
        created = self._create().get_json()
        submitted = self.client.post(
            f"{self._base_url}/{created['id']}/submit",
            json={'expected_version': 1, 'idempotency_key': 'reject-submit'},
            headers=self._headers(self.source_token)).get_json()
        missing_reason = self.client.post(
            f"{self._base_url}/{created['id']}/reject",
            json={'expected_version': submitted['version'],
                  'idempotency_key': 'reject-no-reason'},
            headers=self._headers(self.target_token))
        self.assertEqual(missing_reason.status_code, 400)

        rejected = self.client.post(
            f"{self._base_url}/{created['id']}/reject",
            json={'expected_version': submitted['version'],
                  'reason_code': 'MISSING_COMPATIBILITY_SCOPE',
                  'comment': '请补充兼容范围',
                  'idempotency_key': 'reject-valid'},
            headers=self._headers(self.target_token))
        self.assertEqual(rejected.status_code, 200)
        version_two = self._create(
            key='handoff-version-2', handoff_version=2,
            objective='补充兼容范围后重新交接')
        self.assertEqual(version_two.status_code, 201)
        db.session.expire_all()
        self.assertEqual(
            db.session.get(MissionHandoff, created['id']).status,
            'superseded')

    def test_cross_project_handoffs_are_hidden(self):
        created = self._create().get_json()
        self._login(self.outsider)
        item = self.client.get(f"{self._base_url}/{created['id']}")
        listing = self.client.get(self._base_url)
        self.assertEqual(item.status_code, 404)
        self.assertEqual(listing.status_code, 404)


if __name__ == '__main__':
    unittest.main()
