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
    MissionStage,
    OpenClawInstance,
    Project,
    User,
    WorkflowMission,
    hash_token,
)


class MissionStagesApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='mission-stage-test',
            TESTING=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.project = Project(name='Stage Project')
        self.other_project = Project(name='Other Stage Project')
        self.admin = User(
            username='stage_admin', role='admin', managed_projects=[])
        self.admin.set_password('secret')
        self.outsider = User(
            username='stage_outsider', role='user', managed_projects=[])
        self.outsider.set_password('secret')
        db.session.add_all([
            self.project, self.other_project, self.admin, self.outsider])
        db.session.flush()
        self.outsider.managed_projects = [self.other_project.id]

        self.token = 'stage-agent-token'
        self.other_token = 'stage-agent-other-token'
        self.claw = self._claw(
            'Stage Agent', 'stage-agent', self.token, self.project.id)
        self.other_claw = self._claw(
            'Stage Other Agent', 'stage-other', self.other_token,
            self.project.id)
        db.session.flush()
        self.mission = WorkflowMission(
            mission_key='stage-mission-1', project_id=self.project.id,
            main_claw_id=self.claw.id, objective='验证阶段状态机',
            status='active', control_mode='agent_autonomous',
            created_by_type='user', created_by_id=self.admin.id,
            created_by_name=self.admin.username,
            expires_at=datetime.now() + timedelta(hours=8))
        db.session.add(self.mission)
        db.session.flush()
        self.stage = MissionStage(
            mission_id=self.mission.id,
            stage_key='requirement-analysis',
            stage_version=1,
            role_key='requirement_analyst',
            state='ready',
            input_snapshot_json={
                'requirements': [{'type': 'knowledge', 'id': 334,
                                  'sha256': 'sha256:' + ('a' * 64)}],
            },
            evidence_refs_json=[],
            fencing_token=0,
            version=1,
        )
        db.session.add(self.stage)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _claw(self, name, tag, token, project_id):
        row = OpenClawInstance(
            name=name, safe_name=tag, claw_tag=tag,
            owner=self.admin.username, project_id=project_id,
            api_token_hash=hash_token(token), status='工作',
            role='specialist')
        db.session.add(row)
        return row

    def _headers(self, token=None):
        return {'Authorization': f'Bearer {token or self.token}'}

    def _login(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = user.id

    def _logout(self):
        with self.client.session_transaction() as session:
            session.clear()

    @property
    def _stage_url(self):
        return (
            f'/api/v1/missions/{self.mission.id}/stages/'
            'requirement-analysis')

    def test_claim_assigns_actor_and_increments_fencing_and_version(self):
        self._logout()
        body = {'expected_version': 1, 'idempotency_key': 'claim-1'}
        first = self.client.post(
            self._stage_url + '/claim', json=body,
            headers=self._headers())
        replay = self.client.post(
            self._stage_url + '/claim', json=body,
            headers=self._headers())

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual(first.get_json()['state'], 'running')
        self.assertEqual(first.get_json()['assigned_claw_id'], self.claw.id)
        self.assertEqual(first.get_json()['fencing_token'], 1)
        self.assertEqual(first.get_json()['version'], 2)

    def test_new_claim_invalidates_old_fencing_token(self):
        self._logout()
        first = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 1, 'idempotency_key': 'claim-first'},
            headers=self._headers()).get_json()
        second_response = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': first['version'],
                  'idempotency_key': 'claim-renew'},
            headers=self._headers())
        self.assertEqual(
            second_response.status_code, 200,
            second_response.get_data(as_text=True))
        second = second_response.get_json()
        self.assertEqual(second['fencing_token'], 2)

        stale = self.client.post(
            self._stage_url + '/transition',
            json={
                'expected_version': second['version'],
                'fencing_token': first['fencing_token'],
                'to_state': 'waiting_input',
                'reason_code': 'UPSTREAM_INPUT_MISSING',
                'evidence_refs': [],
                'idempotency_key': 'transition-stale-fence',
            }, headers=self._headers())
        self.assertEqual(stale.status_code, 409, stale.get_data(as_text=True))
        self.assertEqual(stale.get_json()['code'], 'FENCING_TOKEN_STALE')

    def test_transition_uses_optimistic_lock_and_valid_state_machine(self):
        self._logout()
        claimed = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 1, 'idempotency_key': 'claim-transition'},
            headers=self._headers()).get_json()
        transition_body = {
            'expected_version': claimed['version'],
            'fencing_token': claimed['fencing_token'],
            'to_state': 'waiting_input',
            'reason_code': 'UPSTREAM_INPUT_MISSING',
            'evidence_refs': [
                {'type': 'knowledge', 'id': 334,
                 'sha256': 'sha256:' + ('b' * 64)},
            ],
            'idempotency_key': 'transition-waiting',
        }
        transitioned = self.client.post(
            self._stage_url + '/transition', json=transition_body,
            headers=self._headers())
        replay = self.client.post(
            self._stage_url + '/transition', json=transition_body,
            headers=self._headers())
        self.assertEqual(
            transitioned.status_code, 200,
            transitioned.get_data(as_text=True))
        self.assertEqual(transitioned.get_json(), replay.get_json())
        self.assertEqual(transitioned.get_json()['state'], 'waiting_input')
        self.assertEqual(transitioned.get_json()['version'], 3)

        stale_version = dict(transition_body)
        stale_version['to_state'] = 'running'
        stale_version['idempotency_key'] = 'transition-stale-version'
        response = self.client.post(
            self._stage_url + '/transition', json=stale_version,
            headers=self._headers())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['code'], 'STAGE_VERSION_CONFLICT')

    def test_invalid_transition_and_different_claimant_are_rejected(self):
        self._logout()
        invalid = self.client.post(
            self._stage_url + '/transition',
            json={
                'expected_version': 1,
                'fencing_token': 0,
                'to_state': 'submitted',
                'reason_code': 'SKIP_WORK',
                'evidence_refs': [],
                'idempotency_key': 'invalid-transition',
            }, headers=self._headers())
        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(invalid.get_json()['code'], 'INVALID_STAGE_TRANSITION')

        claimed = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 1, 'idempotency_key': 'owner-claim'},
            headers=self._headers()).get_json()
        denied = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': claimed['version'],
                  'idempotency_key': 'other-claim'},
            headers=self._headers(self.other_token))
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.get_json()['code'], 'STAGE_ALREADY_ASSIGNED')

    def test_context_is_project_scoped_and_input_snapshot_is_frozen(self):
        self._login(self.outsider)
        hidden = self.client.get(self._stage_url + '/context')
        self.assertEqual(hidden.status_code, 404)

        self._logout()
        context = self.client.get(
            self._stage_url + '/context', headers=self._headers())
        self.assertEqual(context.status_code, 200, context.get_data(as_text=True))
        payload = context.get_json()
        self.assertEqual(payload['stage']['stage_key'], 'requirement-analysis')
        self.assertEqual(
            payload['input_snapshot']['requirements'][0]['id'], 334)
        self.assertNotIn('idempotency_key', payload['stage'])

    def test_idempotency_key_reuse_with_different_claim_payload_conflicts(self):
        self._logout()
        first = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 1, 'idempotency_key': 'same-claim'},
            headers=self._headers())
        changed = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 2, 'idempotency_key': 'same-claim'},
            headers=self._headers())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_CONFLICT')

    def test_stage_submission_and_acceptance_are_gated_by_artifact_review(self):
        self._logout()
        artifact_response = self.client.post(
            '/api/v1/agent-artifacts',
            json={
                'project_id': self.project.id,
                'mission_id': self.mission.id,
                'stage_id': self.stage.stage_key,
                'artifact_type': 'RequirementArtifact',
                'schema_version': 1,
                'artifact_version': 1,
                'content': {'summary': '可审核的需求分析'},
            },
            headers={**self._headers(), 'Idempotency-Key': 'stage-artifact'})
        self.assertEqual(
            artifact_response.status_code, 201,
            artifact_response.get_data(as_text=True))
        artifact = artifact_response.get_json()
        submitted_artifact = self.client.post(
            f"/api/v1/agent-artifacts/{artifact['id']}/submit",
            headers=self._headers())
        self.assertEqual(submitted_artifact.status_code, 200)

        claimed = self.client.post(
            self._stage_url + '/claim',
            json={'expected_version': 1, 'idempotency_key': 'gate-claim'},
            headers=self._headers()).get_json()
        submitted_stage = self.client.post(
            self._stage_url + '/transition',
            json={
                'expected_version': claimed['version'],
                'fencing_token': claimed['fencing_token'],
                'to_state': 'submitted',
                'reason_code': 'ARTIFACT_READY',
                'artifact_id': artifact['id'],
                'evidence_refs': [],
                'idempotency_key': 'stage-submit',
            }, headers=self._headers())
        self.assertEqual(
            submitted_stage.status_code, 200,
            submitted_stage.get_data(as_text=True))

        blocked_accept = self.client.post(
            self._stage_url + '/transition',
            json={
                'expected_version': submitted_stage.get_json()['version'],
                'fencing_token': claimed['fencing_token'],
                'to_state': 'accepted',
                'reason_code': 'HUMAN_REVIEW_ACCEPTED',
                'artifact_id': artifact['id'],
                'evidence_refs': [],
                'idempotency_key': 'stage-accept-too-early',
            }, headers=self._headers())
        self.assertEqual(blocked_accept.status_code, 409)
        self.assertEqual(
            blocked_accept.get_json()['code'], 'ARTIFACT_NOT_ACCEPTED')

        self._login(self.admin)
        reviewed = self.client.post(
            f"/api/v1/agent-artifacts/{artifact['id']}/review",
            json={'decision': 'accepted', 'comment': '通过'})
        self.assertEqual(reviewed.status_code, 200)
        accepted_stage = self.client.post(
            self._stage_url + '/transition',
            json={
                'expected_version': submitted_stage.get_json()['version'],
                'fencing_token': claimed['fencing_token'],
                'to_state': 'accepted',
                'reason_code': 'HUMAN_REVIEW_ACCEPTED',
                'artifact_id': artifact['id'],
                'evidence_refs': [],
                'idempotency_key': 'stage-accept',
            })
        self.assertEqual(
            accepted_stage.status_code, 200,
            accepted_stage.get_data(as_text=True))
        self.assertEqual(accepted_stage.get_json()['state'], 'accepted')


if __name__ == '__main__':
    unittest.main()
