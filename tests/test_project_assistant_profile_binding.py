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
    AgentPostAssignment,
    AgentProfile,
    ClawMessage,
    ClawSidecarConfig,
    OpenClawInstance,
    OpenClawSkill,
    Project,
    Skill,
)
from bind_project_assistant_profile import apply  # noqa: E402


class ProjectAssistantProfileBindingTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='project-assistant-binding-test',
            TESTING=True,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        db.session.add(Project(id=6, name='RacingGO'))
        self.claw = OpenClawInstance(
            id=61,
            name='小数',
            safe_name='xiaoshu',
            claw_tag='claw-61',
            owner='owner',
            project_id=6,
        )
        db.session.add(self.claw)
        db.session.add(ClawSidecarConfig(
            claw_id=61,
            agent_type='codebuddy',
            config_version=3,
            config_owner='hub',
        ))
        valid = Skill(
            name='valid-skill',
            display_name='Valid',
            review_status='approved',
            visibility='public',
        )
        deleted = Skill(
            name='deleted-skill',
            display_name='Deleted',
            review_status='approved',
            visibility='public',
            is_deleted=True,
        )
        private = Skill(
            name='private-skill',
            display_name='Private',
            review_status='approved',
            visibility='private',
            owner_claw_id=99,
        )
        db.session.add_all([valid, deleted, private])
        db.session.flush()
        db.session.add_all([
            OpenClawSkill(openclaw_id=61, skill_id=valid.id, enabled=True),
            OpenClawSkill(openclaw_id=61, skill_id=deleted.id, enabled=True),
            OpenClawSkill(openclaw_id=61, skill_id=private.id, enabled=True),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_binding_is_versioned_scoped_and_idempotent(self):
        first = apply(61, 6, 'test')
        second = apply(61, 6, 'test')

        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(4, first['config_version'])
        self.assertEqual(4, second['config_version'])
        self.assertEqual(
            {'deleted-skill', 'private-skill'},
            {item['name'] for item in first['disabled_skills']},
        )
        self.assertEqual([], second['disabled_skills'])
        profile = AgentProfile.query.filter_by(
            profile_key='project_assistant').one()
        assignment = AgentPostAssignment.query.filter_by(claw_id=61).one()
        self.assertEqual(profile.version, assignment.profile_version)
        self.assertTrue(assignment.is_primary)
        self.assertEqual(1, ClawMessage.query.filter_by(
            claw_id=61, msg_type='sync_config').count())
        links = {
            link.skill.name: bool(link.enabled)
            for link in OpenClawSkill.query.filter_by(openclaw_id=61).all()
        }
        self.assertEqual({
            'valid-skill': True,
            'deleted-skill': False,
            'private-skill': False,
        }, links)


if __name__ == '__main__':
    unittest.main()
