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

    def test_navigation_and_all_four_roles_are_wired(self):
        base = (ROOT / 'web/templates/base.html').read_text(encoding='utf-8')
        views = (ROOT / 'web/app/views/__init__.py').read_text(encoding='utf-8')
        self.assertIn('href="/agent-teams"', base)
        self.assertIn("@views_bp.route('/agent-teams')", views)
        self.assertIn('id="acl-teams-link"', (ROOT / 'web/templates/automation_closed_loop.html').read_text(encoding='utf-8'))
        for key in ('test_manager', 'project_assistant', 'code_analyst', 'test_executor'):
            self.assertIn(key, self.js)
        self.assertIn('executor_specialties', self.js)
        self.assertIn('id="at-add-assistant"', self.html)
        self.assertIn('版本数据收集', (
            ROOT / 'web/static/js/agent_team_activity.js').read_text(encoding='utf-8'))

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

    def test_team_directory_defaults_to_thumbnail_rail_and_can_expand(self):
        for marker in ('id="at-layout"', 'id="at-directory"',
                       'id="at-directory-toggle"', 'directory-collapsed',
                       'aria-expanded="false"'):
            self.assertIn(marker, self.html)
        for marker in ('directoryCollapsed: true', 'setDirectoryCollapsed',
                       "$('at-directory-toggle').addEventListener",
                       'at-team-thumbnail', 'at-team-summary'):
            self.assertIn(marker, self.js)
        for marker in ('.at-layout.directory-collapsed',
                       '.at-directory.is-collapsed',
                       '.at-team-thumbnail', '.at-directory-toggle'):
            self.assertIn(marker, self.css)
        self.assertIn('20260925teamrail', self.html)

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

    def test_plan_tabs_authoring_escaping_and_deep_links(self):
        js = (ROOT / 'web/static/js/agent_team_plans.js').read_text(encoding='utf-8')
        plans = (ROOT / 'web/templates/testplans.html').read_text(encoding='utf-8')
        for marker in ('role="tablist"', 'aria-controls="at-members-panel"', 'id="at-plans-panel"'):
            self.assertIn(marker, self.js)
        for marker in ('data-period', 'period=','esc(p.name)', 'esc(t.name)', 'generation!==epoch',
                       'can_manage', "status:'draft'", 'ArrowLeft', 'requestId',
                       '监管未启动', '测试经理已被移出', 'Mission #',
                       '任务 #${esc(t.id)}', 'at-plan-task-id', 'at-plan-report',
                       'data-plan-reports', 'p.report_count', 'source_ref_type=${sourceType}',
                       'data-task-reports', 't.report_count', 'sourceType', 'test_task',
                       'linked_test_report_id', 'setReportsFullscreen', 'is-fullscreen',
                       'Promise.all', 'renderReportContent', 'data-report-format',
                       '/html-preview', 'data-plan-task-toggle', '查看全部',
                       'data-plan-extra-task', 'aria-expanded',
                       'at-task-details', '查看详情', '收起详情',
                       '/conclusion', 'saveTaskConclusion',
                       'at-task-references', 't.references'):
            self.assertIn(marker, js)
        plan_css = (ROOT / 'web/static/css/agent_team_plans.css').read_text(encoding='utf-8')
        self.assertIn('.at-plan-task-id', plan_css)
        self.assertIn('.at-task-report', plan_css)
        self.assertIn('.at-task-details', plan_css)
        self.assertIn('.at-plan-reports-dialog.is-fullscreen', plan_css)
        self.assertIn('.at-plan-report', plan_css)
        self.assertIn('20260925taskdetails', self.html)
        self.assertIn('id="at-task-conclusion"', self.html)
        self.assertIn('不会生成测试报告', self.html)
        self.assertIn('/conclusion', plans)
        self.assertIn('saveTaskConclusion', plans)
        for marker in ('执行参考资料', 'task-reference-options',
                       'reference_skill_ids', 'reference_knowledge_ids',
                       'reference_report_ids', 'renderTaskReferencePicker'):
            self.assertIn(marker, plans)
        for forbidden in ('/dispatch', '/supervision/start', 'setInterval('):
            self.assertNotIn(forbidden, js)
        self.assertIn('aria-labelledby="at-plan-dialog-title"', self.html)
        self.assertIn("params.get('plan_id')", plans)
        self.assertIn('async function showLinkedPlan', plans)
        self.assertIn('id="at-plan-reports-dialog"', self.html)
        self.assertIn('id="at-plan-reports-fullscreen"', self.html)
        self.assertIn('vendor/marked.min.js', self.html)
        self.assertIn('vendor/purify.min.js', self.html)
        self.assertIn('未归属迭代', plans)
        views = (ROOT / 'web/app/views/__init__.py').read_text(encoding='utf-8')
        service = (ROOT / 'web/app/services/agent_team_plans.py').read_text(encoding='utf-8')
        self.assertIn("@views_bp.route('/testplans')", views)
        self.assertIn("'url': '/testplans?plan_id=%s'", service)

    def test_team_knowledge_and_skill_shelves_are_on_demand(self):
        resources = (ROOT / 'web/static/js/agent_team_resources.js').read_text(encoding='utf-8')
        for marker in ('共享知识库', '共享 Skills', 'at-knowledge-panel', 'at-skills-panel'):
            self.assertIn(marker, self.js)
        for marker in ('/shared-resources', 'pull_url', '复制拉取地址',
                       'Agent 按需拉取最新版本', 'data-unlink-resource',
                       'at-resource-id', '#${esc(item.id)}'):
            self.assertIn(marker, resources)
        css = (ROOT / 'web/static/css/agent_teams.css').read_text(encoding='utf-8')
        self.assertIn('.at-resource-id', css)
        self.assertIn('id="at-resource-dialog"', self.html)
        self.assertIn('agent_team_resources.js', self.html)
        self.assertIn('20260924resourceids', self.html)
        self.assertNotIn('template_content', resources)

    def test_fixed_team_chat_is_mention_driven_and_does_not_dispatch_work(self):
        chat = (ROOT / 'web/static/js/agent_team_chat.js').read_text(encoding='utf-8')
        plans = (ROOT / 'web/static/js/agent_team_plans.js').read_text(encoding='utf-8')
        for marker in ('at-tab-chat', 'at-chat-panel', 'AgentTeamChat.mount(team)'):
            self.assertIn(marker, self.js)
        for marker in ('agent_team_chat.js', '20260926structuredjson'):
            self.assertIn(marker, self.html)
        for marker in ('mention_claw_ids', 'mention_all', 'Idempotency-Key',
                       'expected_count', 'replied_count', 'setTimeout',
                       'image_ids', 'FormData', 'at-chat-image-preview',
                       "addEventListener('paste'", 'clipboardData',
                       'Ctrl+V 粘贴图片'):
            self.assertIn(marker, chat)
        for marker in ('id="at-chat-side"', 'at-chat-member-mention', 'at-chat-mention-all',
                       "$('at-chat-side').addEventListener('click'",
                       "focus({preventScroll:true})"):
            self.assertIn(marker, chat)
        self.assertNotIn('id="at-chat-mentions"', chat)
        for marker in ('at-chat-lightbox', 'data-chat-image', 'showModal()', 'data-chat-lightbox-close'):
            self.assertIn(marker, chat)
        for marker in ('renderMessageContent', 'at-chat-structured',
                       'data-chat-copy-structured', 'presentation.fields',
                       'renderMemoryOps'):
            self.assertIn(marker, chat)
        self.assertNotIn('target="_blank" rel="noopener"><img', chat)
        self.assertIn("['members','chat','plans','knowledge','skills']", plans)
        for forbidden in ('/dispatch', '/workflow-runs', '/send-to-claw', 'setInterval('):
            self.assertNotIn(forbidden, chat)
        self.assertIn('.at-chat-layout', self.css)
        self.assertIn('.at-chat-images', self.css)
        self.assertIn('.at-chat-structured', self.css)
        self.assertIn('.at-chat-lightbox::backdrop', self.css)


if __name__ == '__main__':
    unittest.main()
