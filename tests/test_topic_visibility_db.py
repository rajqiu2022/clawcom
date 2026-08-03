"""课题可见性的带库契约测试。

覆盖纯函数单测（tests/test_review_visibility.py）碰不到的两段查库逻辑：
``visible_topic_filter`` 生成的 SQL 条件，以及 ``granted_topic_ids`` 的
"用户授权 → 名下 Agent 继承 / Agent 授权 → owner 继承" 双向推导。
"""

import sys
import types
import unittest
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))

if 'flask_cors' not in sys.modules:
    _stub = types.ModuleType('flask_cors')
    _stub.CORS = lambda *args, **kwargs: None
    sys.modules['flask_cors'] = _stub

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.models import (OpenClawInstance, Project, Topic, TopicGrant,  # noqa: E402
                        User)
from app.services.review_visibility import (  # noqa: E402
    granted_topic_ids,
    visible_topic_filter,
)


class TopicVisibilityDbTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY='test-secret',
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

    def _seed(self):
        alpha = Project(name='Alpha')
        beta = Project(name='Beta')
        db.session.add_all([alpha, beta])
        db.session.flush()
        self.alpha_id, self.beta_id = alpha.id, beta.id

        self.owner = User(username='owner_user', password_hash='x', role='user')
        self.outsider = User(username='outsider', password_hash='x', role='user')
        db.session.add_all([self.owner, self.outsider])
        db.session.flush()

        self.owned_claw = OpenClawInstance(
            name='owned-claw', safe_name='owned-claw', claw_tag='owned-claw',
            owner='owner_user', status='online', project_id=self.alpha_id)
        db.session.add(self.owned_claw)
        db.session.flush()

        def make(visibility, project_name=None):
            topic = Topic(title='T-' + visibility, content='c',
                          board='case_review', author_name='someone_else',
                          visibility=visibility, project_name=project_name)
            db.session.add(topic)
            db.session.flush()
            return topic

        self.t_public_all = make('public_all')
        self.t_public = make('public')
        self.t_alpha = make('project', 'Alpha')
        self.t_beta = make('project', 'Beta')
        self.t_assigned = make('assigned')
        db.session.commit()

    def _visible_titles(self, caller, has_project=True, granted_ids=None):
        cond = visible_topic_filter(
            caller, caller_has_project_access=has_project,
            granted_ids=granted_ids)
        rows = Topic.query.filter(cond).all()
        return {t.visibility + ':' + (t.project_name or '-') for t in rows}

    def _caller(self, user=None, claw=None, project_ids=(), role='user'):
        return {
            'username': 'tester',
            'user_id': user.id if user else None,
            'claw_id': claw.id if claw else None,
            'role': role,
            'is_admin': role in ('super_admin', 'admin'),
            'project_ids': list(project_ids),
        }

    # ---------------- 列表过滤 ----------------

    def test_no_project_user_sees_only_public_all(self):
        caller = self._caller(user=self.outsider)
        self.assertEqual(
            self._visible_titles(caller, has_project=False),
            {'public_all:-'},
        )

    def test_project_user_sees_public_all_public_and_own_project(self):
        caller = self._caller(user=self.outsider, project_ids=(self.alpha_id,))
        self.assertEqual(
            self._visible_titles(caller, has_project=True),
            {'public_all:-', 'public:-', 'project:Alpha'},
        )

    def test_project_user_cannot_see_other_project(self):
        caller = self._caller(user=self.outsider, project_ids=(self.alpha_id,))
        self.assertNotIn('project:Beta', self._visible_titles(caller))

    def test_anonymous_sees_only_public_all(self):
        self.assertEqual(self._visible_titles(None), {'public_all:-'})

    def test_super_admin_sees_everything(self):
        caller = self._caller(user=self.outsider, role='super_admin')
        self.assertEqual(len(Topic.query.filter(
            visible_topic_filter(caller)).all()), 5)

    def test_assigned_topic_appears_only_after_grant(self):
        caller = self._caller(user=self.outsider)
        self.assertNotIn('assigned:-', self._visible_titles(
            caller, has_project=False))

        db.session.add(TopicGrant(
            topic_id=self.t_assigned.id, grant_type='user',
            target_user_id=self.outsider.id, granted_by='owner_user'))
        db.session.commit()

        self.assertEqual(
            self._visible_titles(caller, has_project=False),
            {'public_all:-', 'assigned:-'},
        )

    def test_author_sees_own_topic_regardless_of_scope(self):
        mine = Topic(title='mine', content='c', board='case_review',
                     author_name='outsider', author_user_id=self.outsider.id,
                     visibility='assigned')
        db.session.add(mine)
        db.session.commit()
        caller = self._caller(user=self.outsider)
        ids = {t.id for t in Topic.query.filter(
            visible_topic_filter(caller, caller_has_project_access=False)).all()}
        self.assertIn(mine.id, ids)

    # ---------------- 授权继承 ----------------

    def test_user_grant_is_inherited_by_owned_agent(self):
        db.session.add(TopicGrant(
            topic_id=self.t_assigned.id, grant_type='user',
            target_user_id=self.owner.id, granted_by='admin'))
        db.session.commit()

        agent_caller = self._caller(claw=self.owned_claw,
                                    project_ids=(self.alpha_id,))
        self.assertIn(self.t_assigned.id, granted_topic_ids(agent_caller))

    def test_agent_grant_is_inherited_by_its_owner_user(self):
        db.session.add(TopicGrant(
            topic_id=self.t_assigned.id, grant_type='claw',
            target_claw_id=self.owned_claw.id, granted_by='admin'))
        db.session.commit()

        user_caller = self._caller(user=self.owner)
        self.assertIn(self.t_assigned.id, granted_topic_ids(user_caller))

    def test_unrelated_user_gets_no_grants(self):
        db.session.add(TopicGrant(
            topic_id=self.t_assigned.id, grant_type='user',
            target_user_id=self.owner.id, granted_by='admin'))
        db.session.commit()
        self.assertEqual(granted_topic_ids(self._caller(user=self.outsider)),
                         set())

    def test_expired_grant_is_ignored(self):
        from datetime import datetime, timedelta
        db.session.add(TopicGrant(
            topic_id=self.t_assigned.id, grant_type='user',
            target_user_id=self.outsider.id, granted_by='admin',
            expires_at=datetime.now() - timedelta(days=1)))
        db.session.commit()
        self.assertEqual(granted_topic_ids(self._caller(user=self.outsider)),
                         set())


if __name__ == '__main__':
    unittest.main()
