import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'testcase_panorama_links.py'
_SPEC = importlib.util.spec_from_file_location('testcase_panorama_links', _MODULE_PATH)
testcase_panorama_links = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(testcase_panorama_links)


class PanoramaTestMetricsTest(unittest.TestCase):
    def test_agent_payload_cannot_override_system_case_counts(self):
        existing = {
            'direct_case_count': 3,
            'subtree_case_count': 7,
            'linked_library_count': 2,
            'linked_directory_count': 1,
            'bug_count': 1,
            'bug_risk_score': 20,
            'bug_risk_level': 'low',
        }
        payload = {
            'direct_case_count': 99,
            'subtree_case_count': 88,
            'linked_library_count': 77,
            'linked_directory_count': 66,
            'bug_count': 4,
            'bug_risk_score': 80,
            'bug_risk_level': 'high',
            'source': 'agent',
        }

        filtered = testcase_panorama_links.agent_test_metric_payload(existing, payload)

        self.assertEqual(filtered['direct_case_count'], 3)
        self.assertEqual(filtered['subtree_case_count'], 7)
        self.assertEqual(filtered['linked_library_count'], 2)
        self.assertEqual(filtered['linked_directory_count'], 1)
        self.assertEqual(filtered['bug_count'], 4)
        self.assertEqual(filtered['bug_risk_score'], 80)
        self.assertEqual(filtered['bug_risk_level'], 'high')
        self.assertEqual(filtered['source'], 'agent')

    def test_agent_payload_defaults_case_counts_to_zero_without_existing_metric(self):
        filtered = testcase_panorama_links.agent_test_metric_payload(None, {
            'subtree_case_count': 8,
            'bug_count': 2,
        })

        self.assertEqual(filtered['direct_case_count'], 0)
        self.assertEqual(filtered['subtree_case_count'], 0)
        self.assertEqual(filtered['linked_library_count'], 0)
        self.assertEqual(filtered['linked_directory_count'], 0)
        self.assertEqual(filtered['bug_count'], 2)


if __name__ == '__main__':
    unittest.main()
