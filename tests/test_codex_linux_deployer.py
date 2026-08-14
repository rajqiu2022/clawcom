import sys
import types
import unittest
import json
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))
_SIDECAR = (Path(__file__).resolve().parents[1] / 'openclaw-agent' /
            'skills' / 'hub-sse-sidecar' / 'scripts')
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))


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
    _render_codex_wecom_credentials,
    _render_codex_sidecar_unit,
    _render_sidecar_unit,
    _render_sidecar_env,
    build_codex_unit_name,
)
from wecom_channel import WeComChannel  # noqa: E402
from codex_sdk_provider import CodexSdkProvider  # noqa: E402


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

    def test_linux_codex_capabilities_accept_sidecar_wecom_turns(self):
        provider = CodexSdkProvider(
            provider_version='test', sdk_factory=lambda: None,
            sandbox_factory=lambda: {'read_only': object()},
        )
        self.assertIn('wecom', provider.capabilities().task_kinds)

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

    def test_hermes_sidecar_unit_renderer_still_returns_a_unit(self):
        req = DeployRequest(
            openclaw_id=17, claw_name='Hermes Worker', claw_token='hub-token',
            hub_url='https://hub.example', host='linux.example', ssh_user='root',
            agent_type='hermes', hermes_install_dir='/opt/hermes-agent',
            hermes_data_dir='/opt/openclaw-agents/claw-17-hermes/data',
        )
        unit = _render_sidecar_unit(req)
        self.assertIsInstance(unit, str)
        self.assertIn('Description=OpenClaw Hub SSE Sidecar v2', unit)
        self.assertIn('WorkingDirectory=/opt/openclaw-agents/claw-17-hermes/data', unit)

    def test_wecom_secret_is_file_only_not_provider_environment(self):
        req = DeployRequest(
            openclaw_id=17, claw_name='Codex Worker', claw_token='hub-token',
            hub_url='https://hub.example', host='linux.example', ssh_user='root',
            agent_type='codex',
            codex_data_dir='/opt/openclaw-agents/claw-17-codex-worker/data',
            codex_workspace='/srv/projects/example',
            codex_requirements_sha256='b' * 64,
            wecom_bot_id='bot-id', wecom_bot_secret='top-secret',
            owner_wecom_userid='alice',
        )
        env = _render_sidecar_env(req)
        credentials = json.loads(_render_codex_wecom_credentials(req))
        self.assertIn('WECOM_ENABLED=true', env)
        self.assertIn('WECOM_NODE_BIN=', env)
        self.assertNotIn('WECOM_CREDENTIALS_PATH', env)
        self.assertNotIn('top-secret', env)
        self.assertEqual(credentials['secret'], 'top-secret')
        self.assertEqual(credentials['allowed_user_ids'], ['alice'])

    def test_wecom_channel_deduplicates_before_codex_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials = root / 'credentials.json'
            credentials.write_text(json.dumps({
                'schema': 2,
                'bot_id': 'bot-id',
                'secret': 'top-secret',
                'owner_user_id': 'alice',
                'allowed_user_ids': ['alice'],
                'allowed_chat_ids': [],
            }), encoding='utf-8')
            calls = []
            channel = WeComChannel(
                credentials_path=str(credentials),
                database_path=str(root / 'state.db'),
                node_path='/usr/bin/node', bridge_script='/tmp/bridge.mjs',
                sdk_root='/tmp/sdk',
                invoke=lambda prompt, key: (
                    calls.append((prompt, key)) or (True, 'Codex 回复', '')),
                logger=lambda _message: None,
            )

            class FakeBridge:
                def __init__(self):
                    self.replies = []

                def reply(self, event_id, text, stream=False):
                    self.replies.append((event_id, text, stream))

            channel.bridge = FakeBridge()
            event = {
                'event_id': 'event-1', 'sender_id': 'alice', 'text': '你好',
                'conversation': {'kind': 'user', 'id': 'alice'},
            }
            channel._on_event(event)
            channel._on_event(event)

            self.assertEqual(len(calls), 1)
            self.assertNotIn('alice', calls[0][0])
            self.assertEqual(calls[0][1].split(':', 1)[0], 'wecom')
            self.assertEqual(channel.bridge.replies[-1],
                             ('event-1', 'Codex 回复', False))

    def test_wecom_channel_reclaims_expired_processing_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credentials = root / 'credentials.json'
            credentials.write_text(json.dumps({
                'schema': 2, 'bot_id': 'bot-id', 'secret': 'top-secret',
                'owner_user_id': 'alice', 'allowed_user_ids': ['alice'],
                'allowed_chat_ids': [],
            }), encoding='utf-8')
            channel = WeComChannel(
                credentials_path=str(credentials),
                database_path=str(root / 'state.db'), node_path='/usr/bin/node',
                bridge_script='/tmp/bridge.mjs', sdk_root='/tmp/sdk',
                invoke=lambda _prompt, _key: (True, 'ok', ''),
                logger=lambda _message: None,
            )
            with closing(channel._connect()) as db:
                with db:
                    db.execute(
                        'INSERT INTO wecom_events(event_id,status,created_at) VALUES(?,?,?)',
                        ('expired', 'processing', time.time() - 7200),
                    )
            claimed, status, final = channel._claim('expired')
            self.assertTrue(claimed)
            self.assertEqual((status, final), ('processing', ''))


if __name__ == '__main__':
    unittest.main()
