import unittest
import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'testcase_panorama_links.py'
_SPEC = importlib.util.spec_from_file_location('testcase_panorama_links', _MODULE_PATH)
testcase_panorama_links = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(testcase_panorama_links)

aggregate_module_test_metrics = testcase_panorama_links.aggregate_module_test_metrics
collect_descendant_module_ids = testcase_panorama_links.collect_descendant_module_ids
detect_orphan_links = testcase_panorama_links.detect_orphan_links
link_case_count = testcase_panorama_links.link_case_count


class TestCasePanoramaLinkServiceTest(unittest.TestCase):
    def test_collect_descendant_module_ids_includes_root_and_children(self):
        modules = [
            {'id': 1, 'parent_id': None},
            {'id': 2, 'parent_id': 1},
            {'id': 3, 'parent_id': 2},
            {'id': 4, 'parent_id': 1},
            {'id': 9, 'parent_id': None},
        ]

        self.assertEqual(collect_descendant_module_ids(modules, 1), {1, 2, 3, 4})

    def test_link_case_count_supports_library_and_directory_levels(self):
        cases = [
            {'id': 1, 'library_id': 10, 'module_path': '战斗/连招', 'is_placeholder': False},
            {'id': 2, 'library_id': 10, 'module_path': '战斗/连招/异常', 'is_placeholder': False},
            {'id': 3, 'library_id': 10, 'module_path': '战斗/移动', 'is_placeholder': False},
            {'id': 4, 'library_id': 10, 'module_path': '战斗/连招', 'is_placeholder': True},
            {'id': 5, 'library_id': 11, 'module_path': '战斗/连招', 'is_placeholder': False},
        ]

        self.assertEqual(link_case_count({'library_id': 10, 'link_level': 'library'}, cases), 3)
        self.assertEqual(
            link_case_count(
                {'library_id': 10, 'link_level': 'directory', 'module_path': '战斗/连招'},
                cases,
            ),
            2,
        )

    def test_aggregate_metrics_counts_subtree_links_without_deduping_duplicate_libraries(self):
        modules = [
            {'id': 1, 'project_id': 6, 'workspace_id': 20, 'parent_id': None},
            {'id': 2, 'project_id': 6, 'workspace_id': 20, 'parent_id': 1},
            {'id': 3, 'project_id': 6, 'workspace_id': 20, 'parent_id': 1},
        ]
        links = [
            {'module_id': 1, 'library_id': 10, 'module_path': '', 'link_level': 'library', 'case_count': 5},
            {'module_id': 2, 'library_id': 10, 'module_path': '战斗/连招', 'link_level': 'directory', 'case_count': 3},
            {'module_id': 3, 'library_id': 11, 'module_path': '战斗/连招', 'link_level': 'directory', 'case_count': 3},
        ]

        metrics = aggregate_module_test_metrics(modules, links)

        self.assertEqual(metrics[1]['direct_case_count'], 5)
        self.assertEqual(metrics[1]['subtree_case_count'], 11)
        self.assertEqual(metrics[1]['linked_library_count'], 2)
        self.assertEqual(metrics[2]['subtree_case_count'], 3)

    def test_detect_orphan_links_flags_missing_targets_and_empty_directories(self):
        links = [
            {'id': 1, 'module_id': 1, 'library_id': 10, 'link_level': 'library'},
            {'id': 2, 'module_id': 99, 'library_id': 10, 'link_level': 'library'},
            {'id': 3, 'module_id': 1, 'library_id': 99, 'link_level': 'library'},
            {'id': 4, 'module_id': 1, 'library_id': 10, 'link_level': 'directory', 'module_path': '空目录'},
        ]
        cases = [{'id': 1, 'library_id': 10, 'module_path': '有效目录', 'is_placeholder': False}]

        orphans = detect_orphan_links(
            links,
            module_ids={1},
            library_ids={10},
            cases=cases,
        )

        self.assertEqual(
            {item['id']: item['reason'] for item in orphans},
            {2: 'module_deleted', 3: 'library_deleted', 4: 'directory_empty'},
        )


if __name__ == '__main__':
    unittest.main()
