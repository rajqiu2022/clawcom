"""Autonomous Workflow Mission control plane.

The main Agent chooses which Flow to run. Hub deliberately limits itself to
identity, project scope, idempotency, budget and audit persistence.
"""

import copy
from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

from flask import current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user
from app.models import (
    AgentTeam, AgentTeamMission, AuditLog, ClawSidecarConfig, MissionStage, OpenClawInstance, Project, WorkflowDefinition,
    WorkflowMission, WorkflowMissionDispatch, WorkflowRun, WorkflowRunStep,
)
from app.services.worker_runtime import runtime_summary
from app.services.agent_context_snapshots import (
    ContextSnapshotError,
    freeze_run_context,
)
from app.services.agent_teams import (
    TeamError, enabled as team_enabled, integer as team_integer, load_team, mission_team,
    require_manager, require_stage_member,
)
from app.services.workflow_library_snapshots import (
    append_workflow_snapshot_warning,
    freeze_workflow_run_library_snapshot,
    resolve_workflow_library_id,
)
from app.services.workflows import (
    build_workflow_start_context,
    can_execute_workflow,
    workflow_catalog_metadata,
    workflow_outcome_requirements,
)


_EXECUTOR_OVERRIDE_FIELDS = frozenset({
    'executor_claw_ids', 'target_claw_ids', 'selected_claw_ids',
    'executor_user_ids', 'selected_user_ids', 'executor_worker_claw_id',
})


def _error(code, message, status=400, details=None):
    body = {'error': message, 'code': code}
    if details:
        body['details'] = details
    return jsonify(body), status


def _actor():
    claw = get_current_claw()
    if claw:
        return {'type': 'claw', 'id': claw.id, 'name': claw.name or '',
                'claw': claw, 'user': None}
    user = get_current_user()
    if user:
        return {'type': 'user', 'id': user.id,
                'name': user.display_name or user.username,
                'claw': None, 'user': user}
    return None


def _int_ids(value):
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise ValueError('expected an integer array')
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError('expected an integer array') from exc
        if number > 0 and number not in result:
            result.append(number)
    return sorted(result)


def _bounded_int(value, default, minimum, maximum, field):
    try:
        number = int(value if value not in (None, '') else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be an integer') from exc
    if number < minimum or number > maximum:
        raise ValueError(f'{field} must be between {minimum} and {maximum}')
    return number


def _can_create_for_claw(actor, claw, project_id):
    if not actor or not claw or int(claw.project_id or 0) != int(project_id):
        return False
    if actor['type'] == 'claw':
        return int(actor['id']) == int(claw.id)
    user = actor['user']
    if user.role in ('super_admin', 'admin'):
        return True
    return bool(user.bound_claw_id and int(user.bound_claw_id) == int(claw.id))


def _can_read_mission(actor, mission):
    if not actor:
        return False
    team, _ = mission_team(mission, require_enabled=False)
    if team and actor['type'] == 'claw':
        return (actor['claw'].project_id == team.project_id and actor['id'] in {
            team.primary_manager_claw_id, team.backup_manager_claw_id,
            *[member.claw_id for member in team.members]})
    if actor['type'] == 'claw':
        return int(actor['id']) == int(mission.main_claw_id)
    user = actor['user']
    return bool(
        user.role in ('super_admin', 'admin')
        or (mission.created_by_type == 'user'
            and int(mission.created_by_id) == int(user.id))
        or (user.bound_claw_id
            and int(user.bound_claw_id) == int(mission.main_claw_id)))


def _mission_definition_allowed(mission, definition):
    if (not definition or definition.status != 'active'
            or int(definition.project_id or 0) != int(mission.project_id)):
        return False
    allowed = {int(item) for item in (
        mission.allowed_definition_ids_json or [])}
    denied = {int(item) for item in (
        mission.denied_definition_ids_json or [])}
    return definition.id not in denied and (
        not allowed or definition.id in allowed)


def _trusted_worker_runtime(claw_id):
    config = db.session.get(ClawSidecarConfig, int(claw_id))
    if not config:
        return None
    summary = runtime_summary(
        config.runtime_config_json or {}, config.config_owner or 'hub')
    return summary if summary.get('has_worker_runtime') else None


def _claw_can_execute_definition(claw_id, definition):
    from app.services.agent_team_permissions import can_execute
    if can_execute(claw_id, definition):
        return True
    return can_execute_workflow(
        definition.owner_type,
        definition.owner_id,
        definition.executor_acl_json or {},
        'claw',
        int(claw_id),
        is_admin=False,
        editor_acl=definition.editor_acl_json or {},
    )


def _allowed_worker_ids(mission):
    return {
        int(item) for item in (mission.allowed_worker_claw_ids_json or [])
        if str(item).isdigit() and int(item) > 0
    }


def _mission_payload(mission, with_dispatches=False):
    payload = mission.to_dict(with_dispatches=with_dispatches)
    payload['definitions_api'] = (
        f'/api/v1/workflow-missions/{mission.id}/definitions')
    payload['dispatch_api'] = (
        f'/api/v1/workflow-missions/{mission.id}/dispatch')
    payload['dispatch_contract'] = {
        'decision_owner': 'main_agent',
        'required_fields': ['workflow_definition_id'],
        'optional_fields': [
            'start_vars', 'context', 'reason', 'decision_key',
            'idempotency_key', 'run_name', 'worker_claw_id'],
        'default_worker_claw_id': mission.main_claw_id,
        'allowed_worker_claw_ids': sorted(_allowed_worker_ids(mission)),
        'arbitrary_executor_override_forbidden': True,
        'per_step_approval_required': False,
    }
    team, binding = mission_team(mission, require_enabled=False)
    if team:
        payload['team_id'] = team.id
        payload['team_version'] = binding.team_version
        payload['active_manager_claw_id'] = (
            team.primary_manager_claw_id if team.status == 'active' else None)
        payload['manager_epoch'] = team.manager_epoch
        payload['dispatch_contract'].update({
            'decision_owner': 'team_manager',
            'required_fields': ['workflow_definition_id', 'stage_key'],
            'optional_fields': list(dict.fromkeys(
                payload['dispatch_contract']['optional_fields']
                + ['manager_epoch', 'manager_session_id'])),
            'manager_authority': 'team_role_assignment',
            'default_worker_claw_id': None,
        })
    from app.models_plan_supervision import PlanSupervisor
    supervisor = PlanSupervisor.query.filter_by(mission_id=mission.id).first()
    if supervisor:
        required = payload['dispatch_contract']['required_fields']
        if 'plan_supervision' not in required:
            required.append('plan_supervision')
        payload['dispatch_contract']['plan_supervision'] = {
            'required': True,
            'source': ('POST /api/v1/test-plans/%s/supervision/claim'
                       % supervisor.plan_id),
            'preferred_shape': {
                'plan_supervision': {
                    'worker_id': '<claim.worker_id>',
                    'fencing_token': '<claim.fencing_token>',
                },
            },
            'legacy_flat_shape_accepted': True,
            'flat_fields': ['worker_id', 'fencing_token'],
        }
    return payload


def _definition_summary(row):
    definition = row.definition_json or {}
    steps = definition.get('steps') or []
    return {
        'id': row.id,
        'workflow_definition_id': row.id,
        'workflow_key': row.workflow_key,
        'name': row.name,
        'description': row.description or '',
        'project_id': row.project_id,
        'version': row.version or 1,
        'step_count': len(steps),
        'agent_step_count': sum(
            1 for step in steps if step.get('type') == 'agent_task'),
        'worker_step_count': sum(
            1 for step in steps if step.get('type') == 'worker_task'),
    }


def _request_hash(payload):
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _dispatch_key(mission_id, data, request_hash):
    raw = str(
        data.get('idempotency_key')
        or data.get('decision_key')
        or f'auto-{request_hash[:32]}'
    ).strip()
    material = f'mission:{mission_id}:{raw}'
    if len(material) <= 128:
        return material
    return f'mission:{mission_id}:sha256:{hashlib.sha256(material.encode()).hexdigest()}'


def _dispatch_payload(dispatch, idempotent_replay):
    payload = dispatch.to_dict()
    run = db.session.get(WorkflowRun, dispatch.workflow_run_id)
    payload['idempotent_replay'] = bool(idempotent_replay)
    payload['run_deleted'] = run is None
    payload['run'] = run.to_dict(with_steps=False) if run else None
    payload['readback_url'] = (
        f'/api/v1/workflow-runs/{dispatch.workflow_run_id}'
        if run else '')
    return payload


@api_bp.route('/workflow-missions', methods=['POST'])
def create_workflow_mission():
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    team = None
    if data.get('team_id') is not None:
        team = load_team(team_integer(data['team_id'], 'team_id'), lock=True)
        require_manager(team, actor, data)
        if (data.get('project_id') != team.project_id
                or data.get('main_claw_id', actor['id']) != actor['id']):
            raise TeamError('TEAM_SCOPE_MISMATCH', '团队 Mission 必须由当前经理在本项目创建', 400)
    try:
        project_id = int(data.get('project_id'))
        main_claw_id = int(data.get('main_claw_id') or (
            actor['id'] if actor['type'] == 'claw' else 0))
    except (TypeError, ValueError):
        return _error(
            'MISSION_SCOPE_INVALID',
            'project_id and main_claw_id must be integers')
    objective = str(data.get('objective') or '').strip()
    if not objective:
        return _error('MISSION_OBJECTIVE_REQUIRED', 'objective 不能为空')
    project = db.session.get(Project, project_id)
    main_claw = OpenClawInstance.query.filter(
        OpenClawInstance.id == main_claw_id,
        OpenClawInstance.status != 'deleted').first()
    if not project or not main_claw:
        return _error('MISSION_SCOPE_NOT_FOUND', '项目或主 Agent 不存在', 404)
    if not _can_create_for_claw(actor, main_claw, project_id):
        return _error(
            'MISSION_CREATE_FORBIDDEN',
            '无权为该项目和主 Agent 创建 Mission', 403)
    try:
        allowed = _int_ids(data.get('allowed_definition_ids'))
        denied = _int_ids(data.get('denied_definition_ids'))
        allowed_workers = _int_ids(data.get('allowed_worker_claw_ids'))
        max_child_runs = _bounded_int(
            data.get('max_child_runs'), 20, 1, 100,
            'max_child_runs')
        max_retries = _bounded_int(
            data.get('max_retries_per_flow'), 3, 0, 20,
            'max_retries_per_flow')
        expires_in_hours = _bounded_int(
            data.get('expires_in_hours'), 8, 1, 168,
            'expires_in_hours')
    except ValueError as exc:
        return _error('MISSION_POLICY_INVALID', str(exc))
    if team:
        team_flows = set(team.policy_json['allowed_definition_ids'])
        if data.get('allow_destructive_actions') is True or data.get('allow_external_notification') is True:
            raise TeamError('TEAM_AUTHORITY_NOT_GRANTED', '首期团队不允许自行启用破坏性操作或外部通知', 403)
        if allowed and not set(allowed) <= team_flows:
            raise TeamError('TEAM_FLOW_NOT_ALLOWED', 'Mission Flow 超出团队授权', 403)
        allowed = allowed or sorted(team_flows)
        team_workers = {m.claw_id for m in team.members} | {
            team.primary_manager_claw_id, team.backup_manager_claw_id}
        if not set(allowed_workers) <= team_workers:
            raise TeamError('TEAM_WORKER_NOT_ALLOWED', 'Mission Worker 不属于团队', 403)
        max_child_runs = min(max_child_runs, team.policy_json['max_child_runs'])
    if main_claw_id in allowed_workers:
        allowed_workers.remove(main_claw_id)
    if allowed_workers:
        worker_rows = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(allowed_workers),
            OpenClawInstance.status != 'deleted').all()
        workers_by_id = {row.id: row for row in worker_rows}
        invalid_workers = [
            worker_id for worker_id in allowed_workers
            if worker_id not in workers_by_id
            or int(workers_by_id[worker_id].project_id or 0) != project_id
        ]
        if invalid_workers:
            return _error(
                'MISSION_WORKER_SCOPE_INVALID',
                'Mission Worker 必须存在且属于同一项目',
                details={'worker_claw_ids': invalid_workers})
        untrusted_workers = [
            worker_id for worker_id in allowed_workers
            if not _trusted_worker_runtime(worker_id)
        ]
        if untrusted_workers:
            return _error(
                'MISSION_WORKER_RUNTIME_REQUIRED',
                'Mission Worker 必须先注册可信 Claw Worker Runtime', 409,
                {'worker_claw_ids': untrusted_workers})
    scoped_ids = sorted(set(allowed + denied))
    if scoped_ids:
        rows = WorkflowDefinition.query.filter(
            WorkflowDefinition.id.in_(scoped_ids)).all()
        by_id = {row.id: row for row in rows}
        invalid = [
            item for item in scoped_ids
            if item not in by_id
            or int(by_id[item].project_id or 0) != project_id]
        if invalid:
            return _error(
                'MISSION_DEFINITION_SCOPE_INVALID',
                'Mission Flow 必须属于同一项目',
                details={'definition_ids': invalid})
    # Mission creation is the one authorization boundary. Snapshot every Flow
    # the creator can execute now so the main Agent does not re-negotiate each
    # dispatch, while a Mission cannot elevate its creator into private Flows.
    from app.api.workflows import _can_execute_definition, _definition_visible
    candidates = WorkflowDefinition.query.filter_by(
        project_id=project_id, status='active').all()
    executable_ids = {
        row.id for row in candidates
        if _definition_visible(row) and _can_execute_definition(row)
    }
    if allowed:
        unauthorized = sorted(set(allowed) - executable_ids)
        if unauthorized:
            return _error(
                'MISSION_DEFINITION_EXECUTE_FORBIDDEN',
                'Mission 创建者无权执行部分 Flow', 403,
                {'definition_ids': unauthorized})
    else:
        allowed = sorted(executable_ids - set(denied))
    if not allowed:
        return _error(
            'MISSION_NO_EXECUTABLE_FLOW',
            '当前项目没有可授权给主 Agent 的 active Flow', 409)
    mission_key = str(data.get('mission_key') or '').strip()
    if not mission_key:
        mission_key = f'mission-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid4().hex[:10]}'
    if len(mission_key) > 128:
        return _error('MISSION_KEY_TOO_LONG', 'mission_key 不能超过128字符')
    if WorkflowMission.query.filter_by(mission_key=mission_key).first():
        return _error('MISSION_KEY_EXISTS', 'mission_key 已存在', 409)
    mission_context = dict(data.get('context') or {}) if isinstance(data.get('context'), dict) else {}
    # Do not let an ordinary Mission turn revocable team grants into a lasting
    # authorization snapshot. This provenance is written by Hub, not callers.
    mission_context.pop('team_grant_definition_ids', None)
    if not team and actor['type'] == 'claw':
        from app.services.agent_team_permissions import flow_grants
        derived = sorted(set(allowed).intersection(flow_grants(main_claw, candidates)))
        if derived:
            mission_context['team_grant_definition_ids'] = derived
    mission = WorkflowMission(
        mission_key=mission_key,
        project_id=project_id,
        main_claw_id=main_claw_id,
        objective=objective,
        status='active',
        control_mode='team_managed' if team else 'agent_autonomous',
        allowed_definition_ids_json=allowed,
        denied_definition_ids_json=denied,
        allowed_worker_claw_ids_json=allowed_workers,
        max_child_runs=max_child_runs,
        child_run_count=0,
        max_retries_per_flow=max_retries,
        allow_external_notification=(
            data.get('allow_external_notification') is True),
        allow_destructive_actions=(
            data.get('allow_destructive_actions') is True),
        context_json=mission_context,
        created_by_type=actor['type'],
        created_by_id=actor['id'],
        created_by_name=actor['name'],
        expires_at=datetime.now() + timedelta(hours=expires_in_hours),
    )
    db.session.add(mission)
    db.session.flush()
    if data.get('test_plan_id') is not None:
        from app.services import plan_supervision as plan_guard
        if not plan_guard.enabled():
            plan_guard.fail('PLAN_SUPERVISION_DISABLED', '计划监督未启用', 503)
        if type(data['test_plan_id']) is not int:
            plan_guard.fail('PLAN_BODY_INVALID', 'test_plan_id 须为整数', 400)
        supervisor = plan_guard.locked(data['test_plan_id'])
        if not supervisor:
            plan_guard.fail('PLAN_SUPERVISION_NOT_STARTED', '计划尚未启动监督')
        plan_guard.require_lease(supervisor, actor['id'] if actor['type'] == 'claw' else None,
                                 data.get('plan_supervision') or {})
        if supervisor.mission_id:
            plan_guard.fail('PLAN_MISSION_ALREADY_BOUND', '计划已有 Mission，请回读后复用，禁止重复创建')
        if mission.main_claw_id != supervisor.orchestrator_claw_id:
            plan_guard.fail('PLAN_MISSION_FORBIDDEN', 'Mission 主 Agent 必须为计划监督者', 403)
        if supervisor.team_id and (not team or team.id != supervisor.team_id):
            plan_guard.fail('PLAN_MISSION_FORBIDDEN', 'Mission 必须属于计划绑定的团队', 403)
        supervisor.mission_id = mission.id
        mission.context_json = dict(mission.context_json or {}, plan_supervision_id=supervisor.plan_id)
        plan_guard.add_event(supervisor.plan_id, 'mission_created', ['mission', mission.id],
                             {'mission_id': mission.id})
    if team:
        db.session.add(AgentTeamMission(
            mission_id=mission.id, team_id=team.id, team_version=team.version,
            snapshot_json={
                'primary_manager_claw_id': team.primary_manager_claw_id,
                'backup_manager_claw_id': team.backup_manager_claw_id,
                'policy': copy.deepcopy(team.policy_json),
                'members': [member.to_dict() for member in team.members],
                'manager_epoch_at_creation': team.manager_epoch,
            }))
    db.session.add(AuditLog(
        action='create', resource_type='workflow_mission',
        resource_id=mission.id, resource_name=mission.mission_key,
        operator=actor['name'], ip_address=request.remote_addr,
        detail=json.dumps({
            'project_id': project_id,
            'main_claw_id': main_claw_id,
            'allowed_worker_claw_ids': allowed_workers,
            'control_mode': mission.control_mode,
            'team_id': team.id if team else None,
            'max_child_runs': max_child_runs,
        }, ensure_ascii=False, sort_keys=True),
    ))
    db.session.commit()
    return jsonify(_mission_payload(mission)), 201


@api_bp.route('/workflow-missions', methods=['GET'])
def list_workflow_missions():
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    query = WorkflowMission.query.order_by(
        WorkflowMission.updated_at.desc(), WorkflowMission.id.desc())
    if actor['type'] == 'claw':
        conditions = [WorkflowMission.main_claw_id == actor['id']]
        if team_enabled(current_app.config.get('AGENT_TEAMS_ENABLED')):
            # Include missions after manager failover; no per-row permission
            # expansion to projects outside the authenticated Claw's project.
            managed_missions = db.session.query(AgentTeamMission.mission_id).join(
                AgentTeam, AgentTeam.id == AgentTeamMission.team_id).filter(
                    AgentTeam.project_id == actor['claw'].project_id,
                    db.or_(AgentTeam.primary_manager_claw_id == actor['id'],
                           AgentTeam.backup_manager_claw_id == actor['id']))
            conditions.append(WorkflowMission.id.in_(managed_missions))
        query = query.filter(db.or_(*conditions))
    elif actor['user'].role not in ('super_admin', 'admin'):
        conditions = [
            db.and_(
                WorkflowMission.created_by_type == 'user',
                WorkflowMission.created_by_id == actor['id']),
        ]
        if actor['user'].bound_claw_id:
            conditions.append(
                WorkflowMission.main_claw_id == actor['user'].bound_claw_id)
        query = query.filter(db.or_(*conditions))
    status = str(request.args.get('status') or '').strip()
    if status:
        query = query.filter(WorkflowMission.status == status)
    rows = query.limit(100).all()
    return jsonify({
        'items': [_mission_payload(row) for row in rows],
        'count': len(rows),
    })


@api_bp.route('/workflow-missions/<int:mission_id>', methods=['GET'])
def get_workflow_mission(mission_id):
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission:
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    if not _can_read_mission(actor, mission):
        return _error('MISSION_ACCESS_DENIED', '无权访问该 Mission', 403)
    return jsonify(_mission_payload(mission, with_dispatches=True))


def _finish_mission(mission_id, target_status):
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = WorkflowMission.query.filter_by(id=mission_id).with_for_update().first()
    if not mission:
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    if not _can_read_mission(actor, mission):
        return _error('MISSION_ACCESS_DENIED', '无权操作该 Mission', 403)
    from app.services import plan_supervision as plan_guard
    if actor['type'] == 'claw' and (plan_guard.enabled() or (mission.context_json or {}).get('plan_supervision_id')):
        if not plan_guard.enabled():
            return _error('PLAN_SUPERVISION_DISABLED', '计划监督已禁用', 503)
        plan_guard.mission_guard(mission.id, actor['id'], request.get_json(silent=True) or {})
    team, _ = mission_team(mission, lock=True)
    if team:
        require_manager(team, actor, request.get_json(silent=True) or {}, allow_paused=True)
        if target_status == 'completed':
            stages = MissionStage.query.filter_by(mission_id=mission.id).all()
            if not stages or any(stage.state != 'completed' for stage in stages):
                raise TeamError('TEAM_STAGES_INCOMPLETE', '全部阶段完成交付验收后才能结束 Mission')
    if mission.status != 'active':
        return _error(
            'MISSION_NOT_ACTIVE', 'Mission 已经结束', 409,
            {'status': mission.effective_status()})
    now = datetime.now()
    mission.status = target_status
    if target_status == 'completed':
        mission.completed_at = now
    else:
        mission.cancelled_at = now
    mission.version = int(mission.version or 1) + 1
    db.session.add(AuditLog(
        action=target_status,
        resource_type='workflow_mission',
        resource_id=mission.id,
        resource_name=mission.mission_key,
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps({
            'status': target_status,
            'child_runs_unchanged': True,
        }, ensure_ascii=False, sort_keys=True),
    ))
    db.session.commit()
    payload = _mission_payload(mission, with_dispatches=True)
    payload['child_runs_cancelled'] = False
    return jsonify(payload)


@api_bp.route('/workflow-missions/<int:mission_id>/complete', methods=['POST'])
def complete_workflow_mission(mission_id):
    return _finish_mission(mission_id, 'completed')


@api_bp.route('/workflow-missions/<int:mission_id>/cancel', methods=['POST'])
def cancel_workflow_mission(mission_id):
    return _finish_mission(mission_id, 'cancelled')


@api_bp.route(
    '/workflow-missions/<int:mission_id>/definitions', methods=['GET'])
def list_workflow_mission_definitions(mission_id):
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission:
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    if not _can_read_mission(actor, mission):
        return _error('MISSION_ACCESS_DENIED', '无权访问该 Mission', 403)
    rows = WorkflowDefinition.query.filter_by(
        project_id=mission.project_id, status='active').order_by(
            WorkflowDefinition.updated_at.desc(),
            WorkflowDefinition.id.asc()).all()
    items = [
        _definition_summary(row) for row in rows
        if _mission_definition_allowed(mission, row)]
    return jsonify({
        'mission_id': mission.id,
        'control_mode': mission.control_mode,
        'items': items,
        'count': len(items),
    })


@api_bp.route(
    '/workflow-missions/<int:mission_id>/dispatch', methods=['POST'])
def dispatch_workflow_mission(mission_id):
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    if actor['type'] != 'claw':
        return _error(
            'MISSION_MAIN_AGENT_REQUIRED',
            '只有 Mission 主 Agent 可以自主 dispatch', 403)
    data = request.get_json(silent=True) or {}
    override_fields = sorted(_EXECUTOR_OVERRIDE_FIELDS.intersection(data))
    if override_fields:
        return _error(
            'MISSION_EXECUTOR_OVERRIDE_FORBIDDEN',
            'Mission dispatch 不能临时覆盖执行者',
            details={'fields': override_fields})
    try:
        definition_id = int(
            data.get('workflow_definition_id') or data.get('definition_id'))
        requested_worker_id = (
            int(data.get('worker_claw_id'))
            if data.get('worker_claw_id') not in (None, '') else None)
        if requested_worker_id is not None and requested_worker_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _error(
            'MISSION_DEFINITION_REQUIRED',
            'workflow_definition_id 与 worker_claw_id 必须是正整数')
    start_vars = (
        data.get('start_vars')
        if isinstance(data.get('start_vars'), dict) else {})
    raw_context = (
        copy.deepcopy(data.get('context'))
        if isinstance(data.get('context'), dict) else {})
    raw_context.pop('workflow_start', None)
    raw_context.pop('mission', None)
    reason = str(data.get('reason') or '').strip()[:4000]
    decision_key = str(data.get('decision_key') or '').strip()[:128]
    mission = (WorkflowMission.query.filter_by(id=mission_id)
               .with_for_update().first())
    if not mission:
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    from app.services import plan_supervision as plan_guard
    if plan_guard.enabled() or (mission.context_json or {}).get('plan_supervision_id'):
        if not plan_guard.enabled():
            return _error('PLAN_SUPERVISION_DISABLED', '计划监督已禁用，禁止关联 Mission 新派工', 503)
        plan_guard.mission_guard(mission.id, actor['id'], data)
    team, binding = mission_team(mission, lock=True)
    stage = None
    if team:
        require_manager(team, actor, data)
        stage = MissionStage.query.filter_by(
            mission_id=mission.id, stage_key=str(data.get('stage_key') or ''), stage_version=1
        ).with_for_update().first()
        if not stage or not binding.plan_sha256:
            raise TeamError('TEAM_STAGE_REQUIRED', '必须先创建不可变计划并指定 stage_key', 400)
        specialty = (stage.input_snapshot_json or {}).get('team_assignment', {}).get('specialty')
        require_stage_member(team, binding, stage.assigned_claw_id, stage.role_key, specialty)
        if requested_worker_id is not None and requested_worker_id != stage.assigned_claw_id:
            raise TeamError('TEAM_STAGE_WORKER_MISMATCH', 'Worker 必须与计划阶段的角色绑定一致', 403)
        requested_worker_id = stage.assigned_claw_id
        if definition_id not in team.policy_json['allowed_definition_ids']:
            raise TeamError('TEAM_FLOW_NOT_ALLOWED', 'Flow 已不在团队当前授权内', 403)
    elif int(actor['id']) != int(mission.main_claw_id):
        return _error('MISSION_ACCESS_DENIED', '不是该 Mission 主 Agent', 403)
    worker_claw_id = requested_worker_id or int(mission.main_claw_id)
    if (worker_claw_id != int(mission.main_claw_id)
            and worker_claw_id not in _allowed_worker_ids(mission)):
        return _error(
            'MISSION_WORKER_NOT_ALLOWED',
            '所选 Worker 不在 Mission 白名单内', 403,
            {'worker_claw_id': worker_claw_id,
             'allowed_worker_claw_ids': sorted(_allowed_worker_ids(mission))})
    hash_payload = {
        'mission_id': mission_id,
        'workflow_definition_id': definition_id,
        'worker_claw_id': worker_claw_id,
        'start_vars': start_vars,
        'context': raw_context,
        'run_name': str(data.get('run_name') or ''),
        'reason': reason,
        'decision_key': decision_key,
    }
    if team:
        hash_payload['stage_key'] = stage.stage_key
    request_hash = _request_hash(hash_payload)
    idempotency_key = _dispatch_key(mission_id, data, request_hash)
    existing = WorkflowMissionDispatch.query.filter_by(
        mission_id=mission.id, idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_hash != request_hash:
            return _error(
                'MISSION_IDEMPOTENCY_CONFLICT',
                '同一 dispatch key 被不同决策复用', 409)
        return jsonify(_dispatch_payload(existing, True)), 200
    if stage and (stage.workflow_run_id or stage.state != 'ready'):
        raise TeamError('TEAM_STAGE_ALREADY_DISPATCHED', '阶段已派发或不再 ready，禁止重复运行')
    effective_status = mission.effective_status()
    if effective_status == 'expired':
        return _error('MISSION_EXPIRED', 'Mission 已过期', 409)
    if effective_status != 'active':
        return _error(
            'MISSION_NOT_ACTIVE', 'Mission 当前不可调度', 409,
            {'status': effective_status})
    if int(mission.child_run_count or 0) >= int(mission.max_child_runs or 20):
        return _error(
            'MISSION_CHILD_RUN_BUDGET_EXHAUSTED',
            'Mission Child Run 预算已耗尽', 409)
    definition = db.session.get(WorkflowDefinition, definition_id)
    if not _mission_definition_allowed(mission, definition):
        return _error(
            'MISSION_DEFINITION_NOT_ALLOWED',
            '该 Workflow 不在 Mission 项目授权范围内', 403)
    if (not team and definition_id in (mission.context_json or {}).get('team_grant_definition_ids', [])
            and not _claw_can_execute_definition(mission.main_claw_id, definition)):
        return _error('MISSION_TEAM_GRANT_REVOKED', '团队 Flow 授权已撤回', 403)
    selected_worker = OpenClawInstance.query.filter(
        OpenClawInstance.id == worker_claw_id,
        OpenClawInstance.status != 'deleted').first()
    if (not selected_worker
            or int(selected_worker.project_id or 0) != int(mission.project_id)):
        return _error(
            'MISSION_WORKER_SCOPE_INVALID',
            '所选 Worker 不存在或不属于 Mission 项目', 403,
            {'worker_claw_id': worker_claw_id})
    if team or worker_claw_id != int(mission.main_claw_id):
        runtime = _trusted_worker_runtime(worker_claw_id)
        if not runtime:
            return _error(
                'MISSION_WORKER_RUNTIME_REQUIRED',
                '所选 Worker 未注册可信 Claw Worker Runtime', 409,
                {'worker_claw_id': worker_claw_id})
        if not _claw_can_execute_definition(worker_claw_id, definition):
            return _error(
                'MISSION_WORKER_EXECUTE_FORBIDDEN',
                '所选 Worker 没有该 Workflow 的执行权限', 403,
                {'worker_claw_id': worker_claw_id,
                 'workflow_definition_id': definition.id})

    context = build_workflow_start_context(
        start_vars, raw_context, start_mode='mission_dispatch',
        executor_claw_ids=[], executor_user_ids=[],
        worker_claw_id=worker_claw_id,
    )
    context['mission'] = {
        'id': mission.id,
        'mission_key': mission.mission_key,
        'objective': mission.objective,
        'control_mode': 'agent_autonomous',
        'main_claw_id': mission.main_claw_id,
        'worker_claw_id': worker_claw_id,
        'allow_external_notification': bool(
            mission.allow_external_notification),
        'allow_destructive_actions': bool(
            mission.allow_destructive_actions),
    }
    if team:
        context['mission'].update({
            'control_mode': 'team_managed', 'team_id': team.id,
            'team_version': binding.team_version, 'manager_epoch': team.manager_epoch,
            'dispatch_manager_claw_id': actor['id'], 'stage_key': stage.stage_key,
        })
    from app.api.workflows import (
        _workflow_definition_snapshot_fingerprint,
        _workflow_execution_input_snapshot,
    )
    context['workflow_definition_snapshot'] = (
        _workflow_definition_snapshot_fingerprint(definition))
    context['workflow_catalog_snapshot'] = workflow_catalog_metadata(
        definition.definition_json or {})
    context['outcome_requirements_snapshot'] = workflow_outcome_requirements(
        definition.definition_json or {})
    context['execution_input_snapshot'] = _workflow_execution_input_snapshot(
        context['workflow_definition_snapshot'],
        start_vars,
        raw_context,
        (definition.definition_json or {}).get('context'),
    )
    run = WorkflowRun(
        definition_id=definition.id,
        run_name=(str(data.get('run_name') or '').strip()
                  or definition.name),
        status='pending', project_id=mission.project_id,
        triggered_by=actor['name'], idempotency_key=idempotency_key,
        idempotency_request_hash=request_hash,
        controller_run_id=mission.mission_key,
        correlation_id=f'mission:{mission.id}',
        trigger_source='mission_dispatch', context_json=context,
    )
    db.session.add(run)
    try:
        db.session.flush()
        freeze_run_context(
            run, team=team,
            manager_claw_id=actor['id'] if team else mission.main_claw_id)
        if stage:
            stage.workflow_run_id = run.id
            stage.version = int(stage.version or 1) + 1
        definition_json = copy.deepcopy(definition.definition_json or {})
        for position, step in enumerate(definition_json.get('steps') or []):
            initial_status = (
                'waiting_approval'
                if step.get('approval_required') and not step.get('depends_on')
                else 'pending')
            db.session.add(WorkflowRunStep(
                run_id=run.id, step_id=step['id'], position=position,
                name=step.get('name') or step['id'],
                step_type=step.get('type') or 'worker_task',
                runner=step.get('runner') or '',
                target_claw_id=step.get('target_claw_id'),
                target_agent=step.get('target_agent') or '',
                target_post=step.get('target_post') or '',
                status=initial_status,
                depends_on_json=step.get('depends_on') or [],
                step_config_json=step,
            ))
        db.session.flush()
        from app.api.workflows import _recompute_run_status
        _recompute_run_status(run, actor['name'])

        snapshot_library_id, snapshot_warning = resolve_workflow_library_id(
            data, context, workflow_key=definition.workflow_key)
        if snapshot_library_id is not None:
            snapshot_record = None
            try:
                with db.session.begin_nested():
                    snapshot_record, freeze_warning = (
                        freeze_workflow_run_library_snapshot(
                            run, snapshot_library_id, actor['name']))
                snapshot_warning = snapshot_warning or freeze_warning
            except Exception as exc:
                current_app.logger.warning(
                    'Mission Run %s library snapshot failed: %s',
                    run.id, exc, exc_info=True)
                snapshot_warning = {
                    'code': 'TESTCASE_LIBRARY_SNAPSHOT_FAILED',
                    'message': 'Mission Run continues without frozen library',
                    'details': {'library_id': snapshot_library_id},
                }
            updated_context = copy.deepcopy(run.context_json or {})
            if snapshot_record is not None:
                updated_context['testcase_library_snapshot'] = (
                    snapshot_record.to_dict())
            if snapshot_warning:
                updated_context = append_workflow_snapshot_warning(
                    updated_context, snapshot_warning)
            run.context_json = updated_context

        dispatch = WorkflowMissionDispatch(
            mission_id=mission.id, definition_id=definition.id,
            workflow_run_id=run.id,
            decision_key=decision_key or idempotency_key,
            idempotency_key=idempotency_key, request_hash=request_hash,
            reason=reason, status='created',
            created_by_claw_id=actor['id'],
            worker_claw_id=worker_claw_id,
        )
        mission.child_run_count = int(mission.child_run_count or 0) + 1
        db.session.add(dispatch)
        db.session.add(AuditLog(
            action='dispatch', resource_type='workflow_mission',
            resource_id=mission.id, resource_name=mission.mission_key,
            operator=actor['name'], ip_address=request.remote_addr,
            detail=json.dumps({
                'definition_id': definition.id,
                'workflow_run_id': run.id,
                'worker_claw_id': worker_claw_id,
                'decision_key': dispatch.decision_key,
                'reason': reason,
            }, ensure_ascii=False, sort_keys=True),
        ))
        db.session.commit()
    except ContextSnapshotError as exc:
        db.session.rollback()
        return _error(
            exc.code, str(exc), 409,
            {'workflow_definition_id': definition.id,
             'worker_claw_id': worker_claw_id})
    except IntegrityError:
        db.session.rollback()
        existing = WorkflowMissionDispatch.query.filter_by(
            mission_id=mission_id, idempotency_key=idempotency_key).first()
        if existing and existing.request_hash == request_hash:
            return jsonify(_dispatch_payload(existing, True)), 200
        raise

    return jsonify(_dispatch_payload(dispatch, False)), 201
