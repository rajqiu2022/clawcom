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
    ClawSidecarConfig,
    OpenClawInstance,
    User,
    hash_token,
)
from app.services.worker_runtime import (  # noqa: E402
    runtime_from_query,
    validate_worker_runtime,
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

    def test_openclaw_is_retired_as_worker_provider(self):
        with self.assertRaisesRegex(ValueError, 'hermes / codex'):
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


if __name__ == '__main__':
    unittest.main()
