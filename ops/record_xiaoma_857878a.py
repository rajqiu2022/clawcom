"""Record verified Claw #12 rollout to Worker 857878a in Hub."""

import json
from pathlib import Path

import paramiko


REMOTE = r'''
import json
from app import create_app, db
from app.models import AgentDeployment, ClawSidecarConfig, WorkerRelease
from app.services.worker_runtime import runtime_summary, validate_worker_runtime
release_id = 'worker-857878aa4bfcfa108e556ae2012184528711e2a9'
commit = '857878aa4bfcfa108e556ae2012184528711e2a9'
artifact_sha = '2ce2d21dd2c6f9305ab3bec481e33c3bd1bd4e3dcc7a1b8f54178a610b2d2ede'
app = create_app('production')
with app.app_context():
    cfg = db.session.get(ClawSidecarConfig, 12)
    assert cfg and cfg.runtime_reported_at
    validate_worker_runtime(cfg.runtime_config_json)
    runtime = runtime_summary(cfg.runtime_config_json or {}, cfg.config_owner or 'hub')
    raw = cfg.runtime_config_json or {}
    assert runtime.get('has_worker_runtime') is True
    assert raw.get('release_id') == release_id and raw.get('source_commit') == commit
    assert raw.get('artifact_sha256') == artifact_sha
    assert raw.get('runtime_mode') == 'agent_direct'
    release = WorkerRelease.query.filter_by(
        release_id=release_id, platform='linux-x86_64', approval_status='approved').one()
    assert release.id == 84 and release.artifact_sha256 == artifact_sha
    dep = AgentDeployment.query.filter_by(
        openclaw_id=12, worker_release_record_id=release.id,
        status='success').first()
    if dep is None:
        dep = AgentDeployment(
            openclaw_id=12, agent_type='codex', deploy_method='systemd',
            host='9.134.51.249:36000', ssh_user='root',
            remote_base_dir='/var/lib/claw-worker/claw-12',
            container_name='claw-worker-codex-12.service', image='',
            worker_release_record_id=release.id, worker_release_id=release_id,
            worker_source_commit=commit, worker_artifact_sha256=artifact_sha,
            status='success',
            log_tail='release hash verified; Worker and Hub proxy active; Hub runtime readback matched',
            error_message='', started_at=db.func.now(), finished_at=db.func.now(),
            triggered_by='Codex (owner-requested Agent Team direct rollout)')
        db.session.add(dep)
        db.session.commit()
    print(json.dumps({'deployment_id': dep.id, 'release_id': dep.worker_release_id,
                      'status': dep.status, 'runtime_reported_at': str(cfg.runtime_reported_at)}))
'''


def main():
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    ssh.connect('9.134.11.169', port=36000, username='root',
                key_filename=str(Path.home() / '.ssh' / 'id_9.134.11.169'), timeout=30)
    try:
        stdin, stdout, stderr = ssh.exec_command(
            "cd /opt/openclaw-web && SKIP_AUTO_MIGRATE=1 ./venv/bin/python - <<'PY'\n"
            + REMOTE + "\nPY", timeout=90)
        stdin.close()
        output = stdout.read().decode('utf-8', 'replace')
        errors = stderr.read().decode('utf-8', 'replace')
        if stdout.channel.recv_exit_status():
            raise RuntimeError(errors or output)
        print(output.strip().splitlines()[-1])
    finally:
        ssh.close()


if __name__ == '__main__':
    main()
