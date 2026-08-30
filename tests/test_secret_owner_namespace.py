import sys
import types
import unittest
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


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

from flask import Flask, g  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (ClawSecret, OpenClawInstance, Project, User,  # noqa: E402
                        hash_token)


class SecretOwnerNamespaceTest(unittest.TestCase):
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

        project = Project(name='RacingGO')
        db.session.add(project)
        db.session.flush()
        self.owner = User(
            username='rajqiu', password_hash='x', role='user',
            managed_projects=[project.id])
        self.other_user = User(
            username='other', password_hash='x', role='user',
            managed_projects=[project.id])
        db.session.add_all([self.owner, self.other_user])
        db.session.flush()
        self.tokens = {
            'first': 'secret-owner-first',
            'second': 'secret-owner-second',
            'other': 'secret-other',
        }
        self.first = self._claw(
            'First', 'rajqiu', project.id, self.tokens['first'])
        self.second = self._claw(
            'Second', 'rajqiu', project.id, self.tokens['second'])
        self.other = self._claw(
            'Other', 'other', project.id, self.tokens['other'])

        self.user_secret = ClawSecret(
            owner_user_id=self.owner.id, key='user-private',
            share_scope='private')
        self.user_secret.set_value('user-value')
        self.agent_secret = ClawSecret(
            owner_claw_id=self.first.id, key='agent-private',
            share_scope='private')
        self.agent_secret.set_value('agent-value')
        db.session.add_all([self.user_secret, self.agent_secret])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @staticmethod
    def _claw(name, owner, project_id, token):
        claw = OpenClawInstance(
            name=name,
            safe_name=name.lower(),
            claw_tag=name.lower(),
            owner=owner,
            project_id=project_id,
            api_token_hash=hash_token(token),
            status='工作',
        )
        db.session.add(claw)
        db.session.flush()
        return claw

    def _headers(self, actor):
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)
        return {'Authorization': f'Bearer {self.tokens[actor]}'}

    def _resolve(self, key, actor='second'):
        return self.client.post(
            '/api/v1/secrets/resolve',
            json={'text': '${SECRET:%s}' % key},
            headers=self._headers(actor),
        )

    def test_owner_agents_can_resolve_user_and_sibling_private_secrets(self):
        user = self._resolve('user-private').get_json()
        sibling = self._resolve('agent-private').get_json()
        self.assertEqual([], user['missing'])
        self.assertEqual(['user-private'], user['used'])
        self.assertEqual('user-value', user['resolved'])
        self.assertEqual([], sibling['missing'])
        self.assertEqual(['agent-private'], sibling['used'])
        self.assertEqual('agent-value', sibling['resolved'])
        by_id = self.client.get(
            '/api/v1/secrets/%d/value' % self.user_secret.id,
            headers=self._headers('second'),
        )
        self.assertEqual(200, by_id.status_code)
        self.assertEqual('user-value', by_id.get_json()['value'])

    def test_other_owner_agent_cannot_resolve_private_secret(self):
        payload = self._resolve('user-private', actor='other').get_json()
        self.assertEqual(['user-private'], payload['missing'])
        self.assertEqual([], payload.get('used', []))

    def test_inherited_private_read_does_not_grant_delete(self):
        response = self.client.delete(
            '/api/v1/secrets/user-private?id=%d' % self.user_secret.id,
            headers=self._headers('second'),
        )
        self.assertEqual(404, response.status_code)
        self.assertIsNotNone(db.session.get(ClawSecret, self.user_secret.id))


if __name__ == '__main__':
    unittest.main()
