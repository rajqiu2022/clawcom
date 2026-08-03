import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'panorama_snapshot.py'
_SPEC = importlib.util.spec_from_file_location('panorama_snapshot', _MODULE_PATH)
panorama_snapshot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(panorama_snapshot)


class PanoramaWorkspaceArchiveTest(unittest.TestCase):
    def test_snapshot_ids_to_prune_keeps_recent_ten(self):
        snapshots = [{'id': i} for i in range(1, 12)]

        self.assertEqual(
            panorama_snapshot.snapshot_ids_to_prune(snapshots, keep=10),
            [1],
        )

    def test_plan_restore_items_rebuilds_paths_and_relations(self):
        items = [
            {
                'item_type': 'module',
                'item_key': 'module:Root',
                'payload': {'name': 'Root', 'path': 'Root', 'risk_level': 'normal'},
            },
            {
                'item_type': 'module',
                'item_key': 'module:Root/Child',
                'payload': {
                    'name': 'Child',
                    'path': 'Root/Child',
                    'parent_path': 'Root',
                    'risk_level': 'high',
                },
            },
            {
                'item_type': 'relation',
                'item_key': 'relation:Root/Child->Root:depends_on',
                'payload': {
                    'source_path': 'Root/Child',
                    'target_path': 'Root',
                    'relation_type': 'depends_on',
                    'confidence': 0.8,
                    'evidence': {'reason': 'regression'},
                },
            },
        ]

        plan = panorama_snapshot.plan_restore_items(items)

        self.assertEqual([m['path'] for m in plan['modules']], ['Root', 'Root/Child'])
        self.assertEqual(plan['modules'][1]['parent_path'], 'Root')
        self.assertEqual(plan['relations'][0]['source_path'], 'Root/Child')
        self.assertEqual(plan['relations'][0]['target_path'], 'Root')

    def test_snapshot_items_include_code_entities_and_links(self):
        items = panorama_snapshot.build_snapshot_items(
            modules=[{'id': 1, 'path': 'Root', 'name': 'Root'}],
            relations=[],
            code_entities=[{
                'id': 9,
                'repo_key': 'main',
                'file_path': 'src/root.py',
                'entity_type': 'file',
                'symbol_name': '',
                'start_line': 0,
            }],
            module_code_links=[{
                'module_id': 1,
                'entity_id': 9,
                'link_type': 'owns',
                'confidence': 0.7,
            }],
            module_path_by_id={1: 'Root'},
        )

        plan = panorama_snapshot.plan_restore_items(items)

        self.assertEqual(plan['code_entities'][0]['file_path'], 'src/root.py')
        self.assertEqual(plan['code_links'][0]['module_path'], 'Root')
        self.assertEqual(plan['code_links'][0]['entity_key'], plan['code_entities'][0]['entity_key'])


if __name__ == '__main__':
    unittest.main()
