import sys
import types
import unittest
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
_ROOT = _WEB.parent
for value in (str(_WEB), str(_ROOT / 'ops')):
    if value not in sys.path:
        sys.path.insert(0, value)


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
from app.models import (  # noqa: E402
    AgentPostAssignment, AgentProfile, AuditLog, ClawMessage,
    ClawSidecarConfig, OpenClawInstance, Project,
)
from bind_readonly_source_profile import apply  # noqa: E402


class ReadonlySourceProfileBindingTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='readonly-source-profile-test',
            TESTING=True,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add(Project(id=6, name='RacingGO'))
        db.session.add(OpenClawInstance(
            id=62, name='小驴-代码分析专员', safe_name='xiaolv',
            claw_tag='claw-62', owner='rajqiu', project_id=6))
        db.session.add(ClawSidecarConfig(
            claw_id=62, agent_type='codebuddy', config_version=2,
            config_owner='hub', system_context_policy_json={
                'remote_source_ids': [
                    'racinggo_unity', 'racinggo_server',
                ],
            }))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_binding_is_read_only_scoped_and_idempotent(self):
        first = apply(62, 6, 'test')
        second = apply(62, 6, 'test')

        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(first['remote_source_ids'], second['remote_source_ids'])
        self.assertEqual(3, first['config_version'])
        profile = AgentProfile.query.filter_by(
            profile_key='readonly_source_code_analyst').one()
        self.assertEqual('read_only', profile.contract_json['source_access'])
        self.assertFalse(profile.contract_json['external_mutations_allowed'])
        assignment = AgentPostAssignment.query.filter_by(claw_id=62).one()
        self.assertTrue(assignment.is_primary)
        self.assertEqual(profile.version, assignment.profile_version)
        self.assertEqual(1, ClawMessage.query.filter_by(
            claw_id=62, msg_type='sync_config').count())
        self.assertEqual(1, AuditLog.query.filter_by(
            resource_type='agent_profile').count())

    def test_missing_remote_source_scope_fails_closed(self):
        config = db.session.get(ClawSidecarConfig, 62)
        config.system_context_policy_json = {}
        db.session.commit()
        with self.assertRaisesRegex(RuntimeError, 'remote source scope'):
            apply(62, 6, 'test')


if __name__ == '__main__':
    unittest.main()
