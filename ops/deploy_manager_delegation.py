"""Deploy the 2026-09-26 Agent-team/Flow recovery bundle to the Hub host.

Additive release. Ships one reviewed bundle and never touches the files that
production carries ahead of this branch (``app/api/__init__.py`` registers
remote-only blueprints such as ``agent_eval`` / ``agent_control``).

Safety model, in order:

1. dry run by default — every write needs ``--apply``;
2. read the live bytes first and refuse to proceed when a target already
   contains part of this release (a half-applied deploy must be inspected, not
   overwritten);
3. back up each replaced file to ``<path>.bak.<utc-stamp>``;
4. upload, then re-read from the host and compare sha256 (CRLF-normalised)
   against the local working tree, so a truncated transfer cannot pass;
5. ``py_compile`` every target and import the new service module;
6. only with ``--restart`` restart ``openclaw-web`` and prove the new route is
   registered (401 unauthorised, not 404 missing).

Usage (on the Hub host or through this remote harness):

    python ops/deploy_manager_delegation.py                    # dry run
    python ops/deploy_manager_delegation.py --apply            # files only
    python ops/deploy_manager_delegation.py --apply --restart  # files + restart
"""
import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paramiko  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROOT = ROOT / 'web'
REMOTE = '/opt/openclaw-web'
HOST, PORT, USER = '9.134.11.169', 36000, 'root'
KEY = 'C:/Users/rajqiu/.ssh/id_9.134.11.169'
SERVICE = 'openclaw-web'
# Route that only exists once this release is live. Unauthenticated access
# returns 401 when registered and 404 when the blueprint entry is missing.
PROBE_ROUTE = '/api/v1/agent-teams/1/manager-delegation'

FILES = (
    'app/models.py',
    'config.py',
    'app/__init__.py',
    'app/api/agent_teams.py',
    'app/api/plan_supervision.py',
    'app/api/workflow_missions.py',
    'app/api/workflows.py',
    'app/services/agent_team_manager_delegation.py',
    'app/services/chat_message_presentation.py',
    'app/services/chat_rooms.py',
    'app/services/plan_supervision.py',
    'app/services/worker_runtime.py',
    'static/css/agent_teams.css',
    'static/js/agent_team_chat.js',
    'static/js/agent_teams.js',
    'templates/agent_teams.html',
)
# Files the deploy must never replace: production runs ahead of this branch.
GUARDED = ('app/api/__init__.py',)

# Production carries independent hotfixes in these two large API modules.  A
# full-file copy would silently roll them back, so build the deploy candidate
# from the live bytes and only layer this release's delta on top.
LIVE_MERGE_BASE = {
    'app/api/plan_supervision.py': '438dd91',
    'app/api/workflows.py': '438dd91',
}
LIVE_RELEASE_MARKERS = {
    'app/api/plan_supervision.py': (
        b'def create_test_task_recovery_attempt',
        b'def reassign_test_task_stage',
    ),
    'app/api/workflows.py': (
        b'workflow_runtime_compatibility',
        b'def _has_explicit_step_acting_identity',
        b"config['execution_target_source']",
    ),
}

VERIFY_SOURCE = r'''
import json as _json
import py_compile
import sys

TARGETS = _json.loads(sys.argv[1])
report = {'compiled': [], 'compile_errors': [], 'import_ok': None}
for rel in TARGETS:
    if not rel.endswith('.py'):
        continue
    path = '/opt/openclaw-web/' + rel
    try:
        py_compile.compile(path, doraise=True, cfile='/tmp/_pc.pyc')
        report['compiled'].append(rel)
    except Exception as exc:
        report['compile_errors'].append({'file': rel, 'error': str(exc)[:400]})
try:
    from app.services import agent_team_manager_delegation as mod
    from app.services.worker_runtime import workflow_runtime_compatibility
    from app.services.chat_message_presentation import message_presentation
    report['import_ok'] = bool(
        mod.enabled is not None and mod.delegate
        and workflow_runtime_compatibility and message_presentation)
    report['symbols'] = sorted(
        name for name in ('delegate', 'revoke', 'state', 'due_for_revoke',
                          'enabled', 'HISTORY_LIMIT') if hasattr(mod, name))
except Exception as exc:
    report['import_error'] = '%s: %s' % (type(exc).__name__, exc)
print('VERIFY ' + _json.dumps(report, ensure_ascii=False))
'''


def normalise(data):
    return data.replace(b'\r\n', b'\n').replace(b'\r\r\n', b'\n')


def digest(data):
    return hashlib.sha256(normalise(data)).hexdigest()


def _git_blob(revision, rel):
    return subprocess.check_output(
        ['git', 'show', '%s:web/%s' % (revision, rel)], cwd=ROOT)


def _ensure_workflow_identity_helpers(candidate, local):
    marker = b'def _has_explicit_step_acting_identity'
    if marker in candidate:
        return candidate
    start = local.index(marker)
    end = local.index(b'def _resolve_step_target_claw_ids', start)
    insert_at = candidate.index(b'def _resolve_step_target_claw_ids')
    return candidate[:insert_at] + local[start:end] + candidate[insert_at:]


def _ensure_plan_reassign_route(candidate, local):
    """Add only the new manager action route to a drifted live API file."""
    marker = b'def reassign_test_task_stage'
    if marker in candidate:
        return candidate
    function_at = local.index(marker)
    start = local.rfind(b'@api_bp.route', 0, function_at)
    end = local.index(b'@api_bp.route', function_at)
    insert_marker = (
        b"@api_bp.route(\n"
        b"    '/test-plans/<int:plan_id>/supervision/occurrences/")
    insert_at = candidate.index(insert_marker)
    return candidate[:insert_at] + local[start:end] + candidate[insert_at:]


def _live_merge(rel, live, local):
    """Return live production plus this release's changes, or fail closed."""
    markers = LIVE_RELEASE_MARKERS.get(rel, ())
    if markers and all(marker in live for marker in markers):
        return normalise(live), 'already_live_merged'
    if (rel == 'app/api/workflows.py'
            and b'workflow_runtime_compatibility' in live
            and b"config['execution_target_source']" in live):
        return normalise(_ensure_workflow_identity_helpers(live, local)), \
            'complete_partial_live_patch'
    revision = LIVE_MERGE_BASE[rel]
    base = _git_blob(revision, rel)
    if rel == 'app/api/plan_supervision.py':
        return normalise(_ensure_plan_reassign_route(live, local)), \
            'additive_live_route'
    with tempfile.TemporaryDirectory(prefix='hub-live-merge-') as raw_dir:
        temp = Path(raw_dir)
        # The worker-binding fallback hunk is already live.  Apply the other
        # Flow identity/runtime-preflight hunks to the production source.
        patch = subprocess.check_output(
            ['git', 'diff', revision, '--', 'web/' + rel], cwd=ROOT)
        chunks = re.split(r'(?=^@@ )', patch.decode('utf-8'), flags=re.M)
        patch = ''.join(
            chunk for chunk in chunks
            if not (chunk.startswith('@@ ')
                    and '_resolve_step_target_claw_ids' in chunk)
        ).encode('utf-8')
        target = temp / 'web' / rel
        target.parent.mkdir(parents=True)
        target.write_bytes(live)
        patch_path = temp / 'release.patch'
        patch_path.write_bytes(patch)
        proc = subprocess.run(
            ['git', 'apply', '--reject', '--whitespace=nowarn', str(patch_path)],
            cwd=temp, capture_output=True)
        rejects = list(temp.rglob('*.rej'))
        if proc.returncode != 0 or rejects:
            raise RuntimeError(
                'live additive patch conflict for %s (rc=%s, rejects=%s)' %
                (rel, proc.returncode, len(rejects)))
        candidate = _ensure_workflow_identity_helpers(target.read_bytes(), local)
        return normalise(candidate), 'additive_live_patch'


def command(client, cmd, timeout=120):
    _, out, err = client.exec_command(cmd, timeout=timeout)
    output = out.read().decode('utf-8', 'replace')
    errors = err.read().decode('utf-8', 'replace')
    code = out.channel.recv_exit_status()
    if code:
        raise RuntimeError('remote rc=%s cmd=%s err=%s'
                           % (code, cmd[:120], errors[-400:]))
    return output


def read(sftp, path):
    try:
        with sftp.open(path, 'rb') as stream:
            return stream.read()
    except IOError:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Deploy manager-delegation release')
    parser.add_argument('--apply', action='store_true',
                        help='禁止隐式写入：不带此参数只做只读预演')
    parser.add_argument('--restart', action='store_true',
                        help='上传校验通过后重启服务并验证路由')
    args = parser.parse_args(argv)
    stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')

    local = {rel: (LOCAL_ROOT / rel).read_bytes() for rel in FILES}
    missing = [rel for rel in FILES if rel not in local or not local[rel]]
    if missing:
        print('LOCAL_MISSING', missing)
        return 2
    report = {'dry_run': not args.apply, 'stamp': stamp, 'files': {}, 'guarded': {}}

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(HOST, port=PORT, username=USER, key_filename=KEY, timeout=25)
    try:
        with client.open_sftp() as sftp:
            for rel in GUARDED:
                live = read(sftp, REMOTE + '/' + rel)
                report['guarded'][rel] = {
                    'remote_sha256': digest(live)[:16] if live else None,
                    'untouched': True,
                }
            upload = []
            candidates = {}
            for rel in FILES:
                remote = REMOTE + '/' + rel
                live = read(sftp, remote)
                candidate = normalise(local[rel])
                merge_mode = 'replace'
                if live is not None and rel in LIVE_MERGE_BASE:
                    candidate, merge_mode = _live_merge(rel, live, local[rel])
                if rel.endswith('.py'):
                    compile(candidate.decode('utf-8'), rel, 'exec')
                candidates[rel] = candidate
                entry = {
                    'remote_exists': live is not None,
                    'remote_sha256': digest(live)[:16] if live else None,
                    'local_sha256': digest(local[rel])[:16],
                    'candidate_sha256': digest(candidate)[:16],
                    'identical': bool(live is not None and digest(live) == digest(candidate)),
                    'bytes': len(candidate),
                    'merge_mode': merge_mode,
                }
                report['files'][rel] = entry
                # A live file that already contains this release would mean a
                # half-applied deploy. Stop and let a human look, rather than
                # overwriting evidence.
                if live is not None and not entry['identical'] \
                        and b'manager_delegation' in live and 'api/agent_teams' in rel:
                    entry['refused'] = 'live file already mentions manager_delegation'
                    print(json.dumps(report, ensure_ascii=False, indent=1))
                    print('REFUSED: inspect the host before re-running')
                    return 4
                if not entry['identical']:
                    upload.append(rel)
            report['to_upload'] = upload
            if not args.apply:
                print(json.dumps(report, ensure_ascii=False, indent=1))
                print('DRY_RUN: re-run with --apply to write')
                return 0

            for rel in upload:
                remote = REMOTE + '/' + rel
                live = read(sftp, remote)
                if live is not None:
                    backup = '%s.bak.%s' % (remote, stamp)
                    command(client, 'cp -p %s %s' % (shlex.quote(remote), shlex.quote(backup)))
                    report['files'][rel]['backup'] = backup
                payload = candidates[rel]
                with sftp.open(remote, 'wb') as stream:
                    stream.write(payload)
                sftp.chmod(remote, 0o644)
            report['uploaded'] = upload

            mismatched = []
            for rel in FILES:
                live = read(sftp, REMOTE + '/' + rel)
                ok = live is not None and digest(live) == digest(candidates[rel])
                report['files'][rel]['verified'] = ok
                if not ok:
                    mismatched.append(rel)
            report['mismatched'] = mismatched
            if mismatched:
                print(json.dumps(report, ensure_ascii=False, indent=1))
                print('VERIFY_FAILED_HASH')
                return 5

            stage = '/tmp/verify-delegation-' + uuid.uuid4().hex
            command(client, 'mkdir -m 700 ' + stage)
            try:
                with sftp.open(stage + '/verify.py', 'wb') as stream:
                    stream.write(VERIFY_SOURCE.encode('utf-8'))
                output = command(
                    client,
                    'cd %s && PYTHONPATH=%s SKIP_AUTO_MIGRATE=1 ./venv/bin/python %s/verify.py %s'
                    % (REMOTE, REMOTE, stage, shlex.quote(json.dumps(list(FILES)))),
                    timeout=180)
                report['verify'] = json.loads(output.split('VERIFY ', 1)[1])
            finally:
                command(client, 'rm -rf ' + stage)

            if report['verify'].get('compile_errors') or not report['verify'].get('import_ok'):
                print(json.dumps(report, ensure_ascii=False, indent=1))
                print('VERIFY_FAILED_CODE')
                return 6

            if args.restart:
                command(client, 'systemctl restart ' + SERVICE, timeout=180)
                command(client, 'sleep 4; systemctl is-active ' + SERVICE)
                report['service_active'] = command(
                    client, 'systemctl is-active ' + SERVICE).strip()
                # Plain concatenation: no % formatting here, so the curl format
                # string keeps its single % and curl expands it.
                report['root_status'] = command(
                    client, "curl -s -o /dev/null -w '%{http_code}' --max-time 8 "
                            "http://127.0.0.1:18800/").strip()
                report['probe_route_status'] = command(
                    client, "curl -s -o /dev/null -w '%{http_code}' --max-time 8 "
                            "http://127.0.0.1:18800" + PROBE_ROUTE).strip()
                report['gunicorn_workers'] = command(
                    client, "ps -ef | grep -c '[g]unicorn'").strip()
    finally:
        client.close()

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
