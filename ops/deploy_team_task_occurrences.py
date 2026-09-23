"""Deploy recurring team task occurrences; no Worker/DeepFlow/Run changes."""
import deploy_team_activity_knowledge as deployment
import re


release = deployment.release
release.BASE = 'ad94c32'
release.FILES = (
    'app/__init__.py',
    'app/models.py',
    'app/api/plan_supervision.py',
    'app/api/testplans.py',
    'app/services/agent_tasks.py',
    'app/services/agent_team_plans.py',
    'app/services/plan_supervision.py',
    'templates/agent_teams.html',
    'templates/testplans.html',
    'static/css/agent_team_plans.css',
    'static/js/agent_team_plans.js',
)
release.MIGRATIONS = ('20260923_test_task_occurrences.sql',)

# The production template carries an independently deployed chat image
# lightbox cache key on the same one-line script block.  This release changes
# only the team-plan bundle key, so preserve every other live script key rather
# than forcing the generic three-way merge to choose between the two changes.
_candidate = release.candidate


def _occurrence_candidate(path, live):
    if path != 'templates/agent_teams.html' or live is None:
        return _candidate(path, live)
    live = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    pattern = rb"(filename='js/agent_team_plans\.js'\) }}\?v=)[^\"<]+"
    merged, count = re.subn(
        pattern, rb'\g<1>20260923occurrences', live, count=1)
    if count != 1:
        raise RuntimeError(
            'Expected exactly one agent_team_plans.js script reference')
    return merged


release.candidate = _occurrence_candidate

release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    os.umask(0o077)
    os.makedirs(backup, exist_ok=True)
    for table in ('test_tasks', 'test_task_reports'):
        ddl = db.session.execute(text('SHOW CREATE TABLE `' + table + '`')).fetchone()[1]
        with open(backup + '/' + table + '_schema.sql', 'w') as stream:
            stream.write(ddl + ';\n')
    db.session.remove()
    template_columns = {
        'schedule_enabled': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'recurrence_type': "VARCHAR(16) NOT NULL DEFAULT 'once'",
        'recurrence_weekdays_json': 'LONGTEXT DEFAULT NULL',
        'schedule_timezone': "VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'",
        'not_before_time': "VARCHAR(5) DEFAULT ''",
        'due_time': "VARCHAR(5) DEFAULT ''",
        'auto_dispatch': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'execution_role': "VARCHAR(32) NOT NULL DEFAULT 'member_work'",
    }
    inspector = inspect(db.engine)
    existing = {column['name'] for column in inspector.get_columns('test_tasks')}
    with db.engine.begin() as conn:
        for column, column_type in template_columns.items():
            if column not in existing:
                conn.execute(text(
                    'ALTER TABLE test_tasks ADD COLUMN `' + column + '` ' + column_type))
        for name, source in migrations:
            source = '\n'.join(
                line for line in source.splitlines()
                if not line.lstrip().startswith('--'))
            for statement in source.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
            print('TEAM_RELEASE ' + json.dumps({'migration': name, 'applied': True}))
    inspector = inspect(db.engine)
    report_columns = {
        column['name'] for column in inspector.get_columns('test_task_reports')}
    if 'occurrence_id' not in report_columns:
        with db.engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE test_task_reports ADD COLUMN '
                'occurrence_id INT DEFAULT NULL'))
    inspector = inspect(db.engine)
    indexes = {
        tuple(index['column_names'])
        for index in inspector.get_indexes('test_task_reports')}
    if ('occurrence_id',) not in indexes:
        with db.engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE test_task_reports ADD INDEX '
                'ix_test_task_reports_occurrence_id (occurrence_id)'))
    inspector = inspect(db.engine)
    foreign_keys = {
        tuple(key['constrained_columns'])
        for key in inspector.get_foreign_keys('test_task_reports')}
    if ('occurrence_id',) not in foreign_keys:
        with db.engine.begin() as conn:
            conn.execute(text(
                'ALTER TABLE test_task_reports ADD CONSTRAINT '
                'fk_test_task_reports_occurrence FOREIGN KEY '
                '(occurrence_id) REFERENCES test_task_occurrences(id)'))
    inspector = inspect(db.engine)
    assert set(template_columns) <= {
        column['name'] for column in inspector.get_columns('test_tasks')}
    assert 'occurrence_id' in {
        column['name'] for column in inspector.get_columns('test_task_reports')}
    occurrence_columns = {
        column['name'] for column in inspector.get_columns('test_task_occurrences')}
    assert {'occurrence_date', 'not_before_at', 'status', 'agent_task_id',
            'attempt_count', 'next_action', 'last_heartbeat_at'} <= occurrence_columns
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'backup': backup,
        'business_rows_modified': 0,
    }))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, TestPlan, TestTask, TestTaskOccurrence, User, WorkflowRun
from app.services import agent_tasks, plan_supervision
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
    assert '/api/v1/test-plans/<int:plan_id>/tasks/<int:task_id>/occurrences' in rules
    assert 'result_invalid' in agent_tasks.HUB_RETRYABLE_RESULT_CODES
    assert 'promote_and_dispatch_due_occurrences' in pyinspect.getsource(
        plan_supervision.sweep)
    task = TestTask.query.order_by(TestTask.id.asc()).first()
    if task:
        response = client.get(
            '/api/v1/test-plans/%s/tasks/%s/occurrences' %
            (task.plan_id, task.id))
        assert response.status_code == 200, response.status_code
    for path in ('/agent-teams', '/testplans'):
        assert client.get(path).status_code == 200, path
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
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
        assert '20260923occurrences' in response.text
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
        'workers_modified': False,
        'deepflow_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.main()
