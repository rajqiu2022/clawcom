"""目录级评审范围的带库契约测试。

重点验证三件事：
1. `_normalize_review_scope` 对入参的校验（含"未分类"空路径这种边界）
2. `_scope_case_filter` 的前缀匹配是整棵子树，且默认排除目录占位用例
3. 防重复从"库级"下沉到"库 + 范围"，不同目录可以并行评审
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
from app.api.testcases import (_normalize_review_scope,  # noqa: E402
                               _scope_case_filter)
from app.models import (TestCase, TestCaseLibrary,  # noqa: E402
                        TestCaseLibraryReview)


class NormalizeReviewScopeTest(unittest.TestCase):
    def test_defaults_to_library_scope(self):
        scope_type, path, err = _normalize_review_scope({})
        self.assertEqual((scope_type, path, err), ('library', '', None))

    def test_module_scope_requires_explicit_path_field(self):
        scope_type, path, err = _normalize_review_scope({'scope_type': 'module'})
        self.assertIsNone(scope_type)
        self.assertIn('scope_module_path', err)

    def test_module_scope_accepts_empty_path_as_unclassified(self):
        scope_type, path, err = _normalize_review_scope({
            'scope_type': 'module', 'scope_module_path': ''})
        self.assertEqual((scope_type, path, err), ('module', '', None))

    def test_strips_surrounding_slashes(self):
        _, path, err = _normalize_review_scope({
            'scope_type': 'module', 'scope_module_path': '/登录模块/手机号/'})
        self.assertIsNone(err)
        self.assertEqual(path, '登录模块/手机号')

    def test_rejects_unknown_scope_type(self):
        scope_type, _, err = _normalize_review_scope({'scope_type': 'cases'})
        self.assertIsNone(scope_type)
        self.assertIn('scope_type', err)

    def test_rejects_overlong_path(self):
        scope_type, _, err = _normalize_review_scope({
            'scope_type': 'module', 'scope_module_path': 'a' * 501})
        self.assertIsNone(scope_type)
        self.assertIn('超长', err)


class ScopeCaseFilterTest(unittest.TestCase):
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

        self.lib = TestCaseLibrary(name='Lib', owner='owner_user')
        db.session.add(self.lib)
        db.session.flush()

        def add(title, module_path, placeholder=False, priority='P2'):
            db.session.add(TestCase(
                library_id=self.lib.id, title=title, module_path=module_path,
                is_placeholder=placeholder, priority=priority))

        add('未分类用例', '')
        add('登录-主流程', '登录模块')
        add('登录-手机号', '登录模块/手机号登录')
        add('登录-手机号-异常', '登录模块/手机号登录/异常')
        add('[目录] 空子目录', '登录模块/扫码登录', placeholder=True)
        add('支付-退款', '支付模块/退款')
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _titles(self, scope_type, path, include_placeholders=False):
        rows = _scope_case_filter(self.lib.id, scope_type, path,
                                  include_placeholders=include_placeholders).all()
        return {r.title for r in rows}

    def test_library_scope_covers_everything_except_placeholders(self):
        self.assertEqual(self._titles('library', ''), {
            '未分类用例', '登录-主流程', '登录-手机号',
            '登录-手机号-异常', '支付-退款',
        })

    def test_library_scope_can_include_placeholders(self):
        self.assertIn('[目录] 空子目录',
                      self._titles('library', '', include_placeholders=True))

    def test_module_scope_covers_whole_subtree(self):
        self.assertEqual(self._titles('module', '登录模块'), {
            '登录-主流程', '登录-手机号', '登录-手机号-异常',
        })

    def test_nested_module_scope_covers_its_own_subtree(self):
        self.assertEqual(self._titles('module', '登录模块/手机号登录'), {
            '登录-手机号', '登录-手机号-异常',
        })

    def test_module_scope_does_not_leak_sibling_directories(self):
        self.assertNotIn('支付-退款', self._titles('module', '登录模块'))

    def test_prefix_match_does_not_match_partial_directory_name(self):
        db.session.add(TestCase(library_id=self.lib.id, title='登录模块附加',
                                module_path='登录模块附加'))
        db.session.commit()
        self.assertNotIn('登录模块附加', self._titles('module', '登录模块'))

    def test_empty_module_path_means_unclassified_only(self):
        self.assertEqual(self._titles('module', ''), {'未分类用例'})

    def test_empty_directory_has_only_placeholder(self):
        self.assertEqual(self._titles('module', '登录模块/扫码登录'), set())
        self.assertEqual(
            self._titles('module', '登录模块/扫码登录', include_placeholders=True),
            {'[目录] 空子目录'})


class ParallelDirectoryReviewTest(unittest.TestCase):
    """同一库的不同目录应能并行评审，同一目录不行。"""

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
        self.lib = TestCaseLibrary(name='Lib', owner='owner_user')
        db.session.add(self.lib)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _submit(self, scope_type, path):
        review = TestCaseLibraryReview(
            library_id=self.lib.id, status='submitted',
            scope_type=scope_type, scope_module_path=path)
        db.session.add(review)
        db.session.commit()
        return review

    def _find_dup(self, scope_type, path):
        return TestCaseLibraryReview.query.filter_by(
            library_id=self.lib.id, status='submitted',
            scope_type=scope_type, scope_module_path=path).first()

    def test_same_directory_is_detected_as_duplicate(self):
        self._submit('module', '登录模块')
        self.assertIsNotNone(self._find_dup('module', '登录模块'))

    def test_different_directories_do_not_collide(self):
        self._submit('module', '登录模块')
        self.assertIsNone(self._find_dup('module', '支付模块'))

    def test_library_scope_is_independent_of_module_scope(self):
        self._submit('module', '登录模块')
        self.assertIsNone(self._find_dup('library', ''))

    def test_withdrawn_review_frees_the_scope(self):
        review = self._submit('module', '登录模块')
        review.status = 'withdrawn'
        db.session.commit()
        self.assertIsNone(self._find_dup('module', '登录模块'))


if __name__ == '__main__':
    unittest.main()
