"""Immutable, additive Code Analysis release; preserve unrelated live hotfixes."""
import argparse
import ast
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile

import paramiko
import deploy_agent_teams as release

SOURCE = Path(__file__).resolve().parents[1]
release.BASE = '100e56e9500b73061ca1148d6eb9122ef2a70202'
release.FILES = (
    'app/models.py', 'app/api/__init__.py', 'app/api/code_analysis.py',
    'app/api/agent_teams.py', 'app/api/knowledge.py', 'app/api/knowledge_notebooks.py',
    'app/api/skills.py', 'app/api/test_reports.py', 'app/api/testplans.py',
    'app/api/workflows.py', 'app/services/code_analysis.py',
    'app/services/resource_sharing.py', 'app/views/__init__.py', 'config.py',
    'templates/base.html', 'templates/code_analysis.html', 'templates/skills.html',
    'templates/testplans.html', 'static/js/code_analysis.js',
)
release.MIGRATIONS = ('20260930_code_analysis_specialty.sql',)


def merge_api_registration(live):
    """Keep all live blueprint wiring; apply the two reviewed additive edits."""
    source = live.decode('utf-8')
    tree = ast.parse(source)
    auth = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'require_auth')
    loops = [node for node in ast.walk(auth) if isinstance(node, ast.For)
             and isinstance(node.target, ast.Name) and node.target.id == 'cache_key'
             and isinstance(node.iter, ast.Tuple)]
    if len(loops) != 1:
        raise RuntimeError('Live auth cache reset needs review')
    values = ast.literal_eval(loops[0].iter)
    if not {'_collaboration_session', '_chat_guest_session', '_auth_claw',
            '_auth_user', '_auth_user_super'} <= set(values):
        raise RuntimeError('Live auth cache baseline needs review')
    if '_resource_share_policies' not in values:
        node = loops[0].iter
        lines = source.splitlines(True)
        line = lines[node.end_lineno - 1]
        offset = node.end_col_offset - 1
        if line[offset] != ')':
            raise RuntimeError('Unexpected auth cache tuple layout')
        lines[node.end_lineno - 1] = line[:offset] + ", '_resource_share_policies'" + line[offset:]
        source = ''.join(lines)
    if not any(isinstance(node, ast.ImportFrom) and node.module == 'app.api'
               and any(alias.name == 'code_analysis' for alias in node.names)
               for node in ast.walk(tree)):
        source = source.rstrip() + '\n\nfrom app.api import code_analysis  # noqa: F401\n'
    ast.parse(source)
    return source.encode('utf-8')


def candidate(path, live):
    """Merge ONLY committed deltas, never the caller's dirty workspace."""
    local = (release.ROOT / 'web' / path).read_bytes().replace(b'\r\n', b'\n')
    proc = subprocess.run(['git', 'show', release.BASE + ':web/' + path],
                          cwd=str(SOURCE), capture_output=True)
    base = proc.stdout.replace(b'\r\n', b'\n') if proc.returncode == 0 else None
    if live is None:
        if base is not None:
            raise RuntimeError('Missing live baseline: ' + path)
        return local
    live = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    if live == local or live == base:
        return local
    if base is None:
        raise RuntimeError('Unexpected existing new file: ' + path)
    if path == 'app/api/__init__.py':
        return merge_api_registration(live)
    if path == 'app/views/__init__.py':
        source = local.decode('utf-8')
        functions = [node for node in ast.parse(source).body
                     if isinstance(node, ast.FunctionDef) and node.name == 'code_analysis_page']
        if len(functions) != 1:
            raise RuntimeError('Unexpected committed code analysis route')
        existing = [node for node in ast.parse(live).body
                    if isinstance(node, ast.FunctionDef) and
                    (node.name == 'code_analysis_page' or any('/code-analysis' in ast.dump(d)
                                                             for d in node.decorator_list))]
        if existing:
            if len(existing) == 1 and ast.dump(existing[0]) == ast.dump(functions[0]):
                return live
            raise RuntimeError('Unexpected live code analysis route needs review')
        node = functions[0]
        first = min(decorator.lineno for decorator in node.decorator_list)
        addition = '\n'.join(source.splitlines()[first - 1:node.end_lineno])
        return live.rstrip() + b'\n\n' + addition.encode('utf-8') + b'\n'
    with tempfile.TemporaryDirectory(prefix='code-analysis-merge-') as folder:
        paths = [Path(folder) / name for name in ('live', 'base', 'local')]
        for target, data in zip(paths, (live, base, local)):
            target.write_bytes(data)
        merged = subprocess.run(['git', 'merge-file', '-p'] + list(map(str, paths)),
                                capture_output=True)
        if merged.returncode:
            raise RuntimeError('Live hotfix conflict requires review: ' + path)
        return merged.stdout


release.candidate = candidate
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
import tempfile
app = create_app('production')
with app.app_context():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    required = {'projects','knowledge_entries','knowledge_entry_revisions','skills',
        'openclaw_instances','test_plans','workflow_definitions','workflow_runs',
        'shift_left_analysis_runs','shift_left_analysis_findings',
        'shift_left_findings','shift_left_finding_feedback'}
    assert required <= tables, sorted(required - tables)
    os.umask(0o077)
    url = db.engine.url
    def quote(value):
        return '"' + str(value or '').replace('\\','\\\\').replace('"','\\"').replace('\n','\\n') + '"'
    fd, defaults = tempfile.mkstemp(prefix='code-analysis-db-',suffix='.cnf',dir=backup)
    try:
        with os.fdopen(fd,'w') as stream:
            stream.write('[client]\nhost='+quote(url.host or 'localhost')+'\nport='+str(url.port or 3306)+'\nuser='+quote(url.username)+'\npassword='+quote(url.password)+'\n')
        output = backup + '/database-before.sql'
        with open(output,'wb') as stream:
            subprocess.run(['mysqldump','--defaults-extra-file='+defaults,'--max-allowed-packet=1G','--single-transaction',
                '--quick','--skip-lock-tables',url.database],stdout=stream,stderr=subprocess.PIPE,check=True)
        assert os.path.getsize(output) > 0
    finally:
        os.unlink(defaults)
    with db.engine.begin() as conn:
        for name, source in migrations:
            source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
            for statement in source.split(';'):
                if statement.strip(): conn.execute(text(statement))
    inspector = inspect(db.engine)
    names = ('shared_resource_policies','code_analysis_projects','code_analysis_jobs','code_analysis_decisions')
    for name in names:
        actual = {c['name'] for c in inspector.get_columns(name)}
        expected = {c.name for c in db.metadata.tables[name].columns}
        assert expected <= actual, (name, sorted(expected-actual))
        assert inspector.get_foreign_keys(name), name
    print('TEAM_RELEASE '+json.dumps({'schema_verified':True,'new_tables':list(names),
        'full_database_backup':output,'business_rows_modified':0}))
'''
release.SMOKE = r'''
os.environ['CODE_ANALYSIS_ENABLED']='1'
from app import create_app, db
from app.models import User, Project, CodeAnalysisJob
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    project = db.session.get(Project,6) or Project.query.order_by(Project.id).first()
    assert admin and project and app.config['CODE_ANALYSIS_ENABLED']
    client = app.test_client()
    with client.session_transaction() as session: session['user_id']=admin.id
    paths = ['/code-analysis','/agent-teams','/testplans','/skills','/knowledge','/workflows',
        '/api/v1/code-analysis/options?project_id='+str(project.id),
        '/api/v1/code-analysis/runs?project_id='+str(project.id),
        '/api/v1/skills?summary=true']
    checks = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code==200, (path,response.status_code)
        checks[path]=response.status_code
        db.session.remove()
    assert app.test_client().get('/api/v1/code-analysis/options?project_id='+str(project.id)).status_code==401
    live = os.getcwd() == '/opt/openclaw-web'
    if live:
        cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':admin.id})
        headers={'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
        for attempt in range(20):
            try:
                ready=requests.get('http://127.0.0.1:18800/code-analysis',headers=headers,timeout=5,allow_redirects=False)
                if ready.status_code==200:break
            except requests.RequestException: pass
            time.sleep(1)
        else: raise RuntimeError('HTTP service failed to recover')
        assert 'id="ca-project"' in ready.text
        for path in paths+['/static/js/code_analysis.js']:
            response=requests.get('http://127.0.0.1:18800'+path,headers=headers,timeout=20,allow_redirects=False)
            assert response.status_code==200, (path,response.status_code)
    print('TEAM_RELEASE '+json.dumps({'smoke':'passed','live_http':live,'checks':checks,
        'enabled':True,'analysis_runs_started':0,'tapd_bugs_created':0,'workers_modified':False}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--key',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    git_dir=subprocess.check_output(['git','rev-parse','--absolute-git-dir'],cwd=str(SOURCE)).decode().strip()
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(SOURCE)).decode().strip()
    # Build a package from Git objects; a managed worktree isn't needed.
    archive=subprocess.check_output(['git','archive',revision,'web','ops/migrations'],cwd=str(SOURCE))
    with tempfile.TemporaryDirectory(prefix='code-analysis-release-') as folder:
        root=Path(folder).resolve()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                target=(root/member.name).resolve()
                if root not in target.parents or member.issym() or member.islnk():
                    raise RuntimeError('Unsafe package member')
            tar.extractall(str(root),filter='data')
        release.ROOT=root
        os.environ['GIT_DIR']=git_dir
        os.environ['GIT_WORK_TREE']=str(root)
        if not args.apply:
            return release.main()
        client=paramiko.SSHClient();client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect('9.134.11.169',port=36000,username='root',key_filename=args.key,timeout=20)
        sftp=client.open_sftp()
        gate='/etc/systemd/system/openclaw-web.service.d/91-code-analysis.conf'
        transient='/run/systemd/system/openclaw-web.service.d/90-code-analysis-migration.conf'
        gate_content=b'[Service]\nEnvironment="CODE_ANALYSIS_ENABLED=1"\n'
        transient_content=b'[Service]\nEnvironment="SKIP_AUTO_MIGRATE=1"\n'
        original=release.read(sftp,gate)
        if original not in (None,gate_content):
            raise RuntimeError('Existing feature override requires review')
        assert release.read(sftp,transient) is None
        success=False
        try:
            release.write(client,sftp,gate,gate_content,0o644)
            release.write(client,sftp,transient,transient_content,0o644)
            release.command(client,'systemctl daemon-reload')
            release.main()
            success=True
        finally:
            if not success:
                if original is None and release.read(sftp,gate)==gate_content:sftp.remove(gate)
                elif original is not None:release.write(client,sftp,gate,original,0o644)
            if release.read(sftp,transient)==transient_content:sftp.remove(transient)
            release.command(client,'systemctl daemon-reload')
            if not success:release.command(client,'systemctl restart openclaw-web')
            sftp.close();client.close()


if __name__=='__main__':main()
