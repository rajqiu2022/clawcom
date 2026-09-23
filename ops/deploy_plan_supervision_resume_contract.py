"""Deploy the persistent plan recovery contract without touching Worker/DeepFlow.

The release reuses the drift-aware, staged and rollback-capable Agent Team
transport.  It changes Hub application code and additive schema only; business
rows are migrated later through the authenticated schedule-migration API.
"""
import deploy_team_task_occurrences as deployment


release = deployment.release
release.BASE = 'eb1c002'
release.FILES = (
    'app/__init__.py',
    'app/models.py',
    'app/api/plan_supervision.py',
    'app/api/testplans.py',
    'app/services/agent_tasks.py',
    'app/services/hub_capability.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()

release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    os.umask(0o077)
    os.makedirs(backup, exist_ok=True)
    for table in ('test_tasks', 'test_task_occurrences',
                  'agent_tasks', 'test_reports'):
        ddl = db.session.execute(
            text('SHOW CREATE TABLE `' + table + '`')).fetchone()[1]
        with open(backup + '/' + table + '_schema.sql', 'w') as stream:
            stream.write(ddl + ';\n')
    db.session.remove()

    task_columns = {
        'schedule_effective_from': 'DATE DEFAULT NULL',
    }
    occurrence_columns = {
        'action_attempt_count': 'INTEGER NOT NULL DEFAULT 0',
        'recommended_action': "VARCHAR(64) DEFAULT ''",
        'owner_gate': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'action_metadata_json': 'LONGTEXT DEFAULT NULL',
        'execution_goal_json': 'LONGTEXT DEFAULT NULL',
        'checkpoint_json': 'LONGTEXT DEFAULT NULL',
        'resume_contract_json': 'LONGTEXT DEFAULT NULL',
        'condition_state': "VARCHAR(32) NOT NULL DEFAULT ''",
        'next_probe_at': 'DATETIME DEFAULT NULL',
        'last_condition_event_at': 'DATETIME DEFAULT NULL',
        'resume_fencing_token': 'INTEGER NOT NULL DEFAULT 0',
    }
    inspector = inspect(db.engine)
    existing_tasks = {
        column['name'] for column in inspector.get_columns('test_tasks')}
    existing_occurrences = {
        column['name']
        for column in inspector.get_columns('test_task_occurrences')}
    with db.engine.begin() as conn:
        for column, column_type in task_columns.items():
            if column not in existing_tasks:
                conn.execute(text(
                    'ALTER TABLE test_tasks ADD COLUMN `' + column + '` '
                    + column_type))
        for column, column_type in occurrence_columns.items():
            if column not in existing_occurrences:
                conn.execute(text(
                    'ALTER TABLE test_task_occurrences ADD COLUMN `'
                    + column + '` ' + column_type))

    inspector = inspect(db.engine)
    indexes = {
        index['name']
        for index in inspector.get_indexes('test_task_occurrences')}
    with db.engine.begin() as conn:
        if 'ix_test_task_occurrences_condition_state' not in indexes:
            conn.execute(text(
                'ALTER TABLE test_task_occurrences ADD INDEX '
                'ix_test_task_occurrences_condition_state '
                '(condition_state)'))
        if 'ix_test_task_occurrences_next_probe_at' not in indexes:
            conn.execute(text(
                'ALTER TABLE test_task_occurrences ADD INDEX '
                'ix_test_task_occurrences_next_probe_at (next_probe_at)'))

    inspector = inspect(db.engine)
    assert set(task_columns) <= {
        column['name'] for column in inspector.get_columns('test_tasks')}
    assert set(occurrence_columns) <= {
        column['name']
        for column in inspector.get_columns('test_task_occurrences')}
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'backup': backup,
        'business_rows_modified': 0,
    }))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, TestTaskOccurrence, User, WorkflowRun
from app.services import plan_supervision
from sqlalchemy import inspect
import inspect as pyinspect
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    agent_task_count = AgentTask.query.count()
    workflow_run_count = WorkflowRun.query.count()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert ('/api/v1/test-plans/<int:plan_id>/supervision/'
            'schedule-migration') in rules
    assert ('/api/v1/test-plans/<int:plan_id>/supervision/occurrences/'
            '<int:occurrence_id>/condition-events') in rules
    source = pyinspect.getsource(plan_supervision)
    for marker in ('def migrate_schedule_templates',
                   'def record_condition_probe',
                   "'waiting_condition'", 'plan_control_action'):
        assert marker in source, marker
    inspector = inspect(db.engine)
    task_columns = {
        column['name'] for column in inspector.get_columns('test_tasks')}
    occurrence_columns = {
        column['name']
        for column in inspector.get_columns('test_task_occurrences')}
    assert 'schedule_effective_from' in task_columns
    assert {'execution_goal_json', 'checkpoint_json',
            'resume_contract_json', 'condition_state', 'next_probe_at',
            'last_condition_event_at', 'resume_fencing_token',
            'action_attempt_count', 'recommended_action', 'owner_gate',
            'action_metadata_json'} <= occurrence_columns
    for path in ('/agent-teams', '/testplans'):
        assert client.get(path).status_code == 200, path
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {
            'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session')
            + '=' + cookie}
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
        assert requests.get(
            'http://127.0.0.1:18800/testplans', headers=headers,
            timeout=15, allow_redirects=False).status_code == 200
    assert AgentTask.query.count() == agent_task_count
    assert WorkflowRun.query.count() == workflow_run_count
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'occurrence_rows': TestTaskOccurrence.query.count(),
        'agent_tasks_started': AgentTask.query.count() - agent_task_count,
        'workflow_runs_started': WorkflowRun.query.count() - workflow_run_count,
        'business_rows_modified': 0,
        'workers_modified': False,
        'deepflow_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.deployment.main()
