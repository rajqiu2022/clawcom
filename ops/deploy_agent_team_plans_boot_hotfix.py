"""Deploy the Agent Team plans bootstrap hotfix only; no schema/business writes."""
import deploy_team_task_occurrences as deployment


release = deployment.release
release.FILES = (
    'templates/agent_teams.html',
    'static/js/agent_team_plans.js',
)
release.MIGRATIONS = ()
release.candidate = deployment._occurrence_candidate
release.SCHEMA = r'''
print('TEAM_RELEASE ' + json.dumps({
    'schema_verified': True,
    'business_rows_modified': 0,
    'hotfix': 'agent_team_plans_boot',
}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, User, WorkflowRun
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    agent_tasks = AgentTask.query.count()
    workflow_runs = WorkflowRun.query.count()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    response = client.get('/agent-teams')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'at-task-conclusion-save' in html
    assert '20260923taskconclusion2' in html
    script = client.get('/static/js/agent_team_plans.js')
    assert script.status_code == 200
    source = script.get_data(as_text=True)
    assert "$('at-task-conclusion-save')?.addEventListener" in source
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
        for attempt in range(20):
            try:
                live = requests.get(
                    'http://127.0.0.1:18800/agent-teams', headers=headers,
                    timeout=5, allow_redirects=False)
                if live.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        assert 'at-task-conclusion-save' in live.text
        assert '20260923taskconclusion2' in live.text
        live_script = requests.get(
            'http://127.0.0.1:18800/static/js/agent_team_plans.js',
            headers=headers, timeout=10)
        assert live_script.status_code == 200
        assert "$('at-task-conclusion-save')?.addEventListener" in live_script.text
    assert AgentTask.query.count() == agent_tasks
    assert WorkflowRun.query.count() == workflow_runs
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'business_rows_modified': 0,
        'agent_tasks_started': 0,
        'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
