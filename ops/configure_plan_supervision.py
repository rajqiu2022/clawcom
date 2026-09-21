"""Enable the first existing team's Hub capability, not its business Plan.

Requires the matching deployed release. Does not touch Worker hosts or enqueue
messages. The manager must implement the contract before calling Plan /start.
"""
import argparse
import json
import subprocess
from datetime import datetime
import paramiko
import deploy_plan_supervision as deployment

r = deployment.release
TARGET = '/etc/systemd/system/openclaw-web.service.d/60-plan-supervision.conf'

CHECK = r'''
from app import create_app, db
from app.models import AgentTeam, TestPlan
from app.models_plan_supervision import PlanSupervisor
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    first = AgentTeam.query.filter(AgentTeam.status != 'archived').order_by(AgentTeam.id).first()
    assert first and first.id == requested_team and first.status == 'active', 'First team changed'
    assert 'team_id' in {c['name'] for c in inspect(db.engine).get_columns('plan_supervisors')}
    print('TEAM_RELEASE '+json.dumps({'team_id':first.id,'name':first.name,
        'manager':first.primary_manager_claw_id,'project_id':first.project_id,
        'supervisor_count':PlanSupervisor.query.count(),
        'plan50_status':db.session.get(TestPlan,50).status},ensure_ascii=False))
'''

VERIFY = r'''
from app import create_app, db
from app.models import AgentTeam, OpenClawInstance, User, TestPlan
from app.models_plan_supervision import PlanSupervisor
from app.services.agent_team_context import build_team_context
from app.services.plan_supervision import team_enabled
import requests, time
app = create_app('production')
with app.app_context():
    assert app.config['PLAN_SUPERVISION_ENABLED'] is True
    assert app.config['PLAN_SUPERVISION_TEAM_IDS'] == str(requested_team)
    assert os.environ.get('TIMEOUT_WATCHER_ENABLED','1') not in ('0','false','False')
    team = db.session.get(AgentTeam,requested_team)
    manager = db.session.get(OpenClawInstance,team.primary_manager_claw_id)
    context = next(item for item in build_team_context(manager) if item['team_id'] == team.id)
    assert context['plan_supervision']['enabled'] is True
    assert not team_enabled(requested_team+100000)
    admin = User.query.filter_by(role='super_admin').first()
    cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin.id})
    headers = {'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
    result = {'team_id':team.id,'name':team.name,'manager':manager.id,
              'plan50_status':db.session.get(TestPlan,50).status,
              'supervisor_count':PlanSupervisor.query.count(),
              'sidecar_team_capability':context['plan_supervision'],
              'watchdog_enabled':True,'runs_started':0}
    db.session.remove()
    for attempt in range(20):
        try:
            response = requests.get('http://127.0.0.1:18800/api/v1/agent-teams/'+str(requested_team),
                                    headers=headers,timeout=5)
            if response.status_code == 200 and response.json().get('plan_supervision',{}).get('enabled'):
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Scoped capability HTTP readiness failed')
    response = requests.get('http://127.0.0.1:18800/api/v1/test-plans/50/supervision',headers=headers,timeout=10)
    assert response.status_code == 200
    result['http_supervision_status'] = response.status_code
    result['supervision'] = response.json().get('supervision')
    print('TEAM_RELEASE '+json.dumps(result,ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    parser.add_argument('--team-id', required=True, type=int)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    assert args.team_id > 0
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=r.ROOT).decode().strip()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('9.134.11.169',port=36000,username='root',key_filename=args.key,timeout=20)
    sftp = client.open_sftp()
    try:
        manifest = json.loads(r.read(sftp,r.REMOTE+'/AGENT_TEAMS_RELEASE.json'))
        assert manifest['commit'] == revision, 'Enable only the verified deployed commit'
        for path, info in manifest['files'].items():
            assert r.sha(r.read(sftp,r.REMOTE+'/'+path)) == info['after'], 'Deployed file drift: '+path
        stage = '/tmp/plan-supervision-enable-'+datetime.now().strftime('%Y%m%d-%H%M%S')
        r.command(client,'mkdir -m 700 '+stage)
        prefix = r.ENV_SOURCE+'\nrequested_team = %d\n' % args.team_id
        r.run_python(client,sftp,stage,'check',prefix+CHECK)
        old = r.read(sftp,TARGET)
        desired = ('[Service]\nEnvironment="PLAN_SUPERVISION_ENABLED=1"\n'
                   'Environment="PLAN_SUPERVISION_TEAM_IDS=%d"\n' % args.team_id).encode()
        assert old is None or old == desired, 'Existing supervisor override requires review'
        if not args.apply:
            print('ENABLE_PREFLIGHT_OK')
            return
        changed = old != desired
        if old:
            r.write(client,sftp,stage+'/previous.conf',old)
        try:
            if changed:
                r.write(client,sftp,TARGET,desired,0o644)
                r.command(client,'systemctl daemon-reload && systemctl restart openclaw-web')
            r.command(client,'systemctl is-active openclaw-web')
            r.run_python(client,sftp,stage,'verify',prefix+VERIFY)
            audit = {'commit':revision,'team_id':args.team_id,'enabled':True,
                     'at':datetime.now().isoformat(),'auto_started_plans':False}
            r.write(client,sftp,stage+'/activation.json',json.dumps(audit).encode())
            print('ENABLE_OK',json.dumps(audit))
        except Exception:
            if changed:
                if old is None:
                    sftp.remove(TARGET)
                else:
                    r.write(client,sftp,TARGET,old,0o644)
                r.command(client,'systemctl daemon-reload && systemctl restart openclaw-web')
            raise
    finally:
        sftp.close()
        client.close()


if __name__ == '__main__':
    main()
