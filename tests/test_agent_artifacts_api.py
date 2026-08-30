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
    AgentArtifact,
    OpenClawInstance,
    Project,
    User,
    WorkflowMission,
    hash_token,
)


class AgentArtifactsApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='agent-artifact-test',
            TESTING=True,
            AGENT_TEAM_CONTRACTS_ENABLED=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.project = Project(name='Artifact Project')
        self.other_project = Project(name='Other Artifact Project')
        db.session.add_all([self.project, self.other_project])
        db.session.flush()

        self.super_admin = self._user(
            'artifact_super', 'super_admin', [self.project.id])
        self.member = self._user(
            'artifact_member', 'user', [self.project.id])
        self.outsider = self._user(
            'artifact_outsider', 'user', [self.other_project.id])
        self.project_admin = self._user(
            'artifact_project_admin', 'admin', [self.project.id])
        self.other_admin = self._user(
            'artifact_other_admin', 'admin', [self.other_project.id])

        self.claw_token = 'artifact-claw-token'
        self.claw = OpenClawInstance(
            name='产物 Agent', safe_name='artifact-claw',
            claw_tag='artifact-claw', owner=self.member.username,
            project_id=self.project.id,
            api_token_hash=hash_token(self.claw_token), status='工作',
            role='specialist', llm_provider='timiai')
        db.session.add(self.claw)
        db.session.flush()
        self.member.bound_claw_id = self.claw.id
        self.mission = WorkflowMission(
            mission_key='artifact-mission-1', project_id=self.project.id,
            main_claw_id=self.claw.id, objective='交付需求分析产物',
            status='active', control_mode='agent_autonomous',
            created_by_type='user', created_by_id=self.member.id,
            created_by_name=self.member.username,
            expires_at=datetime.now() + timedelta(hours=8))
        db.session.add(self.mission)
        db.session.commit()

        self.client = self.app.test_client()

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

    def _claw_headers(self, key='artifact-create-1'):
        return {
            'Authorization': f'Bearer {self.claw_token}',
            'Idempotency-Key': key,
        }

    def _body(self, **overrides):
        body = {
            'project_id': self.project.id,
            'mission_id': self.mission.id,
            'stage_id': 'requirement-analysis',
            'artifact_type': 'RequirementArtifact',
            'schema_version': 1,
            'artifact_version': 1,
            'input_baseline_sha256': 'sha256:' + ('a' * 64),
            'content': {
                'summary': '需求分析完成',
                'risks': [{'id': 'R-1', 'level': 'high'}],
            },
        }
        body.update(overrides)
        return body

    def test_claw_creates_canonical_project_scoped_artifact(self):
        self._logout()
        response = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers())

        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        payload = response.get_json()
        canonical = json.dumps(
            self._body()['content'], ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        self.assertEqual(
            payload['content_sha256'],
            'sha256:' + hashlib.sha256(canonical).hexdigest())
        self.assertEqual(payload['status'], 'draft')
        self.assertEqual(payload['producer']['claw_id'], self.claw.id)
        self.assertEqual(payload['producer']['provider'], 'timiai')
        self.assertEqual(payload['producer']['role_key'], 'specialist')
        self.assertNotIn('idempotency_key', payload)

    def test_create_replay_is_idempotent_and_payload_change_conflicts(self):
        self._logout()
        first = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers('same-key'))
        replay = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers('same-key'))
        changed = self.client.post(
            '/api/v1/agent-artifacts',
            json=self._body(content={'summary': 'changed'}),
            headers=self._claw_headers('same-key'))

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertEqual(changed.status_code, 409, changed.get_data(as_text=True))
        self.assertEqual(changed.get_json()['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(AgentArtifact.query.count(), 1)

    def test_version_is_unique_and_accepted_artifact_remains_immutable(self):
        self._logout()
        created = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers()).get_json()
        submitted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/submit",
            headers=self._claw_headers('artifact-submit-1'))
        self.assertEqual(submitted.status_code, 200, submitted.get_data(as_text=True))

        self._login(self.project_admin)
        accepted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/review",
            json={'decision': 'accepted', 'comment': '证据完整'})
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.get_json()['status'], 'accepted')

        duplicate = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers={'Idempotency-Key': 'duplicate-version'})
        self.assertEqual(duplicate.status_code, 409, duplicate.get_data(as_text=True))
        self.assertEqual(duplicate.get_json()['code'], 'ARTIFACT_VERSION_CONFLICT')

        next_version = self.client.post(
            '/api/v1/agent-artifacts',
            json=self._body(
                artifact_version=2,
                content={'summary': '补充后的需求分析'}),
            headers={'Idempotency-Key': 'artifact-version-2'})
        self.assertEqual(next_version.status_code, 201, next_version.get_data(as_text=True))
        self.assertEqual(next_version.get_json()['artifact_version'], 2)
        self.assertEqual(
            db.session.get(AgentArtifact, created['id']).status, 'accepted')

    def test_submit_requires_producer_and_review_requires_project_admin(self):
        self._logout()
        created = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers()).get_json()

        self._login(self.outsider)
        denied_submit = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/submit")
        self.assertEqual(denied_submit.status_code, 404)

        self._logout()
        submitted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/submit",
            headers=self._claw_headers('artifact-submit-2'))
        self.assertEqual(submitted.status_code, 200)

        self._login(self.member)
        denied_review = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/review",
            json={'decision': 'accepted'})
        self.assertEqual(denied_review.status_code, 403)

        self._login(self.other_admin)
        hidden_review = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/review",
            json={'decision': 'accepted'})
        self.assertEqual(hidden_review.status_code, 404)

        self._login(self.project_admin)
        accepted = self.client.post(
            f"/api/v1/agent-artifacts/{created['id']}/review",
            json={'decision': 'accepted'})
        self.assertEqual(accepted.status_code, 200)

    def test_cross_project_read_is_hidden_and_mission_list_is_filtered(self):
        self._logout()
        created = self.client.post(
            '/api/v1/agent-artifacts', json=self._body(),
            headers=self._claw_headers()).get_json()

        self._login(self.outsider)
        hidden = self.client.get(
            f"/api/v1/agent-artifacts/{created['id']}")
        hidden_list = self.client.get(
            f'/api/v1/missions/{self.mission.id}/artifacts')
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(hidden_list.status_code, 404)

        self._login(self.member)
        visible = self.client.get(
            f'/api/v1/missions/{self.mission.id}/artifacts')
        self.assertEqual(visible.status_code, 200, visible.get_data(as_text=True))
        self.assertEqual(visible.get_json()['count'], 1)
        self.assertEqual(visible.get_json()['items'][0]['id'], created['id'])

    def test_server_rejects_forged_producer_and_invalid_content(self):
        self._logout()
        forged = self.client.post(
            '/api/v1/agent-artifacts',
            json=self._body(producer={'claw_id': 999}),
            headers=self._claw_headers('forged-producer'))
        invalid = self.client.post(
            '/api/v1/agent-artifacts',
            json=self._body(content='not-an-object'),
            headers=self._claw_headers('invalid-content'))

        self.assertEqual(forged.status_code, 400)
        self.assertEqual(forged.get_json()['code'], 'READ_ONLY_FIELD')
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()['code'], 'VALIDATION_FAILED')

    def test_feature_flag_can_fail_closed_without_exposing_contract_routes(self):
        self.app.config['AGENT_TEAM_CONTRACTS_ENABLED'] = False
        self._login(self.super_admin)
        response = self.client.get(
            f'/api/v1/missions/{self.mission.id}/artifacts')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()['code'], 'AGENT_TEAM_CONTRACTS_DISABLED')


if __name__ == '__main__':
    unittest.main()
