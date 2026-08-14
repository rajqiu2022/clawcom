import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
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

from app import db  # noqa: E402
from app.api import agent_deployments  # noqa: E402
from app.models import ClawSecret, OpenClawInstance  # noqa: E402
from app.services.deployment_secrets import get_deployment_secret  # noqa: E402


class DeploymentSecretTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
            TESTING=True,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _claw(self, name, role='admin', project_id=None):
        claw = OpenClawInstance(
            name=name,
            safe_name=name.lower().replace(' ', '-'),
            claw_tag=name.lower().replace(' ', '-'),
            owner='owner',
            role=role,
            project_id=project_id,
        )
        db.session.add(claw)
        db.session.flush()
        return claw

    def _secret(self, claw, key, value):
        secret = ClawSecret(owner_claw_id=claw.id, key=key)
        secret.set_value(value)
        db.session.add(secret)
        return secret

    def test_only_global_admin_vault_is_used(self):
        global_admin = self._claw('Global Admin')
        project_admin = self._claw('Project Admin', project_id=7)
        self._secret(project_admin, 'deploy_venus_api_key', 'project-value')
        self._secret(global_admin, 'deploy_venus_api_key', 'global-value')
        db.session.commit()

        self.assertEqual(
            get_deployment_secret(('deploy_venus_api_key',)),
            'global-value',
        )

    def test_key_priority_is_caller_defined(self):
        global_admin = self._claw('Global Admin')
        self._secret(global_admin, 'legacy_key', 'legacy-value')
        self._secret(global_admin, 'canonical_key', 'canonical-value')
        db.session.commit()

        self.assertEqual(
            get_deployment_secret(('canonical_key', 'legacy_key')),
            'canonical-value',
        )

    def test_provider_resolvers_delegate_to_vault(self):
        with patch.object(agent_deployments, 'get_deployment_secret',
                          return_value='vault-value') as lookup:
            self.assertEqual(
                agent_deployments._configured_venus_api_key(),
                'vault-value',
            )
            lookup.assert_called_with((
                'deploy_venus_api_key',
                'hermes_venus_api_key',
                'venus_api_key',
            ))

            self.assertEqual(
                agent_deployments._configured_timiai_api_key('gbt'),
                'vault-value',
            )
            timiai_keys = lookup.call_args.args[0]
            self.assertEqual(timiai_keys[0], 'deploy_timiai_api_key_gbt')
            self.assertIn('deploy_timiai_api_key', timiai_keys)

    def test_deploy_rejects_secret_in_extra_env(self):
        claw = SimpleNamespace(
            id=9,
            name='Worker',
            safe_name='worker',
            owner='alice',
            llm_provider='venus',
            llm_model='venus',
            timiai_project='gbt',
            work_dirs=[],
            wecom_bot_id='',
            owner_wecom_userid='',
            get_wecom_bot_secret_plain=lambda: '',
        )
        defaults = {
            'host': '127.0.0.1',
            'ssh_port': '22',
            'ssh_user': 'root',
            'ssh_password': None,
            'ssh_private_key': 'private-key-from-vault',
            'ssh_key_passphrase': None,
        }
        data = {
            'hermes_install_dir': '/opt/hermes-runtime',
            'hermes_data_dir': '/opt/openclaw-agents/claw-9-worker/data',
            'extra_env': {'TIMIAI_API_KEY': 'request-value'},
        }
        with patch.object(agent_deployments, '_deployment_defaults',
                          return_value=defaults), patch.object(
                              agent_deployments,
                              '_configured_venus_api_key',
                              return_value='venus-from-vault'), patch.object(
                                  agent_deployments,
                                  '_configured_timiai_api_key',
                                  return_value='timiai-from-vault'):
            with self.assertRaisesRegex(ValueError, 'Hub 密钥箱'):
                agent_deployments._parse_deploy_options(
                    data,
                    claw=claw,
                    claw_token='hub-token',
                    hub_url='https://hub.example',
                    actor_name='admin',
                )


if __name__ == '__main__':
    unittest.main()
