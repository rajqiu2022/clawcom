"""Release team plans and live Flow grants; preserve production-only hotfixes.

Preflight by default; --apply requires committed files. No Flow/Mission starts,
no manual ACL overwrites, no Worker changes, no inferred Plan associations.
"""
import deploy_team_activity_knowledge as deployment

release = deployment.release
release.BASE = '8186ed5413064fea49f35d6eb2aaba9f38ccf641'
release.FILES = (
    'app/models.py', 'app/api/agent_client.py', 'app/api/agent_teams.py',
    'app/api/plan_supervision.py', 'app/api/testplans.py',
    'app/api/workflow_missions.py', 'app/api/workflows.py',
    'app/services/agent_system_context.py', 'app/services/agent_team_context.py',
    'app/services/agent_team_permissions.py', 'app/services/agent_team_plans.py',
    'templates/agent_teams.html', 'templates/testplans.html',
    'static/js/agent_teams.js', 'static/js/agent_team_activity.js',
    'static/js/agent_team_plans.js', 'static/css/agent_team_plans.css',
)
release.MIGRATIONS = ('20260921_agent_team_test_plans.sql',)
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    os.umask(0o077)
    ddl = db.session.execute(text('SHOW CREATE TABLE test_plans')).fetchone()[1]
    with open(backup+'/test_plans_schema.sql','w') as stream:
        stream.write(ddl+';\n')
    db.session.remove()
    with db.engine.begin() as conn:
        for name, source in migrations:
            source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
            for statement in source.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
            print('TEAM_RELEASE '+json.dumps({'migration':name,'applied':True}))
    inspector = inspect(db.engine)
    assert 'team_id' in {c['name'] for c in inspector.get_columns('test_plans')}
    assert any(i['column_names']==['team_id'] for i in inspector.get_indexes('test_plans'))
    assert any(i['constrained_columns']==['team_id'] and i['referred_table']=='agent_teams'
               for i in inspector.get_foreign_keys('test_plans'))
    print('TEAM_RELEASE '+json.dumps({'schema_verified':True,'backup':backup}))
'''
release.SMOKE = r'''
from app import create_app, db
from app.models import User, AgentTeam, OpenClawInstance, WorkflowDefinition, ClawSidecarConfig
from app.services.agent_team_permissions import flow_grants, can_dispatch_to
from app.services.agent_system_context import allowed_workflow_create_definition_ids, _resolve_workflow_policy
from app.services.worker_runtime import runtime_summary
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    admin_id = admin.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin_id
    paths = ['/agent-teams','/workflows','/test-plans','/test-reports','/knowledge']
    teams = AgentTeam.query.filter_by(status='active').all()
    definitions = WorkflowDefinition.query.filter_by(status='active').all()
    checks = []
    for team in teams:
        selected = set((team.policy_json or {})['allowed_definition_ids'])
        selected_flows = [d for d in definitions if d.id in selected]
        assert len(selected_flows)==len(selected)
        assert all(d.project_id==team.project_id for d in selected_flows)
        members = {m.claw_id for m in team.members}
        people = members | {team.primary_manager_claw_id}
        if team.backup_manager_claw_id:
            people.add(team.backup_manager_claw_id)
        for cid in people:
            claw = db.session.get(OpenClawInstance,cid)
            assert claw and claw.project_id==team.project_id and claw.status!='deleted'
            grants = flow_grants(claw,definitions)
            assert selected <= set(grants), ('team_grants_missing',team.id,cid)
            cfg = db.session.get(ClawSidecarConfig,cid)
            runtime = runtime_summary(cfg.runtime_config_json or {},cfg.config_owner or 'hub') if cfg else {}
            acl = allowed_workflow_create_definition_ids(claw,definitions)
            policy,warnings = _resolve_workflow_policy(runtime.get('provider') or (cfg.agent_type if cfg else ''),
                sorted(set(acl+grants)),cfg.system_context_policy_json if cfg else None,grants)
            assert selected <= set(policy['allowed_workflow_create_definition_ids']), ('sidecar_grants_missing',team.id,cid,warnings)
        for definition in selected_flows:
            for cid in members:
                assert can_dispatch_to(team.primary_manager_claw_id,cid,definition), (team.id,cid,definition.id)
        checks.append({'team_id':team.id,'flow_count':len(selected),'member_count':len(people),
                       'missing_grants':0,'dispatch_pairs':len(selected)*len(members)})
        paths.extend(['/api/v1/agent-teams/%s/test-plans?period=day' % team.id,
                      '/api/v1/agent-teams/%s/test-plans?period=week' % team.id,
                      '/api/v1/agent-teams/%s/members/activity' % team.id])
    db.session.remove()
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path,response.status_code)
        db.session.remove()
    http_checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin_id})
        headers = {'Cookie':app.config.get('SESSION_COOKIE_NAME','session')+'='+cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/agent-teams',headers=headers,timeout=5)
                if response.status_code==200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        for path in paths+['/static/js/agent_team_plans.js','/static/css/agent_team_plans.css']:
            response = requests.get('http://127.0.0.1:18800'+path,headers=headers,timeout=15,allow_redirects=False)
            assert response.status_code==200, (path,response.status_code)
            http_checks[path]=response.status_code
        assert requests.get('http://127.0.0.1:18800/api/v1/agent-teams?project_id=6',timeout=15).status_code==401
    print('TEAM_RELEASE '+json.dumps({'smoke':'passed','team_permissions':checks,'http_checks':http_checks,
        'runs_started':0,'manual_acl_modified':False,'workers_modified':False}))
'''

if __name__ == '__main__':
    deployment.main()
