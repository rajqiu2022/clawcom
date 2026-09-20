"""Additive Team MVP release. Preserve remote hotfixes; no gate/Worker/Run changes."""
import argparse
import ast
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import tempfile
from datetime import datetime

import paramiko

ROOT = Path(__file__).resolve().parents[1]
REMOTE = '/opt/openclaw-web'
BASE = 'c65ab0c'
FILES = (
    'app/models.py', 'app/api/__init__.py', 'app/api/agent_teams.py',
    'app/api/workflow_missions.py', 'app/api/mission_stages.py',
    'app/api/mission_handoffs.py', 'app/api/resource_leases.py',
    'app/api/agent_artifacts.py', 'app/services/agent_artifacts.py',
    'app/services/artifact_contracts.py',
    'app/services/agent_teams.py', 'app/services/resource_leases.py',
    'app/views/__init__.py', 'config.py', 'templates/base.html',
    'templates/automation_closed_loop.html', 'templates/agent_teams.html',
    'static/css/agent_teams.css', 'static/js/agent_teams.js',
)
MIGRATIONS = (
    '20260804_workflow_operation_idempotency.sql',
    '20260828_agent_artifacts.sql', '20260828_mission_stages.sql',
    '20260828_mission_handoffs.sql', '20260920_agent_teams.sql',
)


def sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read(sftp, path):
    try:
        with sftp.open(path, 'rb') as stream:
            return stream.read()
    except IOError as error:
        if error.errno == 2:
            return None
        raise


def command(client, cmd, timeout=90):
    _, out, err = client.exec_command(cmd, timeout=timeout)
    output, errors = out.read().decode('utf-8', 'replace'), err.read().decode('utf-8', 'replace')
    rc = out.channel.recv_exit_status()
    if rc:
        # No raw stderr: config/DB diagnostics can contain sensitive values.
        frames = [line.strip() for line in errors.splitlines() if line.lstrip().startswith('File ')]
        kinds = [line.split(':', 1)[0] for line in errors.splitlines() if re.match(r'^[A-Za-z_.]+(?:Error|Exception):', line)]
        print('REMOTE_ERROR', frames[-3:], kinds[-1:], flush=True)
        raise RuntimeError('Remote command failed rc=%s (output suppressed)' % rc)
    return output


def write(client, sftp, path, data, mode=0o600):
    command(client, 'mkdir -p ' + shlex.quote(os.path.dirname(path)))
    with sftp.open(path, 'wb') as stream:
        stream.write(data)
    sftp.chmod(path, mode)


def candidate(path, live):
    local = (ROOT / 'web' / path).read_bytes().replace(b'\r\n', b'\n')
    proc = subprocess.run(['git', 'show', BASE + ':web/' + path], cwd=str(ROOT), capture_output=True)
    base = proc.stdout.replace(b'\r\n', b'\n') if not proc.returncode else None
    if live is None:
        if base is not None and path not in {
            'app/api/mission_stages.py', 'app/api/mission_handoffs.py',
            'app/api/agent_artifacts.py', 'app/services/agent_artifacts.py',
            'app/services/artifact_contracts.py',
        }:
            raise RuntimeError('Missing deployed baseline: ' + path)
        return local
    live = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    if path == 'app/api/__init__.py' and b'agent_teams' not in live:
        # This release changes only the Team blueprint import. Production keeps
        # separate hotfix imports; never replace its complete registration list.
        return live.rstrip() + b'\n\nfrom app.api import agent_artifacts, mission_stages, mission_handoffs, agent_teams  # noqa: F401\n'
    if path == 'app/views/__init__.py' and b"'/agent-teams'" not in live:
        source = local.decode('utf-8')
        functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                     and any('/agent-teams' in ast.dump(d) for d in n.decorator_list)]
        assert len(functions) == 1
        node = functions[0]
        start = min(d.lineno for d in node.decorator_list)
        addition = '\n'.join(source.splitlines()[start - 1:node.end_lineno])
        return live.rstrip() + b'\n\n' + addition.encode('utf-8') + b'\n'
    if path == 'config.py':
        wanted = {'AGENT_TEAM_CONTRACTS_ENABLED', 'AGENT_TEAMS_ENABLED',
                  'AGENT_TEAMS_PROJECT_IDS', 'RESOURCE_LEASE_RECONCILIATION_ENABLED'}
        source = local.decode('utf-8')
        live_source = live.decode('utf-8')
        remote_class = next(n for n in ast.parse(live_source).body if isinstance(n, ast.ClassDef) and n.name == 'Config')
        local_class = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == 'Config')
        existing = {t.id for n in remote_class.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        additions = []
        for node in local_class.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted - existing for t in node.targets):
                additions.extend(source.splitlines(True)[node.lineno - 1:node.end_lineno])
        lines = live_source.splitlines(True)
        lines[remote_class.lineno:remote_class.lineno] = additions + ['\n']
        return ''.join(lines).encode('utf-8')
    if path == 'templates/base.html':
        source = live.decode('utf-8')
        if 'block body_class' not in source:
            assert source.count('<body>') == 1
            source = source.replace('<body>', '<body class="{% block body_class %}{% endblock %}">', 1)
        if 'block nav_agent_teams' not in source:
            anchor = '<a href="/engineering"'
            assert source.count(anchor) == 1
            addition = re.search(r'<a href="/agent-teams".*?</a>', local.decode('utf-8'), re.S).group(0)
            source = source.replace(anchor, addition + '\n                        ' + anchor, 1)
        return source.encode('utf-8')
    if live == local or live == base:
        return local
    if base is None:
        raise RuntimeError('Unexpected existing new file: ' + path)
    with tempfile.TemporaryDirectory(prefix='team-merge-') as folder:
        paths = [Path(folder) / name for name in ('live', 'base', 'local')]
        for target, value in zip(paths, (live, base, local)):
            target.write_bytes(value)
        merged = subprocess.run(['git', 'merge-file', '-p'] + [str(p) for p in paths], capture_output=True)
        if merged.returncode:
            raise RuntimeError('Remote hotfix merge conflict; manual review required: ' + path)
        return merged.stdout


PROBE = r'''
import os, json, subprocess
pid = subprocess.check_output(['systemctl', 'show', 'openclaw-web', '-p', 'MainPID']).decode().strip().split('=', 1)[1]
with open('/proc/' + pid + '/environ', 'rb') as stream:
    for item in stream.read().split(b'\0'):
        if b'=' in item:
            k, v = item.split(b'=', 1)
            os.environ[k.decode()] = v.decode()
os.environ['SKIP_AUTO_MIGRATE'] = '1'
from dotenv import load_dotenv
load_dotenv('/opt/openclaw-web/.env')
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    names = ['workflow_missions','workflow_mission_dispatches','workflow_operation_idempotencies',
             'agent_artifacts','mission_stages','mission_handoffs','agent_teams','agent_team_members','agent_team_missions']
    result = {'db_version': db.session.execute(text('SELECT VERSION()')).scalar(), 'tables': {}}
    for name in names:
        result['tables'][name] = [c['name'] for c in inspector.get_columns(name)] if name in tables else None
    result['gates'] = {key: app.config.get(key) for key in ['AGENT_TEAM_CONTRACTS_ENABLED', 'AGENT_TEAMS_ENABLED','AGENT_TEAMS_PROJECT_IDS','RESOURCE_LEASE_RECONCILIATION_ENABLED']}
    result['compat_session_get'] = hasattr(db.session, 'get')
    print('TEAM_RELEASE ' + json.dumps(result, ensure_ascii=False))
'''


ENV_SOURCE = PROBE.split('from app import create_app, db')[0]

SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
import tempfile
app = create_app('production')
with app.app_context():
    # Back up full DDL plus the existing idempotency rows. New tables have no data.
    os.umask(0o077)
    os.makedirs(backup, exist_ok=True)
    url = db.engine.url
    def quote(value):
        return '"' + str(value or '').replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'
    fd, defaults = tempfile.mkstemp(prefix='team-db-', suffix='.cnf', dir=backup)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write('[client]\nhost=' + quote(url.host or 'localhost') + '\nport=' + str(url.port or 3306) + '\nuser=' + quote(url.username) + '\npassword=' + quote(url.password) + '\n')
        common = ['mysqldump', '--defaults-extra-file=' + defaults, '--single-transaction', '--skip-lock-tables']
        with open(backup + '/schema.sql', 'wb') as stream:
            subprocess.run(common + ['--no-data', url.database], stdout=stream, stderr=subprocess.PIPE, check=True)
        with open(backup + '/idempotency.sql', 'wb') as stream:
            subprocess.run(common + [url.database, 'workflow_operation_idempotencies'], stdout=stream, stderr=subprocess.PIPE, check=True)
    finally:
        os.unlink(defaults)
    with db.engine.begin() as conn:
        fmt = conn.execute(text("SHOW GLOBAL VARIABLES LIKE 'innodb_file_format'")).fetchone()[1]
        prefix = conn.execute(text("SHOW GLOBAL VARIABLES LIKE 'innodb_large_prefix'")).fetchone()[1]
        try:
            if fmt.lower() != 'barracuda':
                conn.execute(text("SET GLOBAL innodb_file_format = 'Barracuda'"))
            if prefix.upper() != 'ON':
                conn.execute(text("SET GLOBAL innodb_large_prefix = ON"))
            for name, source in migrations:
                source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
                for statement in source.split(';'):
                    if not statement.strip():
                        continue
                    if 'DEFAULT CHARSET=utf8mb4' in statement:
                        statement = statement.replace('DEFAULT CHARSET=utf8mb4', 'DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC')
                    conn.execute(text(statement))
                print('TEAM_RELEASE ' + json.dumps({'migration': name, 'applied': True}))
        finally:
            if prefix.upper() != 'ON':
                conn.execute(text('SET GLOBAL innodb_large_prefix = OFF'))
            if fmt.lower() != 'barracuda':
                assert fmt in ('Antelope', 'Barracuda')
                conn.execute(text("SET GLOBAL innodb_file_format = '%s'" % fmt))
    inspector = inspect(db.engine)
    for name in ['agent_artifacts', 'mission_stages', 'mission_handoffs', 'agent_teams', 'agent_team_members', 'agent_team_missions']:
        actual = {c['name'] for c in inspector.get_columns(name)}
        expected = {c.name for c in db.metadata.tables[name].columns}
        assert expected <= actual, (name, sorted(expected - actual))
    print('TEAM_RELEASE ' + json.dumps({'schema_verified': True, 'backup': backup}))
'''

SMOKE = r'''
from app import create_app, db
from app.models import User, Project, AgentTeam
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    project = Project.query.order_by(Project.id).first()
    assert admin and project
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    paths = ['/agent-teams', '/workflows', '/test-reports', '/knowledge', '/automation-closed-loop']
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
    # Gates overridden only in this short-lived validation process, never the service.
    service_gates = {key: app.config.get(key) for key in ['AGENT_TEAMS_ENABLED','AGENT_TEAMS_PROJECT_IDS','AGENT_TEAM_CONTRACTS_ENABLED']}
    app.config.update(AGENT_TEAMS_ENABLED=True, AGENT_TEAM_CONTRACTS_ENABLED=True, AGENT_TEAMS_PROJECT_IDS=str(project.id))
    for path in ['/agent-teams', '/agent-teams/options', '/agent-teams/roles']:
        response = client.get('/api/v1' + path + '?project_id=' + str(project.id))
        assert response.status_code == 200, (path, response.status_code)
    assert client.get('/api/v1/agent-teams?project_id=' + str(project.id) + '&limit=0').status_code == 400
    anonymous = app.test_client()
    assert anonymous.get('/api/v1/agent-teams?project_id=' + str(project.id)).status_code == 401
    print('TEAM_RELEASE ' + json.dumps({'smoke': 'passed', 'pages': paths, 'team_read_apis': 3,
          'team_count': AgentTeam.query.count(), 'persistent_gates': service_gates, 'production_runs_started': 0}, ensure_ascii=False))
'''


def run_python(client, sftp, stage, name, source, cwd=REMOTE):
    path = stage + '/' + name + '.py'
    write(client, sftp, path, source.encode('utf-8'))
    expr = "exec(compile(open(%r).read(), %r, 'exec'))" % (path, path)
    output = command(client, 'cd ' + shlex.quote(cwd) + ' && SKIP_AUTO_MIGRATE=1 ' + REMOTE + '/venv/bin/python -c ' + shlex.quote(expr), 180)
    for line in output.splitlines():
        if line.startswith('TEAM_RELEASE '):
            print(line, flush=True)
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('9.134.11.169', port=36000, username='root', key_filename=args.key, timeout=20)
    sftp = client.open_sftp()
    try:
        stage = '/tmp/agent-teams-release-' + datetime.now().strftime('%Y%m%d-%H%M%S')
        originals, built = {}, {}
        for path in FILES:
            originals[path] = read(sftp, REMOTE + '/' + path)
            built[path] = candidate(path, originals[path])
            print('FILE', path, 'new' if originals[path] is None else 'update', 'merged=' + str(built[path] != (ROOT / 'web' / path).read_bytes().replace(b'\r\n', b'\n')), flush=True)
        command(client, 'mkdir -m 700 ' + shlex.quote(stage))
        for path, data in built.items():
            write(client, sftp, stage + '/web/' + path, data)
        python_files = [stage + '/web/' + path for path in FILES if path.endswith('.py')]
        command(client, REMOTE + '/venv/bin/python -m py_compile ' + ' '.join(map(shlex.quote, python_files)))
        run_python(client, sftp, stage, 'probe', PROBE)
        runtime = stage + '/runtime'
        prepare = '''import os, shutil
runtime = %r
os.makedirs(runtime)
for directory in ('app', 'templates', 'tools'):
    shutil.copytree(%r + '/' + directory, runtime + '/' + directory, ignore=shutil.ignore_patterns('__pycache__'))
shutil.copy2(%r + '/config.py', runtime + '/config.py')
for path in %r:
    target = runtime + '/' + path
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(%r + '/web/' + path, target)
''' % (runtime, REMOTE, REMOTE, FILES, stage)
        run_python(client, sftp, stage, 'prepare', prepare)
        run_python(client, sftp, stage, 'staged_probe', PROBE, cwd=runtime)
        print('PREFLIGHT_OK', stage, flush=True)
        if not args.apply:
            return
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=str(ROOT)).decode().strip()
        for path in FILES:
            committed = subprocess.check_output(['git', 'show', revision + ':web/' + path], cwd=str(ROOT))
            if committed.replace(b'\r\n', b'\n') != (ROOT / 'web' / path).read_bytes().replace(b'\r\n', b'\n'):
                raise RuntimeError('Commit release files before --apply: ' + path)
        backup = REMOTE + '/backups/agent-teams-' + stage.rsplit('-', 2)[-2] + '-' + stage.rsplit('-', 1)[-1]
        command(client, 'mkdir -m 700 ' + shlex.quote(backup))
        modes = {}
        for path, original in originals.items():
            modes[path] = sftp.stat(REMOTE + '/' + path).st_mode & 0o777 if original is not None else 0o644
            if original is not None:
                write(client, sftp, backup + '/files/' + path, original)
        manifest = {'commit': revision, 'base': BASE, 'created_at': datetime.now().isoformat(),
                    'backup': backup, 'files': {p: {'before': sha(originals[p]), 'after': sha(built[p]), 'mode': modes[p]} for p in FILES}}
        write(client, sftp, backup + '/release.json', json.dumps(manifest, indent=2).encode())
        payload = [(name, (ROOT / 'ops/migrations' / name).read_text(encoding='utf-8')) for name in MIGRATIONS]
        schema_source = ENV_SOURCE + '\nbackup = %r\nmigrations = %r\n' % (backup, payload) + SCHEMA
        run_python(client, sftp, stage, 'schema', schema_source, cwd=runtime)
        run_python(client, sftp, stage, 'staged_smoke', ENV_SOURCE + SMOKE, cwd=runtime)
        for path in FILES:
            if read(sftp, REMOTE + '/' + path) != originals[path]:
                raise RuntimeError('Live files changed concurrently: ' + path)
        switched = []
        stopped = False
        try:
            command(client, 'systemctl stop openclaw-web')
            stopped = True
            for path, data in built.items():
                destination = REMOTE + '/' + path
                write(client, sftp, destination + '.team-release-pending', data, modes[path])
                sftp.posix_rename(destination + '.team-release-pending', destination)
                switched.append(path)
            command(client, 'systemctl start openclaw-web')
            command(client, 'systemctl is-active openclaw-web')
            run_python(client, sftp, stage, 'live_smoke', ENV_SOURCE + SMOKE)
            for path, data in built.items():
                assert sha(read(sftp, REMOTE + '/' + path)) == sha(data), path
            write(client, sftp, REMOTE + '/AGENT_TEAMS_RELEASE.json', json.dumps(manifest, indent=2).encode())
            print('DEPLOY_OK', revision, backup, flush=True)
        except Exception:
            for path in reversed(switched):
                if originals[path] is None:
                    sftp.remove(REMOTE + '/' + path)
                else:
                    write(client, sftp, REMOTE + '/' + path, originals[path], modes[path])
            if stopped:
                command(client, 'systemctl restart openclaw-web')
            print('ROLLBACK_CODE_OK additive schema retained', backup, flush=True)
            raise
    finally:
        sftp.close()
        client.close()


if __name__ == '__main__':
    main()
