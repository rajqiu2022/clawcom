"""评审脑图标记的范围锁定测试。

这是"完全公开评审"的安全边界：外部评审人虽然能看该评审的脑图，
但既不能读到范围外的节点，也不能给范围外的用例打标记。
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

from app.api.topics import _in_review_scope, _parse_node_id  # noqa: E402
from app.models import CaseReviewNodeMark  # noqa: E402


class ParseNodeIdTest(unittest.TestCase):
    def test_parses_case_node(self):
        self.assertEqual(_parse_node_id('case:123'), ('case', '123'))

    def test_parses_module_node(self):
        self.assertEqual(_parse_node_id('mod:登录模块/手机号'),
                         ('module', '登录模块/手机号'))

    def test_parses_root_module_node(self):
        self.assertEqual(_parse_node_id('mod:'), ('module', ''))

    def test_rejects_unknown_prefix(self):
        for bad in ('', None, 'case', 'node:1', '123', 'CASE:1'):
            self.assertEqual(_parse_node_id(bad), (None, None))


class LibraryScopeTest(unittest.TestCase):
    """整库评审：任何路径都在范围内。"""

    def test_everything_is_in_scope(self):
        for path in ('', '登录模块', '登录模块/手机号登录', '支付模块'):
            self.assertTrue(_in_review_scope(path, '', scoped=False))


class ModuleScopeTest(unittest.TestCase):
    """目录评审：只有该子树在范围内。"""

    def test_root_itself_is_in_scope(self):
        self.assertTrue(_in_review_scope('登录模块', '登录模块', scoped=True))

    def test_descendants_are_in_scope(self):
        self.assertTrue(_in_review_scope('登录模块/手机号登录', '登录模块', scoped=True))
        self.assertTrue(_in_review_scope('登录模块/手机号登录/异常', '登录模块', scoped=True))

    def test_siblings_are_out_of_scope(self):
        self.assertFalse(_in_review_scope('支付模块', '登录模块', scoped=True))

    def test_parent_is_out_of_scope(self):
        self.assertFalse(_in_review_scope('登录模块', '登录模块/手机号登录', scoped=True))

    def test_prefix_lookalike_directory_is_out_of_scope(self):
        self.assertFalse(_in_review_scope('登录模块附加', '登录模块', scoped=True))

    def test_unclassified_is_out_of_module_scope(self):
        self.assertFalse(_in_review_scope('', '登录模块', scoped=True))

    def test_paths_are_normalized_before_comparison(self):
        self.assertTrue(_in_review_scope('/登录模块/手机号登录/', '登录模块', scoped=True))


class UnclassifiedScopeTest(unittest.TestCase):
    """范围锁定为"(未分类)"根目录时，只有空路径在范围内。"""

    def test_only_empty_path_is_in_scope(self):
        self.assertTrue(_in_review_scope('', '', scoped=True))

    def test_named_directories_are_out_of_scope(self):
        self.assertFalse(_in_review_scope('登录模块', '', scoped=True))


class NodeKeyHashTest(unittest.TestCase):
    def test_hash_is_fixed_length_regardless_of_path_length(self):
        short = CaseReviewNodeMark.compute_key_hash('module', 'a')
        long = CaseReviewNodeMark.compute_key_hash('module', 'a' * 500)
        self.assertEqual(len(short), 64)
        self.assertEqual(len(long), 64)

    def test_different_node_types_do_not_collide(self):
        self.assertNotEqual(
            CaseReviewNodeMark.compute_key_hash('module', '1'),
            CaseReviewNodeMark.compute_key_hash('case', '1'),
        )

    def test_same_input_is_stable(self):
        self.assertEqual(
            CaseReviewNodeMark.compute_key_hash('module', '登录模块'),
            CaseReviewNodeMark.compute_key_hash('module', '登录模块'),
        )

    def test_hash_is_populated_on_construction(self):
        row = CaseReviewNodeMark(topic_id=1, node_type='case', node_key='42',
                                 mark='question')
        self.assertEqual(row.node_key_hash,
                         CaseReviewNodeMark.compute_key_hash('case', '42'))
        self.assertEqual(row.node_id, 'case:42')


if __name__ == '__main__':
    unittest.main()
