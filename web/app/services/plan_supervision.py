"""Database-driven Plan supervision. No model calls and no implicit Flow grants."""
import copy
import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone

from flask import current_app
from app import db
from app.models import (AgentTeam, AgentTeamMission, AuditLog, ClawMessage,
                        MissionStage, OpenClawInstance, TestPlan, TestTask,
                        WorkflowMission, WorkflowMissionDispatch, WorkflowRun,
                        WorkflowRunStep, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt


class SupervisionError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def enabled():
    return current_app.config.get('PLAN_SUPERVISION_ENABLED', False) is True


def team_enabled(team_id):
    scope = str(current_app.config.get('PLAN_SUPERVISION_TEAM_IDS', '')).strip()
    return enabled() and (scope == '*' or
        (type(team_id) is int and str(team_id) in {s.strip() for s in scope.split(',')}))


def team_binding_valid(team_id, project_id, claw_id):
    if not team_enabled(team_id):
        return False
    # Explicit '*' retains standalone Plan support; scoped production needs a team.
    if team_id is None:
        return True
    team = db.session.get(AgentTeam, team_id)
    return bool(team and team.status == 'active' and team.project_id == project_id
                and team.primary_manager_claw_id == claw_id)


def team_capability(team_id):
    return {'enabled': team_enabled(team_id), 'contract': 'hub.plan_supervision.v1',
            'start_requires': ['team_id', 'orchestrator_claw_id', 'command_key'],
            'supervisor_api': '/api/v1/test-plans/{plan_id}/supervision',
            'worker_lease_receipt_required': True, 'auto_start': bool(team_enabled(team_id)),
            'owner_notification_policy': {
                'enabled': True,
                'delivery': 'agent_wecom',
                'fallback': 'hub_receipt_only',
                'notify_on': ['blocked', 'run_terminal', 'heartbeat_anomaly', 'stale'],
                'quiet_on': ['heartbeat', 'unchanged', 'ordinary_progress'],
            }}


def fail(code, message, status=409):
    raise SupervisionError(code, message, status)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def ends_at(plan):
    return datetime.combine(plan.end_date + timedelta(days=1), time.min)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    except (ValueError, TypeError):
        fail('PLAN_TIME_INVALID', '时间必须为带时区的 ISO 8601', 400)


def locked(plan_id):
    return PlanSupervisor.query.filter_by(plan_id=plan_id).with_for_update().first()


def manager_lease_payload(sup, now=None):
    """Return the manager fencing receipt paired with a Supervisor lease."""
    if not sup or not sup.team_id:
        return None
    now = now or _now()
    team = db.session.get(AgentTeam, sup.team_id)
    if not team:
        return None
    active = bool(
        team.status == 'active'
        and team.active_manager_claw_id == sup.orchestrator_claw_id
        and team.manager_session_id
        and team.manager_lease_expires_at
        and team.manager_lease_expires_at > now)
    return {
        'active': active,
        'epoch': int(team.manager_epoch or 0),
        'manager_claw_id': team.active_manager_claw_id,
        'session_id': team.manager_session_id or '',
        'expires_at': (
            team.manager_lease_expires_at.isoformat() + '+08:00'
            if team.manager_lease_expires_at else None),
    }


def ensure_manager_tenure(sup, now=None, ttl_seconds=300):
    """Acquire/renew the team manager lease for one fenced plan Supervisor.

    A live lease held by another manager is never stolen.  An expired lease is
    reacquired with a stable plan-scoped session so Worker restarts can recover
    from Hub state instead of chat history.
    """
    if not sup.team_id:
        return None
    now = now or _now()
    team = (AgentTeam.query.filter_by(id=sup.team_id)
            .populate_existing().with_for_update().first())
    if (not team or team.status != 'active'
            or team.project_id != db.session.get(TestPlan, sup.plan_id).project_id
            or team.primary_manager_claw_id != sup.orchestrator_claw_id):
        fail('PLAN_TEAM_NOT_ENABLED', '团队已失效，或计划监督者不再是主测试经理', 409)
    live = bool(team.manager_lease_expires_at and team.manager_lease_expires_at > now)
    if live and team.active_manager_claw_id != sup.orchestrator_claw_id:
        fail('PLAN_MANAGER_LEASE_HELD', '另一测试经理仍持有有效调度任期', 409)
    acquired = not live
    if acquired:
        team.manager_epoch = int(team.manager_epoch or 0) + 1
        team.active_manager_claw_id = sup.orchestrator_claw_id
        team.manager_session_id = 'plan-supervisor:%s:%s' % (
            sup.plan_id, team.manager_epoch)
    elif not team.manager_session_id:
        fail('PLAN_MANAGER_LEASE_INVALID', '当前经理任期缺少会话凭据', 409)
    team.manager_lease_expires_at = now + timedelta(
        seconds=max(30, min(int(ttl_seconds or 300), 300)))
    if acquired:
        add_event(sup.plan_id, 'manager_lease_acquired',
                  ['manager-lease', team.manager_epoch], {
                      'manager_claw_id': sup.orchestrator_claw_id,
                      'manager_epoch': team.manager_epoch,
                  }, now)
    return manager_lease_payload(sup, now)


def _task_assignment(team, task):
    if not task.assignee_claw_id:
        return None
    if task.assignee_claw_id in (
            team.primary_manager_claw_id, team.backup_manager_claw_id):
        # A test manager owns orchestration and review, never an execution
        # Stage.  Historical plans may still point a task at the manager; keep
        # that visible as an assignment gap so it can be reassigned explicitly.
        return None
    members = [row for row in team.members
               if row.claw_id == task.assignee_claw_id]
    if not members:
        return None
    text = '%s %s' % (task.name or '', task.description or '')
    prefer_executor = task.task_type in (
        'functional', 'automation', 'activity', 'performance',
        'compatibility', 'security', 'interface')
    rows = sorted(members, key=lambda row: (
        0 if ((row.role_key == 'test_executor') == prefer_executor) else 1,
        row.role_key))
    member = rows[0]
    if member.role_key != 'test_executor':
        return {'role_key': member.role_key, 'specialty': None}
    specialties = sorted(set(member.specialties_json or []))
    choices = []
    if task.task_type == 'performance':
        choices.append('client_performance')
    if re.search(r'android|ios|adb|手机|微信|包', text, re.I):
        choices.append('mobile_package')
    if task.task_type == 'automation' or re.search(r'unity|编辑器|flow\s*#?12', text, re.I):
        choices.append('editor')
    specialty = next((item for item in choices if item in specialties),
                     specialties[0] if specialties else None)
    return {'role_key': 'test_executor', 'specialty': specialty}


def _stage_snapshot(team, task, assignment):
    return {
        'test_plan_id': task.plan_id,
        'test_task_id': task.id,
        'test_task_name': task.name,
        'test_task_type': task.task_type,
        'test_task_priority': task.priority,
        'scheduled_start_date': str(task.start_date) if task.start_date else None,
        'scheduled_end_date': str(task.end_date) if task.end_date else None,
        'team_assignment': {
            'team_id': team.id,
            'team_version': team.version,
            'role_key': assignment['role_key'],
            'specialty': assignment['specialty'],
        },
    }


def sync_plan_stages(sup):
    """Append immutable stages for newly assigned non-terminal TestTasks."""
    if not sup or not sup.mission_id or not sup.team_id:
        return 0
    team = db.session.get(AgentTeam, sup.team_id)
    mission = db.session.get(WorkflowMission, sup.mission_id)
    binding = db.session.get(AgentTeamMission, sup.mission_id)
    if not team or not mission or not binding:
        fail('PLAN_MISSION_INCOMPLETE', '计划 Mission 缺少团队绑定', 409)
    existing = {}
    for stage in MissionStage.query.filter_by(mission_id=mission.id).all():
        task_id = (stage.input_snapshot_json or {}).get('test_task_id')
        if type(task_id) is int:
            existing[task_id] = stage
    created = 0
    for task in TestTask.query.filter_by(plan_id=sup.plan_id).order_by(TestTask.id).all():
        if task.id in existing or task.status in ('completed', 'skipped'):
            continue
        assignment = _task_assignment(team, task)
        if not assignment:
            reason = ('manager_cannot_execute'
                      if task.assignee_claw_id in (
                          team.primary_manager_claw_id,
                          team.backup_manager_claw_id)
                      else 'missing_or_invalid_team_member')
            add_event(sup.plan_id, 'task_assignment_gap',
                      ['task-assignment-gap', task.id, task.assignee_claw_id], {
                          'task_id': task.id,
                          'assignee_claw_id': task.assignee_claw_id,
                          'reason': reason,
                      })
            continue
        snapshot = _stage_snapshot(team, task, assignment)
        db.session.add(MissionStage(
            mission_id=mission.id, stage_key='test_task_%s' % task.id,
            stage_version=1, role_key=assignment['role_key'],
            assigned_claw_id=task.assignee_claw_id, state='ready',
            input_snapshot_json=snapshot, evidence_refs_json=[]))
        add_event(sup.plan_id, 'task_stage_created',
                  ['task-stage', task.id], {
                      'task_id': task.id,
                      'stage_key': 'test_task_%s' % task.id,
                      'assigned_claw_id': task.assignee_claw_id,
                  })
        created += 1
    if created:
        db.session.flush()
        stages = MissionStage.query.filter_by(mission_id=mission.id).order_by(
            MissionStage.stage_key, MissionStage.stage_version).all()
        manifest = [{
            'stage_key': stage.stage_key,
            'role_key': stage.role_key,
            'assigned_claw_id': stage.assigned_claw_id,
            'input_snapshot': stage.input_snapshot_json or {},
        } for stage in stages]
        binding.plan_sha256 = digest(manifest)
        mission.version = int(mission.version or 1) + 1
        context = dict(mission.context_json or {})
        context['test_task_ids'] = sorted(
            (stage.input_snapshot_json or {}).get('test_task_id')
            for stage in stages
            if type((stage.input_snapshot_json or {}).get('test_task_id')) is int)
        context['stage_count'] = len(stages)
        mission.context_json = context
    return created


def ensure_team_mission(sup):
    """Create/recover the one durable Mission owned by a team Plan."""
    if not sup.team_id:
        return None
    plan = db.session.get(TestPlan, sup.plan_id)
    team = db.session.get(AgentTeam, sup.team_id)
    if not plan or not team:
        fail('PLAN_TEAM_NOT_ENABLED', '计划或团队不存在', 409)
    if sup.mission_id:
        mission = db.session.get(WorkflowMission, sup.mission_id)
        if not mission:
            fail('PLAN_MISSION_INCOMPLETE', '监督记录引用的 Mission 不存在', 409)
        sync_plan_stages(sup)
        return mission
    mission_key = 'plan-supervisor-%s' % plan.id
    mission = WorkflowMission.query.filter_by(mission_key=mission_key).first()
    if mission:
        binding = db.session.get(AgentTeamMission, mission.id)
        if (mission.project_id != plan.project_id
                or mission.main_claw_id != sup.orchestrator_claw_id
                or not binding or binding.team_id != team.id):
            fail('PLAN_MISSION_CONFLICT', '稳定 Mission key 已被不兼容记录占用', 409)
    else:
        excluded = {team.primary_manager_claw_id, team.backup_manager_claw_id}
        allowed_workers = sorted({row.claw_id for row in team.members
                                  if row.claw_id not in excluded})
        mission = WorkflowMission(
            mission_key=mission_key, project_id=plan.project_id,
            main_claw_id=sup.orchestrator_claw_id,
            objective='监督测试计划 #%s：%s' % (plan.id, plan.name),
            status='active', control_mode='team_managed',
            allowed_definition_ids_json=sorted(set(
                (team.policy_json or {}).get('allowed_definition_ids') or [])),
            denied_definition_ids_json=[],
            allowed_worker_claw_ids_json=allowed_workers,
            max_child_runs=int((team.policy_json or {}).get('max_child_runs') or 20),
            child_run_count=0, max_retries_per_flow=3,
            allow_external_notification=False, allow_destructive_actions=False,
            context_json={
                'plan_supervision_id': plan.id,
                'test_plan_id': plan.id,
                'team_id': team.id,
                'created_by': 'hub_plan_supervisor',
            },
            created_by_type='system', created_by_id=0,
            created_by_name='Hub Plan Supervisor', expires_at=ends_at(plan))
        db.session.add(mission)
        db.session.flush()
        db.session.add(AgentTeamMission(
            mission_id=mission.id, team_id=team.id, team_version=team.version,
            snapshot_json={
                'primary_manager_claw_id': team.primary_manager_claw_id,
                'backup_manager_claw_id': team.backup_manager_claw_id,
                'policy': copy.deepcopy(team.policy_json or {}),
                'members': [row.to_dict() for row in team.members],
                'manager_epoch_at_creation': team.manager_epoch,
            }))
        db.session.add(AuditLog(
            action='create', resource_type='workflow_mission',
            resource_id=mission.id, resource_name=mission.mission_key,
            operator='Hub Plan Supervisor', ip_address='',
            detail=json.dumps({
                'plan_id': plan.id, 'team_id': team.id,
                'main_claw_id': sup.orchestrator_claw_id,
                'allowed_worker_claw_ids': allowed_workers,
            }, ensure_ascii=False, sort_keys=True)))
    sup.mission_id = mission.id
    db.session.flush()
    sync_plan_stages(sup)
    add_event(sup.plan_id, 'mission_created', ['mission', mission.id], {
        'mission_id': mission.id,
        'stage_count': MissionStage.query.filter_by(mission_id=mission.id).count(),
    })
    return mission


def bootstrap(plan, team_id, claw_id, body, now=None):
    """Atomically activate Plan -> manager tenure -> Supervisor -> Mission."""
    now = now or _now()
    identity_hash = digest({
        'plan_id': plan.id, 'team_id': team_id,
        'orchestrator_claw_id': claw_id,
    })
    sup = locked(plan.id)
    if sup and sup.start_hash != identity_hash:
        fail('PLAN_ALREADY_SUPERVISED', '计划已绑定监督工作项；不可更换团队或身份')
    if not sup:
        sup = PlanSupervisor(
            plan_id=plan.id, team_id=team_id, orchestrator_claw_id=claw_id,
            start_hash=identity_hash, status='waiting', cursor=0,
            acknowledged_cursor=0, fencing_token=0, expired_turns=0)
        db.session.add(sup)
        db.session.flush()

    wake_target = {'claw_id': None}

    def apply():
        plan.status = 'active'
        sup.next_check_at = max(now, datetime.combine(plan.start_date, time.min))
        manager = ensure_manager_tenure(sup, now)
        mission = ensure_team_mission(sup) if team_id else None
        add_event(plan.id, 'plan_started', ['start', plan.id], {
            'orchestrator_claw_id': claw_id,
            'mission_id': mission.id if mission else None,
        }, now)
        target = pump(sup, now)
        wake_target['claw_id'] = target
        return {
            'scheduled': True,
            'mission_id': mission.id if mission else None,
            'stage_count': (MissionStage.query.filter_by(
                mission_id=mission.id).count() if mission else 0),
            'manager_lease': manager,
        }

    result = receipt(sup, 'start', body, apply)
    # Older stored receipts predate the manager/Mission bootstrap.  Repair the
    # durable state before returning instead of replaying a false success.
    if team_id and (not sup.mission_id or not manager_lease_payload(sup, now)['active']):
        result.update(apply())
        record = db.session.get(PlanSupervisorReceipt, result['receipt_id'])
        if record:
            record.response_json = dict(result, supervision=sup.to_dict())
    result['supervision'] = sup.to_dict()
    # Delivery is intentionally outside the persisted idempotency receipt.
    # Replaying start must not emit another SSE wake for the same outbox row.
    result['wake_claw_id'] = wake_target['claw_id']
    return sup, result


def available(sup, now=None):
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    claw = db.session.get(OpenClawInstance, sup.orchestrator_claw_id)
    return bool(plan and plan.status == 'active' and plan.project_id
                and team_binding_valid(sup.team_id, plan.project_id, sup.orchestrator_claw_id)
                and claw and claw.status != 'deleted' and claw.project_id == plan.project_id
                and sup.status not in ('stopped', 'expired', 'blocked')
                and datetime.combine(plan.start_date, time.min) <= now < ends_at(plan))


def add_event(plan_id, kind, key, payload=None, now=None):
    """Same transaction as the source state; unique outbox key survives retries."""
    table = PlanSupervisorEvent.__table__
    statement = table.insert().values(plan_id=plan_id, event_key=digest(key), kind=kind,
                                     payload_json=payload or {}, created_at=now or _now())
    dialect = db.session.get_bind().dialect.name
    if dialect == 'sqlite':
        statement = statement.prefix_with('OR IGNORE')
    elif dialect in ('mysql', 'mariadb'):
        statement = statement.prefix_with('IGNORE')
    else:
        raise RuntimeError('Plan supervisor requires SQLite or MariaDB/MySQL')
    db.session.execute(statement)


def cancel_wake(sup):
    if sup.wake_message_id:
        msg = db.session.get(ClawMessage, sup.wake_message_id)
        if msg and msg.status not in ('done', 'failed'):
            msg.status = 'failed'
            msg.failed_reason = 'PLAN_SUPERVISION_WAKE_SUPERSEDED'
    sup.wake_message_id = None


def stop(sup, reason):
    cancel_wake(sup)
    sup.status = reason
    sup.next_check_at = None
    sup.fencing_token += 1
    sup.lease_owner = None
    sup.lease_expires_at = None
    sup.turn_deadline_at = None


def ingest(sup):
    # Query unsequenced rows, NOT id > cursor: a lower ID may commit later.
    rows = PlanSupervisorEvent.query.filter_by(plan_id=sup.plan_id, sequence=None).order_by(
        PlanSupervisorEvent.id).limit(200).all()
    for row in rows:
        sup.cursor += 1
        row.sequence = sup.cursor
        if row.kind in ('task_changed', 'run_changed', 'step_progress', 'run_created'):
            sup.last_progress_at = max(sup.last_progress_at or row.created_at, row.created_at)
    return rows


def pump(sup, now=None):
    """Create at most one durable wake. SSE is only an optimization after commit."""
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    if not plan or now >= ends_at(plan):
        if sup.status != 'expired':
            stop(sup, 'expired')
        return None
    if plan.status != 'active':
        if sup.status not in ('stopped', 'expired'):
            stop(sup, 'stopped')
        return None
    if not available(sup, now):
        return None
    ingest(sup)
    if sup.status == 'leased':
        if sup.lease_expires_at and sup.lease_expires_at > now:
            return None
        sup.expired_turns += 1
        old_fence = sup.fencing_token
        stop(sup, 'waiting' if sup.expired_turns < 3 else 'blocked')
        add_event(sup.plan_id, 'supervisor_lease_expired', ['lease', old_fence],
                  {'fencing_token': old_fence, 'attempts': sup.expired_turns}, now)
        ingest(sup)
        if sup.status == 'blocked':
            return None
    if sup.wake_message_id:
        message = db.session.get(ClawMessage, sup.wake_message_id)
        if message and message.status not in ('failed', 'done') and sup.last_wake_at and now < sup.last_wake_at + timedelta(minutes=10):
            return None
        sup.expired_turns += 1
        stop(sup, 'waiting' if sup.expired_turns < 3 else 'blocked')
        if sup.status == 'blocked':
            return None
    pending = sup.cursor > sup.acknowledged_cursor
    due = bool(sup.next_check_at and sup.next_check_at <= now)
    if not pending and not due:
        return None
    # Ordinary progress coalesces; anomalies/terminal outcomes bypass cooldown.
    urgent = PlanSupervisorEvent.query.filter(
        PlanSupervisorEvent.plan_id == sup.plan_id,
        PlanSupervisorEvent.sequence > sup.acknowledged_cursor,
        PlanSupervisorEvent.kind.in_(['run_terminal', 'heartbeat_anomaly', 'stale',
                                      'supervisor_lease_expired', 'plan_started'])).first()
    if not due and not urgent and sup.last_wake_at and now < sup.last_wake_at + timedelta(seconds=60):
        return None
    if due:
        add_event(sup.plan_id, 'timer_due', ['timer', sup.next_check_at], {}, now)
        ingest(sup)
        sup.next_check_at = None
    message = ClawMessage(claw_id=sup.orchestrator_claw_id, sender_name='Hub Plan Supervisor',
        msg_type='plan_supervision', direction='to_claw', status='pending',
        content=json.dumps({'contract': 'hub.plan_supervision.v1', 'plan_id': sup.plan_id,
            'team_id': sup.team_id,
            'supervisor_api': '/api/v1/test-plans/%s/supervision' % sup.plan_id,
            'cursor': sup.cursor,
            'owner_notification_policy': {
                'delivery': 'agent_wecom',
                'notify_on': ['blocked', 'run_terminal', 'heartbeat_anomaly', 'stale'],
                'quiet_on': ['heartbeat', 'unchanged', 'ordinary_progress'],
                'decision_response_field': 'owner_notification',
            },
            'instruction': '先回读并 claim 唯一监督租约。恢复关联 Mission/Run，勿重复创建。'
                           '稍后继续须提交 decision 并拿到调度回执；无有效租约不得派工。'
                           '禁止模型轮询；正常心跳和无变化检查不通知 Owner。'
                           'decision 返回 owner_notification.required=true 时，使用本 Agent 自己的企微通道'
                           '向 Owner 汇报；不得调用 Hub /wecom/send 或 SendRTXInfo。'}, ensure_ascii=False))
    db.session.add(message)
    db.session.flush()
    sup.wake_message_id = message.id
    sup.last_wake_at = now
    sup.status = 'pending'
    return sup.orchestrator_claw_id


def require_lease(sup, claw_id, body, now=None):
    now = now or _now()
    if not isinstance(body, dict):
        fail('PLAN_LEASE_INVALID', '租约凭据必须为 JSON 对象', 400)
    if not available(sup, now):
        fail('PLAN_SUPERVISION_INACTIVE', '计划未开始、已结束或监督被停止')
    if (claw_id != sup.orchestrator_claw_id or sup.status != 'leased'
            or not sup.lease_expires_at or sup.lease_expires_at <= now
            or type(body.get('fencing_token')) is not int
            or body['fencing_token'] != sup.fencing_token
            or body.get('worker_id') != sup.lease_owner):
        fail('PLAN_SUPERVISION_FENCED', '必须持有当前计划监督租约', 409)
    ensure_manager_tenure(sup, now)


def lease_credentials(body):
    """Normalize the advertised nested lease and the legacy flat shape.

    Mission dispatch never uses ``worker_id`` for executor selection (that is
    ``worker_claw_id``), so accepting the two lease fields at the top level is
    unambiguous.  Conflicting dual representations fail closed.
    """
    body = body if isinstance(body, dict) else {}
    nested = body.get('plan_supervision')
    flat_present = 'worker_id' in body or 'fencing_token' in body
    if nested is not None and not isinstance(nested, dict):
        fail('PLAN_LEASE_INVALID', 'plan_supervision 必须为 JSON 对象', 400)
    if nested is not None and flat_present:
        flat = {
            'worker_id': body.get('worker_id'),
            'fencing_token': body.get('fencing_token'),
        }
        if any(nested.get(key) != value for key, value in flat.items()):
            fail('PLAN_LEASE_CONFLICT', '嵌套与平铺的监督租约凭据不一致', 400)
    if nested is not None:
        return nested
    if flat_present:
        return {
            'worker_id': body.get('worker_id'),
            'fencing_token': body.get('fencing_token'),
        }
    return {}


def receipt(sup, action, body, apply):
    key = body.get('command_key')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', key):
        fail('PLAN_COMMAND_KEY_REQUIRED', '必须提供稳定 command_key', 400)
    hashed = digest(body)
    old = PlanSupervisorReceipt.query.filter_by(plan_id=sup.plan_id, action=action, command_key=key).first()
    if old:
        if old.request_hash != hashed:
            fail('PLAN_COMMAND_CONFLICT', '同一 command_key 不得改变请求')
        return dict(old.response_json, replayed=True)
    extra = apply() or {}
    record = PlanSupervisorReceipt(plan_id=sup.plan_id, action=action, command_key=key,
                                   request_hash=hashed, response_json={})
    db.session.add(record)
    db.session.flush()
    result = {'receipt_id': record.id, 'replayed': False, 'supervision': sup.to_dict(), **extra}
    record.response_json = result
    return result


def claim(sup, claw_id, body, now=None):
    now = now or _now()
    def apply():
        if not available(sup, now):
            fail('PLAN_SUPERVISION_INACTIVE', '计划当前不可监督')
        if (sup.status == 'leased' or not sup.wake_message_id
                or body.get('wake_message_id') != sup.wake_message_id):
            fail('PLAN_SUPERVISION_BUSY', '没有可领取的唤醒，或已有监督 Turn')
        owner = body.get('worker_id')
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', owner):
            fail('PLAN_WORKER_ID_INVALID', 'worker_id 格式不合法', 400)
        ensure_manager_tenure(sup, now)
        ingest(sup)
        sup.fencing_token += 1
        sup.status = 'leased'
        sup.lease_owner = owner
        sup.lease_cursor = sup.cursor
        sup.lease_expires_at = now + timedelta(seconds=180)
        sup.turn_deadline_at = now + timedelta(minutes=10)
    if claw_id != sup.orchestrator_claw_id:
        fail('PLAN_ORCHESTRATOR_REQUIRED', '不是该计划监督 Agent', 403)
    result = receipt(sup, 'claim', body, apply)
    # Replayed receipts do not grant an expired/replaced lease.
    result['lease_valid'] = bool(available(sup, now) and sup.status == 'leased'
        and sup.lease_expires_at and sup.lease_expires_at > now
        and result['supervision']['fencing_token'] == sup.fencing_token)
    return result


def decide(sup, claw_id, body, now=None):
    now = now or _now()
    def apply():
        require_lease(sup, claw_id, body, now)
        if type(body.get('cursor')) is not int or body['cursor'] != sup.lease_cursor:
            fail('PLAN_CURSOR_CONFLICT', '必须确认本 Turn 领取时的事件游标')
        outcome = body.get('outcome')
        if outcome not in ('wait', 'blocked'):
            fail('PLAN_DECISION_INVALID', 'outcome 仅支持 wait/blocked', 400)
        summary = body.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            fail('PLAN_DECISION_INVALID', '必须提供不超过 4000 字的决策摘要', 400)
        check = None
        if outcome == 'wait':
            check = parse_time(body.get('next_check_at'))
            if not now < check < ends_at(db.session.get(TestPlan, sup.plan_id)):
                fail('PLAN_SCHEDULE_OUT_OF_RANGE', '下一次检查须在未来且不晚于计划结束')
            if body.get('resume_condition') != 'timer_or_event':
                fail('PLAN_RESUME_CONDITION_REQUIRED', '须声明 timer_or_event；重要事件可提前唤醒', 400)
        elif body.get('next_check_at') is not None:
            fail('PLAN_DECISION_INVALID', '人工阻断不能附带自动唤醒时间', 400)
        event_kinds = sorted({row.kind for row in PlanSupervisorEvent.query.filter(
            PlanSupervisorEvent.plan_id == sup.plan_id,
            PlanSupervisorEvent.sequence > sup.acknowledged_cursor,
            PlanSupervisorEvent.sequence <= sup.lease_cursor,
        ).all()})
        notification_reasons = []
        if outcome == 'blocked':
            notification_reasons.append('blocked')
        notification_reasons.extend(kind for kind in (
            'run_terminal', 'heartbeat_anomaly', 'stale') if kind in event_kinds)
        sup.acknowledged_cursor = sup.lease_cursor
        sup.last_decision_json = {'outcome': outcome, 'summary': summary,
            'cursor': sup.lease_cursor, 'at': now.isoformat() + '+08:00'}
        msg = db.session.get(ClawMessage, sup.wake_message_id)
        if msg:
            msg.status, msg.done_at = 'done', now
        sup.wake_message_id = None
        sup.status = 'waiting' if outcome == 'wait' else 'blocked'
        sup.next_check_at = check
        sup.resume_condition = 'timer_or_event' if check else 'manual'
        sup.lease_owner = None
        sup.lease_expires_at = None
        sup.turn_deadline_at = None
        sup.expired_turns = 0
        return {
            'scheduled': bool(check),
            'owner_notification': {
                'required': bool(notification_reasons),
                'reasons': notification_reasons,
                'event_kinds': event_kinds,
                'delivery': 'agent_wecom',
                'status': ('agent_action_required' if notification_reasons
                           else 'not_required'),
            },
        }
    return receipt(sup, 'decision', body, apply)


def mission_guard(mission_id, claw_id, body):
    # Even when feature is disabled, an already-bound Mission remains fenced.
    sup = PlanSupervisor.query.filter_by(mission_id=mission_id).with_for_update().first()
    if sup:
        if not enabled():
            fail('PLAN_SUPERVISION_DISABLED', '计划监督已禁用', 503)
        require_lease(sup, claw_id, lease_credentials(body))
    return sup


def _stage_task_id(stage):
    value = (stage.input_snapshot_json or {}).get('test_task_id') if stage else None
    return value if type(value) is int and value > 0 else None


def task_dispatch_receipts(sup):
    """Read authoritative Mission/Run/claim state for supervised TestTasks."""
    if not sup or not sup.mission_id:
        return []
    items = []
    for stage in MissionStage.query.filter_by(mission_id=sup.mission_id).order_by(
            MissionStage.id).all():
        task_id = _stage_task_id(stage)
        if not task_id:
            continue
        claim = None
        if stage.workflow_run_id:
            claim = WorkflowRunStep.query.filter(
                WorkflowRunStep.run_id == stage.workflow_run_id,
                WorkflowRunStep.claimed_by.isnot(None),
                WorkflowRunStep.claimed_by != '',
            ).order_by(WorkflowRunStep.claimed_at.asc(), WorkflowRunStep.id).first()
        items.append({
            'task_id': task_id,
            'stage_key': stage.stage_key,
            'stage_state': stage.state,
            'executor_claw_id': stage.assigned_claw_id,
            'workflow_run_id': stage.workflow_run_id,
            'claimed': bool(claim),
            'claimed_by': claim.claimed_by if claim else '',
            'claimed_at': str(claim.claimed_at) if claim and claim.claimed_at else None,
            'claim_fencing_token': (
                int(claim.claim_fencing_token or 0) if claim else None),
        })
    return items


def task_dispatch_receipt(task):
    sup = locked(task.plan_id)
    if not sup:
        return None
    return next((row for row in task_dispatch_receipts(sup)
                 if row['task_id'] == task.id), None)


def require_task_dispatch_receipt(task):
    """A supervised TestTask becomes running only after a real Worker claim."""
    sup = locked(task.plan_id)
    if not sup:
        return None
    receipt = next((row for row in task_dispatch_receipts(sup)
                    if row['task_id'] == task.id), None)
    if not receipt or not receipt['workflow_run_id'] or not receipt['claimed']:
        fail('TEST_TASK_DISPATCH_RECEIPT_REQUIRED',
             '任务仅为 pending/assigned；须先取得 Mission、Child Run 与 Worker claim 回执', 409)
    return receipt


def record_workflow_claim(run_id, claw_id, worker_id, now=None):
    """Promote the linked TestTask only after its assigned Worker claims a Run."""
    stage = MissionStage.query.filter_by(workflow_run_id=run_id).first()
    if not stage or stage.assigned_claw_id != claw_id:
        return None
    task_id = _stage_task_id(stage)
    if not task_id:
        return None
    sup = PlanSupervisor.query.filter_by(mission_id=stage.mission_id).with_for_update().first()
    task = db.session.get(TestTask, task_id)
    if (not sup or not task or task.plan_id != sup.plan_id
            or task.assignee_claw_id != claw_id):
        return None
    if task.status in ('assigned', 'pending'):
        task.status = 'in_progress'
        task.progress = max(1, int(task.progress or 0))
    add_event(sup.plan_id, 'task_claimed',
              ['task-claimed', task.id, run_id, claw_id, worker_id], {
                  'task_id': task.id,
                  'mission_id': sup.mission_id,
                  'run_id': run_id,
                  'executor_claw_id': claw_id,
                  'worker_id': worker_id,
              }, now or _now())
    return task


def sweep(now=None):
    """Backfill source state/health and pump due outboxes; never invoke a model."""
    if not enabled():
        return 0
    now = now or _now()
    count = 0
    # Repair active scoped team Plans that were activated before the durable
    # supervision contract was deployed.  This is intentionally bounded and
    # creates no Workflow Run; it only establishes the unique control chain.
    candidates = TestPlan.query.filter(
        TestPlan.status == 'active', TestPlan.team_id.isnot(None),
        TestPlan.start_date <= now.date(), TestPlan.end_date >= now.date(),
    ).order_by(TestPlan.id).limit(100).all()
    for plan in candidates:
        if locked(plan.id) or not team_enabled(plan.team_id):
            continue
        team = db.session.get(AgentTeam, plan.team_id)
        if (not team or team.status != 'active'
                or team.project_id != plan.project_id):
            continue
        try:
            _, result = bootstrap(plan, team.id, team.primary_manager_claw_id, {
                'command_key': 'auto-active-plan:%s' % plan.id,
                'team_id': team.id,
                'orchestrator_claw_id': team.primary_manager_claw_id,
            }, now)
            target = result.pop('wake_claw_id', None)
            db.session.commit()
            if target:
                count += 1
                wake(target)
        except SupervisionError:
            db.session.rollback()
            current_app.logger.exception(
                'Plan supervisor bootstrap failed for plan_id=%s', plan.id)
    ids = [r.plan_id for r in PlanSupervisor.query.filter(
        PlanSupervisor.status.notin_(['expired', 'stopped', 'blocked'])).order_by(
            PlanSupervisor.plan_id).all()]
    # One small transaction per Plan. The caller's scheduler already serializes scans.
    for plan_id in ids:
        sup = locked(plan_id)
        plan = db.session.get(TestPlan, plan_id)
        if available(sup, now):
            observations = dict(sup.observations_json or {})
            for task in TestTask.query.filter_by(plan_id=plan_id).all():
                payload = {'task_id': task.id, 'status': task.status}
                add_event(plan_id, 'task_changed', ['task', task.id, task.status, task.updated_at], payload, now)
            runs = (WorkflowRun.query.join(WorkflowMissionDispatch,
                WorkflowMissionDispatch.workflow_run_id == WorkflowRun.id).filter(
                    WorkflowMissionDispatch.mission_id == sup.mission_id).all()) if sup.mission_id else []
            for run in runs:
                terminal = run.status not in ('pending', 'running', 'retrying', 'waiting_approval')
                kind = 'run_terminal' if terminal else 'run_changed'
                add_event(plan_id, kind, ['run', run.id, run.status, run.current_step_id, run.updated_at],
                          {'run_id': run.id, 'status': run.status}, now)
                if terminal:
                    continue
                for step in WorkflowRunStep.query.filter_by(run_id=run.id).all():
                    if step.status not in ('running', 'retrying'):
                        continue
                    signature = digest([step.status, step.progress_phase, step.progress_percent,
                                        step.progress_message, step.progress_json])
                    observation_key = 'step:%s' % step.id
                    old = observations.get(observation_key)
                    progress = (datetime.fromisoformat(old['changed_at']) if old and old['signature'] == signature
                                else now if old else step.progress_at or step.started_at or run.created_at)
                    observations[observation_key] = {'signature': signature, 'changed_at': progress.isoformat()}
                    add_event(plan_id, 'step_progress', ['step-observation', step.id, signature],
                              {'run_id': run.id, 'step_id': step.step_id, 'progress_percent': step.progress_percent}, now)
                    heartbeat = step.heartbeat_at or step.started_at or run.created_at
                    if progress and now - progress > timedelta(minutes=10):
                        add_event(plan_id, 'stale', ['stale', step.id, progress],
                                  {'run_id': run.id, 'step_id': step.step_id}, now)
                    if heartbeat and now - heartbeat > timedelta(minutes=3):
                        add_event(plan_id, 'heartbeat_anomaly', ['heartbeat', step.id, heartbeat],
                                  {'run_id': run.id, 'step_id': step.step_id}, now)
            sup.observations_json = observations
        target = pump(sup, now)
        db.session.commit()
        if target:
            count += 1
            wake(target)
    return count


def wake(claw_id):
    try:
        from app.api.agent_client import notify_claw
        notify_claw(claw_id)
    except Exception:
        current_app.logger.warning('Plan supervisor outbox pending for claw_id=%s', claw_id)
