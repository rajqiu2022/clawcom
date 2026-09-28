"""Read-only delivery audit for TestTask #274 and Report #799."""

import os
import paramiko


REMOTE = '''
import json
from app import create_app, db
from app.models import TestTask, TestTaskReport, TestReport, AgentTask
app = create_app()
with app.app_context():
    task = db.session.get(TestTask, 274)
    task_280 = db.session.get(TestTask, 280)
    report = db.session.get(TestReport, 799)
    agent_tasks = AgentTask.query.filter(AgentTask.payload.like('%"test_task_id": 274%')).order_by(AgentTask.id.desc()).limit(5).all()
    def compact_result(row):
        value = json.loads(row.result or '{}')
        outputs = value.get('outputs') or {}
        return {'id': row.id, 'task_id': row.task_id, 'status': row.status,
                'result_status': value.get('status'), 'summary': value.get('summary'),
                'warnings': (value.get('logs') or {}).get('warnings'),
                'output_keys': list(outputs), 'freshness': outputs.get('freshness'),
                'report_id': outputs.get('report_id'), 'evidence_report':
                (value.get('evidence') or {}).get('report_id')}
    print(json.dumps({
        'task': {key: getattr(task, key, None) for key in ('id', 'name', 'description', 'status', 'progress', 'result_summary', 'plan_id')},
        'task_274_action_metadata': task.action_metadata_json,
        'task_280': {key: getattr(task_280, key, None) for key in ('id', 'name', 'description', 'status', 'progress', 'result_summary', 'plan_id')},
        'linked_reports': [(row.id, row.linked_test_report_id) for row in TestTaskReport.query.filter_by(task_id=274).all()],
        'report_799': {key: getattr(report, key, None) for key in ('id', 'status', 'is_shared', 'source_ref_type', 'source_ref_id')},
        'agent_tasks': [compact_result(row) for row in agent_tasks],
    }, ensure_ascii=False, default=str))
'''

ssh = paramiko.SSHClient()
ssh.load_system_host_keys()
ssh.connect('9.134.11.169', port=36000, username='root',
            key_filename=os.path.expanduser('~/.ssh/id_9.134.11.169'), timeout=30)
try:
    _, stdout, stderr = ssh.exec_command(
        "cd /opt/openclaw-web && ./venv/bin/python - <<'PY'\n" + REMOTE + "\nPY", timeout=90)
    output = stdout.read().decode('utf-8', 'replace')
    error = stderr.read().decode('utf-8', 'replace')
    if stdout.channel.recv_exit_status():
        raise RuntimeError(error or output)
    print(output.strip().splitlines()[-1])
finally:
    ssh.close()
