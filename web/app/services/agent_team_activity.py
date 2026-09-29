"""Team-local observations. Self reports never mutate Run, Task or Claw state."""
import hashlib
import json
import re

from app import db
from app.models import (AgentTask, AgentTeamMemberStatus, AgentTeamMemberTask,
                        AgentTeamMemberReport, AgentTeamMission,
                        WorkflowMissionDispatch, WorkflowRun, WorkflowRunStep,
                        _now)
from app.services.agent_teams import TeamError, integer

STALE_SECONDS = 180
TASK_STATES = {'working', 'blocked', 'completed', 'failed', 'cancelled'}
TASK_TYPES = ('flow', 'bug_regression', 'code_analysis', 'version_data', 'other')
TERMINAL = {'completed', 'failed', 'cancelled'}


def roster(team):
    people = {}
    def add(claw_id, role, specialties=()):
        if claw_id is None:
            return
        entry = people.setdefault(claw_id, {'claw_id': claw_id, 'roles': [], 'specialties': []})
        if role not in entry['roles']:
            entry['roles'].append(role)
        entry['specialties'] = sorted(set(entry['specialties']) | set(specialties))
    add(team.primary_manager_claw_id, 'primary_manager')
    add(team.backup_manager_claw_id, 'backup_manager')
    for member in team.members:
        add(member.claw_id, member.role_key, member.specialties_json or [])
    return people


def authoritative_execution(team, claw_id, now=None):
    """Derive execution from Hub claims; self reports never overwrite it."""
    now = now or _now()
    mission_ids = [row.mission_id for row in AgentTeamMission.query.filter_by(
        team_id=team.id).all()]
    if mission_ids:
        run_ids = [row.workflow_run_id for row in WorkflowMissionDispatch.query.filter(
            WorkflowMissionDispatch.mission_id.in_(mission_ids)).all()]
        if run_ids:
            step = (WorkflowRunStep.query.join(
                WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id).filter(
                    WorkflowRunStep.run_id.in_(run_ids),
                    WorkflowRunStep.claimed_claw_id == claw_id,
                    WorkflowRunStep.status.in_(['running', 'retrying']),
                    WorkflowRun.status.in_([
                        'pending', 'running', 'retrying', 'waiting_approval']),
                    db.or_(WorkflowRunStep.claim_expires_at.is_(None),
                           WorkflowRunStep.claim_expires_at > now))
                .order_by(WorkflowRunStep.updated_at.desc(),
                          WorkflowRunStep.id.desc()).first())
            if step:
                return {
                    'source': 'workflow_claim', 'state': 'working',
                    'reference': 'workflow-run:%s/step:%s' % (
                        step.run_id, step.step_id),
                    'run_id': step.run_id, 'step_id': step.step_id,
                    'worker_id': step.claimed_by or '',
                    'heartbeat_at': str(step.heartbeat_at) if step.heartbeat_at else None,
                    'claim_expires_at': (str(step.claim_expires_at)
                                         if step.claim_expires_at else None),
                }
    task = (AgentTask.query.filter_by(
        claw_id=claw_id, task_type='test_plan_agent_task', status='running')
        .filter(db.or_(AgentTask.lease_expires_at.is_(None),
                       AgentTask.lease_expires_at > now))
        .order_by(AgentTask.assigned_at.desc(), AgentTask.id.desc()).first())
    if task:
        return {
            'source': 'agent_task_claim', 'state': 'working',
            'reference': 'agent-task:%s' % task.task_id,
            'agent_task_id': task.task_id,
            'heartbeat_at': (str(task.last_heartbeat_at)
                             if task.last_heartbeat_at else None),
            'claim_expires_at': (str(task.lease_expires_at)
                                 if task.lease_expires_at else None),
        }
    from app.models_plan_supervision import PlanSupervisor
    supervisor = (PlanSupervisor.query.filter_by(
        team_id=team.id, orchestrator_claw_id=claw_id, status='leased')
        .filter(PlanSupervisor.lease_expires_at > now)
        .order_by(PlanSupervisor.lease_expires_at.desc()).first())
    if supervisor:
        return {
            'source': 'plan_supervision_claim', 'state': 'working',
            'reference': 'plan-supervisor:%s' % supervisor.plan_id,
            'plan_id': supervisor.plan_id,
            'worker_id': supervisor.lease_owner or '',
            'claim_expires_at': str(supervisor.lease_expires_at),
        }
    return None


def member_summary(person, claw, status, task, team=None, now=None):
    now = now or _now()
    age = max(0, int((now - status.reported_at).total_seconds())) if status else None
    last_seen = claw.last_activity
    connection_age = max(0, int((now - last_seen).total_seconds())) if last_seen else None
    # Claw.status uses human activity labels too; only heartbeat recency is evidence of contact.
    connection = 'recently_seen' if connection_age is not None and connection_age <= STALE_SECONDS and claw.status not in ('offline', 'deleted') else 'not_recently_seen'
    execution = authoritative_execution(team, claw.id, now) if team else None
    reported_effective = (
        ('stale' if age >= STALE_SECONDS else status.state)
        if status else 'unknown')
    effective = execution['state'] if execution else reported_effective
    self_task = task.to_dict() if task else None
    return dict(person, name=claw.name, connection=connection,
                connection_label='近期有连接活动' if connection == 'recently_seen' else '暂无近期连接活动',
                last_seen_at=str(last_seen) if last_seen else None,
                reported_state=status.state if status else None,
                effective_state=effective,
                summary=status.summary if status else '', version=status.version if status else 0,
                reported_at=str(status.reported_at) if status else None,
                report_age_seconds=age, stale_after_seconds=STALE_SECONDS,
                current_task=execution or self_task,
                authoritative_execution=execution,
                self_report={'state': status.state if status else None,
                             'summary': status.summary if status else '',
                             'current_task': self_task,
                             'reported_at': (str(status.reported_at)
                                             if status else None)},
                source=('hub_authoritative_execution' if execution
                        else 'agent_self_report'))


def _text(value, field, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise TeamError('TEAM_ACTIVITY_INVALID', field + ' 格式或长度不合法', 400)
    return value.strip()


def _key(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value):
        raise TeamError('TEAM_ACTIVITY_INVALID', field + ' 须为 1–96 位字母、数字或 ._:-', 400)
    return value


def ingest(team, claw_id, data, on_transition=None):
    """Caller must hold the Team row lock, including first-report inserts."""
    allowed = {'event_id', 'expected_version', 'state', 'summary', 'task'}
    if set(data) - allowed:
        raise TeamError('TEAM_ACTIVITY_INVALID', '未知上报字段', 400)
    event_id = _key(data.get('event_id'), 'event_id')
    expected = integer(data.get('expected_version'), 'expected_version', 0)
    try:
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        raise TeamError('TEAM_ACTIVITY_INVALID', '上报数据须为有效 JSON', 400)
    if len(encoded.encode('utf-8')) > 12000:
        raise TeamError('TEAM_ACTIVITY_INVALID', '上报内容过大', 400)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    receipt = AgentTeamMemberReport.query.filter_by(team_id=team.id, claw_id=claw_id, event_id=event_id).first()
    if receipt:
        if receipt.request_sha256 != digest:
            raise TeamError('TEAM_ACTIVITY_EVENT_CONFLICT', '相同 event_id 不得改变请求内容')
        return dict(receipt.response_json, replayed=True)
    status = AgentTeamMemberStatus.query.filter_by(team_id=team.id, claw_id=claw_id).first()
    previous_state = status.state if status else None
    previous_task_id = status.current_task_id if status else None
    if expected != (status.version if status else 0):
        raise TeamError('TEAM_ACTIVITY_VERSION_CONFLICT', '状态版本已变化，请回读后上报；勿重放旧状态')
    state = data.get('state')
    if not isinstance(state, str) or state not in ('idle', 'working', 'blocked'):
        raise TeamError('TEAM_ACTIVITY_INVALID', 'state 仅支持 idle/working/blocked', 400)
    summary = _text(data.get('summary', ''), 'summary', 500)
    now = _now()
    current = db.session.get(AgentTeamMemberTask, status.current_task_id) if status and status.current_task_id else None
    payload = data.get('task')
    task = None
    if payload is not None:
        if not isinstance(payload, dict) or set(payload) - {'task_key','title','task_type','reference','status','progress_percent','progress_message'}:
            raise TeamError('TEAM_ACTIVITY_INVALID', '无效的 task 字段', 400)
        key = _key(payload.get('task_key'), 'task_key')
        title = _text(payload.get('title'), 'title', 240, True)
        kind = payload.get('task_type')
        task_state = payload.get('status')
        if (not isinstance(kind, str) or not isinstance(task_state, str)
                or kind not in TASK_TYPES or task_state not in TASK_STATES):
            raise TeamError('TEAM_ACTIVITY_INVALID', '无效的任务类型/状态', 400)
        reference = _text(payload.get('reference', ''), 'reference', 240)
        progress = payload.get('progress_percent')
        if progress is not None:
            integer(progress, 'progress_percent', 0, 100)
        message = _text(payload.get('progress_message', ''), 'progress_message', 4000)
        if (task_state in TERMINAL and state != 'idle') or (task_state not in TERMINAL and state != task_state):
            raise TeamError('TEAM_ACTIVITY_INVALID', '任务进行中时成员状态须一致；结束任务时须上报 idle', 400)
        if current and current.task_key != key:
            raise TeamError('TEAM_ACTIVITY_TASK_ACTIVE', '先结束当前任务，再上报新任务')
        task = AgentTeamMemberTask.query.filter_by(team_id=team.id, claw_id=claw_id, task_key=key).first()
        if task and task.status in TERMINAL:
            raise TeamError('TEAM_ACTIVITY_TASK_FINISHED', '已结束任务不可改写；重跑请使用新 task_key')
        if not task:
            if task_state in TERMINAL:
                raise TeamError('TEAM_ACTIVITY_TASK_NOT_STARTED', '须先上报任务开始，再上报结束', 400)
            task = AgentTeamMemberTask(team_id=team.id, claw_id=claw_id, task_key=key, title=title,
                                       task_type=kind, reference=reference, started_at=now)
            db.session.add(task)
        elif (title, kind, reference) != (task.title, task.task_type, task.reference):
            raise TeamError('TEAM_ACTIVITY_TASK_IDENTITY_CHANGED', '任务标题、类型和关联信息不可变；进展写入 progress_message')
        task.status, task.progress_percent, task.progress_message = task_state, progress, message
        task.updated_at = now
        task.finished_at = now if task_state in TERMINAL else None
        db.session.flush()
    elif current:
        raise TeamError('TEAM_ACTIVITY_TASK_REQUIRED', '空闲前须显式结束当前自报任务', 400)
    elif state != 'idle' and not summary:
        raise TeamError('TEAM_ACTIVITY_INVALID', '无任务实例时须说明正在做的工作或阻断', 400)
    if not status:
        status = AgentTeamMemberStatus(team_id=team.id, claw_id=claw_id)
        db.session.add(status)
    status.state, status.summary = state, summary
    status.current_task_id = task.id if task and task.status not in TERMINAL else None
    status.reported_at, status.version = now, expected + 1
    result = {'team_id':team.id, 'claw_id':claw_id, 'version':status.version, 'state':state,
              'summary':summary, 'task':task.to_dict() if task else None, 'reported_at':str(now),
              'event_id':event_id, 'source':'agent_self_report'}
    db.session.add(AgentTeamMemberReport(team_id=team.id, claw_id=claw_id, event_id=event_id,
        request_sha256=digest, task_id=task.id if task else None, response_json=result, created_at=now))
    if on_transition and (
            previous_state != state or previous_task_id != status.current_task_id):
        if not (previous_state is None and state == 'idle' and task is None):
            on_transition(team, claw_id, event_id, previous_state, state, now)
    db.session.commit()
    return dict(result, replayed=False)
