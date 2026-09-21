"""Publish Plan supervision + verified Skill receipts without starting Plans/Runs.

Uses the drift-aware three-way deployment transport and explicit schema window.
Enable a team separately with configure_plan_supervision.py after verification.
"""
import deploy_team_activity_knowledge as deployment
import re
import subprocess
import tempfile
from pathlib import Path

release = deployment.release
release.BASE = 'ae1b086f93bbccdb31f76ecff7253e7dd66f0581'
release.FILES = (
    'app/__init__.py', 'app/models.py', 'app/models_plan_supervision.py',
    'app/api/__init__.py', 'app/api/agent_client.py', 'app/api/agent_teams.py',
    'app/api/openclaws.py', 'app/api/skills.py', 'app/api/testplans.py',
    'app/api/todos.py', 'app/api/workflow_missions.py', 'app/api/workflows.py',
    'app/api/plan_supervision.py', 'app/services/agent_team_context.py',
    'app/services/agent_team_onboarding.py', 'app/services/skill_delivery.py',
    'app/services/skill_installation.py', 'app/services/timeout_watcher.py',
    'app/services/plan_supervision.py', 'app/services/plan_supervision_events.py',
)
release.MIGRATIONS = ('20260921_skill_installation_receipts.sql', '20260921_plan_supervision.sql')
original_candidate = release.candidate


def candidate(path, live):
    if path != 'app/api/skills.py':
        return original_candidate(path, live)
    # Production has existing payload/visibility normalization imports. Only the
    # additive import collision is reviewed here; any other conflict aborts.
    local = (release.ROOT / 'web' / path).read_bytes().replace(b'\r\n', b'\n')
    base = subprocess.check_output(['git', 'show', release.BASE+':web/'+path], cwd=release.ROOT)
    with tempfile.TemporaryDirectory(prefix='plan-import-merge-') as folder:
        paths = [Path(folder)/name for name in ('live', 'base', 'local')]
        for target, value in zip(paths, (live, base, local)):
            target.write_bytes(value.replace(b'\r\n', b'\n'))
        result = subprocess.run(['git','merge-file','-p']+[str(p) for p in paths],capture_output=True)
    if result.returncode == 0:
        return result.stdout
    source = result.stdout.decode('utf-8')
    expected_live = ('from app.services.skill_visibility import normalize_skill_visibility\n'
                     'from app.services.skill_payload import normalize_skill_payload\n')
    expected_local = 'from app.services.skill_installation import reset_installation, installation_view, control_instructions\n'
    pattern = r'<<<<<<<[^\n]*\n'+re.escape(expected_live)+r'=======\n'+re.escape(expected_local)+r'>>>>>>>[^\n]*\n'
    source, count = re.subn(pattern, lambda _: expected_live+expected_local, source)
    if result.returncode != 1 or count != 1 or '<<<<<<<' in source or '>>>>>>>' in source:
        raise RuntimeError('Unreviewed production skills.py conflict')
    return source.encode('utf-8')


release.candidate = candidate
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
import tempfile
app = create_app('production')
with app.app_context():
    os.umask(0o077)
    url = db.engine.url
    def quote(value):
        return '"' + str(value or '').replace('\\','\\\\').replace('"','\\"').replace('\n','\\n') + '"'
    fd, defaults = tempfile.mkstemp(prefix='receipt-db-', suffix='.cnf', dir=backup)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('[client]\nhost=' + quote(url.host or 'localhost') + '\nport=' + str(url.port or 3306)
                + '\nuser=' + quote(url.username) + '\npassword=' + quote(url.password) + '\n')
        common = ['mysqldump', '--defaults-extra-file='+defaults, '--single-transaction', '--skip-lock-tables']
        with open(backup+'/schema.sql','wb') as stream:
            subprocess.run(common+['--no-data',url.database],stdout=stream,stderr=subprocess.PIPE,check=True)
        with open(backup+'/skill_assignments_todos.sql','wb') as stream:
            subprocess.run(common+[url.database,'openclaw_skills','claw_todos'],
                           stdout=stream,stderr=subprocess.PIPE,check=True)
    finally:
        os.unlink(defaults)
    with db.engine.begin() as conn:
        for name, source in migrations:
            source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
            for statement in source.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
            print('TEAM_RELEASE '+json.dumps({'migration':name,'applied':True}))
    inspector = inspect(db.engine)
    for name in ['openclaw_skills','skill_installation_receipts','plan_supervisors',
                 'plan_supervisor_events','plan_supervisor_receipts']:
        actual = {c['name'] for c in inspector.get_columns(name)}
        expected = {c.name for c in db.metadata.tables[name].columns}
        assert expected <= actual, (name,sorted(expected-actual))
    print('TEAM_RELEASE '+json.dumps({'schema_verified':True,'backup':backup}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import User, TestPlan
from app.models_plan_supervision import PlanSupervisor
from app.services.agent_team_context import build_team_context
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin_id
    paths = ['/agent-teams','/workflows','/test-reports','/knowledge',
             '/api/v1/agent-teams?project_id=6','/api/v1/skills/220','/api/v1/skills/221']
    for path in paths:
        response = client.get(path)
        db.session.remove()
        assert response.status_code == 200, (path,response.status_code)
    assert client.get('/api/v1/test-plans/50/supervision').status_code == (
        200 if app.config['PLAN_SUPERVISION_ENABLED'] else 503)
    # No fabricated Worker receipts, Plan activation, or model messages.
    summary = {'plan50_status':db.session.get(TestPlan,50).status,
               'supervisor_count':PlanSupervisor.query.count(),
               'enabled':app.config['PLAN_SUPERVISION_ENABLED'],
               'team_ids':app.config['PLAN_SUPERVISION_TEAM_IDS']}
    db.session.remove()
    checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin_id})
        headers = {'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/agent-teams',headers=headers,timeout=5)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        for path in paths:
            response = requests.get('http://127.0.0.1:18800'+path,headers=headers,timeout=15,allow_redirects=False)
            assert response.status_code == 200, (path,response.status_code)
            checks[path] = response.status_code
        assert requests.get('http://127.0.0.1:18800/api/v1/agent-teams?project_id=6',timeout=15).status_code == 401
    print('TEAM_RELEASE '+json.dumps({'smoke':'passed','http_checks':checks,'supervision':summary,
                                    'workers_modified':False,'runs_started':0}))
'''

if __name__ == '__main__':
    deployment.main()
