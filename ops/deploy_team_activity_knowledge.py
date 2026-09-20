"""Deploy member observations + knowledge FK fix; no Worker or business writes.

Reuses the staged, drift-checked and rollback-capable Team release transport.
Apply only after committing this release. No secret values are printed.
"""
import deploy_agent_teams as release

release.BASE = '5079afe'
release.FILES = (
    'app/models.py', 'app/api/agent_teams.py', 'app/api/knowledge.py',
    'app/services/agent_team_activity.py', 'templates/agent_teams.html',
    'static/css/agent_teams.css', 'static/js/agent_teams.js',
    'static/js/agent_team_activity.js',
)
release.MIGRATIONS = ('20260920_agent_team_member_activity.sql',)
release.PROBE = release.PROBE.replace(
    "'agent_teams','agent_team_members','agent_team_missions']",
    "'agent_teams','agent_team_members','agent_team_missions',"
    "'agent_team_member_tasks','agent_team_member_statuses','agent_team_member_reports']")

release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect, text
app = create_app('production')
with app.app_context():
    names = ['agent_team_member_tasks', 'agent_team_member_statuses', 'agent_team_member_reports']
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    assert {'agent_teams', 'agent_team_members', 'agent_team_missions', 'knowledge_entries'} <= tables
    assert 'project_id' in {c['name'] for c in inspector.get_columns('knowledge_entries')}
    os.umask(0o077)
    # DDL-only backup: this migration only creates new tables, never rewrites
    # existing data/tables. Preserve any tables from an interrupted deployment.
    with open(backup + '/schema.sql', 'w') as stream:
        for name in ['knowledge_entries', 'agent_teams'] + names:
            if name in tables:
                ddl = db.session.execute(text('SHOW CREATE TABLE `' + name + '`')).fetchone()[1]
                stream.write(ddl + ';\n')
    with db.engine.begin() as conn:
        for name, source in migrations:
            source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('--'))
            for statement in source.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
            print('TEAM_RELEASE ' + json.dumps({'migration':name, 'applied':True}))
    inspector = inspect(db.engine)
    for name in names:
        actual = {c['name'] for c in inspector.get_columns(name)}
        expected = {c.name for c in db.metadata.tables[name].columns}
        assert expected <= actual, name
        assert inspector.get_foreign_keys(name), name
        unique_columns = {tuple(c['column_names']) for c in inspector.get_unique_constraints(name)}
        expected_unique = ('team_id', 'claw_id')
        if name.endswith('_tasks'):
            expected_unique += ('task_key',)
        if name.endswith('_reports'):
            expected_unique += ('event_id',)
        assert expected_unique in unique_columns, (name, sorted(unique_columns))
    print('TEAM_RELEASE ' + json.dumps({'schema_verified':True, 'backup':backup, 'business_rows_modified':0}))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import User, Project, AgentTeam, KnowledgeEntry
import requests, time
app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    gates = {k:app.config.get(k) for k in ('AGENT_TEAMS_ENABLED','AGENT_TEAM_CONTRACTS_ENABLED','AGENT_TEAMS_PROJECT_IDS','RESOURCE_LEASE_RECONCILIATION_ENABLED')}
    assert gates['AGENT_TEAMS_ENABLED'] and gates['AGENT_TEAM_CONTRACTS_ENABLED']
    assert str(gates['AGENT_TEAMS_PROJECT_IDS']).strip() == '6'
    assert gates['RESOURCE_LEASE_RECONCILIATION_ENABLED'] is False
    project = db.session.get(Project, 6)
    assert project and project.name.lower() == 'racinggo'
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    paths = ['/agent-teams','/workflows','/test-reports','/knowledge','/automation-closed-loop']
    for path in paths:
        assert client.get(path).status_code == 200, path
    teams = AgentTeam.query.filter_by(project_id=6).all()
    assert teams, 'Expected the user-created team; do not create synthetic production teams'
    card_checks = []
    for team in teams:
        url = '/api/v1/agent-teams/%d/members/activity' % team.id
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
        members = response.get_json()['items']
        assert members
        for member in members:
            detail = '/api/v1/agent-teams/%d/members/%d/activity' % (team.id, member['claw_id'])
            assert client.get(detail).status_code == 200
            # No impersonated reports, not even as admin; no row is written.
            denied = client.post(detail, json={'event_id':'release-check','expected_version':0,'state':'idle'})
            assert denied.status_code == 403
        card_checks.append({'team_id':team.id,'member_count':len(members),
                            'states':[m['effective_state'] for m in members]})
    entry = KnowledgeEntry.query.filter_by(entry_type='article').first()
    assert entry
    url = '/api/v1/knowledge/%d' % entry.id
    rejected = client.put(url, json={'project_id':-1})
    assert rejected.status_code == 400 and rejected.get_json()['code'] == 'KNOWLEDGE_PROJECT_INVALID'
    assert client.get(url).status_code == 200
    assert app.test_client().get('/api/v1/agent-teams?project_id=6').status_code == 401
    # Verify the running server as well, not merely a second imported app.
    http_checks = {}
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id':admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME','session') + '=' + cookie}
        for attempt in range(20):
            try:
                response = requests.get('http://127.0.0.1:18800/agent-teams', headers=headers, timeout=5, allow_redirects=False)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
        assert 'at-activity-dialog' in response.text
        for path in paths + ['/api/v1/agent-teams/%d/members/activity' % t.id for t in teams] + ['/static/js/agent_team_activity.js','/static/css/agent_teams.css']:
            response = requests.get('http://127.0.0.1:18800' + path, headers=headers, timeout=15, allow_redirects=False)
            assert response.status_code == 200, (path,response.status_code)
            http_checks[path] = response.status_code
        response = requests.put('http://127.0.0.1:18800' + url, headers=headers, json={'project_id':-1}, timeout=15)
        assert response.status_code == 400 and response.json()['code'] == 'KNOWLEDGE_PROJECT_INVALID'
        assert requests.get('http://127.0.0.1:18800/api/v1/agent-teams?project_id=6',timeout=15).status_code == 401
    print('TEAM_RELEASE ' + json.dumps({'smoke':'passed','member_cards':card_checks,'http_checks':http_checks,
        'persistent_gates':gates,'knowledge_validation':'passed','business_rows_modified':0,'workers_modified':False,'runs_started':0}))
'''

if __name__ == '__main__':
    release.main()
