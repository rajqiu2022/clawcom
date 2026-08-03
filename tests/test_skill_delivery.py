import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / 'web' / 'app' / 'services' / 'skill_delivery.py'
)
_SPEC = importlib.util.spec_from_file_location('skill_delivery', _MODULE_PATH)
skill_delivery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(skill_delivery)


def _dt(day):
    return datetime(2026, 8, day, 10, 30, 0)


def _file(filename, content, day=1):
    return SimpleNamespace(
        filename=filename,
        content=content,
        updated_at=_dt(day),
    )


def _skill(template_content='', files=None, day=1):
    return SimpleNamespace(
        template_content=template_content,
        updated_at=_dt(day),
        file_entries=list(files or []),
    )


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


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))
_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.models import (AgentPost, AgentPostAssignment, AgentProfile,  # noqa: E402
                        AgentTask, OpenClawInstance, OpenClawSkill,
                        Project, Skill, SkillFile)


class SkillBundleTest(unittest.TestCase):
    def test_template_content_becomes_skill_md_when_file_row_missing(self):
        skill = _skill(template_content='# Demo')

        files = skill_delivery.normalized_skill_files(skill)

        self.assertEqual([item['path'] for item in files], ['SKILL.md'])
        self.assertEqual(files[0]['content'], '# Demo')
        self.assertEqual(files[0]['size'], len('# Demo'.encode('utf-8')))

    def test_existing_skill_md_wins_over_template_content(self):
        skill = _skill(
            template_content='# Old',
            files=[_file('SKILL.md', '# Canonical')],
        )

        files = skill_delivery.normalized_skill_files(skill)

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['content'], '# Canonical')

    def test_bundle_hash_is_independent_of_file_row_order(self):
        first = _skill(files=[
            _file('references/b.md', 'B'),
            _file('SKILL.md', 'A'),
        ])
        second = _skill(files=[
            _file('SKILL.md', 'A'),
            _file('references/b.md', 'B'),
        ])

        a = skill_delivery.skill_bundle_descriptor(first)
        b = skill_delivery.skill_bundle_descriptor(second)

        self.assertEqual(a['sha256'], b['sha256'])
        self.assertEqual(
            [item['path'] for item in a['files']],
            ['SKILL.md', 'references/b.md'],
        )

    def test_content_change_updates_file_and_bundle_hash(self):
        first = skill_delivery.skill_bundle_descriptor(
            _skill(files=[_file('SKILL.md', 'A')]))
        second = skill_delivery.skill_bundle_descriptor(
            _skill(files=[_file('SKILL.md', 'B')]))

        self.assertNotEqual(
            first['files'][0]['sha256'],
            second['files'][0]['sha256'],
        )
        self.assertNotEqual(first['sha256'], second['sha256'])

    def test_rejects_parent_directory_path(self):
        with self.assertRaises(ValueError):
            skill_delivery.normalized_skill_files(
                _skill(files=[_file('../secret.txt', 'secret')]))

    def test_rejects_absolute_and_drive_paths(self):
        for filename in ('/secret.txt', r'C:\secret.txt'):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    skill_delivery.normalized_skill_files(
                        _skill(files=[_file(filename, 'secret')]))

    def test_rejects_duplicate_normalized_paths(self):
        with self.assertRaises(ValueError):
            skill_delivery.normalized_skill_files(_skill(files=[
                _file('references//same.md', 'A'),
                _file('references/same.md', 'B'),
            ]))


class SkillManifestServiceTest(unittest.TestCase):
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
        self._seed()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _add_skill(self, name, **kwargs):
        skill = Skill(
            name=name,
            display_name=name,
            template_content=f'# {name}',
            review_status=kwargs.pop('review_status', 'approved'),
            **kwargs,
        )
        db.session.add(skill)
        db.session.flush()
        return skill

    def _seed(self):
        alpha = Project(name='Alpha')
        beta = Project(name='Beta')
        db.session.add_all([alpha, beta])
        db.session.flush()
        self.alpha = alpha
        self.beta = beta

        self.claw = OpenClawInstance(
            name='A', safe_name='a', claw_tag='claw-a', owner='owner-a',
            project_id=alpha.id, project_name='Alpha')
        self.other_claw = OpenClawInstance(
            name='B', safe_name='b', claw_tag='claw-b', owner='owner-b',
            project_id=beta.id, project_name='Beta')
        db.session.add_all([self.claw, self.other_claw])
        db.session.flush()

        self.assigned = self._add_skill('assigned-skill')
        self.profile_skill = self._add_skill('profile-skill')
        self.requirement = self._add_skill('requirement-analysis')
        self.preflight = self._add_skill('basic-operations-preflight')
        db.session.add(OpenClawSkill(
            openclaw_id=self.claw.id,
            skill_id=self.assigned.id,
            enabled=True,
        ))
        db.session.add(SkillFile(
            skill_id=self.assigned.id,
            filename='references/中文 文档.md',
            content='说明',
        ))

        profile = AgentProfile(
            profile_key='analyst',
            name='分析岗',
            required_skills_json=['profile-skill', 'missing-skill'],
            status='active',
        )
        db.session.add(profile)
        db.session.flush()
        post = AgentPost(
            post_key='analyst',
            name='分析岗',
            project_id=alpha.id,
            profile_id=profile.id,
            status='active',
        )
        db.session.add(post)
        db.session.flush()
        db.session.add(AgentPostAssignment(
            post_id=post.id,
            claw_id=self.claw.id,
            is_primary=True,
            status='active',
        ))

        self.task = AgentTask(
            task_id='task-a',
            claw_id=self.claw.id,
            task_type='analysis',
            command='评审需求',
            payload='{"title":"需求评审"}',
        )
        self.other_task = AgentTask(
            task_id='task-b',
            claw_id=self.other_claw.id,
            task_type='analysis',
            command='评审需求',
            payload='{"title":"需求评审"}',
        )
        db.session.add_all([self.task, self.other_task])
        db.session.commit()

    def test_manifest_merges_assigned_and_profile_sources(self):
        manifest = skill_delivery.build_skill_manifest(self.claw)
        by_name = {item['name']: item for item in manifest['skills']}

        self.assertEqual(by_name['assigned-skill']['sources'], ['assigned'])
        self.assertEqual(by_name['profile-skill']['sources'], ['profile'])
        self.assertEqual(manifest['missing_skills'], [{
            'name': 'missing-skill',
            'sources': ['profile'],
            'reason': 'not_found',
        }])

    def test_task_context_skills_are_automatically_authorized(self):
        manifest = skill_delivery.build_skill_manifest(
            self.claw, ref_type='agent_task', ref_id=self.task.id)
        by_name = {item['name']: item for item in manifest['skills']}

        self.assertEqual(
            by_name['requirement-analysis']['sources'],
            ['task_context'],
        )
        self.assertEqual(
            by_name['basic-operations-preflight']['sources'],
            ['task_context'],
        )

    def test_manifest_url_encodes_each_file_path(self):
        manifest = skill_delivery.build_skill_manifest(self.claw)
        assigned = next(
            item for item in manifest['skills']
            if item['name'] == 'assigned-skill'
        )
        reference = next(
            item for item in assigned['files']
            if item['path'] == 'references/中文 文档.md'
        )

        self.assertIn(
            'references/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md',
            reference['download_url'],
        )

    def test_other_claw_task_is_forbidden(self):
        with self.assertRaises(skill_delivery.SkillDeliveryError) as raised:
            skill_delivery.build_skill_manifest(
                self.claw,
                ref_type='agent_task',
                ref_id=self.other_task.id,
            )

        self.assertEqual(raised.exception.code, 'task_forbidden')
        self.assertEqual(raised.exception.status_code, 403)

    def test_unavailable_required_skills_have_stable_reasons(self):
        profile = self.claw.post_assignments.first().post.profile
        self._add_skill('pending-skill', review_status='pending')
        self._add_skill('deleted-skill', is_deleted=True)
        self._add_skill(
            'project-skill',
            applicable_projects=[self.beta.id],
        )
        self._add_skill(
            'private-skill',
            visibility='private',
            owner_claw_id=self.other_claw.id,
        )
        profile.required_skills_json = [
            'pending-skill',
            'deleted-skill',
            'project-skill',
            'private-skill',
        ]
        db.session.commit()

        manifest = skill_delivery.build_skill_manifest(self.claw)

        reasons = {
            item['name']: item['reason']
            for item in manifest['missing_skills']
        }
        self.assertEqual(reasons, {
            'pending-skill': 'not_approved',
            'deleted-skill': 'deleted',
            'project-skill': 'project_forbidden',
            'private-skill': 'private_forbidden',
        })


if __name__ == '__main__':
    unittest.main()
