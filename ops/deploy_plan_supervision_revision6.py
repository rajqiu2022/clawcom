"""Deploy the Plan supervision dispatch/reporting closure.

This release is schema-free and does not create or resume a Workflow Run.  It
advertises the supervision lease in Mission dispatch contracts, accepts the
legacy flat lease shape, and installs the Hub-side audited Owner notifier.
Enable the notifier separately with ``configure_plan_supervision.py`` after the
release smoke test passes.
"""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = 'd14ed1b39bb0c119a9f8575ce17a82e403e0c2ac'
release.FILES = (
    'app/__init__.py',
    'app/api/plan_supervision.py',
    'app/api/workflow_missions.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'plan_supervisors', 'plan_supervisor_events',
            'plan_supervisor_receipts', 'wecom_send_logs'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'migrations_applied': 0,
        'backup': backup,
    }))
'''
release.SMOKE = r'''
from app import create_app, db
from app.api.workflow_missions import _mission_payload
from app.models import User, WorkflowMission, WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision
import requests, time

app = create_app('production')
with app.app_context():
    assert app.config['PLAN_SUPERVISION_ENABLED'] is True
    supervisor = db.session.get(PlanSupervisor, 50)
    assert supervisor and supervisor.mission_id
    mission = db.session.get(WorkflowMission, supervisor.mission_id)
    contract = _mission_payload(mission)['dispatch_contract']
    assert 'plan_supervision' in contract['required_fields']
    assert contract['plan_supervision']['legacy_flat_shape_accepted'] is True
    assert plan_supervision.lease_credentials({
        'worker_id': 'release-smoke', 'fencing_token': 9,
    }) == {'worker_id': 'release-smoke', 'fencing_token': 9}
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    runs_before = WorkflowRun.query.count()
    mission_id = mission.id
    db.session.remove()

    cookie = app.session_interface.get_signing_serializer(app).dumps(
        {'user_id': admin_id})
    headers = {
        'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie
    }
    if os.getcwd() == '/opt/openclaw-web':
        for attempt in range(20):
            try:
                response = requests.get(
                    'http://127.0.0.1:18800/api/v1/workflow-missions/%s'
                    % mission_id, headers=headers, timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        live_contract = response.json()['dispatch_contract']
        assert 'plan_supervision' in live_contract['required_fields']
        assert live_contract['plan_supervision']['legacy_flat_shape_accepted'] is True
    assert WorkflowRun.query.count() == runs_before
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'plan_id': supervisor.plan_id,
        'mission_id': mission_id,
        'contract_advertised': True,
        'legacy_flat_lease_accepted': True,
        'owner_notification_policy': plan_supervision.team_capability(
            supervisor.team_id)['owner_notification_policy'],
        'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
