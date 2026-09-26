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

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.api.agent_client import agent_bp  # noqa: E402
from app.models import (  # noqa: E402
    AgentDeployment,
    ClawSidecarConfig,
    OpenClawInstance,
    User,
    hash_token,
)
from app.services.worker_runtime import (  # noqa: E402
    runtime_from_query,
    validate_worker_runtime,
    workflow_runtime_compatibility,
)


class WorkerRuntimeValidationTest(unittest.TestCase):
    def test_codex_subscription_defaults_and_aliases(self):
        runtime = validate_worker_runtime({
            'kind': 'claw_worker',
            'provider': 'codex',
            'platform': 'linux-x86_64',
            'auth_mode': 'chatgpt_subscription',
        })
        self.assertEqual(runtime['auth_mode'], 'subscription')
        self.assertEqual(runtime['platform'], 'linux')

    def test_codex_agent_direct_timiai_bridge_is_supported(self):
        runtime = validate_worker_runtime({
            'kind': 'claw_worker',
            'provider': 'codex',
            'runtime_mode': 'agent_direct',
            'platform': 'windows',
            'auth_mode': 'timiai_bridge',
            'llm_provider': 'timiai',
            'source_commit': '76500ef',
            'source': 'operator',
        })
        self.assertEqual(runtime['runtime_mode'], 'agent_direct')
        self.assertEqual(runtime['auth_mode'], 'timiai_bridge')
        self.assertEqual(runtime['provider'], 'codex')

    def test_codex_macos_platform_is_supported(self):
        for reported in ('macos', 'macos-x86_64', 'darwin'):
            with self.subTest(reported=reported):
                runtime = validate_worker_runtime({
                    'kind': 'claw_worker',
                    'provider': 'codex',
                    'runtime_mode': 'agent_direct',
                    'platform': reported,
                    'auth_mode': 'subscription',
                    'source': 'worker',
                })
                self.assertEqual(runtime['platform'], 'macos')

    def test_codebuddy_agent_direct_runtime_is_supported_without_auth_mode(self):
        runtime = validate_worker_runtime({
            'kind': 'claw_worker',
            'provider': 'codebuddy',
            'runtime_mode': 'agent_direct',
            'platform': 'windows-x86_64',
            'provider_version': 'codebuddy-cli',
            'source_commit': '76500ef',
            'source': 'worker',
        })
        self.assertEqual(runtime['provider'], 'codebuddy')
        self.assertEqual(runtime['runtime_mode'], 'agent_direct')
        self.assertEqual(runtime['platform'], 'windows')
        self.assertEqual(runtime['auth_mode'], '')

    def test_openclaw_is_retired_as_worker_provider(self):
        with self.assertRaisesRegex(ValueError, 'hermes / codex / codebuddy'):
            validate_worker_runtime({
                'kind': 'claw_worker',
                'provider': 'openclaw',
            })

    def test_unknown_or_secret_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'api_key'):
            validate_worker_runtime({
                'kind': 'claw_worker',
                'provider': 'hermes',
                'api_key': 'must-not-enter-runtime-contract',
            })

    def test_legacy_agent_type_does_not_claim_worker_runtime(self):
        self.assertIsNone(runtime_from_query({'agent_type': 'hermes'}))

    def test_flow25_controlled_runner_requires_agent_direct(self):
        definition = {'steps': [{
            'id': 'runtime_bootstrap',
            'type': 'worker_task',
            'runner': 'deepflow.racinggo.flow25_worker_v1',
        }]}
        legacy = workflow_runtime_compatibility(definition, {
            'schema': 1, 'kind': 'claw_worker', 'provider': 'codebuddy',
            'runtime_mode': 'legacy_split', 'platform': 'windows',
            'source': 'worker',
        })
        self.assertFalse(legacy['compatible'])
        self.assertIn('runtime_mode_mismatch', legacy['reasons'])
        direct = workflow_runtime_compatibility(definition, {
            'schema': 1, 'kind': 'claw_worker', 'provider': 'codebuddy',
            'runtime_mode': 'agent_direct', 'platform': 'windows',
            'source': 'worker',
        })
        self.assertTrue(direct['compatible'])

    def test_nested_deepflow_runner_contract_requires_agent_direct(self):
        definition = {'steps': [{
            'id': 'runtime_bootstrap',
            'type': 'agent_task',
            'runner': 'agent.skill.racinggo-flow12-v10',
            'inputs': {
                'deepflow_runner_tool': True,
                'agent_direct_contract': {
                    'runtime_mode': 'agent_direct',
                    'operation': 'runtime_bootstrap',
                },
                'runner_tool': {
                    'tool': 'deepflow_runner',
                    'transport': 'mcp',
                    'runtime_mode': 'agent_direct',
                },
            },
        }]}
        legacy = workflow_runtime_compatibility(definition, {
            'schema': 1, 'kind': 'claw_worker', 'provider': 'codebuddy',
            'runtime_mode': 'legacy_split', 'platform': 'windows',
            'source': 'worker',
        })
        self.assertFalse(legacy['compatible'])
        self.assertEqual(
            legacy['requirement']['source'], 'deepflow_controlled_runner')
        self.assertIn('runtime_mode_mismatch', legacy['reasons'])


class WorkerRuntimeApiTest(unittest.TestCase):
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
        self.app.register_blueprint(agent_bp)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.admin = User(username='runtime-admin', role='super_admin')
        self.admin.set_password('secret')
        self.token = 'oc_tk_runtime_contract_test'
        self.claw = OpenClawInstance(
            name='小窗',
            claw_tag='claw-runtime-test',
            owner='runtime-admin',
            api_token_hash=hash_token(self.token),
            llm_provider='venus',
            llm_model='venus',
        )
        db.session.add_all([self.admin, self.claw])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_worker_owned_hermes_runtime_omits_hub_llm_apply(self):
        configured = self.client.put(
            f'/api/v1/openclaws/{self.claw.id}/worker-runtime',
            json={
                'config_owner': 'worker',
                'runtime': {
                    'kind': 'claw_worker',
                    'provider': 'hermes',
                    'runtime_mode': 'legacy_split',
                    'platform': 'windows',
                    'llm_provider': 'timiai',
                    'source': 'operator',
                },
            },
        )
        self.assertEqual(configured.status_code, 200,
                         configured.get_data(as_text=True))
        self.assertTrue(configured.get_json()['has_worker_runtime'])

        sidecar = self.client.get(
            f'/api/openclaws/{self.claw.id}/sidecar-config',
            headers={'Authorization': f'Bearer {self.token}'},
        )
        self.assertEqual(sidecar.status_code, 200, sidecar.get_data(as_text=True))
        payload = sidecar.get_json()
        self.assertEqual(payload['agent_type'], 'hermes')
        self.assertEqual(payload['runtime_kind'], 'claw_worker')
        self.assertEqual(payload['runtime_provider'], 'hermes')
        self.assertEqual(payload['config_owner'], 'worker')
        self.assertNotIn('llm_apply', payload)

        stored = db.session.get(ClawSidecarConfig, self.claw.id)
        self.assertEqual(stored.agent_type, 'hermes')
        self.assertEqual(stored.config_owner, 'worker')

    def test_hub_owned_codex_runtime_keeps_subscription_contract(self):
        configured = self.client.put(
            f'/api/v1/openclaws/{self.claw.id}/worker-runtime',
            json={
                'config_owner': 'hub',
                'runtime': {
                    'kind': 'claw_worker',
                    'provider': 'codex',
                    'runtime_mode': 'legacy_split',
                    'platform': 'linux',
                    'auth_mode': 'subscription',
                    'llm_provider': 'openai',
                    'source': 'operator',
                },
            },
        )
        self.assertEqual(configured.status_code, 200,
                         configured.get_data(as_text=True))

        sidecar = self.client.get(
            f'/api/openclaws/{self.claw.id}/sidecar-config',
            headers={'Authorization': f'Bearer {self.token}'},
        )
        payload = sidecar.get_json()
        self.assertEqual(sidecar.status_code, 200, sidecar.get_data(as_text=True))
        self.assertEqual(payload['agent_type'], 'codex')
        self.assertEqual(payload['llm_provider'], 'openai')
        self.assertEqual(payload['worker_runtime']['auth_mode'], 'subscription')
        self.assertNotIn('llm_apply', payload)

    def test_worker_owned_codebuddy_runtime_is_trusted_by_sidecar_config(self):
        configured = self.client.put(
            f'/api/v1/openclaws/{self.claw.id}/worker-runtime',
            json={
                'config_owner': 'worker',
                'runtime': {
                    'kind': 'claw_worker',
                    'provider': 'codebuddy',
                    'runtime_mode': 'agent_direct',
                    'platform': 'windows',
                    'provider_version': 'codebuddy-cli',
                    'source': 'worker',
                },
            },
        )
        self.assertEqual(configured.status_code, 200,
                         configured.get_data(as_text=True))
        self.assertTrue(configured.get_json()['has_worker_runtime'])
        self.assertEqual(configured.get_json()['runtime_provider'], 'codebuddy')

        sidecar = self.client.get(
            f'/api/openclaws/{self.claw.id}/sidecar-config',
            headers={'Authorization': f'Bearer {self.token}'},
        )
        self.assertEqual(sidecar.status_code, 200, sidecar.get_data(as_text=True))
        payload = sidecar.get_json()
        self.assertEqual(payload['agent_type'], 'codebuddy')
        self.assertEqual(payload['runtime_kind'], 'claw_worker')
        self.assertEqual(payload['runtime_provider'], 'codebuddy')
        self.assertEqual(payload['worker_runtime']['auth_mode'], '')
        self.assertNotIn('llm_apply', payload)

    def test_unmanaged_windows_deployment_does_not_lock_worker_card(self):
        configured = self.client.put(
            f'/api/v1/openclaws/{self.claw.id}/worker-runtime',
            json={
                'config_owner': 'worker',
                'runtime': {
                    'kind': 'claw_worker',
                    'provider': 'codebuddy',
                    'runtime_mode': 'legacy_split',
                    'platform': 'windows',
                    'llm_provider': 'codebuddy',
                    'source_commit': '81af7995',
                    'source': 'operator',
                },
            },
        )
        self.assertEqual(configured.status_code, 200,
                         configured.get_data(as_text=True))
        db.session.add(AgentDeployment(
            openclaw_id=self.claw.id,
            agent_type='codex',
            deploy_method='windows',
            status='pending',
            host='10.30.131.48',
        ))
        db.session.commit()

        listed = self.client.get('/api/v1/openclaws')
        self.assertEqual(listed.status_code, 200,
                         listed.get_data(as_text=True))
        row = next(item for item in listed.get_json()
                   if item['id'] == self.claw.id)
        self.assertTrue(row['has_worker_runtime'])
        self.assertEqual(row['runtime_provider'], 'codebuddy')
        self.assertFalse(row['agent_deployment_managed'])
        self.assertEqual(row['agent_deployment_status'], '')
        self.assertIsNone(row['agent_deployment_id'])

    def test_systemd_deployment_still_locks_card_while_pending(self):
        db.session.add(AgentDeployment(
            openclaw_id=self.claw.id,
            agent_type='codex',
            deploy_method='systemd',
            status='pending',
            host='managed.example',
        ))
        db.session.commit()

        listed = self.client.get('/api/v1/openclaws')
        self.assertEqual(listed.status_code, 200,
                         listed.get_data(as_text=True))
        row = next(item for item in listed.get_json()
                   if item['id'] == self.claw.id)
        self.assertTrue(row['agent_deployment_managed'])
        self.assertEqual(row['agent_deployment_status'], 'pending')
        self.assertIsNotNone(row['agent_deployment_id'])


if __name__ == '__main__':
    unittest.main()
