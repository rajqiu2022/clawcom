"""Scoped Hub release for room Flow launch and task delivery acceptance."""

import deploy_agent_teams as release


release.BASE = '052faa9e47ee9e4598d49c15f191417f1b1017dd'
release.FILES = (
    'app/api/chat_rooms.py',
    'app/api/testplans.py',
    'app/models.py',
    'app/services/agent_tasks.py',
    'app/services/chat_rooms.py',
    'app/services/plan_supervision.py',
)
release.MIGRATIONS = ()
release.SCHEMA = r'''
from app import create_app, db
from sqlalchemy import inspect
app = create_app('production')
with app.app_context():
    tables = set(inspect(db.engine).get_table_names())
    assert {'test_tasks', 'test_reports', 'agent_tasks', 'chat_room_deliveries'} <= tables
    print('TEAM_RELEASE ' + json.dumps({
        'schema_verified': True, 'migrations_applied': 0,
        'business_rows_modified': 0, 'backup': backup,
    }))
'''
release.SMOKE = r'''
import requests
import time
from app import create_app, db
from app.api.testplans import _normalize_delivery_acceptance
from app.models import TestTask, TestReport, User
app = create_app('production')
with app.app_context():
    assert _normalize_delivery_acceptance({
        'published_report': True, 'tapd_entities': ['bugs', 'requirements']
    })['published_report']
    rules = [rule.rule for rule in app.url_map.iter_rules()]
    assert any('/chat-rooms/' in path and '/workflow-runs' in path
               for path in rules)
    task = db.session.get(TestTask, 274)
    report = db.session.get(TestReport, 799)
    assert task and report and report.status == 'draft'
    assert not report.is_shared
    admin = User.query.filter_by(role='super_admin').first()
    assert admin
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = admin.id
    assert client.get('/agent-teams').status_code == 200
    db.session.remove()
    if os.getcwd() == '/opt/openclaw-web':
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': admin.id})
        headers = {'Cookie': app.config.get('SESSION_COOKIE_NAME', 'session') + '=' + cookie}
        for attempt in range(20):
            try:
                live = requests.get('http://127.0.0.1:18800/agent-teams',
                                    headers=headers, timeout=5,
                                    allow_redirects=False)
                if live.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed', 'room_flow_route': True,
        'task_acceptance': True, 'draft_799_preserved': True,
        'workflow_runs_started': 0,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    release.main()
