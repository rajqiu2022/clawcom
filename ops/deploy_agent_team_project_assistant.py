"""Deploy the Agent Team project-assistant role without changing business rows."""
import deploy_team_activity_knowledge as deployment
import re


release = deployment.release
release.BASE = '349c7b11330e3026f775ba979bf98997a76ae7bd'
release.FILES = (
    'app/api/agent_teams.py',
    'app/services/agent_context_snapshots.py',
    'app/services/agent_team_activity.py',
    'app/services/agent_team_context.py',
    'app/services/agent_team_permissions.py',
    'app/services/agent_teams.py',
    'templates/agent_teams.html',
    'static/js/agent_team_activity.js',
    'static/js/agent_teams.js',
)
release.MIGRATIONS = ()

_candidate = release.candidate


def _project_assistant_candidate(path, live):
    """Preserve independently deployed Team page bundle versions."""
    if path != 'templates/agent_teams.html' or live is None:
        return _candidate(path, live)
    source = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    replacements = (
        (
            '每个团队各有一名在任测试经理；代码分析员和测试执行员可配置多名。',
            '每个团队各有一名在任测试经理；项目助理、代码分析员和测试执行员可配置多名。',
        ),
        (
            '团队勾选的 Flow 自动授予经理调度、代码分析员和测试执行员执行权限；',
            '团队勾选的 Flow 自动授予经理调度、项目助理及其他成员执行权限；',
        ),
        (
            '<div class="at-member-actions"><button type="button" id="at-add-analyst"',
            '<div class="at-member-actions"><button type="button" id="at-add-assistant" class="btn btn-secondary btn-sm">＋ 项目助理</button><button type="button" id="at-add-analyst"',
        ),
        (
            '执行员至少选择一项二级角色，可同时负责编辑器、手机包和客户端性能。',
            '项目助理负责版本数据收集与团队信息整理，不代替测试经理调度或验收；执行员至少选择一项二级角色。',
        ),
    )
    text = source.decode('utf-8')
    for old, new in replacements:
        if new in text:
            continue
        if text.count(old) != 1:
            raise RuntimeError(
                'Expected one project-assistant template anchor: ' + old)
        text = text.replace(old, new, 1)
    for bundle in ('agent_team_activity', 'agent_teams'):
        pattern = r"(filename='js/%s\.js'\) }}\?v=)[^\"<]+" % bundle
        text, count = re.subn(
            pattern, r'\g<1>20260924projectassistant', text, count=1)
        if count != 1:
            raise RuntimeError('Expected one script reference: ' + bundle)
    return text.encode('utf-8')


release.candidate = _project_assistant_candidate

release.SCHEMA = r'''
print('TEAM_RELEASE ' + json.dumps({
    'migration_required': False,
    'business_rows_modified': 0,
}))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, AgentTeam, User, WorkflowRun
from app.services.agent_context_snapshots import ROLE_CONTRACTS
from app.services.agent_team_activity import TASK_TYPES
from app.services.agent_teams import TEAM_ROLES
import requests, time

app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    agent_task_count = AgentTask.query.count()
    workflow_run_count = WorkflowRun.query.count()
    assert TEAM_ROLES['project_assistant'] == '项目助理'
    contract = ROLE_CONTRACTS['project_assistant']
    assert '版本' in contract['purpose']
    assert any('调度' in item for item in contract['forbidden'])
    assert 'version_data' in TASK_TYPES

    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    roles = client.get('/api/v1/agent-teams/roles?project_id=6')
    assert roles.status_code == 200, roles.status_code
    assert roles.get_json()['roles']['project_assistant'] == '项目助理'
    page = client.get('/agent-teams')
    assert page.status_code == 200
    assert b'at-add-assistant' in page.data
    teams = AgentTeam.query.filter_by(project_id=6).all()
    for team in teams:
        response = client.get(
            '/api/v1/agent-teams/%s/members/activity' % team.id)
        assert response.status_code == 200
        assert 'version_data' in response.get_json()['report_contract']['task_types']
    db.session.remove()

    http_checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {
            'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session')
            + '=' + cookie}
        for attempt in range(20):
            try:
                response = requests.get(
                    'http://127.0.0.1:18800/agent-teams', headers=headers,
                    timeout=5, allow_redirects=False)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        assert 'at-add-assistant' in response.text
        paths = (
            '/static/js/agent_teams.js',
            '/static/js/agent_team_activity.js',
            '/api/v1/agent-teams/roles?project_id=6',
        )
        for path in paths:
            response = requests.get(
                'http://127.0.0.1:18800' + path, headers=headers,
                timeout=15, allow_redirects=False)
            assert response.status_code == 200, (path, response.status_code)
            http_checks[path] = response.status_code
        assert 'project_assistant' in requests.get(
            'http://127.0.0.1:18800/static/js/agent_teams.js',
            timeout=15).text

    assert AgentTask.query.count() == agent_task_count
    assert WorkflowRun.query.count() == workflow_run_count
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'project_assistant_role': True,
        'team_count': len(teams),
        'http_checks': http_checks,
        'business_rows_modified': 0,
        'agent_tasks_started': 0,
        'workflow_runs_started': 0,
        'workers_modified': False,
        'deepflow_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
