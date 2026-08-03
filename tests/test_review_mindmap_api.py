"""评审脑图与节点标记的 HTTP 端到端契约测试。

纯函数单测（test_case_mindmap / test_review_marks）验证的是构图与范围判断，
这里补上真正走 `api_bp` 的一层，确认：

1. `public_all` 评审能让**没有任何项目权限**的用户过全局项目门禁；
2. 同一个人拿不到 `public` / `project` 评审（豁免没有顺带放开别的课题）；
3. 目录级评审的标记范围锁定在子树内，越界节点进 `skipped`；
4. 标记读写幂等，并能回到脑图节点上。
"""

import sys
import types
import unittest
from pathlib import Path


_WEB = Path(__file__).resolve().parents[1] / 'web'
if str(_WEB) not in sys.path:
    sys.path.insert(0, str(_WEB))


def _stub(name, **attrs):
    """本地环境不一定装齐 Flask 扩展，端到端测试用不到它们的真实行为。"""
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


class _Noop:
    def __init__(self, *a, **kw):
        pass

    def __call__(self, *a, **kw):
        return self

    def __getattr__(self, item):
        return _Noop()


_stub('flask_cors', CORS=_Noop)
_stub('flask_socketio', SocketIO=_Noop, emit=_Noop(),
      join_room=_Noop(), leave_room=_Noop())

from flask import Flask  # noqa: E402

from app import db  # noqa: E402
from app.api import api_bp  # noqa: E402
from app.models import (CaseReviewNodeMark, Project, TestCase,  # noqa: E402
                        TestCaseLibrary, Topic, TopicGrant, User)


class ReviewMindmapApiTest(unittest.TestCase):
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
        alpha = Project(name='Alpha')
        db.session.add(alpha)
        db.session.flush()

        # member 有项目权限（能走用例库接口）；outsider 完全没有，用来验证
        # public_all 的项目门禁豁免
        self.member = User(username='member', password_hash='x', role='user',
                           managed_projects=[alpha.id])
        self.outsider = User(username='outsider', password_hash='x', role='user')
        db.session.add_all([self.member, self.outsider])
        db.session.flush()

        self.lib = TestCaseLibrary(name='登录用例库', project_name='Alpha',
                                   owner='member', created_by='member')
        db.session.add(self.lib)
        db.session.flush()

        # 目录结构：登录/手机号 (2 条)、登录/验证码 (1 条)、支付 (1 条，范围外)
        for path, title, prio in [
            ('登录/手机号', '手机号正常登录', 'P0'),
            ('登录/手机号', '手机号格式非法', 'P2'),
            ('登录/验证码', '验证码超时', 'P1'),
            ('支付', '微信支付成功', 'P0'),
        ]:
            db.session.add(TestCase(library_id=self.lib.id, title=title,
                                    priority=prio, module_path=path))
        db.session.flush()
        self.case_ids = {
            c.title: c.id for c in TestCase.query.filter_by(library_id=self.lib.id)
        }

        def make_topic(visibility, module_paths):
            topic = Topic(title='评审-' + visibility, content='c',
                          board='case_review', author_name='member',
                          author_user_id=self.member.id,
                          visibility=visibility, project_name='Alpha',
                          review_library_id=self.lib.id,
                          review_module_paths=module_paths)
            db.session.add(topic)
            db.session.flush()
            return topic

        self.t_open = make_topic('public_all', ['登录'])
        self.t_hub = make_topic('public', ['登录'])
        self.t_whole_lib = make_topic('public_all', None)
        db.session.commit()

    def _login(self, user):
        with self.client.session_transaction() as sess:
            sess['user_id'] = user.id
            sess['username'] = user.username

    # ---------------- 可见性豁免 ----------------

    def test_no_project_user_reads_public_all_mindmap(self):
        """无项目权限用户能读完全公开评审的脑图（项目门禁豁免生效）"""
        self._login(self.outsider)
        resp = self.client.get('/api/v1/topics/%d/review-mindmap' % self.t_open.id)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data['scope_type'], 'module')
        self.assertEqual(data['scope_module_path'], '登录')
        self.assertEqual(data['text'], '登录')

    def test_no_project_user_blocked_on_hub_visibility(self):
        """同一个无项目权限用户拿不到 visibility=public 的评审"""
        self._login(self.outsider)
        resp = self.client.get('/api/v1/topics/%d/review-mindmap' % self.t_hub.id)
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_rejected(self):
        resp = self.client.get('/api/v1/topics/%d/review-mindmap' % self.t_open.id)
        self.assertEqual(resp.status_code, 401)

    # ---------------- 脑图结构 ----------------

    def test_mindmap_scoped_to_review_subtree(self):
        """目录级评审的脑图只含该子树，范围外的"支付"不出现"""
        self._login(self.member)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % self.t_open.id).get_json()

        self.assertEqual(data['case_count'], 3)
        self.assertEqual(data['total_case_count'], 3)
        child_names = sorted(c['text'] for c in data['children'])
        self.assertEqual(child_names, ['手机号', '验证码'])

        titles = set()

        def walk(node):
            if node['node_type'] == 'case':
                titles.add(node['text'])
            for kid in node['children']:
                walk(kid)

        walk(data)
        self.assertNotIn('微信支付成功', titles)
        self.assertEqual(titles, {'手机号正常登录', '手机号格式非法', '验证码超时'})

    def test_whole_library_review_includes_all_directories(self):
        self._login(self.member)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % self.t_whole_lib.id).get_json()
        self.assertEqual(data['scope_type'], 'library')
        self.assertEqual(data['text'], '登录用例库')
        self.assertEqual(sorted(c['text'] for c in data['children']), ['支付', '登录'])

    def test_library_directory_mindmap_endpoint(self):
        self._login(self.member)
        resp = self.client.get(
            '/api/v1/testcase-libraries/%d/directory-mindmap?module_path=%s'
            % (self.lib.id, '登录'))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data['text'], '登录')
        self.assertEqual(data['case_count'], 3)
        # 用例库视图与评审镜像层隔离：不带标记
        self.assertIsNone(data['mark'])

    # ---------------- 就地展开：子树与用例详情 ----------------

    def test_subtree_param_returns_only_that_directory(self):
        """带 module_path 时只返回该子目录，供前端就地补齐被截断的目录"""
        self._login(self.member)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap?module_path=%s'
            % (self.t_open.id, '登录/手机号')).get_json()
        self.assertEqual(data['text'], '手机号')
        self.assertEqual(data['subtree_module_path'], '登录/手机号')
        self.assertEqual(data['case_count'], 2)
        titles = sorted(c['text'] for c in data['children'])
        self.assertEqual(titles, ['手机号格式非法', '手机号正常登录'])
        # 课题自身的评审范围不能被子树参数改写
        self.assertEqual(data['scope_module_path'], '登录')

    def test_subtree_param_rejects_path_outside_review_scope(self):
        """课题只评审「登录」，不能借 module_path 越权读到「支付」"""
        self._login(self.member)
        resp = self.client.get(
            '/api/v1/topics/%d/review-mindmap?module_path=%s'
            % (self.t_open.id, '支付'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('超出', resp.get_json()['error'])

    def test_subtree_param_allowed_anywhere_for_whole_library_review(self):
        self._login(self.member)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap?module_path=%s'
            % (self.t_whole_lib.id, '支付')).get_json()
        self.assertEqual(data['text'], '支付')
        self.assertEqual(data['case_count'], 1)

    def test_whole_library_review_without_subtree_still_covers_all(self):
        """加了子树参数后，不传它的整库评审不能退化成只看未分类"""
        self._login(self.member)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % self.t_whole_lib.id).get_json()
        self.assertEqual(data['case_count'], 4)
        self.assertIsNone(data['subtree_module_path'])

    def test_review_case_detail_readable_without_project_access(self):
        """外部评审人没有用例库权限，也必须能看到用例步骤"""
        self._login(self.outsider)
        case_id = self.case_ids['手机号正常登录']
        case = TestCase.query.get(case_id)
        case.content = {'preconditions': '已注册',
                        'steps': ['输入手机号', '输入验证码'],
                        'expected_results': ['登录成功']}
        db.session.commit()

        resp = self.client.get('/api/v1/topics/%d/review-cases/%d'
                               % (self.t_open.id, case_id))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data['title'], '手机号正常登录')
        self.assertEqual(data['content']['steps'], ['输入手机号', '输入验证码'])

    def test_review_case_detail_rejects_out_of_scope_case(self):
        self._login(self.member)
        resp = self.client.get('/api/v1/topics/%d/review-cases/%d'
                               % (self.t_open.id, self.case_ids['微信支付成功']))
        self.assertEqual(resp.status_code, 403)

    def test_review_case_detail_hidden_from_non_viewer(self):
        """看不到课题的人也拿不到用例详情"""
        self._login(self.outsider)
        resp = self.client.get('/api/v1/topics/%d/review-cases/%d'
                               % (self.t_hub.id, self.case_ids['手机号正常登录']))
        self.assertEqual(resp.status_code, 403)

    # ---------------- 标记读写 ----------------

    def test_mark_roundtrip_shows_on_mindmap(self):
        self._login(self.member)
        case_id = self.case_ids['验证码超时']
        resp = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [
                {'node_id': 'case:%d' % case_id, 'mark': 'question',
                 'note': '缺少异常分支'},
                {'node_id': 'mod:登录/手机号', 'mark': 'risk'},
            ]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body['applied'], 2)
        self.assertEqual(body['skipped'], [])

        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % self.t_open.id).get_json()
        found = {}

        def walk(node):
            found[node['id']] = node
            for kid in node['children']:
                walk(kid)

        walk(data)
        self.assertEqual(found['case:%d' % case_id]['mark'], 'question')
        self.assertEqual(found['case:%d' % case_id]['icons'], ['P1', 'question'])
        self.assertEqual(found['mod:登录/手机号']['mark'], 'risk')

    def test_mark_is_idempotent_and_clearable(self):
        self._login(self.member)
        node = 'mod:登录/验证码'
        payload = {'marks': [{'node_id': node, 'mark': 'flag'}]}
        for _ in range(3):
            resp = self.client.put(
                '/api/v1/topics/%d/review-marks' % self.t_open.id, json=payload)
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            CaseReviewNodeMark.query.filter_by(topic_id=self.t_open.id).count(), 1)

        resp = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [{'node_id': node, 'mark': None}]})
        self.assertEqual(resp.get_json()['cleared'], 1)
        self.assertEqual(
            CaseReviewNodeMark.query.filter_by(topic_id=self.t_open.id).count(), 0)

    def test_out_of_scope_mark_is_skipped(self):
        """借目录评审去标范围外的用例必须被拒（最关键的安全断言）"""
        self._login(self.member)
        resp = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [
                {'node_id': 'case:%d' % self.case_ids['微信支付成功'],
                 'mark': 'question'},
                {'node_id': 'mod:支付', 'mark': 'risk'},
            ]})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body['applied'], 0)
        self.assertEqual([s['reason'] for s in body['skipped']],
                         ['OUT_OF_SCOPE', 'OUT_OF_SCOPE'])
        self.assertEqual(
            CaseReviewNodeMark.query.filter_by(topic_id=self.t_open.id).count(), 0)

    def test_public_all_reviewer_without_project_can_mark(self):
        """完全公开评审下，无项目权限的评审人也能打标记"""
        self._login(self.outsider)
        mindmap = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % self.t_open.id).get_json()
        self.assertTrue(mindmap['can_mark'])

        resp = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [{'node_id': 'mod:登录/手机号', 'mark': 'flag'}]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()['applied'], 1)

    def test_others_mark_not_overwritable_by_plain_reviewer(self):
        """普通评审人不能改他人标记；发起人可以"""
        self._login(self.outsider)
        self.client.put('/api/v1/topics/%d/review-marks' % self.t_open.id,
                        json={'marks': [{'node_id': 'mod:登录', 'mark': 'flag'}]})

        other = User(username='third', password_hash='x', role='user')
        db.session.add(other)
        db.session.commit()
        self._login(other)
        body = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [{'node_id': 'mod:登录', 'mark': 'risk'}]}).get_json()
        self.assertEqual([s['reason'] for s in body['skipped']], ['NOT_YOUR_MARK'])

        self._login(self.member)   # 评审发起人
        body = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [{'node_id': 'mod:登录', 'mark': 'risk'}]}).get_json()
        self.assertEqual(body['applied'], 1)
        self.assertEqual(body['skipped'], [])

    def test_bad_node_id_is_skipped_not_fatal(self):
        self._login(self.member)
        body = self.client.put(
            '/api/v1/topics/%d/review-marks' % self.t_open.id,
            json={'marks': [
                {'node_id': 'garbage', 'mark': 'flag'},
                {'node_id': 'case:abc', 'mark': 'flag'},
                {'node_id': 'mod:登录/验证码', 'mark': 'flag'},
            ]}).get_json()
        self.assertEqual(body['applied'], 1)
        self.assertEqual([s['reason'] for s in body['skipped']],
                         ['BAD_NODE_ID', 'BAD_CASE_ID'])

    def test_marks_list_endpoint(self):
        self._login(self.member)
        self.client.put('/api/v1/topics/%d/review-marks' % self.t_open.id,
                        json={'marks': [{'node_id': 'mod:登录', 'mark': 'flag'}]})
        data = self.client.get(
            '/api/v1/topics/%d/review-marks' % self.t_open.id).get_json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['node_id'], 'mod:登录')
        self.assertIn('flag', data['mark_legend'])

    # ---------------- 发起目录评审 ----------------

    def test_submit_module_review_creates_scoped_public_all_topic(self):
        """目录级发起评审：范围与可见性都要落到自动创建的课题上"""
        self._login(self.member)
        resp = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '登录/验证码',
                  'visibility': 'public_all', 'submit_note': '验证码专项'})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        review = resp.get_json()
        self.assertEqual(review['scope_type'], 'module')
        self.assertEqual(review['scope_module_path'], '登录/验证码')
        self.assertEqual(review['scope_case_count'], 1)

        topic = Topic.query.get(review['related_topic_id'])
        self.assertEqual(topic.visibility, 'public_all')
        self.assertEqual(topic.review_module_paths, ['登录/验证码'])

        # 无项目权限的评审人能直接读到这个新评审的脑图
        self._login(self.outsider)
        data = self.client.get(
            '/api/v1/topics/%d/review-mindmap' % topic.id).get_json()
        self.assertEqual(data['text'], '验证码')
        self.assertEqual(data['total_case_count'], 1)

    def test_parallel_reviews_on_different_directories(self):
        """不同目录可以并行评审，同一目录重复发起才 409"""
        self._login(self.member)
        first = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '登录/手机号'})
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))

        other_dir = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '支付'})
        self.assertEqual(other_dir.status_code, 201, other_dir.get_data(as_text=True))

        dup = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '登录/手机号'})
        self.assertEqual(dup.status_code, 409)
        self.assertIn('current_review_id', dup.get_json())

    def test_submit_review_rejects_empty_directory(self):
        self._login(self.member)
        resp = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '不存在的目录'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('目录不存在', resp.get_json()['error'])

    def test_submit_assigned_review_requires_grants(self):
        self._login(self.member)
        resp = self.client.post(
            '/api/v1/testcase-libraries/%d/reviews' % self.lib.id,
            json={'scope_type': 'module', 'scope_module_path': '支付',
                  'visibility': 'assigned'})
        self.assertEqual(resp.status_code, 400)

    # ---------------- 授权名单 ----------------

    def test_assigned_topic_visible_only_to_granted(self):
        topic = Topic(title='定向评审', content='c', board='case_review',
                      author_name='member', author_user_id=self.member.id,
                      visibility='assigned', project_name='Alpha',
                      review_library_id=self.lib.id,
                      review_module_paths=['登录'])
        db.session.add(topic)
        db.session.flush()
        db.session.add(TopicGrant(topic_id=topic.id, grant_type='user',
                                  target_user_id=self.outsider.id,
                                  target_name='outsider'))
        db.session.commit()

        self._login(self.outsider)
        self.assertEqual(
            self.client.get(
                '/api/v1/topics/%d/review-mindmap' % topic.id).status_code, 200)

        stranger = User(username='stranger', password_hash='x', role='user')
        db.session.add(stranger)
        db.session.commit()
        self._login(stranger)
        self.assertEqual(
            self.client.get(
                '/api/v1/topics/%d/review-mindmap' % topic.id).status_code, 403)


if __name__ == '__main__':
    unittest.main()
