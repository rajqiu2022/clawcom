"""Deploy Profile/Skill assignment safeguards; never starts a Workflow."""

import deploy_agent_team_project_assistant as deployment


release = deployment.release
release.BASE = '7e36092f41898697b090d59d0b16b8db3d329832'
release.FILES = (
    'app/api/openclaws.py',
    'app/api/packs.py',
    'app/api/skills.py',
    'app/services/skill_delivery.py',
)
release.MIGRATIONS = ()

_candidate = release.candidate


def _profile_contract_candidate(path, live):
    """Keep the live task-Skill contract and add only this release's helper."""
    if path != 'app/services/skill_delivery.py' or live is None:
        return _candidate(path, live)
    source = live.replace(b'\r\r\n', b'\n').replace(b'\r\n', b'\n')
    text = source.decode('utf-8')
    helper = '''def skill_assignment_unavailable_reason(skill, claw) -> str | None:
    """Return why a Skill must not be assigned to this Claw.

    Assignment writers use the same predicate as manifest generation so Hub
    cannot create an enabled relation that the authenticated Worker is then
    forbidden to download.
    """

    return _skill_unavailable_reason(skill, claw)
'''
    if helper in text:
        return source
    anchors = (
        '\n\ndef _source_contract(',
        '\n\ndef _source_map(',
    )
    for anchor in anchors:
        if text.count(anchor) == 1:
            return text.replace(anchor, '\n\n' + helper + anchor, 1).encode('utf-8')
    raise RuntimeError('Expected one Skill source-contract anchor')


release.candidate = _profile_contract_candidate

release.SCHEMA = r'''
from app import create_app
from app.services.skill_delivery import skill_assignment_unavailable_reason
app = create_app('production')
with app.app_context():
    assert callable(skill_assignment_unavailable_reason)
    print('TEAM_RELEASE ' + json.dumps({
        'migration_required': False,
        'business_rows_modified': 0,
    }))
'''

release.SMOKE = r'''
from app import create_app, db
from app.models import AgentTask, OpenClawInstance, Skill, User, WorkflowRun
from app.services.skill_delivery import (
    build_skill_manifest,
    skill_assignment_unavailable_reason,
)
import requests, time

app = create_app('production')
with app.app_context():
    admin = User.query.filter_by(role='super_admin').first()
    claw = db.session.get(OpenClawInstance, 61)
    assert admin and claw and claw.project_id == 6
    agent_task_count = AgentTask.query.count()
    workflow_run_count = WorkflowRun.query.count()
    manifest = build_skill_manifest(claw)
    for item in manifest['missing_skills']:
        skill = Skill.query.filter_by(name=item['name']).first()
        if skill:
            assert skill_assignment_unavailable_reason(skill, claw) == item['reason']
    if os.getcwd() == '/opt/openclaw-web':
        for attempt in range(20):
            try:
                response = requests.get(
                    'http://127.0.0.1:18800/login', timeout=5,
                    allow_redirects=False)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('HTTP readiness did not recover')
    assert AgentTask.query.count() == agent_task_count
    assert WorkflowRun.query.count() == workflow_run_count
    print('TEAM_RELEASE ' + json.dumps({
        'smoke': 'passed',
        'claw_id': claw.id,
        'manifest_missing_before_repair': manifest['missing_skills'],
        'business_rows_modified': 0,
        'agent_tasks_started': 0,
        'workflow_runs_started': 0,
        'workers_modified': False,
    }, ensure_ascii=False))
'''


if __name__ == '__main__':
    deployment.deployment.main()
