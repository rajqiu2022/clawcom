"""Read-only production team/Flow authorization inventory; never reads tokens."""
import argparse
from datetime import datetime
import paramiko
import deploy_agent_teams as transport

AUDIT = r'''
from app import create_app, db
from app.models import AgentTeam, OpenClawInstance, WorkflowDefinition, ClawSidecarConfig
from app.services.agent_system_context import allowed_workflow_create_definition_ids, _resolve_workflow_policy
from app.services.worker_runtime import runtime_summary
app = create_app('production')
with app.app_context():
    try:
        from app.services.agent_team_permissions import flow_grants, can_dispatch_to
        upgraded = True
    except ImportError:
        upgraded = False
    teams = AgentTeam.query.order_by(AgentTeam.id).all()
    definitions = WorkflowDefinition.query.filter_by(status='active').all()
    for team in teams:
        flow_ids = (team.policy_json or {}).get('allowed_definition_ids', [])
        members = {m.claw_id for m in team.members}
        ids = members | {team.primary_manager_claw_id}
        if team.backup_manager_claw_id:
            ids.add(team.backup_manager_claw_id)
        flows, invalid = [], []
        for fid in flow_ids:
            definition = db.session.get(WorkflowDefinition, fid)
            valid = bool(definition and definition.status == 'active' and definition.project_id == team.project_id)
            if not valid:
                invalid.append(fid)
            flows.append({'id':fid,'name':definition.name if definition else None,'valid':valid})
        people = []
        for cid in sorted(ids):
            claw = db.session.get(OpenClawInstance, cid)
            cfg = db.session.get(ClawSidecarConfig, cid)
            valid = bool(claw and claw.status != 'deleted' and claw.project_id == team.project_id)
            manual = allowed_workflow_create_definition_ids(claw, definitions) if claw else []
            derived = flow_grants(claw, definitions) if upgraded and valid else []
            runtime = runtime_summary(cfg.runtime_config_json or {}, cfg.config_owner or 'hub') if cfg else {}
            provider = runtime.get('provider') or (cfg.agent_type if cfg else '')
            policy, warnings = _resolve_workflow_policy(provider, sorted(set(manual+derived)),
                cfg.system_context_policy_json if cfg else None, **(
                    {'team_workflow_create_definition_ids':derived} if upgraded else {}))
            allowed = policy.get('allowed_workflow_create_definition_ids', [])
            people.append({'claw_id':cid,'name':claw.name if claw else None,'valid':valid,
                'manager':cid in (team.primary_manager_claw_id,team.backup_manager_claw_id),
                'roles':[m.role_key for m in team.members if m.claw_id==cid],
                'manual_missing':sorted(set(flow_ids)-set(manual)),
                'effective_missing':sorted(set(flow_ids)-set(allowed)),
                'effective_allowed':allowed,'warnings':warnings,'provider':provider,
                'has_worker_runtime':runtime.get('has_worker_runtime'),
                'last_sidecar_refresh':str(cfg.last_heartbeat_at) if cfg else None})
        dispatch_checks = []
        if upgraded and team.status == 'active':
            for fid in flow_ids:
                definition = db.session.get(WorkflowDefinition,fid)
                if definition:
                    dispatch_checks.append({'flow_id':fid,'authorized_workers':[
                        cid for cid in sorted(members)
                        if can_dispatch_to(team.primary_manager_claw_id,cid,definition)]})
        print('TEAM_RELEASE '+json.dumps({'team_id':team.id,'name':team.name,'status':team.status,
            'project_id':team.project_id,'version':team.version,'upgraded':upgraded,
            'flows':flows,'invalid_flow_ids':invalid,'members':people,'dispatch':dispatch_checks},ensure_ascii=False))
    db.session.remove()
'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('9.134.11.169', port=36000, username='root', key_filename=args.key, timeout=20)
    sftp = client.open_sftp()
    try:
        stage = '/tmp/team-grants-audit-' + datetime.now().strftime('%Y%m%d-%H%M%S')
        transport.run_python(client, sftp, stage, 'audit', transport.ENV_SOURCE+AUDIT)
    finally:
        sftp.close()
        client.close()

if __name__ == '__main__':
    main()
