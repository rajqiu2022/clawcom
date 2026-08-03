import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'panorama_graph.py'
_SPEC = importlib.util.spec_from_file_location('panorama_graph', _MODULE_PATH)
panorama_graph = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(panorama_graph)


class PanoramaGraphTest(unittest.TestCase):
    def test_build_graph_payload_combines_explicit_and_legacy_edges(self):
        modules = [
            {
                'id': 1,
                'name': 'Racing',
                'path': 'Racing',
                'parent_id': None,
                'risk_level': 'normal',
                'related_case_libraries': [],
                'extra': {},
            },
            {
                'id': 2,
                'name': 'Drift',
                'path': 'Racing/Drift',
                'parent_id': 1,
                'risk_level': 'high',
                'related_case_libraries': [],
                'extra': {'dependencies': [3]},
            },
            {
                'id': 3,
                'name': 'Physics',
                'path': 'Physics',
                'parent_id': None,
                'risk_level': 'critical',
                'related_case_libraries': [{'library_id': 7}],
                'extra': {'cross_module': {'affects': [{'id': 2, 'reason': 'speed coupling'}]}},
            },
            {
                'id': 4,
                'name': 'Shop',
                'path': 'Shop',
                'parent_id': None,
                'risk_level': 'low',
                'related_case_libraries': [],
                'extra': {},
            },
        ]
        relations = [
            {
                'id': 9,
                'source_module_id': 2,
                'target_module_id': 3,
                'relation_type': 'depends_on',
                'confidence': 0.9,
                'evidence': {'reason': 'explicit'},
                'source': 'agent',
            }
        ]

        graph = panorama_graph.build_graph_payload(modules, relations)

        edge_keys = {
            (e['source_module_id'], e['target_module_id'], e['relation_type'])
            for e in graph['edges']
        }
        self.assertIn((1, 2, 'child'), edge_keys)
        self.assertIn((2, 3, 'depends_on'), edge_keys)
        self.assertIn((3, 2, 'affects'), edge_keys)
        self.assertEqual(len([e for e in graph['edges'] if e['source_module_id'] == 2 and e['target_module_id'] == 3]), 1)
        self.assertEqual(graph['metrics']['hub_modules'][0]['id'], 2)
        self.assertEqual(graph['metrics']['isolated_modules'][0]['id'], 4)
        self.assertEqual(graph['metrics']['untested_high_risk_modules'][0]['id'], 2)


if __name__ == '__main__':
    unittest.main()
