import sys
import types
import unittest
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))


class _Noop:
    def __init__(self, *args, **kwargs): pass
    def __call__(self, *args, **kwargs): return self
    def __getattr__(self, name): return _Noop()


def _stub(name, **attrs):
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask, g  # noqa: E402
from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (  # noqa: E402
    KnowledgeEntry, KnowledgeEntryRevision, KnowledgeNotebook,
    OpenClawInstance, Project, User, hash_token,
)


class KnowledgeNotebooksApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret', TESTING=True,
        )
        db.init_app(self.app)
        self.app.register_blueprint(api_bp, url_prefix='/api/v1')
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.project = Project(name='RacingGO')
        self.other_project = Project(name='Other')
        db.session.add_all([self.project, self.other_project]); db.session.flush()
        self.owner = User(username='owner', password_hash='x', role='user',
                          managed_projects=[self.project.id])
        self.peer = User(username='peer', password_hash='x', role='user',
                         managed_projects=[self.project.id])
        self.outsider = User(username='outsider', password_hash='x', role='user',
                             managed_projects=[self.other_project.id])
        db.session.add_all([self.owner, self.peer, self.outsider]); db.session.flush()
        self.token = 'hub_tk_wiki_agent'
        self.agent = OpenClawInstance(
            name='Wiki Agent', safe_name='wiki-agent', claw_tag='wiki-agent',
            owner='owner', project_id=self.project.id, project_name='RacingGO',
            api_token_hash=hash_token(self.token), status='online')
        db.session.add(self.agent); db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _login(self, user):
        with self.client.session_transaction() as session:
            session['user_id'] = user.id
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)

    def _create_notebook_and_page(self):
        self._login(self.owner)
        notebook = self.client.post('/api/v1/knowledge-notebooks', json={
            'project_id': self.project.id,
            'title': 'M3版本测试纪要', 'version_name': 'M3',
        })
        self.assertEqual(notebook.status_code, 201, notebook.get_data(as_text=True))
        nid = notebook.get_json()['id']
        page = self.client.post(
            f'/api/v1/knowledge-notebooks/{nid}/pages',
            json={'title': '登录变更', 'module_name': '研发需求变动',
                  'content': '# 登录变更\n\n旧规则'},
            headers={'Idempotency-Key': 'create-login-page'})
        self.assertEqual(page.status_code, 201, page.get_data(as_text=True))
        return nid, page.get_json()['id']

    def test_project_members_create_and_read_notebook(self):
        nid, page_id = self._create_notebook_and_page()
        self._login(self.peer)
        listing = self.client.get(
            f'/api/v1/knowledge-notebooks?project_id={self.project.id}')
        detail = self.client.get(f'/api/v1/knowledge/journal-pages/{page_id}')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.get_json()['items'][0]['id'], nid)
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.get_json()['can_edit'])

        self._login(self.outsider)
        denied = self.client.get(
            f'/api/v1/knowledge-notebooks?project_id={self.project.id}')
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            self.client.get(f'/api/v1/knowledge/{page_id}').status_code,
            404)

    def test_page_create_replays_idempotently(self):
        self._login(self.owner)
        notebook = self.client.post('/api/v1/knowledge-notebooks', json={
            'project_id': self.project.id, 'title': 'M3纪要',
        }).get_json()
        body = {'title': '质量问题', 'module_name': '质量问题',
                'content': '# 问题'}
        headers = {'Idempotency-Key': 'create-quality-page'}
        first = self.client.post(
            f'/api/v1/knowledge-notebooks/{notebook["id"]}/pages',
            json=body, headers=headers)
        replay = self.client.post(
            f'/api/v1/knowledge-notebooks/{notebook["id"]}/pages',
            json=body, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.get_json()['id'], replay.get_json()['id'])
        self.assertEqual(KnowledgeEntry.query.filter_by(
            entry_type='test_journal').count(), 1)

    def test_revision_conflict_idempotency_compare_and_rollback(self):
        _, page_id = self._create_notebook_and_page()
        update = {
            'expected_revision': 1, 'title': '登录变更',
            'content': '# 登录变更\n\n新规则', 'change_summary': '更新规则',
        }
        headers = {'Idempotency-Key': 'revision-2'}
        first = self.client.post(
            f'/api/v1/knowledge/{page_id}/revisions', json=update,
            headers=headers)
        replay = self.client.post(
            f'/api/v1/knowledge/{page_id}/revisions', json=update,
            headers=headers)
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(replay.status_code, 201, replay.get_data(as_text=True))
        self.assertEqual(KnowledgeEntryRevision.query.filter_by(
            knowledge_id=page_id).count(), 2)

        conflict = self.client.post(
            f'/api/v1/knowledge/{page_id}/revisions',
            json=dict(update, content='stale'),
            headers={'Idempotency-Key': 'stale-update'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()['code'],
                         'KNOWLEDGE_REVISION_CONFLICT')

        compared = self.client.get(
            f'/api/v1/knowledge/{page_id}/compare?from=1&to=2')
        self.assertEqual(compared.status_code, 200)
        self.assertGreater(compared.get_json()['stats']['added'], 0)

        rollback = self.client.post(
            f'/api/v1/knowledge/{page_id}/rollback', json={
                'expected_revision': 2, 'target_revision': 1,
            }, headers={'Idempotency-Key': 'rollback-to-1'})
        self.assertEqual(rollback.status_code, 201, rollback.get_data(as_text=True))
        self.assertEqual(rollback.get_json()['page']['current_revision'], 3)
        self.assertEqual(rollback.get_json()['revision']['rollback_from_revision'], 1)
        self.assertIn('旧规则', rollback.get_json()['revision']['content'])

    def test_project_agent_can_edit_and_legacy_put_cannot_bypass_history(self):
        _, page_id = self._create_notebook_and_page()
        with self.client.session_transaction() as session:
            session.clear()
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Idempotency-Key': 'agent-revision-2',
        }
        saved = self.client.post(
            f'/api/v1/knowledge/{page_id}/revisions', json={
                'expected_revision': 1, 'title': '登录变更',
                'content': '# Agent 补充', 'change_summary': 'Agent 更新',
            }, headers=headers)
        self.assertEqual(saved.status_code, 201, saved.get_data(as_text=True))
        self.assertEqual(saved.get_json()['revision']['editor_type'], 'claw')

        self._login(self.owner)
        blocked = self.client.put(f'/api/v1/knowledge/{page_id}', json={
            'content': '绕过版本历史',
        })
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()['code'],
                         'VERSIONED_KNOWLEDGE_REQUIRES_REVISION')
        deleted = self.client.delete(f'/api/v1/knowledge/{page_id}')
        shared = self.client.post(f'/api/v1/knowledge/{page_id}/share')
        self.assertEqual(deleted.status_code, 409)
        self.assertEqual(shared.status_code, 409)

    def test_journal_pages_do_not_pollute_article_listing(self):
        _, page_id = self._create_notebook_and_page()
        article = KnowledgeEntry(
            title='普通知识', content='正文', category='general',
            scope='project', project_name='RacingGO', entry_type='article')
        db.session.add(article); db.session.commit()
        listing = self.client.get('/api/v1/knowledge').get_json()
        self.assertEqual([item['id'] for item in listing], [article.id])
        self.assertIsNotNone(db.session.get(KnowledgeEntry, page_id))
        self.assertEqual(KnowledgeNotebook.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
