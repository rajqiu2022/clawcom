"""Deploy Plan scope validation and deterministic overdue-task recovery."""
import deploy_plan_supervision_resume_contract as deployment
import re
import subprocess


release = deployment.release
release.BASE = 'ef82be2'
release.FILES = (
    'app/__init__.py',
    'app/models.py',
    'app/api/plan_supervision.py',
    'app/api/testplans.py',
    'app/api/workflow_missions.py',
    'app/services/agent_team_plans.py',
    'app/services/plan_supervision.py',
    'templates/agent_teams.html',
    'static/css/agent_teams.css',
    'static/css/agent_team_plans.css',
    'static/js/agent_team_chat.js',
    'static/js/agent_team_plans.js',
)
release.MIGRATIONS = ()
# This release owns the current team-plan bundle and template cache key; do not
# reuse the older task-conclusion-only template merger.
release.candidate = deployment.deployment._candidate
_candidate = release.candidate


def _scope_recovery_candidate(path, live):
    owned_paths = {
        'app/api/plan_supervision.py',
        'app/services/agent_team_plans.py',
        'app/services/plan_supervision.py',
        'static/css/agent_teams.css',
        'static/css/agent_team_plans.css',
        'static/js/agent_team_plans.js',
    }
    if path in owned_paths and live is not None:
        # The immediately preceding release already owns the inline recovery
        # contract. Accept only that exact committed production baseline before
        # replacing it; any unrelated hotfix still fails closed in the generic
        # three-way merger below.
        previous = subprocess.check_output(
            ['git', 'show', '1a95a5c:web/' + path],
            cwd=str(release.ROOT)).replace(b'\r\n', b'\n')
        normalized = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
        if normalized == previous:
            return (release.ROOT / 'web' / path).read_bytes().replace(
                b'\r\n', b'\n')
    if path == 'static/js/agent_team_chat.js' and live is not None:
        normalized = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
        # Production predates the committed clipboard-image client changes.
        # Accept only that exact known bundle before replacing it with the
        # current chat client; an unknown hotfix must still fail closed.
        if release.sha(normalized) == 'fef4194d87359eb5538241d4df136849611ea6f1a3a9caba58ac4248821c544e':
            return (release.ROOT / 'web' / path).read_bytes().replace(
                b'\r\n', b'\n')
    if path != 'templates/agent_teams.html' or live is None:
        return _candidate(path, live)
    live = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    replacements = (
        (rb"(filename='js/agent_team_plans\.js'\) }}\?v=)[^\"<]+",
         rb'\g<1>20260924teamtaskrefresh'),
        (rb"(filename='css/agent_team_plans\.css'\) }}\?v=)[^\"<]+",
         rb'\g<1>20260924teamtaskrefresh'),
        (rb"(filename='js/agent_team_chat\.js'\) }}\?v=)[^\"<]+",
         rb'\g<1>20260924sidebarmentions'),
        (rb"(filename='css/agent_teams\.css'\) }}\?v=)[^\"<]+",
         rb'\g<1>20260924sidebarmentions'),
    )
    merged = live
    for pattern, replacement in replacements:
        merged, count = re.subn(pattern, replacement, merged, count=1)
        if count != 1:
            raise RuntimeError('Expected exactly one Agent Team plan asset reference')
    return merged


release.candidate = _scope_recovery_candidate

release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    os.umask(0o077)
    os.makedirs(backup, exist_ok=True)
    ddl = db.session.execute(text('SHOW CREATE TABLE test_tasks')).fetchone()[1]
    with open(backup + '/test_tasks_schema.sql', 'w') as stream:
        stream.write(ddl + ';\n')
    db.session.remove()
    columns = {
        'workflow_definition_id': 'INTEGER DEFAULT NULL',
        'workflow_start_vars_json': 'LONGTEXT DEFAULT NULL',
    }
    inspector = inspect(db.engine)
    existing = {column['name'] for column in inspector.get_columns('test_tasks')}
    with db.engine.begin() as conn:
        for column, column_type in columns.items():
            if column not in existing:
                conn.execute(text(
                    'ALTER TABLE test_tasks ADD COLUMN `' + column + '` '
                    + column_type))
    inspector = inspect(db.engine)
    assert set(columns) <= {
        column['name'] for column in inspector.get_columns('test_tasks')}
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True,
        'backup': backup,
        'business_rows_modified': 0,
    }))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, TestTask, User, WorkflowRun
from app.services import plan_supervision
from sqlalchemy import inspect
import inspect as pyinspect
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    task_count = AgentTask.query.count()
    run_count = WorkflowRun.query.count()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert '/api/v1/test-plans/<int:plan_id>/supervision/resume' in rules
    columns = {
        column['name'] for column in inspect(db.engine).get_columns('test_tasks')}
    assert {'workflow_definition_id', 'workflow_start_vars_json'} <= columns
    source = pyinspect.getsource(plan_supervision)
    for marker in ('PLAN_BLOCK_SCOPE_CONFLICT',
                   'plan_block_reason_code',
                   'manager_auto_resume_allowed',
                   'valid_plan_owner_gate',
                   'def _dispatch_scheduled_workflow',
                   'task_workflow_dispatched'):
        assert marker in source, marker
    response = client.get('/agent-teams')
    assert response.status_code == 200
    assert '20260924teamtaskrefresh' in response.get_data(as_text=True)
    assert '20260924sidebarmentions' in response.get_data(as_text=True)
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session')
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
        assert '20260924teamtaskrefresh' in response.text
        assert '20260924sidebarmentions' in response.text
    assert AgentTask.query.count() == task_count
    assert WorkflowRun.query.count() == run_count
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'agent_tasks_started': AgentTask.query.count() - task_count,
        'workflow_runs_started': WorkflowRun.query.count() - run_count,
        'business_rows_modified': 0,
        'workers_modified': False,
        'deepflow_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.deployment.deployment.main()
