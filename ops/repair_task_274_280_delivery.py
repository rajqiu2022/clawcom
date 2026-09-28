"""One-time, preconditioned correction of false-completed Task #274/#280."""

import argparse
import os

import paramiko


REMOTE = r'''
import json
import os
from datetime import datetime
from app import create_app, db
from app.models import AgentTask, AuditLog, MissionStage, TestReport, TestTask, _now
from app.services import plan_supervision

expected = {274: (4470, 69), 280: (4502, 75)}
rules = {'published_report': True, 'tapd_entities': ['bugs', 'requirements']}
app = create_app('production')
with app.app_context():
    report = db.session.get(TestReport, 799)
    assert report and report.status == 'draft' and not report.is_shared
    snapshot = {'report_799': {'status': report.status, 'is_shared': report.is_shared},
                'tasks': {}}
    rows = []
    for task_id, (agent_id, stage_id) in expected.items():
        task = db.session.get(TestTask, task_id)
        agent = db.session.get(AgentTask, agent_id)
        stage = db.session.get(MissionStage, stage_id)
        assert task and task.plan_id == 53 and task.status == 'completed' and task.progress == 100
        assert agent and agent.status == 'completed' and stage and stage.state == 'completed'
        link = plan_supervision._agent_task_plan_link(agent)
        assert link and link['test_task_id'] == task_id and link['mission_stage_id'] == stage_id
        result = json.loads(agent.result or '{}')
        assert result.get('status') == 'passed'
        snapshot['tasks'][str(task_id)] = {
            'status': task.status, 'progress': task.progress,
            'result_summary': task.result_summary,
            'action_metadata_json': task.action_metadata_json,
            'stage_id': stage.id, 'stage_state': stage.state,
            'stage_reason': stage.last_reason_code,
            'agent_task_id': agent.id, 'agent_status': agent.status,
        }
        rows.append((task, agent, stage, result))
    if not APPLY:
        print('TASK_DELIVERY ' + json.dumps({'dry_run': True, 'snapshot': snapshot},
                                            ensure_ascii=False, default=str))
    else:
        backup = '/opt/openclaw-web/backups/task-delivery-274-280-' + _now().strftime('%Y%m%d-%H%M%S') + '.json'
        os.umask(0o077)
        with open(backup, 'x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2, default=str)
        for task, agent, stage, result in rows:
            metadata = dict(task.action_metadata_json or {})
            metadata['delivery_acceptance'] = dict(rules)
            task.action_metadata_json = metadata
            db.session.flush()
            plan_supervision.record_agent_task_terminal(
                agent, result, 'completed', now=_now())
            assert task.status == 'blocked' and task.progress < 100
            assert stage.state == 'blocked'
            db.session.add(AuditLog(
                action='delivery_correction', resource_type='test_task',
                resource_id=task.id, resource_name=task.name,
                operator='codex:owner-request',
                detail=json.dumps({
                    'reason': 'failed TAPD verification and missing published task report',
                    'prior_status': 'completed', 'new_status': task.status,
                    'agent_task_id': agent.id, 'stage_id': stage.id,
                    'acceptance': rules, 'backup': backup,
                }, ensure_ascii=False)))
        db.session.commit()
        db.session.refresh(report)
        assert report.status == 'draft' and not report.is_shared
        print('TASK_DELIVERY ' + json.dumps({
            'applied': True, 'backup': backup,
            'tasks': {str(task.id): {'status': task.status, 'progress': task.progress,
                                    'stage': stage.state} for task, _, stage, _ in rows},
            'report_799': {'status': report.status, 'is_shared': report.is_shared},
        }, ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.connect('9.134.11.169', port=36000, username='root',
                key_filename=os.path.expanduser('~/.ssh/id_9.134.11.169'), timeout=30)
    try:
        source = 'APPLY = %r\n' % args.apply + REMOTE
        _, stdout, stderr = ssh.exec_command(
            "cd /opt/openclaw-web && SKIP_AUTO_MIGRATE=1 ./venv/bin/python - <<'PY'\n"
            + source + "\nPY", timeout=120)
        output = stdout.read().decode('utf-8', 'replace')
        error = stderr.read().decode('utf-8', 'replace')
        if stdout.channel.recv_exit_status():
            raise RuntimeError(error or output)
        for line in output.splitlines():
            if line.startswith('TASK_DELIVERY '):
                print(line)
    finally:
        ssh.close()


if __name__ == '__main__':
    main()
