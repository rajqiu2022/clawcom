import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'panorama_impact.py'
_SPEC = importlib.util.spec_from_file_location('panorama_impact', _MODULE_PATH)
panorama_impact = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(panorama_impact)


class PanoramaImpactTest(unittest.TestCase):
    def test_analyze_changed_files_matches_code_links_and_relations(self):
        modules = [
            {
                'id': 1,
                'name': 'Drift',
                'path': 'Racing/Drift',
                'risk_level': 'high',
                'code_paths': ['Assets/Scripts/Racing/Drift/'],
                'related_case_libraries': [{'library_id': 10, 'library_name': 'Drift Cases'}],
                'test_focus': '漂移手感和碰撞边界',
            },
            {
                'id': 2,
                'name': 'Physics',
                'path': 'Physics',
                'risk_level': 'critical',
                'code_paths': [],
                'related_case_libraries': [{'library_id': 11, 'library_name': 'Physics Cases'}],
                'test_focus': '物理结算一致性',
            },
            {
                'id': 3,
                'name': 'Shop',
                'path': 'Shop',
                'risk_level': 'low',
                'code_paths': [],
                'related_case_libraries': [],
            },
        ]
        entities = [
            {
                'id': 100,
                'file_path': 'Assets/Scripts/Racing/Drift/DriftController.cs',
                'entity_type': 'class',
                'symbol_name': 'DriftController',
            },
            {
                'id': 101,
                'file_path': 'Assets/Scripts/Shop/ShopController.cs',
                'entity_type': 'class',
                'symbol_name': 'ShopController',
            },
        ]
        links = [
            {'module_id': 1, 'entity_id': 100, 'link_type': 'owns', 'confidence': 0.95}
        ]
        relations = [
            {'source_module_id': 1, 'target_module_id': 2, 'relation_type': 'depends_on', 'confidence': 0.8}
        ]

        result = panorama_impact.analyze_changed_files(
            ['Assets/Scripts/Racing/Drift/DriftController.cs'],
            modules=modules,
            code_entities=entities,
            module_code_links=links,
            relations=relations,
        )

        affected = {m['module_id']: m for m in result['affected_modules']}
        self.assertEqual(affected[1]['impact_type'], 'direct')
        self.assertEqual(affected[2]['impact_type'], 'related')
        self.assertNotIn(3, affected)
        self.assertEqual(result['risk_level'], 'critical')
        self.assertEqual([c['library_id'] for c in result['recommended_case_libraries']], [10, 11])
        self.assertLess(result['token_savings']['selected_context_files'], result['token_savings']['full_context_files'])


if __name__ == '__main__':
    unittest.main()
