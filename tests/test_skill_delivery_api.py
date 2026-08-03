import io
import sys
import types
import unittest
import zipfile
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

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (AgentTask, OpenClawInstance, OpenClawSkill,  # noqa: E402
                        Project, Skill, SkillFile, hash_token)


class SkillDeliveryApiTest(unittest.TestCase):
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
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self._seed()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _seed(self):
        project = Project(name='Alpha')
        db.session.add(project)
        db.session.flush()
        self.token = 'hub_tk_agent_a'
        self.other_token = 'hub_tk_agent_b'
        self.claw = OpenClawInstance(
            name='A',
            safe_name='a',
            claw_tag='claw-a',
            owner='owner-a',
            project_id=project.id,
            project_name='Alpha',
            api_token_hash=hash_token(self.token),
        )
        self.other_claw = OpenClawInstance(
            name='B',
            safe_name='b',
            claw_tag='claw-b',
            owner='owner-b',
            project_id=project.id,
            project_name='Alpha',
            api_token_hash=hash_token(self.other_token),
        )
        db.session.add_all([self.claw, self.other_claw])
        db.session.flush()

        self.skill = Skill(
            name='demo-skill',
            display_name='Demo Skill',
            template_content='# fallback',
            review_status='approved',
        )
        self.forbidden_skill = Skill(
            name='forbidden-skill',
            display_name='Forbidden',
            template_content='# forbidden',
            review_status='approved',
        )
        db.session.add_all([self.skill, self.forbidden_skill])
        db.session.flush()
        db.session.add_all([
            SkillFile(
                skill_id=self.skill.id,
                filename='SKILL.md',
                content='# Demo',
                file_type='markdown',
            ),
            SkillFile(
                skill_id=self.skill.id,
                filename='references/info.json',
                content='{"ok":true}',
                file_type='json',
            ),
            OpenClawSkill(
                openclaw_id=self.claw.id,
                skill_id=self.skill.id,
                enabled=True,
            ),
        ])
        self.task = AgentTask(
            task_id='task-a',
            claw_id=self.claw.id,
            task_type='analysis',
            command='普通任务',
        )
        self.other_task = AgentTask(
            task_id='task-b',
            claw_id=self.other_claw.id,
            task_type='analysis',
            command='普通任务',
        )
        db.session.add_all([self.task, self.other_task])
        db.session.commit()

    def _headers(self, token=None):
        return {
            'Authorization': f'Bearer {token or self.token}',
        }

    def test_manifest_requires_matching_claw_token(self):
        url = f'/api/v1/openclaws/{self.claw.id}/skill-manifest'

        missing = self.client.get(url)
        mismatched = self.client.get(
            url,
            headers=self._headers(self.other_token),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(mismatched.status_code, 403)

    def test_manifest_returns_metadata_without_file_content(self):
        response = self.client.get(
            f'/api/v1/openclaws/{self.claw.id}/skill-manifest',
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload['manifest_version'], 1)
        self.assertEqual(payload['claw_id'], self.claw.id)
        item = payload['skills'][0]
        self.assertEqual(item['name'], 'demo-skill')
        self.assertIn('sha256', item)
        self.assertNotIn('content', item['files'][0])

    def test_manifest_validates_task_reference_and_ownership(self):
        base = f'/api/v1/openclaws/{self.claw.id}/skill-manifest'

        partial = self.client.get(
            base + '?ref_type=agent_task',
            headers=self._headers(),
        )
        foreign = self.client.get(
            base + (
                '?ref_type=agent_task'
                f'&ref_id={self.other_task.id}'
            ),
            headers=self._headers(),
        )

        self.assertEqual(partial.status_code, 400)
        self.assertEqual(partial.get_json()['code'], 'invalid_task_ref')
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(foreign.get_json()['code'], 'task_forbidden')

    def test_authorized_file_download_supports_etag(self):
        manifest = self.client.get(
            f'/api/v1/openclaws/{self.claw.id}/skill-manifest',
            headers=self._headers(),
        ).get_json()
        skill_md = next(
            item for item in manifest['skills'][0]['files']
            if item['path'] == 'SKILL.md'
        )

        first = self.client.get(
            skill_md['download_url'],
            headers=self._headers(),
        )
        cached = self.client.get(
            skill_md['download_url'],
            headers={
                **self._headers(),
                'If-None-Match': first.headers['ETag'],
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(as_text=True), '# Demo')
        self.assertEqual(first.headers['Cache-Control'], 'private, max-age=60')
        self.assertTrue(first.headers['X-Skill-Content-Version'])
        self.assertEqual(cached.status_code, 304)

    def test_authorized_pack_contains_normalized_skill_directory(self):
        manifest = self.client.get(
            f'/api/v1/openclaws/{self.claw.id}/skill-manifest',
            headers=self._headers(),
        ).get_json()
        response = self.client.get(
            manifest['skills'][0]['pack_url'],
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(sorted(archive.namelist()), [
                'demo-skill/SKILL.md',
                'demo-skill/references/info.json',
            ])
            self.assertEqual(
                archive.read('demo-skill/SKILL.md').decode('utf-8'),
                '# Demo',
            )

    def test_unassigned_skill_download_is_forbidden(self):
        response = self.client.get(
            (
                f'/api/v1/openclaws/{self.claw.id}/skills/'
                f'{self.forbidden_skill.id}/pack'
            ),
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['code'], 'skill_forbidden')


if __name__ == '__main__':
    unittest.main()
