import hashlib
import io
import sys
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))


class _Noop:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return _Noop()


for name, attrs in (
    ('flask_cors', {'CORS': _Noop}),
    ('flask_socketio', {'SocketIO': _Noop, 'emit': _Noop(),
                       'join_room': _Noop(), 'leave_room': _Noop()}),
):
    if name not in sys.modules:
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module

from flask import Flask
from app import db
from app.api import api_bp, _token_claw_cache
from app.models import Skill, SkillAttachment, User, OpenClawInstance, Project, hash_token


class SkillAttachmentsApiTest(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False, SECRET_KEY='test', TESTING=True,
            SKILL_ATTACHMENT_ROOT=self.storage.name)
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        project = Project(name='附件项目')
        db.session.add(project)
        db.session.flush()
        self.owner = User(username='attachment-owner', password_hash='unused', role='user',
                          managed_projects=[project.id])
        self.reader = User(username='attachment-reader', password_hash='unused', role='user',
                           managed_projects=[project.id])
        self.skill = Skill(name='attachments-demo', display_name='附件示例',
                           template_content='# Skill', created_by=self.owner.username,
                           visibility='private', review_status='approved')
        self.other = Skill(name='other-demo', display_name='其他',
                           review_status='approved', visibility='public')
        db.session.add_all([self.owner, self.reader, self.skill, self.other])
        db.session.commit()
        self.client = self.app.test_client()
        self.login(self.owner)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.storage.cleanup()
        _token_claw_cache.clear()

    def login(self, user):
        with self.client.session_transaction() as session:
            session['user_id'] = user.id

    def upload(self, name, content):
        return self.client.post(f'/api/v1/skills/{self.skill.id}/attachments',
                                data={'file': (io.BytesIO(content), name)})

    def test_binary_roundtrip_and_pack_preserve_original_bytes(self):
        raw = b'\x00\xff\x81\x00binary'
        response = self.upload('测试文件.bin', raw)
        self.assertEqual(response.status_code, 201)
        metadata = response.get_json()
        self.assertEqual(metadata['sha256'], hashlib.sha256(raw).hexdigest())
        download = self.client.get(metadata['download_url'])
        self.assertEqual(download.data, raw)
        self.assertIn('attachment;', download.headers['Content-Disposition'])
        self.assertEqual(download.headers['X-Content-Type-Options'], 'nosniff')
        pack = self.client.get(f'/api/v1/skills/{self.skill.id}/pack')
        with zipfile.ZipFile(io.BytesIO(pack.data)) as archive:
            self.assertEqual(archive.read('attachments-demo/attachments/测试文件.bin'), raw)
            self.assertEqual(archive.read('attachments-demo/SKILL.md'), b'# Skill')

    def test_attachment_counts_follow_upload_and_delete_in_summary_and_detail(self):
        def counts():
            items = self.client.get('/api/v1/skills?summary=true').get_json()
            item = next(item for item in items if item['id'] == self.skill.id)
            self.assertNotIn('attachments', item)
            detail = self.client.get(f'/api/v1/skills/{self.skill.id}').get_json()
            return item['attachment_count'], detail['attachment_count']
        self.assertEqual(counts(), (0, 0))
        metadata = self.upload('script.py', b'print("ok")').get_json()
        self.assertEqual(counts(), (1, 1))
        self.assertEqual(self.client.delete(metadata['detail_url']).status_code, 200)
        self.assertEqual(counts(), (0, 0))

    def test_script_and_zip_details_do_not_execute_or_extract(self):
        script = b'print("never execute")\n'
        metadata = self.upload('probe.py', script).get_json()
        detail = self.client.get(metadata['detail_url']).get_json()
        self.assertEqual(detail['text_preview'], script.decode())
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../outside.txt', 'do not extract')
        metadata = self.upload('bundle.zip', data.getvalue()).get_json()
        detail = self.client.get(metadata['detail_url']).get_json()
        self.assertEqual(detail['archive_entries'][0]['name'], '../outside.txt')
        self.assertEqual(len(list(Path(self.storage.name).rglob('*'))), 3)

    def test_private_attachments_and_pack_are_denied_to_other_reader(self):
        metadata = self.upload('private.txt', b'private').get_json()
        self.login(self.reader)
        for path in (metadata['download_url'], metadata['detail_url'],
                     f'/api/v1/skills/{self.skill.id}',
                     f'/api/v1/skills/{self.skill.id}/pack',
                     f'/api/v1/skills/{self.skill.id}/attachments'):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.upload('other.txt', b'no').status_code, 403)
        self.assertEqual(self.client.delete(metadata['detail_url']).status_code, 403)

    def test_cross_skill_attachment_id_cannot_be_downloaded(self):
        metadata = self.upload('a.txt', b'a').get_json()
        response = self.client.get(
            f'/api/v1/skills/{self.other.id}/attachments/{metadata["id"]}/download')
        self.assertEqual(response.status_code, 404)

    def test_reject_path_and_oversized_file_without_storage_leaks(self):
        for name in ('../script.py', 'dir/script.py', 'dir\\script.py'):
            self.assertEqual(self.upload(name, b'x').status_code, 400)
        with patch('app.api.skills.MAX_SKILL_ATTACHMENT_SIZE', 4):
            self.assertEqual(self.upload('large.bin', b'12345').status_code, 413)
        self.assertEqual(SkillAttachment.query.count(), 0)
        self.assertFalse(list(Path(self.storage.name).rglob('*')))

    def test_duplicate_is_not_overwritten_and_delete_removes_bytes(self):
        metadata = self.upload('same.txt', b'first').get_json()
        attachment = db.session.get(SkillAttachment, metadata['id'])
        stored_path = Path(self.storage.name) / str(self.skill.id) / attachment.stored_name
        self.assertEqual(self.upload('same.txt', b'second').status_code, 409)
        self.assertEqual(self.client.get(metadata['download_url']).data, b'first')
        self.assertEqual(self.client.delete(metadata['detail_url']).status_code, 200)
        self.assertEqual(SkillAttachment.query.count(), 0)
        self.assertFalse(stored_path.exists())

    def test_agent_token_can_upload_and_public_change_requires_review(self):
        token = 'hub_tk_attachment_agent'
        claw = OpenClawInstance(name='script-agent', owner=self.owner.username,
                                safe_name='script-agent', claw_tag='script-agent',
                                api_token_hash=hash_token(token))
        self.skill.visibility = 'public'
        self.skill.created_by = claw.name
        db.session.add(claw)
        db.session.commit()
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post(f'/api/v1/skills/{self.skill.id}/attachments',
            headers={'Authorization': f'Bearer {token}'},
            data={'file': (io.BytesIO(b'echo ok'), 'test.sh')})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()['uploaded_by'], 'script-agent')
        self.assertEqual(self.skill.review_status, 'pending')


class SkillAttachmentDownloadUiTest(unittest.TestCase):
    def test_download_buttons_only_render_with_attachments(self):
        if not shutil.which('node'):
            self.skipTest('Node.js required for frontend rendering test')
        page = (Path(__file__).resolve().parents[1] / 'web/templates/skills.html').read_text(encoding='utf-8')
        function = page.split('function renderSkillPackDownload', 1)[1].split('function renderSkillAttachments', 1)[0]
        script = 'function renderSkillPackDownload' + function + '''
const assert = require('node:assert/strict');
for (const skill of [{id:1}, {id:1,attachment_count:0}, {id:1,attachments:[]},
                     {id:1,attachment_count:3,attachments:[]}]) {
    assert.equal(renderSkillPackDownload(skill), '');
    assert.equal(renderSkillPackDownload(skill,true), '');
}
assert.match(renderSkillPackDownload({id:1,attachment_count:1},true), /下载文档包/);
assert.match(renderSkillPackDownload({id:1,attachments:[{id:2}]}), /下载完整文档包/);
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True)
        self.assertIn('${renderSkillPackDownload(s, true)}', page)
        self.assertIn('${renderSkillPackDownload(s)}', page)
