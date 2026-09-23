"""Deploy durable Agent-Team manager authority without schema changes.

Manager authorization follows the configured team role until an administrator
changes the roster/status.  PlanSupervisor turn leases remain short-lived and
fenced; this release does not start a Workflow Run.
"""
import deploy_agent_teams as release


release.BASE = 'e531c66'
release.FILES = (
    'app/models.py',
    'app/models_plan_supervision.py',
    'app/api/agent_teams.py',
    'app/api/workflow_missions.py',
    'app/services/agent_teams.py',
    'app/services/agent_team_plans.py',
    'app/services/plan_supervision.py',
    'app/services/agent_context_snapshots.py',
    'app/services/agent_system_context.py',
    'app/services/agent_team_context.py',
    'app/services/agent_team_onboarding.py',
    'templates/agent_teams.html',
    'static/js/agent_teams.js',
    'static/js/agent_team_plans.js',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'agent_teams', 'plan_supervisors', 'workflow_missions'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'migrations_applied': 0,
        'backup': backup,
    }))
'''
release.SMOKE = r'''
import inspect
from app import create_app, db
from app.models import AgentTeam, User, WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision
from app.services.agent_teams import require_manager
app = create_app('production')
with app.app_context():
    team = db.session.get(AgentTeam, 1)
    supervisor = db.session.get(PlanSupervisor, 50)
    assert team and team.status == 'active'
    assert team.primary_manager_claw_id == 54
    assert team.has_manager_authority(54)
    assert not team.has_manager_authority(11)
    payload = team.to_dict()
    assert payload['manager_authority_active'] is True
    assert payload['manager_authority_mode'] == 'team_role_assignment'
    assert payload['manager_lease_expires_at'] is None
    assert supervisor and supervisor.orchestrator_claw_id == 54
    authority = plan_supervision.manager_lease_payload(supervisor)
    assert authority['active'] is True
    assert authority['manager_claw_id'] == 54
    assert authority['mode'] == 'team_role_assignment'
    assert authority['expires_at'] is None
    assert 'manager_lease_expires_at' not in inspect.getsource(require_manager)
    assert 'requires_team_manager_assignment' in inspect.getsource(
        __import__('app.services.agent_context_snapshots', fromlist=['x']))
    assert 'team_role_assignment' in inspect.getsource(
        __import__('app.api.workflow_missions', fromlist=['x']))
    runs_before = WorkflowRun.query.count()
    admin = User.query.filter_by(role='super_admin').first()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    assert client.get('/agent-teams').status_code == 200
    response = client.get('/api/v1/agent-teams/1')
    assert response.status_code == 200, response.get_data(as_text=True)
    live = response.get_json()
    assert live['manager_authority_active'] is True
    assert live['manager_lease_expires_at'] is None
    assert WorkflowRun.query.count() == runs_before
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'team_id': team.id,
        'manager_claw_id': team.primary_manager_claw_id,
        'manager_authority': authority,
        'supervisor_status': supervisor.status,
        'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
