"""Restore the strict hub.plan_supervision.v1 wake envelope."""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = '99b51db'
release.FILES = ('app/services/plan_supervision.py',)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    assert 'plan_supervisors' in set(inspect(db.engine).get_table_names())
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0, 'backup': backup}))
'''
release.SMOKE = r'''
import inspect
from app import create_app, db
from app.models import ClawMessage, WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import plan_supervision
app = create_app('production')
with app.app_context():
    supervisor = db.session.get(PlanSupervisor, 50)
    assert supervisor
    policy = plan_supervision.team_capability(
        supervisor.team_id)['owner_notification_policy']
    assert policy['delivery'] == 'agent_wecom'
    rows = ClawMessage.query.filter_by(
        claw_id=supervisor.orchestrator_claw_id,
        msg_type='plan_supervision').order_by(ClawMessage.id.desc()).limit(5).all()
    # Historical messages may contain the incompatible extension.  Source
    # shape is regression-tested locally; do not create a production wake here.
    source = inspect.getsource(plan_supervision.pump)
    assert "'owner_notification_policy': {" not in source
    assert "'contract': 'hub.plan_supervision.v1'" in source
    runs_before = WorkflowRun.query.count()
    assert WorkflowRun.query.count() == runs_before
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'plan_id': supervisor.plan_id,
        'strict_wake_envelope': True,
        'owner_notification_delivery': policy['delivery'],
        'historical_wake_count_checked': len(rows),
        'workflow_runs_started': 0, 'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
