import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return _Noop()


for name, attrs in (
    ('flask_cors', {'CORS': _Noop}),
    ('flask_socketio', {
        'SocketIO': _Noop, 'emit': _Noop(),
        'join_room': _Noop(), 'leave_room': _Noop(),
    }),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


from app.api import agent_deployments  # noqa: E402
from app.services.agent_deployer import (  # noqa: E402
    DeployRequest,
    _render_codex_sidecar_unit,
    _render_sidecar_env,
    build_codex_unit_name,
)


class CodexLinuxDeployerTest(unittest.TestCase):
    @staticmethod
    def _claw():
        return SimpleNamespace(
            id=17,
            name='Codex Worker',
            safe_name='codex-worker',
            owner='alice',
            llm_provider='venus',
            llm_model='venus',
            timiai_project='gbt',
            work_dirs=[],
            wecom_bot_id='',
            owner_wecom_userid='',
            get_wecom_bot_secret_plain=lambda: '',
        )

    @staticmethod
    def _defaults():
        return {
            'host': 'linux.example',
            'ssh_port': '22',
            'ssh_user': 'root',
            'ssh_password': None,
            'ssh_private_key': 'vault-private-key',
            'ssh_key_passphrase': None,
        }

    def test_codex_parse_defaults_to_subscription_auth_without_hermes_key(self):
        payload = {
            'agent_type': 'codex',
            'codex_workspace': '/srv/projects/example',
            'codex_requirements_sha256': 'a' * 64,
        }
        with patch.object(agent_deployments, '_deployment_defaults',
                          return_value=self._defaults()), patch.object(
                              agent_deployments, '_configured_venus_api_key',
                              return_value=''), patch.object(
                                  agent_deployments, '_configured_timiai_api_key',
                                  return_value=''):
            req = agent_deployments._parse_deploy_options(
                payload, self._claw(), 'hub-token', 'https://hub.example', 'admin')

        self.assertEqual(req.agent_type, 'codex')
        self.assertEqual(req.codex_auth_mode, 'chatgpt_subscription')
        self.assertEqual(req.codex_home_dir(),
                         '/opt/openclaw-agents/claw-17-codex-worker/data/home/.codex')
        self.assertEqual(req.container_name(), build_codex_unit_name(17))

    def test_pi_is_explicitly_retired(self):
        with self.assertRaisesRegex(ValueError, 'Pi provider 已退役'):
            agent_deployments._parse_deploy_options(
                {'agent_type': 'pi'}, self._claw(), '', '', 'admin')

    def test_codex_unit_and_env_bind_auth_to_service_user_home(self):
        req = DeployRequest(
            openclaw_id=17,
            claw_name='Codex Worker',
            claw_token='hub-token',
            hub_url='https://hub.example',
            host='linux.example',
            ssh_user='root',
            agent_type='codex',
            codex_data_dir='/opt/openclaw-agents/claw-17-codex-worker/data',
            codex_workspace='/srv/projects/example',
            codex_requirements_sha256='b' * 64,
        )
        env = _render_sidecar_env(req)
        unit = _render_codex_sidecar_unit(req)

        self.assertIn('AGENT_TYPE=codex', env)
        self.assertIn('CODEX_AUTH_MODE=chatgpt_subscription', env)
        self.assertIn('CODEX_HOME=/opt/openclaw-agents/claw-17-codex-worker/data/home/.codex', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertIn('User=oclaw_17', unit)
        self.assertIn('WorkingDirectory=/srv/projects/example', unit)
        self.assertIn('ReadOnlyPaths=/opt/codex-runtime /srv/projects/example', unit)


if __name__ == '__main__':
    unittest.main()
