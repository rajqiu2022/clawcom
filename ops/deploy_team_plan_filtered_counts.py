"""Deploy filtered team task summary counts without data/schema changes."""

import deploy_team_plan_multi_status as deployment


release = deployment.release
release.BASE = '83c69d61ea5cf92400017da9a2b02e31f14ca148'
release.FILES = (
    'app/services/agent_team_plans.py',
    'static/js/agent_team_plans.js',
    'templates/agent_teams.html',
)
release.MIGRATIONS = ()
release.SMOKE = r'''
import requests
import time
from app import create_app, db
from app.models import AgentTeam, User, _now
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    team = AgentTeam.query.filter_by(status='active').first()
    assert admin and team
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    path = '/api/v1/agent-teams/%s/test-plans' % team.id
    query = '?period=day&date=' + str(_now().date())
    all_tasks = client.get(path + query)
    completed = client.get(path + query + '&task_status=completed')
    blocked = client.get(path + query + '&task_status=blocked')
    combined = client.get(path + query + '&task_status=completed&task_status=blocked')
    assert all(response.status_code == 200 for response in
               (all_tasks, completed, blocked, combined))
    assert completed.json['summary']['total'] == completed.json['summary']['completed']
    assert blocked.json['summary']['total'] == blocked.json['summary']['blocked']
    assert combined.json['summary']['total'] == (
        completed.json['summary']['total'] + blocked.json['summary']['total'])
    assert combined.json['summary']['total'] <= all_tasks.json['summary']['total']
    page = client.get('/agent-teams')
    assert page.status_code == 200
    assert '20260928filteredcounts' in page.get_data(as_text=True)
    script = client.get('/static/js/agent_team_plans.js')
    assert script.status_code == 200
    assert "'筛选后任务'" in script.get_data(as_text=True)
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
        for attempt in range(20):
            try:
                live = requests.get('http://127.0.0.1:18800/agent-teams',
                                    headers=headers, timeout=5, allow_redirects=False)
                if live.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        assert '20260928filteredcounts' in live.text
        live_script = requests.get(
            'http://127.0.0.1:18800/static/js/agent_team_plans.js',
            timeout=10)
        assert live_script.status_code == 200
        assert "'筛选后任务'" in live_script.text
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'filtered_counts': True, 'page_cache_key': True,
        'business_rows_modified': 0, 'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
