"""Approve one pinned, immutable Worker release in the Hub catalog."""

import json
from pathlib import Path

import paramiko


HOST = '9.134.11.169'
PORT = 36000
KEY = Path.home() / '.ssh' / 'id_9.134.11.169'
HUB_ROOT = '/opt/openclaw-web'
REPO_PATH = HUB_ROOT + '/hub-store/worker-release-source-8273d2d'
REPO_URL = 'https://git.woa.com/rajqiu/claw-worker-windows.git'
BRANCH = 'codex/ordinary-agent-task-contract'


def _run(ssh, command, timeout=900):
    stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    stdin.close()
    output = stdout.read().decode('utf-8', 'replace')
    errors = stderr.read().decode('utf-8', 'replace')
    if stdout.channel.recv_exit_status():
        raise RuntimeError('Worker release remote command failed: ' +
                           (errors or output)[-2000:])
    return output


def publish_release(*, catalog_commit, source_commit, manifest_sha, artifacts):
    release_id = 'worker-' + source_commit
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    ssh.connect(HOST, port=PORT, username='root', key_filename=str(KEY), timeout=30)
    try:
        _run(ssh, f'''set -eu
test -d {REPO_PATH}/.git
test "$(git -C {REPO_PATH} remote get-url origin)" = "{REPO_URL}"
git -C {REPO_PATH} fetch --prune origin {BRANCH}
git -C {REPO_PATH} checkout --detach {catalog_commit}
test "$(git -C {REPO_PATH} rev-parse HEAD)" = "{catalog_commit}"
test "$(git -C {REPO_PATH} rev-parse HEAD^)" = "{source_commit}"
test "$(sha256sum {REPO_PATH}/dist/worker-releases/{release_id}/release.json | cut -d' ' -f1)" = "{manifest_sha}"
''')
        script = r'''
import json
from app import create_app, db
from app.models import User, WorkerRelease
from app.services.worker_releases import verify_record_artifact

release_id = __RELEASE_ID__
source_commit = __SOURCE_COMMIT__
manifest_sha = __MANIFEST_SHA__
artifacts = __ARTIFACTS__

app = create_app()
with app.app_context():
    admin = User.query.filter_by(username='rajqiu', role='super_admin').first()
    assert admin is not None
    admin_id = admin.id
client = app.test_client()
with client.session_transaction() as session:
    session['user_id'] = admin_id
results = {}
for platform, expected_sha in artifacts.items():
    response = client.post('/api/v1/worker-releases/refresh', json={
        'platform': platform,
    })
    body = response.get_json(silent=True) or {}
    assert response.status_code in (200, 201), (platform, response.status_code, body)
    record = body.get('release') or {}
    assert record.get('release_id') == release_id, record
    assert record.get('source_commit') == source_commit, record
    assert record.get('release_manifest_sha256') == manifest_sha, record
    assert record.get('platform') == platform, record
    assert record.get('artifact_sha256') == expected_sha, record
    response = client.post('/api/v1/worker-releases/%s/approve' % record['id'],
                           json={'confirm_unsigned': True})
    body = response.get_json(silent=True) or {}
    assert response.status_code == 200, (platform, response.status_code, body)
    record = body.get('release') or {}
    assert record.get('approval_status') == 'approved', record
    with app.app_context():
        stored = db.session.get(WorkerRelease, int(record['id']))
        artifact = verify_record_artifact(stored)
        results[platform] = {
            'record_id': stored.id,
            'release_id': stored.release_id,
            'approval_status': stored.approval_status,
            'artifact_sha256': stored.artifact_sha256,
            'artifact_verified': artifact.is_file(),
        }
print(json.dumps(results, ensure_ascii=False, sort_keys=True))
'''
        script = (script.replace('__RELEASE_ID__', json.dumps(release_id))
                        .replace('__SOURCE_COMMIT__', json.dumps(source_commit))
                        .replace('__MANIFEST_SHA__', json.dumps(manifest_sha))
                        .replace('__ARTIFACTS__', json.dumps(artifacts)))
        output = _run(ssh, "cd %s && ./venv/bin/python - <<'PY'\n%s\nPY" % (
            HUB_ROOT, script.strip()))
        return json.loads(output.strip().splitlines()[-1])
    finally:
        ssh.close()
