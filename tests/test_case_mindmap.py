import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


_MODULE_PATH = (Path(__file__).resolve().parents[1]
                / 'web' / 'app' / 'services' / 'case_mindmap.py')
_SPEC = importlib.util.spec_from_file_location('case_mindmap', _MODULE_PATH)
case_mindmap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(case_mindmap)

build_directory_mindmap = case_mindmap.build_directory_mindmap
normalize_mark = case_mindmap.normalize_mark
collect_node_keys = case_mindmap.collect_node_keys


def case(cid, title, module_path, priority='P2', placeholder=False):
    return SimpleNamespace(id=cid, case_id='TC_%d' % cid, title=title,
                           module_path=module_path, priority=priority,
                           is_placeholder=placeholder)


SAMPLE = [
    case(1, '未分类用例', ''),
    case(2, '登录-主流程', '登录模块', priority='P0'),
    case(3, '登录-手机号', '登录模块/手机号登录', priority='P1'),
    case(4, '登录-手机号-异常', '登录模块/手机号登录/异常', priority='P2'),
    case(5, '[目录] 扫码登录', '登录模块/扫码登录', placeholder=True),
    case(6, '支付-退款', '支付模块/退款', priority='P0'),
]


def find(node, node_id):
    if node['id'] == node_id:
        return node
    for child in node.get('children') or []:
        hit = find(child, node_id)
        if hit:
            return hit
    return None


def child_texts(node):
    return [c['text'] for c in node['children']]


class NormalizeMarkTest(unittest.TestCase):
    def test_valid_marks(self):
        for value in ('question', 'risk', 'flag'):
            self.assertEqual(normalize_mark(value), value)

    def test_invalid_marks_become_none(self):
        for value in ('', None, 'blocker', 'QUESTION', 0):
            self.assertIsNone(normalize_mark(value))


class DirectoryStructureTest(unittest.TestCase):
    def test_builds_real_directory_hierarchy(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        self.assertEqual(root['node_type'], 'module')
        self.assertEqual(root['text'], 'Lib')
        # 目录排在用例叶子之前；目录之间按 Unicode 码点稳定排序
        # （中文拼音序由前端 localeCompare 负责，与侧边栏目录树保持一致）
        self.assertEqual(child_texts(root), ['支付模块', '登录模块', '未分类用例'])

        login = find(root, 'mod:登录模块')
        self.assertEqual(child_texts(login), ['手机号登录', '扫码登录', '登录-主流程'])

    def test_placeholder_creates_empty_directory_without_leaf(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        scan = find(root, 'mod:登录模块/扫码登录')
        self.assertIsNotNone(scan)
        self.assertEqual(scan['children'], [])
        self.assertEqual(scan['case_count'], 0)

    def test_case_count_accumulates_up_the_ancestor_chain(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        self.assertEqual(find(root, 'mod:登录模块/手机号登录/异常')['case_count'], 1)
        self.assertEqual(find(root, 'mod:登录模块/手机号登录')['case_count'], 2)
        self.assertEqual(find(root, 'mod:登录模块')['case_count'], 3)
        self.assertEqual(root['case_count'], 5)
        self.assertEqual(root['total_case_count'], 5)

    def test_case_leaves_carry_priority_icon(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        leaf = find(root, 'case:2')
        self.assertEqual(leaf['node_type'], 'case')
        self.assertEqual(leaf['priority'], 'P0')
        self.assertEqual(leaf['icons'], ['P0'])
        self.assertEqual(leaf['case_id'], 2)

    def test_unknown_priority_yields_no_priority_icon(self):
        root = build_directory_mindmap('Lib', [case(9, 'x', '', priority='P9')])
        self.assertIsNone(find(root, 'case:9')['priority'])
        self.assertEqual(find(root, 'case:9')['icons'], [])


class ScopedRootTest(unittest.TestCase):
    def test_starting_from_subdirectory_only_returns_subtree(self):
        root = build_directory_mindmap('登录模块', SAMPLE,
                                       root_module_path='登录模块')
        self.assertEqual(root['module_path'], '登录模块')
        self.assertEqual(child_texts(root), ['手机号登录', '扫码登录', '登录-主流程'])
        self.assertIsNone(find(root, 'case:6'))
        self.assertIsNone(find(root, 'case:1'))
        self.assertEqual(root['case_count'], 3)

    def test_deep_subdirectory_root(self):
        root = build_directory_mindmap('手机号登录', SAMPLE,
                                       root_module_path='登录模块/手机号登录')
        self.assertEqual(child_texts(root), ['异常', '登录-手机号'])
        self.assertEqual(root['case_count'], 2)

    def test_sibling_prefix_directory_is_not_included(self):
        cases = SAMPLE + [case(7, '同名前缀用例', '登录模块附加')]
        root = build_directory_mindmap('登录模块', cases,
                                       root_module_path='登录模块')
        self.assertIsNone(find(root, 'case:7'))

    def test_root_path_is_normalized(self):
        root = build_directory_mindmap('登录模块', SAMPLE,
                                       root_module_path='/登录模块/')
        self.assertEqual(root['case_count'], 3)


class MarkTest(unittest.TestCase):
    def test_case_and_module_marks_are_applied(self):
        marks = {'case:3': 'question', 'mod:支付模块': 'flag'}
        root = build_directory_mindmap('Lib', SAMPLE, marks=marks)

        leaf = find(root, 'case:3')
        self.assertEqual(leaf['mark'], 'question')
        self.assertEqual(leaf['icons'], ['P1', 'question'])

        pay = find(root, 'mod:支付模块')
        self.assertEqual(pay['mark'], 'flag')
        self.assertEqual(pay['icons'], ['flag'])

    def test_invalid_mark_is_dropped(self):
        root = build_directory_mindmap('Lib', SAMPLE,
                                       marks={'case:3': 'not-a-mark'})
        self.assertIsNone(find(root, 'case:3')['mark'])

    def test_unmarked_nodes_have_no_mark(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        self.assertIsNone(find(root, 'case:2')['mark'])
        self.assertIsNone(find(root, 'mod:登录模块')['mark'])

    def test_legend_is_exposed_for_frontend(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        self.assertEqual(set(root['mark_legend']), {'question', 'risk', 'flag'})
        self.assertEqual(root['mark_legend']['flag']['icon'], '🚩')


class TruncationTest(unittest.TestCase):
    """截断只能砍用例叶子，不能砍目录结构、不能砍计数。

    这一组是线上事故的回归测试：早期版本目录与叶子共用一个预算，
    深度优先把预算耗在第一个顶层目录的子树里，导致 2311 条用例的库
    **丢掉一个顶层目录**、根节点 case_count 只有 627（真实值的 1/4）。
    """

    def test_large_library_truncates_leaves_only(self):
        cases = [case(i, 'case-%d' % i, 'dir%d' % (i % 20)) for i in range(500)]
        root = build_directory_mindmap('Lib', cases, max_nodes=50)
        self.assertTrue(root['truncated'])
        self.assertEqual(root['shown_case_count'], 50)
        # 20 个目录 + 1 个根，全都要在，不受叶子预算影响
        self.assertEqual(root['module_count'], 21)
        self.assertEqual(len(root['children']), 20)

    def test_small_library_is_not_truncated(self):
        root = build_directory_mindmap('Lib', SAMPLE)
        self.assertFalse(root['truncated'])

    def test_case_count_stays_accurate_when_truncated(self):
        cases = [case(i, 'case-%d' % i, 'dir') for i in range(100)]
        root = build_directory_mindmap('Lib', cases, max_nodes=10)
        self.assertTrue(root['truncated'])
        self.assertEqual(root['total_case_count'], 100)
        self.assertEqual(root['case_count'], 100)

    def test_no_top_level_directory_is_lost_when_truncated(self):
        """顶层目录一个都不能少 —— 丢目录会让用户以为库里没这块内容"""
        cases = []
        for top in ('A类', 'B类', 'C类', '系统类'):
            for i in range(200):
                cases.append(case(len(cases), 'c%d' % len(cases),
                                  '%s/子模块%d/末级%d' % (top, i % 5, i % 3)))
        root = build_directory_mindmap('Lib', cases, max_nodes=100)

        self.assertTrue(root['truncated'])
        self.assertEqual(sorted(c['text'] for c in root['children']),
                         ['A类', 'B类', 'C类', '系统类'])

    def test_all_directory_counts_accurate_when_truncated(self):
        """每一级目录的 case_count 都必须等于真实用例数，与预算无关"""
        cases = []
        for top in ('A类', 'B类'):
            for i in range(150):
                cases.append(case(len(cases), 'c%d' % len(cases),
                                  '%s/子模块%d' % (top, i % 3)))
        root = build_directory_mindmap('Lib', cases, max_nodes=20)

        self.assertTrue(root['truncated'])
        self.assertEqual(root['case_count'], 300)
        self.assertEqual(root['total_case_count'], 300)
        for top_node in root['children']:
            self.assertEqual(top_node['case_count'], 150, top_node['text'])
            self.assertEqual(sum(k['case_count'] for k in top_node['children']
                                 if k['node_type'] == 'module'), 150)

    def test_truncation_is_independent_of_directory_order(self):
        """不管哪个目录先被遍历到，结果的目录集合与计数都一致"""
        def build(order):
            cases = []
            for top in order:
                for i in range(120):
                    cases.append(case(len(cases), 'c%d' % len(cases),
                                      '%s/sub%d' % (top, i % 4)))
            return build_directory_mindmap('Lib', cases, max_nodes=30)

        a = build(['X', 'Y', 'Z'])
        b = build(['Z', 'Y', 'X'])
        self.assertEqual(sorted(c['text'] for c in a['children']),
                         sorted(c['text'] for c in b['children']))
        self.assertEqual(a['case_count'], b['case_count'])
        self.assertEqual({c['text']: c['case_count'] for c in a['children']},
                         {c['text']: c['case_count'] for c in b['children']})


if __name__ == '__main__':
    unittest.main()
