"""Team configuration, manager fencing and immutable membership contracts."""
import re
from flask import current_app

from app import db
from app.models import AgentTeam, AgentTeamMission, OpenClawInstance, WorkflowDefinition


TEAM_ROLES = {
    'test_manager': '测试经理',
    'code_analyst': '代码分析员',
    'test_executor': '测试执行员',
}
EXECUTOR_SPECIALTIES = {
    'editor': '编辑器测试',
    'mobile_package': '手机包测试',
    'client_performance': '客户端性能测试',
}


class TeamError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def enabled(value):
    return value is True or str(value).lower() in ('1', 'true', 'yes', 'on')


def require_team_project(project_id):
    projects = current_app.config.get('AGENT_TEAMS_PROJECT_IDS', [])
    if isinstance(projects, str):
        projects = projects.split(',')
    if (not enabled(current_app.config.get('AGENT_TEAMS_ENABLED'))
            or not enabled(current_app.config.get('AGENT_TEAM_CONTRACTS_ENABLED'))
            or str(project_id) not in {str(item).strip() for item in projects}):
        raise TeamError('AGENT_TEAMS_DISABLED', '该项目尚未开启 Agent 团队', 404)


def integer(value, field, minimum=1, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TeamError('TEAM_VALIDATION_FAILED', field + ' 必须是范围内的整数', 400)
    return value


def scoped_claw(claw_id, project_id):
    row = db.session.get(OpenClawInstance, claw_id)
    if not row or row.status == 'deleted' or row.project_id != project_id:
        raise TeamError('TEAM_MEMBER_SCOPE_INVALID', '团队 Agent 必须存在且属于同一项目', 400)
    return row


def normalize_config(data, project_id):
    unknown = set(data) - {'project_id', 'name', 'objective', 'status',
                          'primary_manager_claw_id', 'backup_manager_claw_id',
                          'members', 'policy', 'expected_version'}
    if unknown:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知团队字段: ' + ', '.join(sorted(unknown)), 400)
    name, objective = data.get('name'), data.get('objective')
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise TeamError('TEAM_VALIDATION_FAILED', 'name 必须为 1–160 字符', 400)
    if not isinstance(objective, str) or not objective.strip() or len(objective) > 16000:
        raise TeamError('TEAM_VALIDATION_FAILED', 'objective 必须为 1–16000 字符', 400)
    status = data.get('status', 'active')
    if status not in ('active', 'paused', 'archived'):
        raise TeamError('TEAM_VALIDATION_FAILED', '无效的团队状态', 400)
    primary = integer(data.get('primary_manager_claw_id'), 'primary_manager_claw_id')
    backup = data.get('backup_manager_claw_id')
    scoped_claw(primary, project_id)
    if backup is not None:
        integer(backup, 'backup_manager_claw_id')
        scoped_claw(backup, project_id)
        if backup == primary:
            raise TeamError('TEAM_VALIDATION_FAILED', '主备经理不能相同', 400)
    members = data.get('members', [])
    if not isinstance(members, list) or len(members) > 100:
        raise TeamError('TEAM_VALIDATION_FAILED', 'members 必须是至多 100 项的数组', 400)
    normalized, seen = [], set()
    for item in members:
        if not isinstance(item, dict) or set(item) - {'claw_id', 'role_key', 'specialties'}:
            raise TeamError('TEAM_VALIDATION_FAILED', '无效的团队成员字段', 400)
        claw_id = integer(item.get('claw_id'), 'claw_id')
        scoped_claw(claw_id, project_id)
        role, specialties = item.get('role_key'), item.get('specialties', [])
        if role not in ('code_analyst', 'test_executor'):
            raise TeamError('TEAM_ROLE_INVALID', '成员仅允许代码分析员或测试执行员；经理在主备字段配置', 400)
        if (not isinstance(specialties, list)
                or any(not isinstance(value, str) or value not in EXECUTOR_SPECIALTIES for value in specialties)
                or (role == 'code_analyst' and specialties)
                or (role == 'test_executor' and not specialties)):
            raise TeamError('TEAM_SPECIALTY_INVALID', '执行员须指定合法二级角色，分析员不使用二级角色', 400)
        if (claw_id, role) in seen:
            raise TeamError('TEAM_MEMBER_DUPLICATE', '同一团队角色不可重复添加同一 Agent', 400)
        seen.add((claw_id, role))
        normalized.append({'claw_id': claw_id, 'role_key': role, 'specialties': sorted(set(specialties))})
    policy = data.get('policy')
    if not isinstance(policy, dict) or set(policy) - {
            'allowed_definition_ids', 'max_child_runs', 'supervision_schedule'}:
        raise TeamError('TEAM_POLICY_INVALID', 'policy 包含不支持的字段', 400)
    ids = policy.get('allowed_definition_ids')
    if not isinstance(ids, list) or not ids or len(ids) > 100:
        raise TeamError('TEAM_POLICY_INVALID', '必须显式配置允许的 Flow', 400)
    for flow_id in ids:
        integer(flow_id, 'allowed_definition_ids')
        flow = db.session.get(WorkflowDefinition, flow_id)
        if not flow or flow.project_id != project_id or flow.status != 'active':
            raise TeamError('TEAM_POLICY_INVALID', 'Flow 必须 active 且属于团队项目', 400)
    schedule = policy.get('supervision_schedule') or {
        'timezone': 'Asia/Shanghai',
        'morning_check': '09:30',
        'progress_summaries': ['13:30'],
        'day_close': '18:30',
    }
    if (not isinstance(schedule, dict)
            or set(schedule) - {
                'timezone', 'morning_check', 'progress_summaries', 'day_close'}
            or schedule.get('timezone') != 'Asia/Shanghai'):
        raise TeamError(
            'TEAM_POLICY_INVALID',
            'supervision_schedule 仅支持 Asia/Shanghai 及固定日程字段', 400)
    clock = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    morning = schedule.get('morning_check')
    close = schedule.get('day_close')
    summaries = schedule.get('progress_summaries', [])
    if (not isinstance(morning, str) or not clock.fullmatch(morning)
            or not isinstance(close, str) or not clock.fullmatch(close)
            or not isinstance(summaries, list) or len(summaries) > 8
            or any(not isinstance(value, str) or not clock.fullmatch(value)
                   for value in summaries)):
        raise TeamError('TEAM_POLICY_INVALID', '监督日程须为有效 HH:MM，阶段摘要最多 8 个', 400)
    normalized_schedule = {
        'timezone': 'Asia/Shanghai',
        'morning_check': morning,
        'progress_summaries': sorted(set(summaries)),
        'day_close': close,
    }
    return {
        'name': name.strip(), 'objective': objective.strip(), 'status': status,
        'primary_manager_claw_id': primary, 'backup_manager_claw_id': backup,
        'members': normalized,
        'policy': {'allowed_definition_ids': sorted(set(ids)),
                   'max_child_runs': integer(policy.get('max_child_runs', 20), 'max_child_runs', 1, 100),
                   'supervision_schedule': normalized_schedule},
    }


def load_team(team_id, lock=False):
    if not enabled(current_app.config.get('AGENT_TEAMS_ENABLED')):
        raise TeamError('AGENT_TEAMS_DISABLED', 'Agent 团队尚未开启', 404)
    query = AgentTeam.query.filter_by(id=team_id)
    team = query.populate_existing().with_for_update().first() if lock else query.first()
    if not team:
        raise TeamError('TEAM_NOT_FOUND', '团队不存在', 404)
    require_team_project(team.project_id)
    return team


def require_manager(team, actor, data, allow_paused=False):
    if team.status != 'active' and not (allow_paused and team.status == 'paused'):
        raise TeamError('TEAM_NOT_ACTIVE', '团队暂停或归档，不接受新调度')
    if (not actor or actor['type'] != 'claw'
            or actor['id'] not in (
                team.primary_manager_claw_id,
                team.backup_manager_claw_id)):
        raise TeamError(
            'TEAM_MANAGER_REQUIRED',
            '必须由团队配置的测试经理操作', 403)
    scoped_claw(actor['id'], team.project_id)
    # Legacy manager_epoch / manager_session_id receipts remain accepted, but
    # role assignment is now the only manager authorization boundary.


def mission_team(mission, lock=False, require_enabled=True):
    # No new table queries for legacy Missions, including before migration.
    if mission.control_mode != 'team_managed':
        return None, None
    binding = db.session.get(AgentTeamMission, mission.id)
    if not binding:
        raise TeamError('TEAM_BINDING_MISSING', 'Mission 缺失可信团队绑定')
    if require_enabled:
        team = load_team(binding.team_id, lock=lock)
    else:
        # Historical readback must remain available after disabling the pilot;
        # otherwise one Team Mission breaks the whole legacy Mission list.
        team = db.session.get(AgentTeam, binding.team_id)
        if not team:
            raise TeamError('TEAM_BINDING_MISSING', 'Mission 关联团队缺失')
    if team.project_id != mission.project_id:
        raise TeamError('TEAM_SCOPE_MISMATCH', 'Mission 项目与团队不一致')
    return team, binding


def member_matches(members, claw_id, role, specialty=None):
    return any(item['claw_id'] == claw_id and item['role_key'] == role
               and (role != 'test_executor' or specialty in item['specialties'])
               for item in members)


def require_stage_member(team, binding, claw_id, role, specialty=None):
    scoped_claw(claw_id, team.project_id)
    if role not in TEAM_ROLES or (role != 'test_executor' and specialty is not None):
        raise TeamError('TEAM_ROLE_INVALID', '阶段角色或细分角色无效', 400)
    if role == 'test_manager' and specialty is None:
        snapshot = binding.snapshot_json
        if (claw_id in (snapshot['primary_manager_claw_id'], snapshot.get('backup_manager_claw_id'))
                and claw_id in (team.primary_manager_claw_id, team.backup_manager_claw_id)):
            return
    if (not member_matches(binding.snapshot_json['members'], claw_id, role, specialty)
            or not member_matches([m.to_dict() for m in team.members], claw_id, role, specialty)):
        raise TeamError('TEAM_STAGE_MEMBER_INVALID', '执行者必须匹配 Mission 快照及团队当前角色/细分角色', 403)
