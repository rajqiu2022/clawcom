"""Deploy the scoped Agent Team direct-collaboration Hub candidate."""

import deploy_agent_teams as release


release.BASE = '5b462e7d20620f2a75a3108c561229bc2b335a49'
release.FILES = (
    'app/api/agent_teams.py',
    'app/services/agent_context_snapshots.py',
    'app/services/agent_system_context.py',
    'app/services/agent_team_activity.py',
    'app/services/agent_team_context.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'agent_teams', 'agent_team_member_statuses',
            'agent_team_member_reports'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'business_rows_modified': 0, 'backup': backup,
    }))
'''
release.SMOKE = r'''
import requests
import time
from app import create_app, db
from app.models import AgentTeam, User
from app.services.agent_team_context import COLLABORATION_MODEL, team_snapshot
from app.services.plan_supervision import team_capability
app = create_app('production')
with app.app_context():
    assert COLLABORATION_MODEL['mode'] == 'agent_direct'
    team = AgentTeam.query.filter_by(id=1).first()
    assert team and team.status == 'active'
    assert team_capability(team.id)['ordinary_team_work_requires_supervisor'] is False
    snapshot = team_snapshot(team, team.primary_manager_claw_id, {})
    assert snapshot['collaboration_model']['ordinary_work_requires_test_task'] is False
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    assert client.get('/agent-teams').status_code == 200
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/agent-teams',
                                        headers=headers, timeout=5,
                                        allow_redirects=False)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'team_id': team.id,
        'collaboration_mode': 'agent_direct',
        'workflow_runs_started': 0, 'business_rows_modified': 0,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
