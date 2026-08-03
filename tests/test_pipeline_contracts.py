import importlib.util
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / 'web' / 'app' / 'services' / 'pipeline_contracts.py'
_SPEC = importlib.util.spec_from_file_location('pipeline_contracts', _MODULE_PATH)
pipeline_contracts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pipeline_contracts)


class PipelineContractsTest(unittest.TestCase):
    def test_requirement_review_contract_requires_all_items_reviewed(self):
        result = pipeline_contracts.evaluate_requirement_review_contract({
            'total_requirements': 3,
            'reviewed_requirements': 2,
            'high_risk_issues': [{'id': 1, 'status': 'open'}],
        })
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('需求评审结论不完整', result['reasons'][0])

    def test_requirement_review_contract_passes_with_complete_review(self):
        result = pipeline_contracts.evaluate_requirement_review_contract({
            'total_requirements': 3,
            'reviewed_requirements': 3,
            'high_risk_issues': [{'id': 1, 'status': 'open'}],
        })
        self.assertTrue(result['passed'])
        self.assertEqual(result['status'], 'passed')

    def test_engineering_contract_allows_unknown_but_requires_explicit_status(self):
        result = pipeline_contracts.evaluate_engineering_analysis_contract({
            'total_requirements': 2,
            'status_counts': {'implemented': 1, 'unknown': 1},
            'requirements_without_status': [],
        })
        self.assertTrue(result['passed'])

        missing = pipeline_contracts.evaluate_engineering_analysis_contract({
            'total_requirements': 2,
            'status_counts': {'implemented': 1},
            'requirements_without_status': ['ST-2'],
        })
        self.assertFalse(missing['passed'])
        self.assertIn('实现状态未评估', missing['reasons'][0])

    def test_case_design_contract_blocks_coverage_gaps(self):
        result = pipeline_contracts.evaluate_case_design_contract({
            'total_requirements': 4,
            'covered_requirements': 3,
            'gap_requirements': [{'id': 9, 'title': '短信失败重试'}],
        })
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('需求到用例覆盖缺口', result['reasons'][0])

    def test_review_contract_requires_independent_reviewer(self):
        result = pipeline_contracts.evaluate_independent_review_contract({
            'reviewer_claw_id': 5,
            'producer_claw_ids': [3, 5],
            'open_review_marks': [],
            'review_comments': 1,
        })
        self.assertFalse(result['passed'])
        self.assertIn('不能评审自己的产出', result['reasons'][0])


if __name__ == '__main__':
    unittest.main()
