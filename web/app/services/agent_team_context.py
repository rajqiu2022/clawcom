"""Non-secret, team-scoped identity snapshots for authenticated Sidecar config."""
from sqlalchemy.orm import selectinload

from app.models import AgentTeam, OpenClawInstance
from app.services.agent_team_activity import TASK_TYPES, roster
from app.services.agent_teams import TeamError, require_team_project
from app.services.agent_context_snapshots import ROLE_CONTRACTS


def team_snapshot(team, claw_id, names):
    from app.services.plan_supervision import team_capability
    people = roster(team)
    if claw_id not in people:
        return None
    ids = sorted(people)
    # Keep self even for a large team; full roster is available at the API.
    selected = sorted(set(ids[:49]) | {claw_id})
    def identity(person_id):
        value = dict(people[person_id], name=names.get(person_id, ''))
        roles = value.get('roles') or []
        if 'primary_manager' in roles or 'backup_manager' in roles:
            value['effective_role_key'] = 'test_manager'
            value['manager_kind'] = (
                'primary' if 'primary_manager' in roles else 'backup')
            value['has_manager_authority'] = team.has_manager_authority(
                person_id)
        elif 'project_assistant' in roles:
            value['effective_role_key'] = 'project_assistant'
        elif 'code_analyst' in roles:
            value['effective_role_key'] = 'code_analyst'
        elif 'test_executor' in roles:
            value['effective_role_key'] = 'test_executor'
        return value
    return {
        'team_id': team.id, 'name': team.name, 'project_id': team.project_id,
        'version': team.version, 'status': team.status,
        'self': identity(claw_id),
        'primary_manager_claw_id': team.primary_manager_claw_id,
        'backup_manager_claw_id': team.backup_manager_claw_id,
        'members': [identity(cid) for cid in selected],
        'role_contracts': ROLE_CONTRACTS,
        'members_truncated': len(selected) < len(ids),
        'definition_api': '/api/v1/agent-teams/%s' % team.id,
        'activity_api': '/api/v1/agent-teams/%s/members/activity' % team.id,
        'activity_reporting': {
            'self_only': True,
            'task_types': list(TASK_TYPES),
            'project_assistant_default_task_type': 'version_data',
        },
        'workflow_permissions': {
            'source': 'active_team_selection',
            'allowed_definition_ids': list((team.policy_json or {}).get(
                'allowed_definition_ids', [])) if team.status == 'active' else [],
            'can_dispatch_members': team.status == 'active' and claw_id in (
                team.primary_manager_claw_id, team.backup_manager_claw_id),
            'note': '勾选 Flow 自动授予经理调度、项目助理及其他成员执行权限，不授予编辑权限；'
                    '经理权限随团队角色持续有效，Mission 仍须阶段绑定和可信 Runtime。',
        },
        'plan_supervision': team_capability(team.id),
        'test_plans': {'api': '/api/v1/agent-teams/%s/test-plans' % team.id,
                       'create_role': 'test_manager', 'periods': ['day', 'week', 'all'],
                       'task_reference_contract': {
                           'fields': [
                               'reference_skill_ids',
                               'reference_knowledge_ids',
                               'reference_report_ids',
                           ],
                           'options_api': (
                               '/api/v1/test-plans/{plan_id}/'
                               'task-reference-options'),
                           'read_policy': 'on_demand_before_execution',
                       },
                       'note': '团队计划创建为草稿；排期概览不是自动执行调度。测试经理可给任务绑定团队 Skill、知识库及项目报告，执行者须按 references 地址读取。'},
        'shared_resources': {
            'manifest_api': '/api/v1/agent-teams/%s/shared-resources' % team.id,
            'mode': 'on_demand',
            'editable_by': 'project_people_and_explicit_team_agents',
            'note': '需要团队知识或 Skill 时先读取清单，再按 pull_url 拉取完整内容；不要把全部正文长期注入上下文。',
        },
    }


def build_team_context(claw):
    try:
        require_team_project(claw.project_id)
    except TeamError:
        return []
    teams = AgentTeam.query.filter(
        AgentTeam.project_id == claw.project_id,
        (AgentTeam.primary_manager_claw_id == claw.id)
        | (AgentTeam.backup_manager_claw_id == claw.id)
        | AgentTeam.members.any(claw_id=claw.id),
    ).options(selectinload(AgentTeam.members)).order_by(AgentTeam.id).all()
    ids = {cid for team in teams for cid in roster(team)}
    names = dict(OpenClawInstance.query.with_entities(
        OpenClawInstance.id, OpenClawInstance.name).filter(
            OpenClawInstance.id.in_(ids),
            OpenClawInstance.project_id == claw.project_id).all()) if ids else {}
    return [team_snapshot(team, claw.id, names) for team in teams]
