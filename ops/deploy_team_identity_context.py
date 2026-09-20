"""Deploy team identity context only; preserve Worker releases and business data."""
import deploy_team_onboarding as deployment

release = deployment.release
release.BASE = '1a4eef656096cc6b98a582ec5f7d85d47e966cb3'
release.FILES = (
    'app/api/agent_client.py', 'app/services/agent_system_context.py',
    'app/services/agent_team_context.py', 'app/services/agent_team_onboarding.py',
)
release.SMOKE += r'''
from app.models import OpenClawInstance
from app.services.agent_team_context import build_team_context
from app.services.agent_system_context import build_agent_system_context
with app.app_context():
    claw = db.session.get(OpenClawInstance, 60)
    teams = build_team_context(claw)
    team = next(t for t in teams if t['team_id'] == 1)
    assert team['self']['claw_id'] == 60
    assert team['self']['roles'] == ['test_executor']
    assert team['primary_manager_claw_id'] == 54
    assert next(m for m in team['members'] if m['claw_id'] == 12)['roles'] == ['code_analyst']
    context = build_agent_system_context(claw, 'codebuddy', [], [], agent_teams=teams)
    assert any(r['name'] == 'agent_team_identity' for r in context['system_context']['rules'])
    db.session.remove()
    print('TEAM_RELEASE '+json.dumps({'team_identity_verified':True,'claw_id':60,
        'team_id':1,'manager':54,'roles':team['self']['roles']}))
'''

if __name__ == '__main__':
    deployment.deployment.main()
