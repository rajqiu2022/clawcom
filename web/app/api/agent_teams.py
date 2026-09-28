"""Incremental Agent Team API; no scheduler, inference or Claw role mutation."""
import copy
import hashlib
import json
import re
from flask import current_app, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.api import api_bp
from app.api.mission_stages import _actor, _can_access_project, _can_administer
from app.models import (
    AgentTeam, AgentTeamKnowledgeResource, AgentTeamMember, AgentTeamMission,
    AgentTeamSkillResource, AuditLog, ClawSidecarConfig, KnowledgeEntry,
    MissionStage, OpenClawInstance, Project, Skill, WorkflowDefinition,
    WorkflowMission,
)
from app.services.worker_runtime import runtime_summary
from app.services import agent_team_activity as activity
from app.services import agent_team_manager_delegation as delegation
from app.services import plan_supervision as plan_supervision
from app.services.agent_team_onboarding import queue_join_notifications, wake_join_recipients
from app.models import AgentTeamMemberStatus, AgentTeamMemberTask, AgentTeamMemberReport
from app.services.agent_teams import (
    EXECUTOR_SPECIALTIES, TEAM_ROLES, TeamError, integer, load_team,
    mission_team, normalize_config, require_manager, require_stage_member,
    require_team_project, scoped_claw,
)
from app.services.agent_team_chat import (
    create_team_message, serialize_round, sync_team_room, team_room_payload,
)
from app.services.chat_rooms import serialize_message


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
        if current_app.config.get('CHAT_ROOM_ENABLED', False):
            sync_team_room(team)
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
    return jsonify({'items': [_team_read_payload(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset})


def _team_read_payload(team):
    from app.services.plan_supervision import team_capability
    return dict(team.to_dict(), plan_supervision=team_capability(team.id))


def _team_resource_editor(team, actor):
    """Project people may curate; Agents must be an explicit team member."""
    if actor['type'] == 'user':
        return True
    return actor['id'] in activity.roster(team)


def _knowledge_resource(row):
    entry = row.knowledge
    is_wiki = (entry.entry_type or 'article') == 'test_journal'
    return {
        'id': entry.id, 'title': entry.title, 'kind': 'knowledge',
        'entry_type': entry.entry_type or 'article',
        'category': entry.category, 'module_name': entry.module_name,
        'status': entry.status, 'revision': int(entry.current_revision or 0),
        'updated_at': str(entry.updated_at) if entry.updated_at else None,
        'linked_at': str(row.created_at) if row.created_at else None,
        'linked_by': row.linked_by_name,
        'web_url': ('/knowledge/wiki/%s' % entry.id if is_wiki
                    else '/knowledge?entry_id=%s' % entry.id),
        'detail_api': ('/api/v1/knowledge/journal-pages/%s' % entry.id if is_wiki
                       else '/api/v1/knowledge/%s' % entry.id),
        'pull_url': '/api/v1/knowledge/%s/export.md' % entry.id,
        'revisions_api': ('/api/v1/knowledge/%s/revisions' % entry.id
                          if is_wiki else None),
    }


def _skill_resource(row):
    skill = row.skill
    return {
        'id': skill.id, 'name': skill.name, 'title': skill.display_name,
        'kind': 'skill', 'category': skill.category, 'scope': skill.scope,
        'review_status': skill.review_status, 'visibility': skill.visibility,
        'updated_at': str(skill.updated_at) if skill.updated_at else None,
        'linked_at': str(row.created_at) if row.created_at else None,
        'linked_by': row.linked_by_name,
        'web_url': '/skills?skill_id=%s' % skill.id,
        'detail_api': '/api/v1/skills/%s' % skill.id,
        'pull_url': '/api/v1/skills/%s/pack' % skill.id,
        'raw_url': '/api/v1/skills/%s/raw' % skill.id,
    }


def _resource_manifest(team, actor):
    knowledge = (AgentTeamKnowledgeResource.query
                 .filter_by(team_id=team.id)
                 .order_by(AgentTeamKnowledgeResource.id.desc()).all())
    skills = (AgentTeamSkillResource.query
              .join(Skill, Skill.id == AgentTeamSkillResource.skill_id)
              .filter(AgentTeamSkillResource.team_id == team.id,
                      db.or_(Skill.is_deleted.is_(False), Skill.is_deleted.is_(None)))
              .order_by(AgentTeamSkillResource.id.desc()).all())
    return {
        'schema_version': 1, 'team_id': team.id,
        'project_id': team.project_id, 'team_version': team.version,
        'can_manage': _team_resource_editor(team, actor),
        'knowledge': [_knowledge_resource(row) for row in knowledge],
        'skills': [_skill_resource(row) for row in skills],
        'pull_policy': {
            'mode': 'on_demand',
            'note': '清单只返回元数据；通过 pull_url 拉取完整内容，更新后同一 URL 始终读取最新版本。',
        },
    }


@api_bp.route('/agent-teams/<int:team_id>/shared-resources', methods=['GET'])
def team_shared_resources(team_id):
    team = load_team(team_id)
    actor = _access(team.project_id)
    return jsonify(_resource_manifest(team, actor))


@api_bp.route('/agent-teams/<int:team_id>/shared-resources/options', methods=['GET'])
def team_shared_resource_options(team_id):
    team = load_team(team_id)
    actor = _access(team.project_id)
    if not _team_resource_editor(team, actor):
        raise TeamError('TEAM_RESOURCE_EDITOR_REQUIRED', '仅项目成员或团队 Agent 可维护共享资源', 403)
    linked_knowledge = {row.knowledge_id for row in
                        AgentTeamKnowledgeResource.query.filter_by(team_id=team.id).all()}
    linked_skills = {row.skill_id for row in
                     AgentTeamSkillResource.query.filter_by(team_id=team.id).all()}
    knowledge = (KnowledgeEntry.query.filter_by(project_id=team.project_id)
                 .order_by(KnowledgeEntry.updated_at.desc(), KnowledgeEntry.id.desc())
                 .limit(500).all())
    skill_rows = (Skill.query.filter(
                      db.or_(Skill.is_deleted.is_(False), Skill.is_deleted.is_(None)),
                      db.or_(Skill.review_status != 'rejected', Skill.review_status.is_(None)))
                  .order_by(Skill.updated_at.desc(), Skill.id.desc()).limit(500).all())
    skills = []
    team_claw_ids = set(activity.roster(team))
    for skill in skill_rows:
        projects = {int(value) for value in (skill.applicable_projects or [])
                    if str(value).isdigit()}
        if projects and team.project_id not in projects:
            continue
        if skill.visibility == 'private' and skill.owner_claw_id not in team_claw_ids:
            continue
        skills.append({'id': skill.id, 'name': skill.name,
                       'title': skill.display_name, 'category': skill.category,
                       'linked': skill.id in linked_skills})
    return jsonify({
        'knowledge': [{'id': row.id, 'title': row.title,
                       'entry_type': row.entry_type or 'article',
                       'module_name': row.module_name,
                       'revision': int(row.current_revision or 0),
                       'linked': row.id in linked_knowledge}
                      for row in knowledge],
        'skills': skills,
    })


def _link_resource(team, actor, kind, resource_id):
    if not _team_resource_editor(team, actor):
        raise TeamError('TEAM_RESOURCE_EDITOR_REQUIRED', '仅项目成员或团队 Agent 可维护共享资源', 403)
    if kind == 'knowledge':
        resource = db.session.get(KnowledgeEntry, resource_id)
        if not resource or resource.project_id != team.project_id:
            raise TeamError('TEAM_RESOURCE_SCOPE_INVALID', '知识不存在或不属于团队项目', 400)
        model, field = AgentTeamKnowledgeResource, 'knowledge_id'
    else:
        resource = db.session.get(Skill, resource_id)
        if not resource or resource.is_deleted or resource.review_status == 'rejected':
            raise TeamError('TEAM_RESOURCE_SCOPE_INVALID', 'Skill 不存在或已下架', 400)
        if (resource.visibility == 'private'
                and resource.owner_claw_id not in set(activity.roster(team))):
            raise TeamError('TEAM_RESOURCE_SCOPE_INVALID', '私有 Skill 不属于团队成员', 400)
        projects = {int(value) for value in (resource.applicable_projects or [])
                    if str(value).isdigit()}
        if projects and team.project_id not in projects:
            raise TeamError('TEAM_RESOURCE_SCOPE_INVALID', 'Skill 不适用于团队项目', 400)
        model, field = AgentTeamSkillResource, 'skill_id'
    existing = model.query.filter_by(team_id=team.id, **{field: resource_id}).first()
    if existing:
        return existing, False
    row = model(team_id=team.id, **{field: resource_id},
                linked_by_type=actor['type'], linked_by_id=actor['id'],
                linked_by_name=actor['name'])
    db.session.add(row)
    db.session.flush()
    _audit(team, actor, 'link_%s' % kind, {'resource_id': resource_id})
    return row, True


@api_bp.route('/agent-teams/<int:team_id>/shared-resources/<string:kind>', methods=['POST'])
def link_team_shared_resource(team_id, kind):
    if kind not in ('knowledge', 'skills'):
        raise TeamError('TEAM_RESOURCE_KIND_INVALID', '资源类型仅支持 knowledge/skills', 404)
    team = load_team(team_id)
    actor = _access(team.project_id)
    data = _body()
    resource_id = integer(data.get('resource_id'), 'resource_id')
    row, created = _link_resource(team, actor, 'knowledge' if kind == 'knowledge' else 'skill', resource_id)
    db.session.commit()
    payload = _knowledge_resource(row) if kind == 'knowledge' else _skill_resource(row)
    return jsonify(payload), 201 if created else 200


@api_bp.route('/agent-teams/<int:team_id>/shared-resources/<string:kind>/<int:resource_id>', methods=['DELETE'])
def unlink_team_shared_resource(team_id, kind, resource_id):
    if kind not in ('knowledge', 'skills'):
        raise TeamError('TEAM_RESOURCE_KIND_INVALID', '资源类型仅支持 knowledge/skills', 404)
    team = load_team(team_id)
    actor = _access(team.project_id)
    if not _team_resource_editor(team, actor):
        raise TeamError('TEAM_RESOURCE_EDITOR_REQUIRED', '仅项目成员或团队 Agent 可维护共享资源', 403)
    model, field = ((AgentTeamKnowledgeResource, 'knowledge_id') if kind == 'knowledge'
                    else (AgentTeamSkillResource, 'skill_id'))
    row = model.query.filter_by(team_id=team.id, **{field: resource_id}).first()
    if row:
        db.session.delete(row)
        _audit(team, actor, 'unlink_%s' % kind, {'resource_id': resource_id})
        db.session.commit()
    return jsonify({'removed': bool(row), 'resource_id': resource_id})


@api_bp.route('/agent-teams/<int:team_id>/test-plans', methods=['GET', 'POST'])
def team_test_plans(team_id):
    team = load_team(team_id)
    _access(team.project_id)
    if request.method == 'POST':
        from app.api.testplans import create_test_plan
        return create_test_plan(team_id=team_id)
    from app.services.agent_team_plans import overview
    limit, offset = _pagination(default=6)
    if limit > 24:
        raise TeamError('TEAM_VALIDATION_FAILED', '计划卡片每页最多 24 项', 400)
    return jsonify(overview(team, request.args.get('period', 'week'),
                            request.args.get('date'), limit, offset,
                            request.args.get('task_status', 'all')))


@api_bp.route('/agent-teams/<int:team_id>', methods=['GET'])
def get_agent_team(team_id):
    team = load_team(team_id)
    _access(team.project_id)
    return jsonify(_team_read_payload(team))


@api_bp.route('/agent-teams/<int:team_id>/chat-room', methods=['GET'])
def get_agent_team_chat_room(team_id):
    team = load_team(team_id)
    actor = _access(team.project_id)
    if not current_app.config.get('CHAT_ROOM_ENABLED', False):
        raise TeamError('CHAT_ROOM_DISABLED', '聊天室能力尚未开启', 404)
    limit = min(max(request.args.get('limit', 120, type=int), 1), 200)
    return jsonify(team_room_payload(team, actor, limit=limit))


@api_bp.route('/agent-teams/<int:team_id>/chat-room/messages', methods=['POST'])
def post_agent_team_chat_message(team_id):
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id)
    if not current_app.config.get('CHAT_ROOM_ENABLED', False):
        raise TeamError('CHAT_ROOM_DISABLED', '聊天室能力尚未开启', 404)
    data = _body()
    allowed = {
        'content', 'mention_claw_ids', 'mention_all', 'timeout_seconds',
        'client_message_id', 'image_ids',
    }
    if set(data) - allowed:
        raise TeamError('TEAM_CHAT_MESSAGE_INVALID', '包含未知聊天室消息字段', 400)
    key = request.headers.get('Idempotency-Key') or data.get('client_message_id')
    if not key or len(str(key)) > 100:
        raise TeamError('IDEMPOTENCY_KEY_REQUIRED', '必须提供 Idempotency-Key', 400)
    message, round_row, created = create_team_message(
        team, actor, data.get('content'), str(key),
        data.get('mention_claw_ids') or [], bool(data.get('mention_all')),
        data.get('timeout_seconds', 300), data.get('image_ids') or [])
    payload = {
        'message': serialize_message(message),
        'round': serialize_round(round_row) if round_row else None,
    }
    db.session.commit()
    return jsonify(payload), 201 if created else 200


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
            tasks.get(status.current_task_id) if status else None, team=team))
    return jsonify({'team_id':team.id, 'items':items, 'stale_after_seconds':activity.STALE_SECONDS,
        'report_contract': {'method':'POST', 'path':'/api/v1/agent-teams/%d/members/{claw_id}/activity' % team.id,
                            'self_only':True, 'recommended_interval_seconds':60,
                            'requires_expected_version':True,
                            'task_types':list(activity.TASK_TYPES)}})


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
    return jsonify({'member':activity.member_summary(
                        person, claw, status, current, team=team),
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
    # A live handover owns the primary/backup slots. Letting the config form
    # overwrite them would leave the delegation record active while the
    # supervisor binding points at the delegate — a split-brain team.
    record = team.manager_delegation_json or {}
    if record.get('active') and (
            config['primary_manager_claw_id'] != record.get('delegate_claw_id')
            or config['backup_manager_claw_id'] != record.get('previous_primary_claw_id')):
        raise TeamError(
            'TEAM_DELEGATION_ACTIVE',
            '团队正处于测试经理临时转正期，主备经理由转正开关管理；'
            '请先在经理转正入口回退，再修改主备配置', 409)
    previous_member_ids = set(activity.roster(team))
    _apply_config(team, config)
    team.version += 1
    # Configuration generation remains auditable, but manager authorization is
    # the team role itself. Existing executions keep immutable Stage/Run and
    # fencing tokens; pause never cancels a running task.
    team.manager_epoch += 1
    team.manager_session_id = (
        'team-manager:%s:%s:%s' % (
            team.id, team.version, team.primary_manager_claw_id)
        if team.status == 'active' else '')
    team.manager_lease_expires_at = None
    team.active_manager_claw_id = (
        team.primary_manager_claw_id if team.status == 'active' else None)
    _audit(team, actor, 'update', dict(config, version=team.version, manager_epoch=team.manager_epoch))
    try:
        if current_app.config.get('CHAT_ROOM_ENABLED', False):
            sync_team_room(team)
        notified_ids = queue_join_notifications(team, previous_member_ids)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TeamError('TEAM_NAME_EXISTS', '同项目团队名称已存在')
    wake_join_recipients(notified_ids)
    return jsonify(team.to_dict())


@api_bp.route('/agent-teams/<int:team_id>/manager-delegation', methods=['GET'])
def get_team_manager_delegation(team_id):
    """Read the temporary primary-manager handover, if any."""
    team = load_team(team_id)
    _access(team.project_id)
    return jsonify({'team': team.to_dict(),
                    'manager_delegation': delegation.state(team)})


@api_bp.route('/agent-teams/<int:team_id>/manager-delegation', methods=['POST'])
def create_team_manager_delegation(team_id):
    """Temporarily promote the configured backup manager to primary.

    The displaced primary takes the backup slot, so the handover is a clean swap
    that can be reversed byte-for-byte by the revoke endpoint.
    """
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id, administer=True)
    data = _body()
    if set(data) - {'delegate_claw_id', 'expected_version', 'reason', 'expires_at'}:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知转正请求字段', 400)
    result = delegation.delegate(
        team.id, data.get('delegate_claw_id'), actor['name'],
        request.remote_addr, data.get('reason') or '', data.get('expires_at'),
        data.get('expected_version'))
    target = result.pop('wake_claw_id', None)
    db.session.commit()
    if target:
        plan_supervision.wake(target)
    return jsonify(result), 201


@api_bp.route('/agent-teams/<int:team_id>/manager-delegation', methods=['DELETE'])
@api_bp.route('/agent-teams/<int:team_id>/manager-delegation/revoke', methods=['POST'])
def revoke_team_manager_delegation(team_id):
    """Restore the original primary manager; the delegate returns to backup."""
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id, administer=True)
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TeamError('TEAM_VALIDATION_FAILED', '请求必须为 JSON 对象', 400)
    if set(data) - {'expected_version', 'reason'}:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知回退请求字段', 400)
    result = delegation.revoke(
        team.id, actor['name'], request.remote_addr, data.get('reason') or '',
        data.get('expected_version'))
    target = result.pop('wake_claw_id', None)
    db.session.commit()
    if target:
        plan_supervision.wake(target)
    return jsonify(result)


@api_bp.route('/agent-teams/<int:team_id>/manager-lease', methods=['POST'])
def acquire_team_manager_lease(team_id):
    """Compatibility endpoint returning durable role authority without TTL."""
    team = load_team(team_id, lock=True)
    actor = _access(team.project_id)
    data = _body()
    if set(data) - {'expected_epoch', 'manager_session_id', 'ttl_seconds'}:
        raise TeamError('TEAM_VALIDATION_FAILED', '未知任期请求字段', 400)
    if actor['type'] != 'claw' or actor['id'] not in (team.primary_manager_claw_id, team.backup_manager_claw_id):
        raise TeamError('TEAM_MANAGER_REQUIRED', '仅配置的主备测试经理可读取调度授权', 403)
    scoped_claw(actor['id'], team.project_id)
    if team.status != 'active':
        raise TeamError('TEAM_NOT_ACTIVE', '暂停/归档的团队不提供新调度授权')
    _audit(team, actor, 'manager_authority_read', {
        'manager_epoch': team.manager_epoch,
        'manager_claw_id': actor['id'],
        'authority_mode': 'team_role_assignment'})
    db.session.commit()
    return jsonify(dict(team.to_dict(), manager_authority={
        'active': True,
        'manager_claw_id': actor['id'],
        'mode': 'team_role_assignment',
        'expires_at': None,
    }))


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
