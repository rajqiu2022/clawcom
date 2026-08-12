import unittest
from pathlib import Path


class WorkflowFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).resolve().parents[1]
            / 'web' / 'templates' / 'workflows.html'
        ).read_text(encoding='utf-8')

    def test_run_card_long_title_stays_inside_sidebar(self):
        self.assertIn(
            '.wf-run-head { display:grid;grid-template-columns:minmax(0,1fr) auto;',
            self.template,
        )
        self.assertIn('overflow-wrap:anywhere', self.template)
        self.assertIn('class="wf-run-title"', self.template)
        self.assertIn('class="wf-run-actions"', self.template)
        self.assertIn('white-space:nowrap', self.template)

    def test_code_analysis_node_has_configurator_and_visual_badge(self):
        self.assertIn('wf-analysis-badge', self.template)
        self.assertIn('configureAnalysisNode', self.template)
        self.assertIn('基线变量映射（JSON 对象）', self.template)
        self.assertIn("method: 'PATCH'", self.template)


if __name__ == '__main__':
    unittest.main()
