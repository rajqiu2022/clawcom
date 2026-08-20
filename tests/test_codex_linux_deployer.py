import sys
import os
import types
import unittest
import json
import subprocess
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
from claw_hub_mcp import tool_definitions  # noqa: E402
import sidecar_v2 as deployed_sidecar  # noqa: E402


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
        self.assertEqual(req.codex_cli,
                         '/opt/codex-runtime/venv/bin/codex')
        self.assertIn('/srv/projects/example', req.work_dirs)
        self.assertEqual(req.container_name(), build_codex_unit_name(17))

    def test_codex_deployer_uses_bundled_cli_and_cp311_runtime(self):
        source = (Path(__file__).resolve().parents[1] / 'web' / 'app' /
                  'services' / 'agent_deployer.py').read_text(encoding='utf-8')
        self.assertIn("sys.version_info[:2] == (3, 11)", source)
        self.assertIn("import codex_cli_bin", source)
        self.assertIn("venv/bin/codex", source)

    def test_linux_codex_capabilities_accept_sidecar_wecom_turns(self):
        provider = CodexSdkProvider(
            provider_version='test', sdk_factory=lambda: None,
            sandbox_factory=lambda: {
                'read_only': object(), 'workspace_write': object(),
            },
        )
        self.assertIn('wecom', provider.capabilities().task_kinds)
        self.assertIn('repo_write', provider.capabilities().execution_scopes)

    def test_linux_codex_uses_isolated_timeout_runner(self):
        captured = {}

        def runner(command, **kwargs):
            captured.update(command=command, **kwargs)
            return subprocess.CompletedProcess(
                command, 0,
                json.dumps({
                    'ok': True,
                    'thread_id': 'linux-thread',
                    'final_response': 'done',
                    'usage': {},
                }), '',
            )

        provider = CodexSdkProvider(
            provider_version='test',
            process_runner=runner,
            sandbox_factory=lambda: {'read_only': object()},
            python_executable='/venv/bin/python',
            worker_path='/sidecar/codex_sdk_worker.py',
        )
        from provider_runtime import CancellationToken, ProviderInvocation
        result = provider.invoke(ProviderInvocation(
            invocation_id='inv-1', task_kind='message', prompt='hello',
            context={}, session_key='message:1', timeout_seconds=23,
            workspace='/srv/project', execution_scope='repo_read',
            result_schema=None, environment={'CODEX_HOME': '/data/.codex'},
        ), lambda _event: None, CancellationToken())

        self.assertTrue(result.ok)
        self.assertEqual(23, captured['timeout'])
        self.assertEqual({'CODEX_HOME': '/data/.codex'}, captured['env'])

    def test_linux_codex_environment_excludes_hub_and_channel_secrets(self):
        environment = deployed_sidecar._codex_environment({
            'CODEX_HOME': '/data/home/.codex',
            'HOME': '/data/home',
            'PATH': '/usr/bin',
            'CLAW_TOKEN': 'hub-secret',
            'OPENAI_API_KEY': 'api-secret',
            'CODEX_ACCESS_TOKEN': 'enterprise-secret',
            'WECOM_BOT_SECRET': 'wecom-secret',
        })
        self.assertEqual('/data/home/.codex', environment['CODEX_HOME'])
        self.assertEqual('/data/home', environment['HOME'])
        for key in (
            'CLAW_TOKEN', 'OPENAI_API_KEY', 'CODEX_ACCESS_TOKEN',
            'WECOM_BOT_SECRET',
        ):
            self.assertNotIn(key, environment)

    def test_deployer_downloads_the_isolated_codex_worker_asset(self):
        source = (Path(__file__).resolve().parents[1] / 'web' / 'app' /
                  'services' / 'agent_deployer.py').read_text(encoding='utf-8')
        for asset in (
            'codex_sdk_worker.py', 'codex_permissions.py', 'hub_proxy.py',
            'hub_plugin.py', 'claw_hub_mcp.py',
        ):
            self.assertIn(repr(asset), source)

        worker_source = (Path(__file__).resolve().parents[1] /
                         'openclaw-agent' / 'skills' / 'hub-sse-sidecar' /
                         'scripts' / 'codex_sdk_worker.py').read_text(encoding='utf-8')
        provider_source = (Path(__file__).resolve().parents[1] /
                           'openclaw-agent' / 'skills' / 'hub-sse-sidecar' /
                           'scripts' / 'codex_sdk_provider.py').read_text(encoding='utf-8')
        self.assertIn('options["cwd"] = workspace', worker_source)
        self.assertIn('options["cwd"] = invocation.workspace', provider_source)
        self.assertNotIn('options["working_directory"]', worker_source)
        self.assertNotIn('options["working_directory"]', provider_source)

    def test_todo_prompt_hides_token_and_uses_todo_contract(self):
        secret = 'hub-token-must-not-reach-codex'
        captured = {}

        def invoke(prompt, **kwargs):
            captured['prompt'] = prompt
            captured.update(kwargs)
            return True, json.dumps({
                'status': 'completed',
                'result_summary': '只读分析完成',
            }, ensure_ascii=False), ''

        with patch.object(deployed_sidecar, 'CLAW_TOKEN', secret), patch.object(
            deployed_sidecar, 'HUB_URL', 'https://hub.example'
        ), patch.object(deployed_sidecar, 'CLAW_ID', '17'), patch.object(
            deployed_sidecar, 'get_cfg',
            side_effect=lambda _key, default=None: default,
        ), patch.object(
            deployed_sidecar, 'fetch_task_context', return_value={}
        ), patch.object(
            deployed_sidecar, 'call_llm', side_effect=invoke
        ), patch.object(
            deployed_sidecar, '_post_complete', return_value=True
        ) as complete:
            ok = deployed_sidecar.handle_todo({
                'id': 42,
                'title': '检查工程',
                'description': '分析只读结果',
            })

        self.assertTrue(ok)
        self.assertEqual('todo', captured['task_kind'])
        self.assertEqual('todo:42', captured['session_key'])
        self.assertNotIn(secret, captured['prompt'])
        self.assertNotIn('Authorization: Bearer', captured['prompt'])
        self.assertNotIn('/todos/42/complete', captured['prompt'])
        self.assertNotIn('send_message', captured['prompt'])
        complete.assert_called_once_with(42, '只读分析完成')

    def test_todo_remains_incomplete_when_sidecar_callback_fails(self):
        with patch.object(
            deployed_sidecar, 'get_cfg',
            side_effect=lambda _key, default=None: default,
        ), patch.object(
            deployed_sidecar, 'fetch_task_context', return_value={}
        ), patch.object(
            deployed_sidecar, 'call_llm',
            return_value=(True, json.dumps({
                'status': 'completed',
                'result_summary': 'result',
            }), ''),
        ) as invoke, patch.object(
            deployed_sidecar, '_post_complete', return_value=False
        ) as complete:
            ok = deployed_sidecar.handle_todo({'id': 43, 'title': '检查'})

        self.assertFalse(ok)
        self.assertEqual('todo', invoke.call_args.kwargs['task_kind'])
        self.assertEqual('todo:43', invoke.call_args.kwargs['session_key'])
        complete.assert_called_once_with(43, 'result')

    def test_todo_blocked_result_never_completes_in_hub(self):
        with patch.object(
            deployed_sidecar, 'get_cfg',
            side_effect=lambda _key, default=None: default,
        ), patch.object(
            deployed_sidecar, 'fetch_task_context', return_value={}
        ), patch.object(
            deployed_sidecar, 'call_llm',
            return_value=(True, json.dumps({
                'status': 'blocked',
                'result_summary': '需要写权限',
            }, ensure_ascii=False), ''),
        ), patch.object(
            deployed_sidecar, '_post_complete', return_value=True
        ) as complete:
            ok = deployed_sidecar.handle_todo({'id': 44, 'title': '修改配置'})

        self.assertFalse(ok)
        complete.assert_not_called()

    def test_codex_choke_point_redacts_echoed_local_hub_token(self):
        captured = {}

        class Provider:
            def invoke(self, invocation, _on_event, _cancel):
                captured['invocation'] = invocation
                return SimpleNamespace(
                    ok=True, final_response='ok', error=None,
                )

        secret = 'local-hub-token-never-for-codex'
        with patch.object(
            deployed_sidecar, 'CLAW_TOKEN', secret
        ), patch.object(
            deployed_sidecar, '_codex_provider', Provider()
        ), patch.dict(
            os.environ,
            {'CODEX_HOME': '/data/home/.codex', 'HOME': '/data/home'},
            clear=True,
        ):
            ok, response, error = deployed_sidecar._call_codex_provider(
                f'untrusted content echoed {secret}',
                30,
                'todo',
                'todo:44',
            )

        self.assertEqual((True, 'ok', ''), (ok, response, error))
        invocation = captured['invocation']
        self.assertNotIn(secret, invocation.prompt)
        self.assertIn('<redacted>', invocation.prompt)
        self.assertNotIn('CLAW_TOKEN', invocation.environment)
        self.assertEqual('repo_write', invocation.execution_scope)

    def test_sidecar_complete_does_not_falsely_claim_wecom_notification(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def open_request(request, timeout):
            captured['request'] = request
            captured['timeout'] = timeout
            return Response()

        with patch.object(
            deployed_sidecar, 'HUB_URL', 'https://hub.example'
        ), patch.object(
            deployed_sidecar, 'CLAW_ID', '17'
        ), patch.object(
            deployed_sidecar, 'CLAW_TOKEN', 'sidecar-only-token'
        ), patch.object(
            deployed_sidecar.urllib.request, 'urlopen', side_effect=open_request
        ):
            ok = deployed_sidecar._post_complete(45, '已完成')

        self.assertTrue(ok)
        payload = json.loads(captured['request'].data.decode('utf-8'))
        self.assertEqual({'result_summary': '已完成'}, payload)
        self.assertEqual(30, captured['timeout'])

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
        self.assertIn('PROVIDER_ALLOWED_DIRS=', env)
        self.assertIn(
            'SIDECAR_INSTANCE_DIR=/opt/openclaw-agents/claw-17-codex-worker/data',
            env,
        )
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertIn('User=oclaw_17', unit)
        self.assertIn('WorkingDirectory=/srv/projects/example', unit)
        self.assertIn(
            'ReadWritePaths=/opt/openclaw-agents/claw-17-codex-worker/data '
            '/opt/agent_share /srv/projects/example', unit)
        self.assertIn('ReadOnlyPaths=/opt/codex-runtime', unit)

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

    def test_mcp_exposes_hub_and_bound_owner_reply_tools(self):
        definitions = tool_definitions((12, 25), wecom_reply_enabled=True)
        self.assertEqual(
            ['hub_api', 'wecom_reply'],
            [item['name'] for item in definitions],
        )
        self.assertNotIn('target_userid', json.dumps(definitions))

    def test_cycle_repair_runs_before_verified_child_flow(self):
        config = {
            'session_key': 'orchestrator:project',
            'allowed_next_flows': (25,),
            'max_retries': 2,
            'review_success': True,
        }
        item = {
            'run_id': 198, 'cycle_id': 'cycle-198',
            'session_key': 'orchestrator:project',
            'parent_run_id': None, 'retry_index': 0,
        }
        calls = []

        def invoke(prompt, **kwargs):
            calls.append((prompt, kwargs))
            if prompt.startswith('[CODEX_CYCLE_REVIEW]'):
                return True, json.dumps({
                    'classification': 'RECOVERABLE',
                    'action': 'repair',
                    'summary': '可恢复',
                    'repair_operation': 'workspace_hygiene',
                    'next_flow_id': 25,
                    'human_required': False,
                }, ensure_ascii=False), ''
            return True, json.dumps({
                'status': 'repaired', 'summary': '已修复',
                'changes': ['moved generated files'],
                'validation': ['git status clean'],
                'restart_allowed': True,
            }, ensure_ascii=False), ''

        def hub_http(method, path, body=None, **_kwargs):
            if method == 'POST':
                self.assertEqual(25, body['definition_id'])
                return 201, {'id': 199, 'status': 'pending'}
            return 200, {
                'id': 199, 'definition_id': 25, 'status': 'pending',
            }

        with patch.object(
            deployed_sidecar, 'call_llm', side_effect=invoke
        ), patch.object(
            deployed_sidecar, 'http', side_effect=hub_http
        ), patch.object(
            deployed_sidecar, '_register_cycle_run', return_value=True
        ) as register, patch.object(
            deployed_sidecar, '_complete_cycle_run'
        ) as complete, patch.object(
            deployed_sidecar, '_codex_wecom_reply', return_value={'queued': True}
        ):
            deployed_sidecar._process_cycle_terminal(
                item, {'id': 198, 'status': 'blocked'}, config
            )

        self.assertEqual(2, len(calls))
        self.assertEqual(
            ['orchestrator:project', 'orchestrator:project'],
            [call[1]['session_key'] for call in calls],
        )
        register.assert_called_once()
        complete.assert_called_once_with(198, 'blocked')

    def test_owner_can_approve_permission_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            deployed_sidecar, '_sidecar_data_dir', return_value=tmp
        ), patch.object(
            deployed_sidecar, 'get_cfg',
            side_effect=lambda key, default=None: (
                'alice' if key == 'owner_wecom_userid' else default
            ),
        ):
            with deployed_sidecar._codex_state_connection() as db:
                deployed_sidecar._init_codex_permission_tables(db)
                db.execute(
                    'INSERT INTO codex_permission_requests('
                    'request_id,fingerprint,request_json,session_key,state,'
                    'expires_at,created_at) VALUES(?,?,?,?,?,?,?)',
                    (
                        'cp-12345678', 'fingerprint', '{}', 'session',
                        'pending', time.time() + 60, time.time(),
                    ),
                )
            reply = deployed_sidecar._handle_codex_permission_command({
                'sender_id': 'alice',
                'text': '/codex-allow-once cp-12345678',
            })
            with deployed_sidecar._codex_state_connection() as db:
                state = db.execute(
                    'SELECT state FROM codex_permission_requests '
                    'WHERE request_id=?', ('cp-12345678',)
                ).fetchone()[0]
        self.assertIn('一次授权', reply)
        self.assertEqual('approved_once', state)

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
