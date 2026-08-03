import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'requirement_coverage.py'
_SPEC = importlib.util.spec_from_file_location('requirement_coverage', _MODULE_PATH)
requirement_coverage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(requirement_coverage)


class RequirementCoverageTest(unittest.TestCase):
    def test_summarizes_covered_partial_gap_and_outdated_links(self):
        requirements = [
            {'id': 1, 'tapd_story_id': 'S1', 'title': '手机号登录'},
            {'id': 2, 'tapd_story_id': 'S2', 'title': '验证码重试'},
            {'id': 3, 'tapd_story_id': 'S3', 'title': '风控锁定'},
            {'id': 4, 'tapd_story_id': 'S4', 'title': '旧需求'},
        ]
        links = [
            {'requirement_item_id': 1, 'coverage_status': 'covered'},
            {'requirement_item_id': 2, 'coverage_status': 'partial'},
            {'requirement_item_id': 3, 'coverage_status': 'gap'},
            {'requirement_item_id': 4, 'coverage_status': 'outdated'},
        ]
        result = requirement_coverage.summarize_requirement_coverage(
            requirements, links)
        self.assertEqual(result['total_requirements'], 4)
        self.assertEqual(result['covered_requirements'], 1)
        self.assertEqual(result['partial_requirements'], 1)
        self.assertEqual(result['gap_requirements'][0]['tapd_story_id'], 'S3')
        self.assertEqual(result['outdated_requirements'][0]['tapd_story_id'], 'S4')
        self.assertFalse(result['passed'])

    def test_requirement_without_link_is_a_gap(self):
        requirements = [
            {'id': 1, 'tapd_story_id': 'S1', 'title': '手机号登录'},
            {'id': 2, 'tapd_story_id': 'S2', 'title': '验证码重试'},
        ]
        links = [{'requirement_item_id': 1, 'coverage_status': 'covered'}]
        result = requirement_coverage.summarize_requirement_coverage(
            requirements, links)
        self.assertEqual(result['covered_requirements'], 1)
        self.assertEqual(result['gap_requirements'][0]['id'], 2)
        self.assertFalse(result['passed'])

    def test_all_covered_passes(self):
        requirements = [
            {'id': 1, 'tapd_story_id': 'S1', 'title': '手机号登录'},
        ]
        links = [{'requirement_item_id': 1, 'coverage_status': 'covered'}]
        result = requirement_coverage.summarize_requirement_coverage(
            requirements, links)
        self.assertTrue(result['passed'])
        self.assertEqual(result['coverage_rate'], 1.0)


if __name__ == '__main__':
    unittest.main()
