import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'panorama_snapshot.py'
_SPEC = importlib.util.spec_from_file_location('panorama_snapshot', _MODULE_PATH)
panorama_snapshot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(panorama_snapshot)


class PanoramaSnapshotTest(unittest.TestCase):
    def test_build_snapshot_items_from_modules_and_relations(self):
        items = panorama_snapshot.build_snapshot_items(
            modules=[
                {'id': 1, 'path': 'Racing/Drift', 'name': 'Drift', 'risk_level': 'high', 'status': 'active'},
                {'id': 2, 'path': 'Physics', 'name': 'Physics', 'risk_level': 'critical', 'status': 'active'},
            ],
            relations=[
                {
                    'source_module_id': 1,
                    'target_module_id': 2,
                    'relation_type': 'depends_on',
                    'confidence': 0.9,
                },
            ],
            module_path_by_id={1: 'Racing/Drift', 2: 'Physics'},
        )
        keys = {item['item_key'] for item in items}
        self.assertIn('module:Racing/Drift', keys)
        self.assertIn('module:Physics', keys)
        self.assertIn('relation:Racing/Drift->Physics:depends_on', keys)

    def test_diff_snapshots_detects_module_and_relation_changes(self):
        from_items = panorama_snapshot.build_snapshot_items(
            modules=[
                {'id': 1, 'path': 'Racing/Drift', 'name': 'Drift', 'risk_level': 'high', 'status': 'active'},
                {'id': 2, 'path': 'Physics', 'name': 'Physics', 'risk_level': 'normal', 'status': 'active'},
            ],
            relations=[],
            module_path_by_id={1: 'Racing/Drift', 2: 'Physics'},
        )
        to_items = panorama_snapshot.build_snapshot_items(
            modules=[
                {'id': 1, 'path': 'Racing/Drift', 'name': 'Drift', 'risk_level': 'critical', 'status': 'active'},
                {'id': 3, 'path': 'Shop', 'name': 'Shop', 'risk_level': 'low', 'status': 'active'},
            ],
            relations=[
                {
                    'source_module_id': 1,
                    'target_module_id': 3,
                    'relation_type': 'affects',
                    'confidence': 0.8,
                },
            ],
            module_path_by_id={1: 'Racing/Drift', 3: 'Shop'},
        )

        diff = panorama_snapshot.diff_snapshot_items(from_items, to_items)
        self.assertEqual(diff['added_modules'][0]['path'], 'Shop')
        self.assertEqual(diff['removed_modules'][0]['path'], 'Physics')
        self.assertEqual(diff['changed_modules'][0]['path'], 'Racing/Drift')
        self.assertEqual(diff['changed_modules'][0]['changes']['risk_level'], {'from': 'high', 'to': 'critical'})
        self.assertEqual(diff['added_relations'][0]['relation_type'], 'affects')
        self.assertEqual(diff['summary']['module_added'], 1)
        self.assertEqual(diff['summary']['risk_escalations'], 1)


if __name__ == '__main__':
    unittest.main()
