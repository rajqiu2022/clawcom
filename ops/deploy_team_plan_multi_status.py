"""Deploy multi-select team task status filtering without schema changes."""

import deploy_team_plan_status_and_wake as deployment


release = deployment.release
release.BASE = '8ef117a3e82a9dc24527a3dd7f808e4a73a2c7ba'
release.FILES = (
    'app/api/agent_teams.py',
    'app/services/agent_team_plans.py',
    'static/css/agent_team_plans.css',
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
    combined = client.get(path + query + '&task_status=blocked&task_status=completed')
    assert combined.status_code == 200, combined.status_code
    assert combined.json['task_statuses'] == ['blocked', 'completed']
    assert all(t['status'] in ('blocked', 'failed', 'analysis_incomplete', 'completed')
               for plan in combined.json['items'] for t in plan['tasks'])
    unfiltered = client.get(path + query)
    assert unfiltered.status_code == 200 and unfiltered.json['task_statuses'] == []
    invalid = client.get(path + '?task_status=all&task_status=blocked')
    assert invalid.status_code == 400, invalid.status_code
    page = client.get('/agent-teams')
    assert page.status_code == 200
    assert '20260928multistatus' in page.get_data(as_text=True)
    script = client.get('/static/js/agent_team_plans.js')
    assert script.status_code == 200
    assert 'taskStatuses.map(status=>' in script.get_data(as_text=True)
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
        assert '20260928multistatus' in live.text
        live_script = requests.get(
            'http://127.0.0.1:18800/static/js/agent_team_plans.js',
            timeout=10)
        assert live_script.status_code == 200
        assert 'taskStatuses.map(status=>' in live_script.text
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'multi_status': True, 'page_cache_key': True,
        'business_rows_modified': 0, 'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
