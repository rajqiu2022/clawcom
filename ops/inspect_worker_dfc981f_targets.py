"""Read-only release and target inventory for Worker dfc981f rollout."""

import os
import paramiko


REMOTE = '''
import json
from app import create_app, db
from app.models import AgentDeployment, AgentTask, ClawSidecarConfig, OpenClawInstance, WorkerRelease
app = create_app('production')
with app.app_context():
    rows = []
    for claw_id in (12, 54, 60):
        claw = db.session.get(OpenClawInstance, claw_id)
        cfg = db.session.get(ClawSidecarConfig, claw_id)
        dep = AgentDeployment.query.filter_by(openclaw_id=claw_id).order_by(AgentDeployment.id.desc()).first()
        rows.append({
            'claw_id': claw_id, 'name': claw.name if claw else None,
            'runtime': {key: (cfg.runtime_config_json or {}).get(key)
                        for key in ('kind', 'provider', 'runtime_mode', 'platform',
                                    'release_id', 'source_commit', 'install_root',
                                    'data_dir', 'workspace_dir', 'host')}
                        if cfg else None,
            'sidecar_heartbeat': str(cfg.last_heartbeat_at) if cfg else None,
            'deployment': ({key: getattr(dep, key) for key in ('id', 'status', 'host',
                           'remote_base_dir', 'worker_release_id', 'worker_source_commit')}
                           if dep else None),
            'active_agent_tasks': AgentTask.query.filter_by(claw_id=claw_id).filter(
                AgentTask.status.in_(('pending', 'running', 'waiting_condition'))).count(),
        })
    releases = [{key: getattr(row, key) for key in ('id', 'platform',
                 'approval_status', 'artifact_sha256')} for row in WorkerRelease.query.filter_by(
                 release_id='worker-dfc981f89d92e7bcbf517b41badd7a6958abf692').all()]
    print(json.dumps({'targets': rows, 'releases': releases}, ensure_ascii=False, default=str))
'''

ssh = paramiko.SSHClient()
ssh.load_system_host_keys()
ssh.connect('9.134.11.169', port=36000, username='root',
            key_filename=os.path.expanduser('~/.ssh/id_9.134.11.169'), timeout=30)
try:
    _, stdout, stderr = ssh.exec_command(
        "cd /opt/openclaw-web && SKIP_AUTO_MIGRATE=1 ./venv/bin/python - <<'PY'\n"
        + REMOTE + "\nPY", timeout=90)
    output = stdout.read().decode('utf-8', 'replace')
    error = stderr.read().decode('utf-8', 'replace')
    if stdout.channel.recv_exit_status():
        raise RuntimeError(error or output)
    print(output.strip().splitlines()[-1])
finally:
    ssh.close()
