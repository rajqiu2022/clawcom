"""Template + client contracts, in addition to local Chrome interaction checks."""
import unittest
from pathlib import Path
from flask import Flask, render_template


ROOT = Path(__file__).resolve().parents[1]


class AgentTeamsUiTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / 'web/templates/agent_teams.html').read_text(encoding='utf-8')
        self.js = (ROOT / 'web/static/js/agent_teams.js').read_text(encoding='utf-8')
        self.css = (ROOT / 'web/static/css/agent_teams.css').read_text(encoding='utf-8')

    def test_real_template_renders_with_existing_base(self):
        app = Flask(__name__, template_folder=str(ROOT / 'web/templates'))
        with app.test_request_context():
            rendered = render_template('agent_teams.html', hub_public_url='', hub_web_url='')
        self.assertIn('一个项目，多支协作团队', rendered)
        self.assertIn('id="at-project"', rendered)
        self.assertIn('aria-labelledby="at-editor-title"', rendered)

    def test_navigation_and_all_three_roles_are_wired(self):
        base = (ROOT / 'web/templates/base.html').read_text(encoding='utf-8')
        views = (ROOT / 'web/app/views/__init__.py').read_text(encoding='utf-8')
        self.assertIn('href="/agent-teams"', base)
        self.assertIn("@views_bp.route('/agent-teams')", views)
        self.assertIn('id="acl-teams-link"', (ROOT / 'web/templates/automation_closed_loop.html').read_text(encoding='utf-8'))
        for key in ('test_manager', 'code_analyst', 'test_executor'):
            self.assertIn(key, self.js)
        self.assertIn('executor_specialties', self.js)

    def test_versioning_safe_rendering_and_no_scheduler_side_effects(self):
        for marker in ('expected_version', 'TEAM_VERSION_CONFLICT', 'AGENT_TEAMS_DISABLED',
                       'loadEpoch', 'missionEpoch', 'can_manage', 'esc(team.name)', 'esc(m.objective)'):
            self.assertIn(marker, self.js)
        for forbidden in ('setInterval(', '/dispatch', '/manager-lease', "API.post('/workflow-missions'"):
            self.assertNotIn(forbidden, self.js)

    def test_accessible_modal_and_responsive_scroll_containers(self):
        self.assertIn('<dialog', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('type="number" min="1" max="100"', self.html)
        self.assertIn('@media(max-width:720px)', self.css)
        self.assertIn('overflow:auto', self.css)
        self.assertIn('.at-shell [hidden]', self.css)

    def test_member_activity_is_read_only_safe_and_refreshes_while_visible(self):
        js = (ROOT / 'web/static/js/agent_team_activity.js').read_text(encoding='utf-8')
        for marker in ('document.hidden', 'detailEpoch', 'epoch', 'clearTimeout(timer)',
                       'esc(m.name)', 'esc(task.title)', 'esc(task.progress_message)',
                       'setTimeout(refresh, 15000)'):
            self.assertIn(marker, js)
        for forbidden in ('API.post', 'API.put', 'API.delete', '/dispatch', 'setInterval('):
            self.assertNotIn(forbidden, js)
        self.assertIn('AgentTeamActivity.reset()', self.js)
        self.assertIn('aria-labelledby="at-activity-title"', self.html)
        self.assertIn('at-agent-card', js)
        self.assertIn('data-history', js)
        self.assertIn('.at-activity-dialog[open]', self.css)


if __name__ == '__main__':
    unittest.main()
