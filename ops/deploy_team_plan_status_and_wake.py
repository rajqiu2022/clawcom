"""Release the team task status filter and isolated Plan wake scan."""

import deploy_agent_teams as release


release.BASE = '0458c4b999862dfb29e04c7bcb4b79b544b5b9cf'
release.FILES = (
    'app/api/agent_teams.py',
    'app/services/agent_tasks.py',
    'app/services/agent_team_plans.py',
    'app/services/plan_supervision.py',
    'static/css/agent_team_plans.css',
    'static/js/agent_team_plans.js',
    'templates/agent_teams.html',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'agent_teams', 'test_plans', 'test_tasks',
            'test_task_occurrences', 'plan_supervisors'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'business_rows_modified': 0, 'backup': backup,
    }))
'''
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
    valid = client.get(path + '?period=day&date=' + str(_now().date()) +
                       '&task_status=blocked&limit=6&offset=0')
    assert valid.status_code == 200, valid.status_code
    assert valid.json['task_status'] == 'blocked'
    assert all(t['status'] in ('blocked', 'failed', 'analysis_incomplete')
               for plan in valid.json['items'] for t in plan['tasks'])
    invalid = client.get(path + '?task_status=not-a-status')
    assert invalid.status_code == 400, invalid.status_code
    page = client.get('/agent-teams')
    assert page.status_code == 200
    assert '20260928statusfilter' in page.get_data(as_text=True)
    script = client.get('/static/js/agent_team_plans.js')
    assert script.status_code == 200
    assert 'task_status=' in script.get_data(as_text=True)
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
        assert '20260928statusfilter' in live.text
        live_script = requests.get(
            'http://127.0.0.1:18800/static/js/agent_team_plans.js',
            timeout=10)
        assert live_script.status_code == 200 and 'task_status=' in live_script.text
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'status_filter': True, 'page_cache_key': True,
        'business_rows_modified': 0, 'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
