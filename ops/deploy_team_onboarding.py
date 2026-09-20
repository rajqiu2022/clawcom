"""Deploy only Team join notifications, using the hardened Team release wrapper."""
import deploy_team_activity_knowledge as deployment

release = deployment.release
release.BASE = '97ea71a'
release.FILES = ('app/api/agent_teams.py', 'app/services/agent_team_onboarding.py')
release.MIGRATIONS = ()
release.SCHEMA = r'''
print('TEAM_RELEASE ' + json.dumps({'migration_required':False,'business_rows_modified':0}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import User
from app.services.agent_team_onboarding import TEAM_SKILLS, queue_missing_join_notifications
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin_id})
    headers = {'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
    db.session.remove()
    paths = ['/agent-teams','/workflows','/test-reports','/knowledge',
             '/api/v1/agent-teams?project_id=6','/api/v1/skills/220','/api/v1/skills/221']
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin_id
    for path in paths:
        response = client.get(path)
        db.session.remove()
        assert response.status_code == 200, (path,response.status_code)
    checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/agent-teams', headers=headers, timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        for path in paths:
            response = requests.get('http://127.0.0.1:18800'+path, headers=headers, timeout=15, allow_redirects=False)
            assert response.status_code == 200, (path,response.status_code)
            checks[path] = response.status_code
        assert requests.get('http://127.0.0.1:18800/api/v1/agent-teams?project_id=6',timeout=15).status_code == 401
    print('TEAM_RELEASE '+json.dumps({'smoke':'passed','http_checks':checks,
        'business_rows_modified':0,'workers_modified':False,'runs_started':0}))
'''

if __name__ == '__main__':
    deployment.main()
