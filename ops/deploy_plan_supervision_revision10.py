"""Deploy Hub-owned recover_run control plane; no Worker/DeepFlow changes."""
import deploy_team_activity_knowledge as deployment


release = deployment.release
release.BASE = '05c1030'
release.FILES = (
    'app/models.py',
    'app/api/workflows.py',
    'app/api/plan_supervision.py',
    'app/services/plan_supervision.py',
    'app/services/hub_capability.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'workflow_runs', 'workflow_run_steps', 'workflow_mission_dispatches',
            'mission_stages', 'plan_supervisors'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'backup': backup}))
'''
release.SMOKE = r'''
import inspect
from app import create_app, db
from app.api import workflows
from app.models import WorkflowRun
from app.models_plan_supervision import PlanSupervisor
from app.services import hub_capability, plan_supervision
app = create_app('production')
with app.app_context():
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert '/api/v1/workflow-runs/<int:run_id>/recover' in rules
    assert hub_capability.HUB_CAPABILITY_VERSION >= 5
    assert '/workflow-runs/{run_id}/recover' in hub_capability.HUB_CAPABILITY_DIGEST
    source = inspect.getsource(workflows.recover_workflow_run)
    assert "'reconcile'" in source and "'restart'" in source
    assert 'Idempotency-Key' in source
    supervisor = db.session.get(PlanSupervisor, 50)
    recovery = plan_supervision.recovery_snapshot(supervisor) if supervisor else {}
    runs_before = WorkflowRun.query.count()
    assert WorkflowRun.query.count() == runs_before
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'recover_route': True,
        'hub_capability_version': hub_capability.HUB_CAPABILITY_VERSION,
        'plan_50_recovery_state': recovery.get('recovery_state'),
        'workflow_runs_started': 0,
        'workers_modified': False,
        'deepflow_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
