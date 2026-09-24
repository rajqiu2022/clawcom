"""Deploy Agent Team resource ID badges; no schema or business-row writes."""

import deploy_team_activity_knowledge as deployment
import re


release = deployment.release
release.BASE = '4af73995afa52ff1c6abebaea3092c17986223ce'
release.FILES = (
    'templates/agent_teams.html',
    'static/css/agent_teams.css',
    'static/js/agent_team_resources.js',
)
release.MIGRATIONS = ()


def _resource_id_candidate(path, live):
    """Apply only the ID badge delta while preserving live Team hotfixes."""
    if live is None:
        raise RuntimeError('Missing deployed Team resource file: ' + path)
    source = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    text = source.decode('utf-8')
    if path == 'templates/agent_teams.html':
        patterns = (
            (r"(filename='css/agent_teams\.css'\) }}\?v=)[^\"<]+",
             r'\g<1>20260924resourceids'),
            (r"(filename='js/agent_team_resources\.js'\) }}\?v=)[^\"<]+",
             r'\g<1>20260924resourceids'),
        )
        for pattern, replacement in patterns:
            text, count = re.subn(pattern, replacement, text, count=1)
            if count != 1:
                raise RuntimeError('Expected one Team bundle reference: ' + pattern)
        return text.encode('utf-8')
    if path == 'static/js/agent_team_resources.js':
        if 'class="at-resource-id"' in text:
            return source
        anchor = "${type === 'knowledge' ? 'KNOWLEDGE' : 'SKILL'}</span>"
        replacement = ("${type === 'knowledge' ? 'KNOWLEDGE' : 'SKILL'} "
                       "<b class=\"at-resource-id\">#${esc(item.id)}</b></span>")
        if text.count(anchor) != 1:
            raise RuntimeError('Expected one resource-kind card anchor')
        return text.replace(anchor, replacement, 1).encode('utf-8')
    if path == 'static/css/agent_teams.css':
        if '.at-resource-id{' in text:
            return source
        rule = ('.at-resource-kind{display:inline-flex;align-items:center;gap:7px}'
                '.at-resource-id{padding:2px 5px;border:1px solid '
                'color-mix(in srgb,var(--at-accent) 38%,transparent);'
                'border-radius:4px;background:color-mix(in srgb,'
                'var(--at-accent) 8%,transparent);font-size:9px;letter-spacing:0}')
        return (text.rstrip() + '\n' + rule + '\n').encode('utf-8')
    raise RuntimeError('Unexpected release path: ' + path)


release.candidate = _resource_id_candidate

release.SCHEMA = r'''
print('TEAM_RELEASE ' + json.dumps({
    'migration_required': False,
    'business_rows_modified': 0,
}))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, AgentTeam, User, WorkflowRun
import requests, time

app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    team = AgentTeam.query.filter_by(project_id=6).first()
    assert admin and team
    agent_task_count = AgentTask.query.count()
    workflow_run_count = WorkflowRun.query.count()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    page = client.get('/agent-teams')
    assert page.status_code == 200
    assert b'20260924resourceids' in page.data
    manifest = client.get(
        '/api/v1/agent-teams/%s/shared-resources' % team.id)
    assert manifest.status_code == 200
    payload = manifest.get_json()
    assert all(isinstance(item.get('id'), int)
               for kind in ('knowledge', 'skills')
               for item in payload[kind])

    http_checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session')
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
        assert '20260924resourceids' in response.text
        checks = {
            '/static/js/agent_team_resources.js': 'at-resource-id',
            '/static/css/agent_teams.css': '.at-resource-id',
        }
        for path, marker in checks.items():
            response = requests.get(
                'http://127.0.0.1:18800' + path, timeout=15,
                allow_redirects=False)
            assert response.status_code == 200, (path, response.status_code)
            assert marker in response.text, path
            http_checks[path] = response.status_code

    assert AgentTask.query.count() == agent_task_count
    assert WorkflowRun.query.count() == workflow_run_count
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'team_id': team.id,
        'resource_ids_visible': True,
        'http_checks': http_checks,
        'business_rows_modified': 0,
        'agent_tasks_started': 0,
        'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
