"""Deploy the Skills project selector without schema or business data writes."""
import deploy_agent_teams as release


release.BASE = 'c8bfac5'
release.FILES = ('templates/skills.html',)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    assert 'skills' in inspect(db.engine).get_table_names()
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'business_rows_modified': 0, 'backup': backup,
    }))
'''
release.SMOKE = r'''
import requests, time
from app import create_app, db
from app.models import User
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    page = client.get('/skills')
    assert page.status_code == 200
    markers = ('id="filter-project"', 'onSkillProjectChange()',
               'initProjectIdFilter(select, allProjects, renderSkills)',
               'const sorted = filterSkillsForProject(filtered)',
               'filterSkillsForProject(results).length === 0')
    assert all(marker in page.get_data(as_text=True) for marker in markers)
    with open('/opt/openclaw-web/static/js/api.js') as stream:
        assert 'function initProjectIdFilter(' in stream.read()
    assert client.get('/api/v1/projects').status_code == 200
    assert client.get('/api/v1/skills?summary=true').status_code == 200
    live = os.getcwd() == '/opt/openclaw-web'
    if live:
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin.id})
        headers = {'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/skills', headers=headers, timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP service unavailable')
        assert all(marker in response.text for marker in markers)
        shared_script = requests.get('http://127.0.0.1:18800/static/js/api.js', timeout=10)
        assert shared_script.status_code == 200
        assert 'function initProjectIdFilter(' in shared_script.text
    print('TEAM_RELEASE ' + json.dumps({
        'smoke':'passed', 'live_http':live, 'project_selector':True,
        'business_rows_modified':0, 'workflow_runs_started':0, 'workers_modified':False,
    }))
'''


if __name__ == '__main__':
    release.main()
