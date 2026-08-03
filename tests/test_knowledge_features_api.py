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

    def __getattr__(self, name):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask, g  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (KnowledgeEntry, KnowledgeFavorite,  # noqa: E402
                        OpenClawInstance, Project, User, hash_token)


class KnowledgeFeaturesApiTest(unittest.TestCase):
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
        self.author = User(
            username='alice', password_hash='x', role='user',
            managed_projects=[project.id])
        self.other = User(
            username='mallory', password_hash='x', role='user',
            managed_projects=[project.id])
        db.session.add_all([self.author, self.other])
        db.session.flush()
        self.agent_token = 'hub_tk_knowledge_agent'
        self.claw = OpenClawInstance(
            name='Knowledge Agent',
            safe_name='knowledge-agent',
            claw_tag='knowledge-agent',
            owner='alice',
            project_id=project.id,
            project_name='Alpha',
            api_token_hash=hash_token(self.agent_token),
        )
        db.session.add(self.claw)
        db.session.flush()
        self.entry = KnowledgeEntry(
            title='登录/支付回归',
            content='## 检查\n\n- 支付成功',
            category='best-practice',
            scope='global',
            project_name='Alpha',
            status='draft',
            created_by='alice',
            source_openclaw_id=self.claw.id,
        )
        self.other_entry = KnowledgeEntry(
            title='其他知识',
            content='不可收藏筛选出来',
            category='general',
            scope='global',
            status='approved',
            created_by='mallory',
        )
        db.session.add_all([self.entry, self.other_entry])
        db.session.commit()

    def _login(self, user):
        with self.client.session_transaction() as session:
            session['user_id'] = user.id
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)

    def _logout(self):
        with self.client.session_transaction() as session:
            session.clear()
        for key in ('_auth_user_super', '_auth_user', '_auth_claw'):
            g.pop(key, None)

    def test_favorite_is_idempotent_and_list_can_filter_favorites(self):
        self._login(self.author)
        url = f'/api/v1/knowledge/{self.entry.id}/favorite'
        first = self.client.post(url)
        second = self.client.post(url)
        listing = self.client.get('/api/v1/knowledge?favorite=1')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            [row['id'] for row in listing.get_json()],
            [self.entry.id],
        )
        self.assertTrue(listing.get_json()[0]['is_favorite'])

        removed = self.client.delete(url)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            KnowledgeFavorite.query.filter_by(
                knowledge_id=self.entry.id,
                user_id=self.author.id,
            ).count(),
            0,
        )

    def test_bearer_agent_has_independent_favorite_and_can_share_own_entry(self):
        headers = {'Authorization': f'Bearer {self.agent_token}'}
        favorited = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/favorite',
            headers=headers,
        )
        shared = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share',
            headers=headers,
        )
        self.assertEqual(favorited.status_code, 201)
        self.assertEqual(shared.status_code, 200)
        row = KnowledgeFavorite.query.filter_by(
            knowledge_id=self.entry.id,
            claw_id=self.claw.id,
        ).one()
        self.assertIsNone(row.user_id)

    def test_only_author_can_manage_share_and_refresh_invalidates_old_token(self):
        self._login(self.other)
        forbidden = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share')
        self.assertEqual(forbidden.status_code, 403)

        self._login(self.author)
        enabled = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share')
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        old_token = enabled.get_json()['share_token']

        refreshed = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share?refresh=1')
        new_token = refreshed.get_json()['share_token']
        self.assertNotEqual(old_token, new_token)

        self._login(self.other)
        private_view = self.client.get(
            f'/api/v1/knowledge/{self.entry.id}').get_json()
        self.assertFalse(private_view['can_manage_share'])
        self.assertNotIn('share_token', private_view)
        self.assertNotIn('share_url', private_view)

        self._logout()
        self.assertEqual(
            self.client.get(
                f'/api/v1/knowledge/shared/{old_token}').status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f'/api/v1/knowledge/shared/{new_token}').status_code,
            200,
        )

    def test_anonymous_payload_is_sanitized_and_supports_markdown_download(self):
        self._login(self.author)
        share = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share').get_json()
        token = share['share_token']
        self._logout()

        detail = self.client.get(f'/api/v1/knowledge/shared/{token}')
        self.assertEqual(detail.status_code, 200)
        payload = detail.get_json()
        self.assertEqual(payload['title'], '登录/支付回归')
        for forbidden in (
            'id', 'memos_id', 'source_openclaw_id',
            'reviewer_notes', 'approved_by',
        ):
            self.assertNotIn(forbidden, payload)

        download = self.client.get(
            f'/api/v1/knowledge/shared/{token}/export.md')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, 'text/markdown')
        self.assertIn('attachment;', download.headers['Content-Disposition'])
        self.assertEqual(
            download.headers['X-Content-Type-Options'],
            'nosniff',
        )
        self.assertIn('# 登录/支付回归', download.get_data(as_text=True))

    def test_authenticated_export_and_share_revoke(self):
        self._login(self.author)
        exported = self.client.get(
            f'/api/v1/knowledge/{self.entry.id}/export.md')
        self.assertEqual(exported.status_code, 200)
        self.assertIn(
            "filename*=UTF-8''",
            exported.headers['Content-Disposition'],
        )

        share = self.client.post(
            f'/api/v1/knowledge/{self.entry.id}/share').get_json()
        revoked = self.client.delete(
            f'/api/v1/knowledge/{self.entry.id}/share')
        self.assertEqual(revoked.status_code, 200)
        self._logout()
        self.assertEqual(
            self.client.get(
                f"/api/v1/knowledge/shared/{share['share_token']}"
            ).status_code,
            404,
        )

    def test_delete_cleans_favorites(self):
        self._login(self.author)
        self.client.post(f'/api/v1/knowledge/{self.entry.id}/favorite')
        deleted = self.client.delete(
            f'/api/v1/knowledge/{self.entry.id}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            KnowledgeFavorite.query.filter_by(
                knowledge_id=self.entry.id).count(),
            0,
        )


if __name__ == '__main__':
    unittest.main()
