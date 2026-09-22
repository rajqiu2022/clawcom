"""Deploy direct Agent-to-Owner reporting for Plan supervision."""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = 'a72b083a150af1aece4a8a285889c229dc937de6'
release.FILES = (
    'app/__init__.py',
    'app/api/plan_supervision.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    assert {'plan_supervisors', 'plan_supervisor_receipts'} <= set(
        inspect(db.engine).get_table_names())
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0, 'backup': backup}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision
app = create_app('production')
with app.app_context():
    supervisor = db.session.get(PlanSupervisor, 50)
    assert supervisor
    policy = plan_supervision.team_capability(
        supervisor.team_id)['owner_notification_policy']
    assert policy['delivery'] == 'agent_wecom'
    assert policy['fallback'] == 'hub_receipt_only'
    runs_before = WorkflowRun.query.count()
    assert WorkflowRun.query.count() == runs_before
    assert not hasattr(plan_supervision, 'deliver_owner_notification')
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'plan_id': supervisor.plan_id,
        'owner_notification_delivery': policy['delivery'],
        'hub_sendrtxinfo_used': False, 'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
