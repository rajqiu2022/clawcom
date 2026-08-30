import sys
import types
import unittest
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
from app.models import (  # noqa: E402
    AgentEvalCase, AgentEvalDataset, AgentPost, AgentProfile, Project, Skill,
    SkillFile,
)
from app.seed import (  # noqa: E402
    seed_game_test_agent_team, seed_game_test_eval_data,
)


class GameTestAgentTeamSeedTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='agent-team-seed-test', TESTING=True,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_seed_creates_skill_packs_profiles_posts_and_is_idempotent(self):
        first = seed_game_test_agent_team()
        second = seed_game_test_agent_team()
        self.assertEqual(
            {'skills': 4, 'profiles': 3, 'posts': 3}, first)
        self.assertEqual(
            {'skills': 0, 'profiles': 0, 'posts': 0}, second)
        self.assertEqual(4, Skill.query.count())
        self.assertEqual(3, AgentProfile.query.count())
        self.assertEqual(3, AgentPost.query.count())
        self.assertEqual(3, SkillFile.query.count())
        requirement = AgentProfile.query.filter_by(
            profile_key='mission_requirement_analyst').one()
        self.assertEqual(
            ['agent-operating-protocol', 'mission-requirement-analysis'],
            requirement.required_skills_json)
        self.assertEqual(
            'requirement_analysis', requirement.contract_json['artifact_type'])

    def test_seed_preserves_same_version_profile_and_human_edited_skill(self):
        seed_game_test_agent_team()
        profile = AgentProfile.query.filter_by(
            profile_key='mission_requirement_analyst').one()
        profile.system_prompt = '管理员同版本定制'
        skill = Skill.query.filter_by(name='mission-requirement-analysis').one()
        skill.template_content = '管理员定制 Skill'
        skill.last_modified_source = 'web'
        db.session.commit()

        seed_game_test_agent_team()
        self.assertEqual(
            '管理员同版本定制',
            AgentProfile.query.get(profile.id).system_prompt)
        self.assertEqual(
            '管理员定制 Skill', Skill.query.get(skill.id).template_content)

    def test_seed_never_overwrites_legacy_operating_protocol_without_provenance(self):
        legacy = Skill(
            name='agent-operating-protocol', display_name='旧运转协议',
            description='历史人工维护', category='standard', scope='global',
            template_content='历史内容', created_by='system',
            review_status='approved', last_modified_source=None)
        db.session.add(legacy)
        db.session.commit()

        result = seed_game_test_agent_team()
        self.assertEqual(3, result['skills'])
        self.assertEqual(
            '历史内容', db.session.get(Skill, legacy.id).template_content)

    def test_eval_seed_creates_six_draft_datasets_and_twenty_four_cases(self):
        db.session.add(Project(name='QQ飞车'))
        db.session.commit()
        first = seed_game_test_eval_data()
        second = seed_game_test_eval_data()
        self.assertEqual({'datasets': 6, 'cases': 24}, first)
        self.assertEqual({'datasets': 0, 'cases': 0}, second)
        self.assertEqual(6, AgentEvalDataset.query.count())
        self.assertEqual(24, AgentEvalCase.query.count())
        for dataset in AgentEvalDataset.query.all():
            expected_cases = 5 if dataset.split == 'golden' else 3
            self.assertEqual(expected_cases, len(dataset.cases))
            self.assertEqual('draft', dataset.status)
            self.assertEqual('pending', dataset.review_status)
            self.assertEqual(
                2, dataset.review_policy_json['required_reviewers'])

    def test_eval_seed_rejects_dataset_key_owned_by_manual_data(self):
        project = Project(name='QQ飞车')
        db.session.add(project)
        db.session.flush()
        db.session.add(AgentEvalDataset(
            project_id=project.id,
            dataset_key='mission_requirement_analyst_golden',
            role_key='requirement_analyst', version=1, split='golden',
            status='draft', rubric_version=1, dataset_sha256='',
            review_policy_json={}, review_status='not_required',
            review_records_json=[], idempotency_key='manual-data',
            request_sha256='sha256:' + ('a' * 64),
            created_by_type='user', created_by_id=9,
            created_by_name='manual'))
        db.session.commit()
        with self.assertRaisesRegex(ValueError, 'owned by non-seed data'):
            seed_game_test_eval_data()


if __name__ == '__main__':
    unittest.main()
