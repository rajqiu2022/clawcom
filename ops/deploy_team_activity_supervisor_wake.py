"""Deploy the scoped Team activity-to-Supervisor wake contract."""

import deploy_agent_teams as release


release.BASE = '2f850b8f3d1631c6456ef9bee75567d6a692f9e5'
release.FILES = (
    'app/api/agent_teams.py',
    'app/services/agent_team_activity.py',
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
            'agent_team_member_reports', 'plan_supervisor_events'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'business_rows_modified': 0, 'backup': backup,
    }))
'''
release.SMOKE = r'''
import inspect
from app import create_app, db
from app.models import AgentTeam, User
from app.services import agent_team_activity, plan_supervision
app = create_app('production')
with app.app_context():
    assert 'on_transition' in inspect.signature(agent_team_activity.ingest).parameters
    assert 'team_member_activity_changed' in inspect.getsource(plan_supervision.pump)
    team = db.session.get(AgentTeam, 1)
    assert team and team.status == 'active'
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    assert client.get('/api/v1/agent-teams/1/members/activity').status_code == 200
    db.session.remove()
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'team_id': team.id,
        'supervisor_event': 'team_member_activity_changed',
        'workflow_runs_started': 0, 'business_rows_modified': 0,
    }))
'''


if __name__ == '__main__':
    release.main()
