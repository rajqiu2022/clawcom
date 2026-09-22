"""Deploy stage-scoped blocking for Plan supervision.

Schema-free Hub release.  It changes no Worker, task, Run or Mission row while
deploying; Plan #50 is resumed separately after the release is verified.
"""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = 'ea4dd96'
release.FILES = (
    'app/api/plan_supervision.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    assert {'plan_supervisors', 'plan_supervisor_events',
            'plan_supervisor_receipts'} <= set(inspect(db.engine).get_table_names())
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0, 'backup': backup}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import User, WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision
import requests, time

app = create_app('production')
with app.app_context():
    supervisor = db.session.get(PlanSupervisor, 50)
    assert supervisor and supervisor.mission_id
    policy = plan_supervision.team_capability(
        supervisor.team_id)['blocking_policy']
    assert policy['default_scope'] == 'stage'
    assert policy['plan_scope_requires'] == {'block_scope': 'plan'}
    stages = plan_supervision.undispatched_stages(supervisor)
    runs_before = WorkflowRun.query.count()
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    db.session.remove()

    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin_id
    response = client.get('/api/v1/test-plans/50/supervision')
    assert response.status_code == 200, response.status_code
    assert 'undispatched_stages' in response.get_json()
    db.session.remove()

    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin_id})
        headers = {
            'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie
        }
        for attempt in range(20):
            try:
                response = requests.get(
                    'http://127.0.0.1:18800/api/v1/test-plans/50/supervision',
                    headers=headers, timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        assert 'undispatched_stages' in response.json()
    assert WorkflowRun.query.count() == runs_before
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'plan_id': supervisor.plan_id,
        'default_block_scope': policy['default_scope'],
        'undispatched_stage_count': len(stages),
        'workflow_runs_started': 0, 'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
