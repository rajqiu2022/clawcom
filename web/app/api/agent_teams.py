"""Incremental Agent Team API; no scheduler, inference or Claw role mutation."""
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.api import api_bp
from app.api.mission_stages import _actor, _can_access_project, _can_administer
from app.models import (
    AgentTeam, AgentTeamMember, AgentTeamMission, AuditLog, ClawSidecarConfig,
    MissionStage, OpenClawInstance, Project, WorkflowDefinition, WorkflowMission,
)
from app.services.worker_runtime import runtime_summary
from app.services import agent_team_activity as activity
from app.services.agent_team_onboarding import queue_join_notifications, wake_join_recipients
from app.models import AgentTeamMemberStatus, AgentTeamMemberTask, AgentTeamMemberReport
from app.services.agent_teams import (
    EXECUTOR_SPECIALTIES, TEAM_ROLES, TeamError, integer, load_team,
    mission_team, normalize_config, require_manager, require_stage_member,
    require_team_project, scoped_claw,
)


@api_bp.errorhandler(TeamError)
def team_error(exc):
    db.session.rollback()
    return jsonify({'code': exc.code, 'error': str(exc)}), exc.status


def _access(project_id, administer=False):
    require_team_project(project_id)
    actor = _actor()
    if not actor:
        raise TeamError('AUTH_REQUIRED', '未认证', 401)
    if not _can_access_project(actor, project_id):
        raise TeamError('TEAM_ACCESS_DENIED', '无权访问团队项目', 403)
    if administer and (actor['type'] != 'user' or not _can_administer(actor, project_id)):
        raise TeamError('TEAM_ADMIN_REQUIRED', '团队配置由项目管理员管理', 403)
    return actor


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise TeamError('TEAM_VALIDATION_FAILED', '请求必须为 JSON 对象', 400)
    return data


def _pagination(default=50):
    try:
        limit = int(request.args.get('limit', default))
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        raise TeamError('TEAM_VALIDATION_FAILED', '分页参数必须为整数', 400)
    if not 1 <= limit <= 100 or offset < 0:
        raise TeamError('TEAM_VALIDATION_FAILED', 'limit 范围 1–100，offset 不得为负', 400)
    return limit, offset


@api_bp.route('/agent-teams/options', methods=['GET'])
def agent_team_options():
    """Minimal project-scoped roster: never return tokens or runtime config."""
    project_id = request.args.get('project_id', type=int)
    actor = _access(project_id)
    claws = OpenClawInstance.query.filter(
        OpenClawInstance.project_id == project_id,
        OpenClawInstance.status != 'deleted').order_by(OpenClawInstance.id).all()
    configs = {row.claw_id: row for row in ClawSidecarConfig.query.filter(
        ClawSidecarConfig.claw_id.in_([claw.id for claw in claws])).all()} if claws else {}
    agents = []
    for claw in claws:
        cfg = configs.get(claw.id)
        runtime = runtime_summary(cfg.runtime_config_json or {}, cfg.config_owner or 'hub') if cfg else {}
        agents.append({'id': claw.id, 'name': claw.name, 'status': claw.status,
                       'has_worker_runtime': bool(runtime.get('has_worker_runtime'))})
    from app.api.workflows import _definition_visible
    flows = [row for row in WorkflowDefinition.query.filter_by(project_id=project_id, status='active').order_by(WorkflowDefinition.id).all()
             if _definition_visible(row)]
    return jsonify({
        'project_id': project_id, 'can_manage': actor['type'] == 'user' and _can_administer(actor, project_id),
        'roles': TEAM_ROLES, 'executor_specialties': EXECUTOR_SPECIALTIES,
        'agents': agents, 'flows': [{'id': row.id, 'name': row.name, 'version': row.version} for row in flows],
    })


def _audit(team, actor, action, detail):
    db.session.add(AuditLog(
        action=action, resource_type='agent_team', resource_id=team.id,
        resource_name=team.name, operator=actor['name'], ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _apply_config(team, config):
    for field in ('name', 'objective', 'status', 'primary_manager_claw_id', 'backup_manager_claw_id'):
        setattr(team, field, config[field])
    team.policy_json = config['policy']
    current = {(m.claw_id, m.role_key): m for m in team.members}
    retained = []
    for item in config['members']:
        member = current.get((item['claw_id'], item['role_key'])) or AgentTeamMember(
            claw_id=item['claw_id'], role_key=item['role_key'])
        member.specialties_json = item['specialties']
        retained.append(member)
    team.members = retained


@api_bp.route('/agent-teams/roles', methods=['GET'])
def agent_team_roles():
    project_id = request.args.get('project_id', type=int)
    _access(project_id)
    return jsonify({'roles': TEAM_ROLES, 'executor_specialties': EXECUTOR_SPECIALTIES,
                    'one_active_manager': True, 'independent_of_claw_roles': True})


@api_bp.route('/agent-teams', methods=['POST'])
def create_agent_team():
    data = _body()
    project_id = integer(data.get('project_id'), 'project_id')
    actor = _access(project_id, administer=True)
    if not db.session.get(Project, project_id):
        raise TeamError('PROJECT_NOT_FOUND', '项目不存在', 404)
    config = normalize_config(data, project_id)
    team = AgentTeam(project_id=project_id)
    _apply_config(team, config)
    db.session.add(team)
    try:
        db.session.flush()
        _audit(team, actor, 'create', config)
        notified_ids = queue_join_notifications(team)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TeamError('TEAM_NAME_EXISTS', '同项目团队名称已存在')
    wake_join_recipients(notified_ids)
    return jsonify(team.to_dict()), 201


@api_bp.route('/agent-teams', methods=['GET'])
def list_agent_teams():
    project_id = request.args.get('project_id', type=int)
    _access(project_id)
    limit, offset = _pagination()
    query = AgentTeam.query.filter_by(project_id=project_id)
    total = query.count()
    rows = query.options(selectinload(AgentTeam.members)).order_by(AgentTeam.id).offset(offset).limit(limit).all()
    return jsonify({'items': [row.to_dict() for row in rows], 'total': total, 'limit': limit, 'offset': offset})


@api_bp.route('/agent-teams/<int:team_id>', methods=['GET'])
def get_agent_team(team_id):
    team = load_team(team_id)
    _access(team.project_id)
    return jsonify(team.to_dict())


def _activity_member(team, claw_id):
    person = activity.roster(team).get(claw_id)
    if not person:
        raise TeamError('TEAM_ACTIVITY_NOT_MEMBER', '该 Agent 不属于当前团队', 403)
    claw = scoped_claw(claw_id, team.project_id)
    return person, claw


@api_bp.route('/agent-teams/<int:team_id>/members/activity', methods=['GET'])
def list_team_member_activity(team_id):
    team = load_team(team_id)
    _access(team.project_id)
    people = activity.roster(team)
    claws = OpenClawInstance.query.filter(OpenClawInstance.id.in_(list(people)),
        OpenClawInstance.project_id == team.project_id, OpenClawInstance.status != 'deleted').all()
    statuses = {row.claw_id: row for row in AgentTeamMemberStatus.query.filter_by(team_id=team.id).all()}
    task_ids = [row.current_task_id for row in statuses.values() if row.current_task_id]
    tasks = {row.id:row for row in AgentTeamMemberTask.query.filter(AgentTeamMemberTask.team_id == team.id,
        AgentTeamMemberTask.id.in_(task_ids)).all()} if task_ids else {}
    items = []
    for claw in claws:
        status = statuses.get(claw.id)
        items.append(activity.member_summary(people[claw.id], claw, status,
            tasks.get(status.current_task_id) if status else None))
    return jsonify({'team_id':team.id, 'items':items, 'stale_after_seconds':activity.STALE_SECONDS,
        'report_contract': {'method':'POST', 'path':'/api/v1/agent-teams/%d/members/{claw_id}/activity' % team.id,
                            'self_only':True, 'recommended_interval_seconds':60, 'requires_expected_version':True}})


@api_bp.route('/agent-teams/<int:team_id>/members/<int:claw_id>/activity', methods=['GET', 'POST'])
def team_member_activity(team_id, claw_id):
    team = load_team(team_id, lock=request.method == 'POST')
    actor = _access(team.project_id)
    person, claw = _activity_member(team, claw_id)
    if request.method == 'POST':
        if actor['type'] != 'claw' or actor['id'] != claw_id:
            raise TeamError('TEAM_ACTIVITY_SELF_ONLY', '仅 Agent 自身可上报；用户或其他 Agent 不可代报', 403)
        return jsonify(activity.ingest(team, claw_id, _body()))
    limit, offset = _pagination(default=20)
    status = AgentTeamMemberStatus.query.filter_by(team_id=team.id, claw_id=claw_id).first()
    current = db.session.get(AgentTeamMemberTask, status.current_task_id) if status and status.current_task_id else None
    history = AgentTeamMemberTask.query.filter_by(team_id=team.id, claw_id=claw_id).filter(AgentTeamMemberTask.status.in_(activity.TERMINAL))
    total = history.count()
    rows = history.order_by(AgentTeamMemberTask.id.desc()).offset(offset).limit(limit).all()
    reports = AgentTeamMemberReport.query.filter_by(team_id=team.id, claw_id=claw_id)
    if current:
        reports = reports.filter_by(task_id=current.id)
    recent = reports.order_by(AgentTeamMemberReport.id.desc()).limit(20).all()
    return jsonify({'member':activity.member_summary(person, claw, status, current),
                    'history':{'items':[row.to_dict() for row in rows], 'total':total, 'limit':limit, 'offset':offset},
                    'recent_reports':[row.response_json for row in recent]})


@api_bp.route('/agent-teams/<int:team_id>', methods=['PUT'])
def update_agent_team(team_id):
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id, administer=True)
    data = _body()
    if data.get('project_id', team.project_id) != team.project_id:
        raise TeamError('TEAM_SCOPE_IMMUTABLE', '团队不可跨项目移动', 400)
    if integer(data.get('expected_version'), 'expected_version') != team.version:
        raise TeamError('TEAM_VERSION_CONFLICT', '团队配置已更新，请回读重试')
    config = normalize_config(data, team.project_id)
    previous_member_ids = set(activity.roster(team))
    _apply_config(team, config)
    team.version += 1
    # Any config change revokes the old planner. Existing executions keep their
    # immutable Stage/Run and fencing tokens; pause never cancels a running task.
    team.manager_epoch += 1
    team.manager_session_id = ''
    team.manager_lease_expires_at = None
    team.active_manager_claw_id = None
    _audit(team, actor, 'update', dict(config, version=team.version, manager_epoch=team.manager_epoch))
    try:
        notified_ids = queue_join_notifications(team, previous_member_ids)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TeamError('TEAM_NAME_EXISTS', '同项目团队名称已存在')
    wake_join_recipients(notified_ids)
    return jsonify(team.to_dict())


@api_bp.route('/agent-teams/<int:team_id>/manager-lease', methods=['POST'])
def acquire_team_manager_lease(team_id):
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id)
    data = _body()
    if set(data) - {'expected_epoch', 'manager_session_id', 'ttl_seconds'}:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知任期请求字段', 400)
    if actor['type'] != 'claw' or actor['id'] not in (team.primary_manager_claw_id, team.backup_manager_claw_id):
        raise TeamError('TEAM_MANAGER_REQUIRED', '仅配置的主备经理可申请任期', 403)
    scoped_claw(actor['id'], team.project_id)
    if team.status != 'active':
        raise TeamError('TEAM_NOT_ACTIVE', '暂停/归档的团队不能申请调度任期')
    expected = integer(data.get('expected_epoch'), 'expected_epoch', 0)
    ttl = integer(data.get('ttl_seconds', 120), 'ttl_seconds', 30, 300)
    session = data.get('manager_session_id')
    if not isinstance(session, str) or not session.strip() or len(session) > 128:
        raise TeamError('TEAM_VALIDATION_FAILED', 'manager_session_id 必须为 1–128 字符', 400)
    if expected != team.manager_epoch:
        raise TeamError('STALE_MANAGER_EPOCH', '经理任期已变化')
    now = datetime.now()
    live = team.manager_lease_expires_at and team.manager_lease_expires_at > now
    if live:
        if team.active_manager_claw_id != actor['id'] or team.manager_session_id != session:
            raise TeamError('TEAM_MANAGER_LEASE_HELD', '另一经理或会话仍持有有效任期')
    else:
        team.manager_epoch += 1
        team.active_manager_claw_id = actor['id']
        team.manager_session_id = session
    team.manager_lease_expires_at = now + timedelta(seconds=ttl)
    _audit(team, actor, 'manager_renew' if live else 'manager_acquire', {
        'manager_epoch': team.manager_epoch, 'active_manager_claw_id': actor['id'],
        'expires_at': str(team.manager_lease_expires_at)})
    db.session.commit()
    return jsonify(team.to_dict())


@api_bp.route('/agent-teams/<int:team_id>/missions', methods=['GET'])
def list_team_missions(team_id):
    team = load_team(team_id)
    _access(team.project_id)
    # A projection, not a second queue/claim state machine.
    limit, offset = _pagination(default=20)
    query = WorkflowMission.query.join(
        AgentTeamMission, AgentTeamMission.mission_id == WorkflowMission.id
    ).filter(AgentTeamMission.team_id == team.id)
    total = query.count()
    rows = query.options(selectinload(WorkflowMission.main_claw)).order_by(WorkflowMission.id.desc()).offset(offset).limit(limit).all()
    stages = MissionStage.query.filter(MissionStage.mission_id.in_([row.id for row in rows])).order_by(MissionStage.id).all() if rows else []
    grouped = {}
    for stage in stages:
        grouped.setdefault(stage.mission_id, []).append({
            'stage_key': stage.stage_key, 'state': stage.state, 'role_key': stage.role_key,
            'assigned_claw_id': stage.assigned_claw_id, 'workflow_run_id': stage.workflow_run_id,
            'last_reason_code': stage.last_reason_code,
        })
    items = []
    for row in rows:
        item = row.to_dict()
        item['stages'] = grouped.get(row.id, [])
        items.append(item)
    return jsonify({'team_id': team.id, 'items': items, 'total': total, 'limit': limit, 'offset': offset})


@api_bp.route('/workflow-missions/<int:mission_id>/team-plan', methods=['POST'])
def create_team_mission_plan(mission_id):
    mission = WorkflowMission.query.filter_by(id=mission_id).with_for_update().first()
    if not mission:
        raise TeamError('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    team, binding = mission_team(mission, lock=True)
    if not team:
        raise TeamError('TEAM_MISSION_REQUIRED', '只支持团队 Mission', 400)
    actor = _access(team.project_id)
    data = _body()
    require_manager(team, actor, data)
    if set(data) - {'manager_epoch', 'manager_session_id', 'stages'}:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知计划字段', 400)
    stages = data.get('stages')
    if not isinstance(stages, list) or not stages or len(stages) > 50:
        raise TeamError('TEAM_VALIDATION_FAILED', 'stages 必须为 1–50 项数组', 400)
    digest = hashlib.sha256(json.dumps(stages, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if binding.plan_sha256:
        if binding.plan_sha256 != digest:
            raise TeamError('TEAM_PLAN_IMMUTABLE', '已有计划不能原地覆盖；新基线需新建 Mission')
        return jsonify({'mission_id': mission.id, 'plan_sha256': digest, 'idempotent_replay': True})
    if mission.effective_status() != 'active':
        raise TeamError('MISSION_NOT_ACTIVE', 'Mission 已结束或过期')
    seen = set()
    for item in stages:
        if not isinstance(item, dict) or set(item) - {'stage_key', 'role_key', 'specialty', 'assigned_claw_id', 'input_snapshot'}:
            raise TeamError('TEAM_VALIDATION_FAILED', '无效的阶段字段', 400)
        key = item.get('stage_key')
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key) or key in seen:
            raise TeamError('TEAM_VALIDATION_FAILED', 'stage_key 必须合法且唯一', 400)
        seen.add(key)
        claw_id = integer(item.get('assigned_claw_id'), 'assigned_claw_id')
        role, specialty = item.get('role_key'), item.get('specialty')
        require_stage_member(team, binding, claw_id, role, specialty)
        snapshot = item.get('input_snapshot')
        if not isinstance(snapshot, dict):
            raise TeamError('TEAM_VALIDATION_FAILED', 'input_snapshot 必须为对象', 400)
        snapshot = copy.deepcopy(snapshot)
        snapshot['team_assignment'] = {'team_id': team.id, 'team_version': binding.team_version,
                                       'role_key': role, 'specialty': specialty}
        db.session.add(MissionStage(mission_id=mission.id, stage_key=key, stage_version=1,
                                   role_key=role, assigned_claw_id=claw_id, state='ready',
                                   input_snapshot_json=snapshot, evidence_refs_json=[]))
    binding.plan_sha256 = digest
    _audit(team, actor, 'plan', {'mission_id': mission.id, 'manager_epoch': team.manager_epoch,
                               'plan_sha256': digest, 'stage_keys': sorted(seen)})
    db.session.commit()
    return jsonify({'mission_id': mission.id, 'plan_sha256': digest, 'idempotent_replay': False}), 201
