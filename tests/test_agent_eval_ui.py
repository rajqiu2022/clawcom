import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


class AgentEvalUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            _ROOT / 'web' / 'templates' / 'agent_eval.html'
        ).read_text(encoding='utf-8')
        cls.base = (
            _ROOT / 'web' / 'templates' / 'base.html'
        ).read_text(encoding='utf-8')
        cls.views = (
            _ROOT / 'web' / 'app' / 'views' / '__init__.py'
        ).read_text(encoding='utf-8')

    def test_route_navigation_and_feature_flag_are_wired(self):
        self.assertIn("@views_bp.route('/agent-eval')", self.views)
        self.assertIn("'agent_eval.html'", self.views)
        self.assertIn('AGENT_TEAM_CONTRACTS_ENABLED', self.views)
        self.assertIn('href="/agent-eval"', self.base)
        self.assertIn('{% block nav_agent_eval %}', self.base)

    def test_ui_supports_project_filters_dual_review_and_run_readback(self):
        markers = (
            'id="ev-project"', 'id="ev-role"', 'id="ev-split"',
            'id="ev-review"', '/agent-eval/datasets?',
            '/human-review', '/freeze', '/agent-eval/runs?',
            'contract_quality', 'evidence_quality',
            'difficulty_calibration', 'calibration_required',
        )
        for marker in markers:
            self.assertIn(marker, self.template)
        self.assertIn('function evEsc', self.template)
        self.assertIn('EVAL_ENABLED', self.template)
        self.assertIn('if(!d.can_review)', self.template)

    def test_layout_has_responsive_workbench_and_scrollable_evidence(self):
        self.assertIn('grid-template-columns:minmax(270px,.68fr)', self.template)
        self.assertIn('@media(max-width:820px)', self.template)
        self.assertIn('max-height:190px;overflow:auto', self.template)
        self.assertIn('aria-label=', self.template)


if __name__ == '__main__':
    unittest.main()
