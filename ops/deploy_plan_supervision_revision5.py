"""Deploy Plan supervision Revision 5 without starting a business Workflow.

The release is drift-aware and rollback-capable.  On the live runtime its
smoke check repairs the already-active scoped team Plan control chain, then
proves that no Workflow Run was created by that repair.
"""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = '70a520c318e6c7f0fc9904e28a5994fc8c5a4fb0'
release.FILES = (
    'app/models_plan_supervision.py',
    'app/api/plan_supervision.py',
    'app/api/testplans.py',
    'app/api/workflows.py',
    'app/services/agent_team_plans.py',
    'app/services/plan_supervision.py',
    'static/js/agent_team_plans.js',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    inspector = inspect(db.engine)
    expected = {
        'plan_supervisors', 'plan_supervisor_events',
        'plan_supervisor_receipts', 'workflow_missions',
        'agent_team_missions', 'mission_stages',
    }
    assert expected <= set(inspector.get_table_names())
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'migrations_applied': 0,
        'backup': backup,
    }))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import (
    AgentTeam, AgentTeamMission, MissionStage, TestPlan, User,
    WorkflowMission, WorkflowMissionDispatch, WorkflowRun,
)
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision as supervision
import requests, time

app = create_app('production')
with app.app_context():
    assert app.config['PLAN_SUPERVISION_ENABLED'] is True
    team_ids = str(app.config['PLAN_SUPERVISION_TEAM_IDS'])
    assert team_ids and team_ids != '*'
    scoped = [int(value.strip()) for value in team_ids.split(',') if value.strip()]
    assert scoped
    team = db.session.get(AgentTeam, scoped[0])
    assert team and team.status == 'active'
    assert team.primary_manager_claw_id
    assert supervision.team_capability(team.id)['auto_start'] is True
    team_id = team.id
    project_id = team.project_id
    manager_claw_id = team.primary_manager_claw_id
    backup_manager_claw_id = team.backup_manager_claw_id

    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin_id
    paths = [
        '/agent-teams',
        '/api/v1/agent-teams/%s/test-plans?period=week' % team_id,
    ]
    for path in paths:
        response = client.get(path)
        db.session.remove()
        assert response.status_code == 200, (path, response.status_code)

    active_plans = TestPlan.query.filter_by(
        team_id=team_id, project_id=project_id, status='active').all()
    assert active_plans, 'Scoped team has no active Plan to repair/verify'
    active_plan_ids = [plan.id for plan in active_plans]
    runs_before = WorkflowRun.query.count()
    live = os.getcwd() == '/opt/openclaw-web'
    if live:
        supervision.sweep()
    db.session.remove()
    assert WorkflowRun.query.count() == runs_before

    controls = []
    for plan_id in active_plan_ids:
        supervisor = db.session.get(PlanSupervisor, plan_id)
        if live:
            assert supervisor and supervisor.mission_id
        if not supervisor:
            controls.append({'plan_id': plan_id, 'staged_pending_repair': True})
            continue
        mission = db.session.get(WorkflowMission, supervisor.mission_id)
        binding = db.session.get(AgentTeamMission, supervisor.mission_id)
        assert mission and binding and binding.team_id == team_id
        assert mission.main_claw_id == manager_claw_id
        assert WorkflowMissionDispatch.query.filter_by(
            mission_id=mission.id).count() == 0
        stages = MissionStage.query.filter_by(mission_id=mission.id).all()
        assert all(stage.assigned_claw_id not in {
            manager_claw_id, backup_manager_claw_id
        } for stage in stages)
        controls.append({
            'plan_id': plan_id,
            'supervisor_status': supervisor.status,
            'mission_id': mission.id,
            'stage_count': len(stages),
            'manager_lease_active': bool(
                (supervisor.to_dict().get('manager_lease') or {}).get('active')),
        })
    db.session.remove()

    http_checks = {}
    if live:
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin_id})
        headers = {
            'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie
        }
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
        for path in paths + [
                '/static/js/agent_team_plans.js',
                '/api/v1/test-plans/%s/supervision' % active_plan_ids[0]]:
            response = requests.get(
                'http://127.0.0.1:18800' + path, headers=headers,
                timeout=15, allow_redirects=False)
            assert response.status_code == 200, (path, response.status_code)
            http_checks[path] = response.status_code
        assert '监管未启动' in requests.get(
            'http://127.0.0.1:18800/static/js/agent_team_plans.js',
            timeout=15).text

    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'team_id': team_id,
        'manager_claw_id': manager_claw_id,
        'active_plan_controls': controls,
        'workflow_runs_started': 0,
        'http_checks': http_checks,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
