"""Database-driven Plan supervision. No model calls and no implicit Flow grants."""
import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone

from flask import current_app
from app import db
from app.models import (AgentTeam, ClawMessage, OpenClawInstance, TestPlan, TestTask,
                        WorkflowMission, WorkflowMissionDispatch, WorkflowRun, WorkflowRunStep, _now)
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
            'worker_lease_receipt_required': True, 'auto_start': False}


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
            'instruction': '先回读并 claim 唯一监督租约。恢复关联 Mission/Run，勿重复创建。'
                           '稍后继续须提交 decision 并拿到调度回执；无有效租约不得派工。'
                           '禁止模型轮询；正常心跳和无变化检查不通知 Owner。'}, ensure_ascii=False))
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
        return {'scheduled': bool(check)}
    return receipt(sup, 'decision', body, apply)


def mission_guard(mission_id, claw_id, body):
    # Even when feature is disabled, an already-bound Mission remains fenced.
    sup = PlanSupervisor.query.filter_by(mission_id=mission_id).with_for_update().first()
    if sup:
        if not enabled():
            fail('PLAN_SUPERVISION_DISABLED', '计划监督已禁用', 503)
        require_lease(sup, claw_id, body.get('plan_supervision') or {})
    return sup


def sweep(now=None):
    """Backfill source state/health and pump due outboxes; never invoke a model."""
    if not enabled():
        return 0
    now = now or _now()
    ids = [r.plan_id for r in PlanSupervisor.query.filter(
        PlanSupervisor.status.notin_(['expired', 'stopped', 'blocked'])).order_by(
            PlanSupervisor.plan_id).all()]
    # One small transaction per Plan. The caller's scheduler already serializes scans.
    count = 0
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
