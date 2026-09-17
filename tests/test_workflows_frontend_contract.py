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

    def test_project_filter_controls_definitions_favorites_runs_and_url(self):
        self.assertIn('id="wf-project-filter"', self.template)
        self.assertIn('changeWorkflowProject(this.value)', self.template)
        self.assertIn("params.project_id = selectedWorkflowProjectId", self.template)
        self.assertIn("await loadWorkflowProjects()", self.template)
        self.assertIn("syncWorkflowProjectUrl(true)", self.template)
        self.assertIn(
            "pagedPath('/workflow-definitions', state, workflowProjectQuery())",
            self.template)
        self.assertIn(
            "pagedPath('/workflow-runs', state, workflowProjectQuery())",
            self.template)

    def test_project_filter_options_keep_readable_theme_colors(self):
        self.assertIn('.wf-project-filter .form-select option {', self.template)
        self.assertIn('background:var(--bg-card)', self.template)
        self.assertIn('color:var(--text-primary)', self.template)

    def test_single_selected_agent_is_submitted_as_bound_worker(self):
        self.assertIn('executorClawIds.length === 1', self.template)
        self.assertIn(
            'payload.worker_claw_id = executorClawIds[0]', self.template)

    def test_step_outputs_are_condensed_and_open_a_formatted_modal(self):
        self.assertIn('class="wf-output-preview-wrap"', self.template)
        self.assertIn('renderWorkflowOutputPreview(step.outputs, step.step_id)', self.template)
        self.assertIn('id="wf-output-modal"', self.template)
        self.assertIn('function openWorkflowOutput(stepId)', self.template)
        self.assertIn('highlightWorkflowJson(output.value)', self.template)
        self.assertIn("marked.parse(text || '', {breaks:true, gfm:true})", self.template)
        self.assertIn('DOMPurify.sanitize(rendered)', self.template)


if __name__ == '__main__':
    unittest.main()
