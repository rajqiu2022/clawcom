"""Database-driven Plan supervision. No model calls and no implicit Flow grants."""
import copy
import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone

from flask import current_app
from app import db
from app.models import (AgentTask, AgentTeam, AgentTeamMemberStatus,
                        AgentTeamMemberTask, AgentTeamMission,
                        AnalysisRefreshBatch, AuditLog, CapabilityGap,
                        ClawMessage, MissionStage, OpenClawInstance,
                        RequirementItem, TestIteration, TestPlan, TestReport,
                        TestTask, WorkflowMission, WorkflowMissionDispatch,
                        WorkflowRun, WorkflowRunStep, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt


class SupervisionError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


RECOVERABLE_STATUSES = {'waiting', 'pending', 'leased', 'degraded', 'retryable'}
OWNER_GATE_STATUSES = {'blocked_owner_gate'}
RUN_ACTIVE_STATUSES = {'pending', 'running', 'retrying', 'waiting_approval'}
DEFAULT_SCHEDULE = {
    'timezone': 'Asia/Shanghai',
    'morning_check': '09:30',
    'progress_summaries': ['13:30'],
    'day_close': '18:30',
}


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
    team = db.session.get(AgentTeam, team_id) if type(team_id) is int else None
    schedule = ((team.policy_json or {}).get('supervision_schedule')
                if team else None) or DEFAULT_SCHEDULE
    return {'enabled': team_enabled(team_id), 'contract': 'hub.plan_supervision.v1',
            'contract_extensions': [
                'recoverable_states_v2', 'persistent_schedule_v1',
                'terminal_projection_v1', 'authoritative_execution_v1'],
            'start_requires': ['team_id', 'orchestrator_claw_id', 'command_key'],
            'supervisor_api': '/api/v1/test-plans/{plan_id}/supervision',
            'direct_task_dispatch_api': (
                '/api/v1/test-plans/{plan_id}/supervision/agent-tasks'),
            'worker_lease_receipt_required': True, 'auto_start': bool(team_enabled(team_id)),
            'persistent_schedule': schedule,
            'decision_outcomes': [
                'wait', 'degraded', 'retryable', 'blocked'],
            'supervisor_states': [
                'waiting', 'pending', 'leased', 'degraded', 'retryable',
                'blocked_owner_gate', 'stopped', 'expired'],
            'blocking_policy': {
                'default_scope': 'stage',
                'plan_scope_requires': {'block_scope': 'plan'},
                'continue_when_undispatched_stages_remain': True,
                'note': ('单个 Child Run/Stage 阻断不得暂停整个计划；主 Agent 应继续判断并派发'
                         '无依赖、无资源冲突的 Stage。只有团队级问题才显式使用 block_scope=plan。'),
            },
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


def schedule_policy(sup):
    """Return the persisted team schedule, with a safe legacy default."""
    team = db.session.get(AgentTeam, sup.team_id) if sup and sup.team_id else None
    configured = ((team.policy_json or {}).get('supervision_schedule')
                  if team else None)
    if not isinstance(configured, dict):
        configured = {}
    return {
        'timezone': 'Asia/Shanghai',
        'morning_check': str(configured.get('morning_check') or
                             DEFAULT_SCHEDULE['morning_check']),
        'progress_summaries': list(configured.get('progress_summaries') or
                                   DEFAULT_SCHEDULE['progress_summaries']),
        'day_close': str(configured.get('day_close') or
                         DEFAULT_SCHEDULE['day_close']),
    }


def _clock_at(day, value):
    hour, minute = (int(part) for part in str(value).split(':', 1))
    return datetime.combine(day, time(hour=hour, minute=minute))


def next_schedule_at(sup, now=None):
    """Compute the next Hub-owned daily tick independent of a model turn."""
    now = now or _now()
    policy = schedule_policy(sup)
    clocks = [policy['morning_check'], *policy['progress_summaries'],
              policy['day_close']]
    candidates = [_clock_at(now.date(), value) for value in clocks]
    candidates += [_clock_at(now.date() + timedelta(days=1), value)
                   for value in clocks]
    return min(value for value in candidates if value > now)


def enqueue_schedule_ticks(sup, now=None):
    """Persist every due daily slot once; late watchdog runs still catch up."""
    now = now or _now()
    policy = schedule_policy(sup)
    slots = [('morning_version_check', policy['morning_check'])]
    slots += [('progress_summary_%s' % index, value)
              for index, value in enumerate(policy['progress_summaries'], 1)]
    slots.append(('day_close', policy['day_close']))
    for kind, value in slots:
        due_at = _clock_at(now.date(), value)
        if due_at <= now:
            add_event(sup.plan_id, 'schedule_tick', [
                'schedule', sup.plan_id, now.date().isoformat(), kind,
            ], {
                'schedule_kind': kind,
                'scheduled_at': due_at.isoformat() + '+08:00',
                'timezone': 'Asia/Shanghai',
            }, now)
    scheduled = next_schedule_at(sup, now)
    if not sup.next_check_at or sup.next_check_at > scheduled:
        sup.next_check_at = scheduled
    return scheduled


def project_portfolio_snapshot(sup, now=None):
    """Bounded authoritative facts for one manager turn, never full documents."""
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    if not plan:
        return {}
    active_iteration = (TestIteration.query.filter_by(
        project_id=plan.project_id, status='active')
        .order_by(TestIteration.updated_at.desc(), TestIteration.id.desc()).first())
    iteration_id = getattr(plan, 'iteration_id', None) or (
        active_iteration.id if active_iteration else None)
    requirements = (RequirementItem.query.filter_by(iteration_id=iteration_id).count()
                    if iteration_id else 0)
    recent_reports = (TestReport.query.filter_by(
        project_id=plan.project_id, is_deleted=False)
        .order_by(TestReport.updated_at.desc(), TestReport.id.desc())
        .limit(10).all())
    gap_counts = dict(db.session.query(
        CapabilityGap.status, db.func.count(CapabilityGap.id)).filter_by(
            project_id=plan.project_id).group_by(CapabilityGap.status).all())
    tasks = TestTask.query.filter_by(plan_id=plan.id).all()
    task_counts = {}
    for task in tasks:
        task_counts[task.status] = task_counts.get(task.status, 0) + 1
    runs = (WorkflowRun.query.join(
        WorkflowMissionDispatch,
        WorkflowMissionDispatch.workflow_run_id == WorkflowRun.id).filter(
            WorkflowMissionDispatch.mission_id == sup.mission_id).all()
            if sup.mission_id else [])
    recent_code = (AnalysisRefreshBatch.query.filter_by(
        project_id=plan.project_id)
        .order_by(AnalysisRefreshBatch.updated_at.desc(),
                  AnalysisRefreshBatch.id.desc()).first())
    team = db.session.get(AgentTeam, sup.team_id) if sup.team_id else None
    member_execution = []
    if team:
        from app.services import agent_team_activity as activity
        people = activity.roster(team)
        claws = {row.id: row for row in OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(list(people))).all()} if people else {}
        statuses = {row.claw_id: row for row in
                    AgentTeamMemberStatus.query.filter_by(team_id=team.id).all()}
        for claw_id in sorted(people):
            claw = claws.get(claw_id)
            if not claw:
                continue
            status = statuses.get(claw_id)
            task = (db.session.get(AgentTeamMemberTask, status.current_task_id)
                    if status and status.current_task_id else None)
            summary = activity.member_summary(
                people[claw_id], claw, status, task, team=team, now=now)
            member_execution.append({
                'claw_id': claw_id, 'name': claw.name,
                'effective_state': summary['effective_state'],
                'authoritative_execution': summary['authoritative_execution'],
                'self_report': summary['self_report'],
            })
    return {
        'captured_at': now.isoformat() + '+08:00',
        'project_id': plan.project_id,
        'test_plan': {'id': plan.id, 'name': plan.name, 'status': plan.status},
        'iteration': (active_iteration.to_dict() if active_iteration else None),
        'requirement_count': requirements,
        'code_change': ({
            'batch_id': recent_code.id, 'from_commit': recent_code.from_commit,
            'to_commit': recent_code.to_commit,
            'commit_count': int(recent_code.commit_count or 0),
            'changed_file_count': int(recent_code.changed_file_count or 0),
            'risk_level': recent_code.risk_level,
            'status': recent_code.status,
            'updated_at': str(recent_code.updated_at),
        } if recent_code else None),
        'test_task_status_counts': task_counts,
        'bug_count': sum(int(task.bug_count or 0) for task in tasks),
        'workflow_runs': [{
            'run_id': run.id, 'definition_id': run.definition_id,
            'status': run.status, 'current_step_id': run.current_step_id,
            'updated_at': str(run.updated_at) if run.updated_at else None,
        } for run in sorted(runs, key=lambda row: row.id)[-50:]],
        'recent_reports': [{
            'id': row.id, 'title': row.title, 'status': row.status,
            'risk_level': row.risk_level, 'updated_at': str(row.updated_at),
        } for row in recent_reports],
        'capability_gap_status_counts': gap_counts,
        'team_member_execution': member_execution,
        'schedule': schedule_policy(sup),
    }


def locked(plan_id):
    return PlanSupervisor.query.filter_by(plan_id=plan_id).with_for_update().first()


def manager_lease_payload(sup, now=None):
    """Return durable manager authority in the legacy response envelope.

    The PlanSupervisor still owns a short-lived turn lease/fencing token so
    concurrent turns cannot mutate one plan. That runtime lock is deliberately
    separate from the test manager's role authorization.
    """
    if not sup or not sup.team_id:
        return None
    now = now or _now()
    team = db.session.get(AgentTeam, sup.team_id)
    if not team:
        return None
    active = team.has_manager_authority(sup.orchestrator_claw_id)
    return {
        'active': active,
        'epoch': int(team.manager_epoch or 0),
        'manager_claw_id': sup.orchestrator_claw_id if active else None,
        'session_id': (
            'team-manager:%s:%s:%s' % (
                team.id, int(team.version or 1), sup.orchestrator_claw_id)
            if active else ''),
        'expires_at': None,
        'mode': 'team_role_assignment',
    }


def ensure_manager_tenure(sup, now=None, ttl_seconds=300):
    """Validate durable team-manager authority for this Supervisor.

    ``ttl_seconds`` remains in the signature for old callers but no longer
    creates or renews an authorization lease.
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
                and sup.status not in ('stopped', 'expired', 'blocked_owner_gate')
                and datetime.combine(plan.start_date, time.min) <= now < ends_at(plan))


def repair_recoverable_state(sup, now=None):
    """Repair legacy/transient blocked states without crossing an Owner gate."""
    now = now or _now()
    if sup.status == 'blocked':
        decision = sup.last_decision_json or {}
        if decision.get('block_scope') == 'plan' or sup.resume_condition == 'manual':
            sup.status = 'blocked_owner_gate'
            return False
        sup.status = 'retryable'
        sup.next_check_at = min(sup.next_check_at or now, now)
        sup.resume_condition = 'timer_or_event'
        add_event(sup.plan_id, 'supervisor_auto_repaired', [
            'auto-repair', sup.plan_id, sup.fencing_token,
        ], {'from_status': 'blocked', 'to_status': 'retryable'}, now)
        return True
    return False


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
    repair_recoverable_state(sup, now)
    if not available(sup, now):
        return None
    ensure_manager_tenure(sup, now)
    enqueue_schedule_ticks(sup, now)
    ingest(sup)
    if sup.status == 'leased':
        if sup.lease_expires_at and sup.lease_expires_at > now:
            return None
        sup.expired_turns += 1
        old_fence = sup.fencing_token
        cancel_wake(sup)
        sup.status = 'retryable'
        sup.fencing_token += 1
        sup.lease_owner = None
        sup.lease_expires_at = None
        sup.turn_deadline_at = None
        delay = min(900, 30 * (2 ** min(sup.expired_turns - 1, 5)))
        sup.next_check_at = now + timedelta(seconds=delay)
        sup.resume_condition = 'timer_or_event'
        add_event(sup.plan_id, 'supervisor_lease_expired', ['lease', old_fence],
                  {'fencing_token': old_fence, 'attempts': sup.expired_turns,
                   'retryable': True, 'next_check_at':
                       sup.next_check_at.isoformat() + '+08:00'}, now)
        ingest(sup)
        return None
    if sup.wake_message_id:
        message = db.session.get(ClawMessage, sup.wake_message_id)
        if message and message.status not in ('failed', 'done') and sup.last_wake_at and now < sup.last_wake_at + timedelta(minutes=10):
            return None
        sup.expired_turns += 1
        cancel_wake(sup)
        sup.status = 'degraded'
        delay = min(900, 30 * (2 ** min(sup.expired_turns - 1, 5)))
        sup.next_check_at = now + timedelta(seconds=delay)
        sup.resume_condition = 'timer_or_event'
        add_event(sup.plan_id, 'supervisor_delivery_degraded', [
            'delivery-degraded', sup.plan_id, sup.expired_turns,
            sup.next_check_at,
        ], {'attempts': sup.expired_turns, 'next_check_at':
            sup.next_check_at.isoformat() + '+08:00'}, now)
        ingest(sup)
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
                                      'supervisor_lease_expired', 'plan_started',
                                      'run_recovery_required',
                                      'run_reconciliation_resolved'])).first()
    if not due and not urgent and sup.last_wake_at and now < sup.last_wake_at + timedelta(seconds=60):
        return None
    if due:
        add_event(sup.plan_id, 'timer_due', ['timer', sup.next_check_at], {}, now)
        ingest(sup)
        # Keep the next Hub-owned daily tick even if the model does not
        # schedule another turn.
        sup.next_check_at = next_schedule_at(sup, now)
    message = ClawMessage(claw_id=sup.orchestrator_claw_id, sender_name='Hub Plan Supervisor',
        msg_type='plan_supervision', direction='to_claw', status='pending',
        content=json.dumps({'contract': 'hub.plan_supervision.v1', 'plan_id': sup.plan_id,
            'team_id': sup.team_id,
            'supervisor_api': '/api/v1/test-plans/%s/supervision' % sup.plan_id,
            'cursor': sup.cursor,
            'instruction': '先回读并 claim 唯一监督租约。恢复关联 Mission/Run，勿重复创建。'
                           '已有 Run 的恢复统一调用 /workflow-runs/{run_id}/recover，'
                           '由 Hub 判定等待、对账、原地重启或替代；不要自行串联低层接口。'
                           '稍后继续须提交 decision 并拿到调度回执；无有效租约不得派工。'
                           '单个 Child Run 或 Stage 阻断时，继续判断未派发 Stage 的依赖与资源冲突；'
                           '默认 blocked 仅按阶段级处理，不暂停全队。只有团队级问题才提交 '
                           'block_scope=plan。'
                           '无需 Flow 的独立测试任务由你决策后调用 '
                           '/api/v1/test-plans/{plan_id}/supervision/agent-tasks，'
                           '提交 test_task_id、稳定 command_key 和可选 instruction；'
                           'Hub 将创建可租约、可回写的普通 AgentTask。禁止用 Todo 代替正式派工。'
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


def undispatched_stages(sup):
    """Return stages that still have no Child Run for the manager to assess.

    Hub deliberately does not infer cross-task dependencies here.  The main
    Agent remains responsible for deciding which of these stages can run in
    parallel; this list only prevents one terminal Child Run from silently
    turning into a plan-wide stop.
    """
    if not sup or not sup.mission_id:
        return []
    items = []
    rows = MissionStage.query.filter_by(
        mission_id=sup.mission_id, state='ready', workflow_run_id=None,
    ).order_by(MissionStage.id).all()
    for stage in rows:
        snapshot = stage.input_snapshot_json or {}
        items.append({
            'stage_key': stage.stage_key,
            'test_task_id': _stage_task_id(stage),
            'test_task_name': snapshot.get('test_task_name') or '',
            'executor_claw_id': stage.assigned_claw_id,
            'role_key': stage.role_key,
            'agent_task_dispatch_api': (
                '/api/v1/test-plans/%s/supervision/agent-tasks'
                % sup.plan_id),
        })
    return items


_PLAN_AGENT_TASK_CONTRACT = 'hub.plan_test_task.agent_task.v1'


def _agent_task_plan_link(agent_task):
    """Return a trusted Plan/TestTask link from an ordinary AgentTask."""
    try:
        payload = json.loads(agent_task.payload or '{}')
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict)
            or payload.get('contract') != _PLAN_AGENT_TASK_CONTRACT):
        return None
    try:
        return {
            'plan_id': int(payload['test_plan_id']),
            'test_task_id': int(payload['test_task_id']),
            'mission_id': int(payload['mission_id']),
            'mission_stage_id': int(payload['mission_stage_id']),
            'stage_key': str(payload['stage_key']),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _plan_agent_tasks(sup):
    """Index the bounded ordinary task history belonging to one Plan Mission."""
    if not sup or not sup.mission_id:
        return {}
    rows = (AgentTask.query.filter(
        AgentTask.task_type == 'test_plan_agent_task',
        AgentTask.task_id.like('plan_%s_test_task_%%' % sup.plan_id))
        .order_by(AgentTask.id.desc()).limit(1000).all())
    indexed = {}
    for row in rows:
        link = _agent_task_plan_link(row)
        if (link and link['plan_id'] == sup.plan_id
                and link['mission_id'] == sup.mission_id):
            indexed.setdefault(link['mission_stage_id'], row)
    return indexed


def dispatch_test_task(sup, manager_claw_id, test_task_id, body):
    """Dispatch a non-Flow TestTask through the ordinary AgentTask channel.

    This is intentionally independent from the Todo worker and from a blocked
    Plan Supervisor turn.  The durable team manager assignment is the authority;
    the command receipt supplies idempotency while the AgentTask lease supplies
    execution fencing.
    """
    if not sup or not sup.team_id or not sup.mission_id:
        fail('PLAN_MISSION_INCOMPLETE', '计划尚未建立团队 Mission', 409)
    retry_value = body.get('retry_max', 1)
    if isinstance(retry_value, bool):
        fail('AGENT_TASK_RETRY_INVALID', 'retry_max 须为 0 到 3 的整数', 400)
    try:
        retry_max = int(retry_value)
    except (TypeError, ValueError):
        fail('AGENT_TASK_RETRY_INVALID', 'retry_max 须为 0 到 3 的整数', 400)
    if retry_max < 0 or retry_max > 3:
        fail('AGENT_TASK_RETRY_INVALID', 'retry_max 须为 0 到 3 的整数', 400)
    team = db.session.get(AgentTeam, sup.team_id)
    plan = db.session.get(TestPlan, sup.plan_id)
    if (not team or team.status != 'active' or not plan
            or plan.status != 'active'
            or team.primary_manager_claw_id != manager_claw_id
            or sup.orchestrator_claw_id != manager_claw_id):
        fail('PLAN_MANAGER_REQUIRED', '仅当前团队主测试经理可以直接派发测试任务', 403)
    task = TestTask.query.filter_by(
        id=test_task_id, plan_id=sup.plan_id).with_for_update().first()
    if not task:
        fail('TEST_TASK_NOT_FOUND', '测试任务不存在', 404)
    if task.status not in ('assigned', 'pending'):
        fail('TEST_TASK_NOT_DISPATCHABLE', '仅新分配或待开始任务可直接派发', 409)
    stage = MissionStage.query.filter_by(
        mission_id=sup.mission_id,
        stage_key='test_task_%s' % task.id,
        stage_version=1,
    ).with_for_update().first()
    if (not stage or stage.assigned_claw_id != task.assignee_claw_id
            or not task.assignee_claw_id):
        fail('TEST_TASK_STAGE_INVALID', '任务缺少有效的团队执行阶段或执行 Agent', 409)
    if stage.workflow_run_id:
        fail('TEST_TASK_ALREADY_FLOW_DISPATCHED', '任务已经通过 Workflow 派发', 409)

    def apply():
        active = _plan_agent_tasks(sup).get(stage.id)
        if active and active.status in ('pending', 'running'):
            fail('TEST_TASK_ALREADY_DISPATCHED', '任务已有待领取或执行中的 AgentTask', 409)
        if stage.state != 'ready':
            fail('TEST_TASK_STAGE_NOT_READY', '任务阶段已派发或不再可执行', 409)
        instruction = str(body.get('instruction') or '').strip()
        if len(instruction) > 4000:
            fail('TEST_TASK_INSTRUCTION_INVALID', '执行说明不能超过 4000 字', 400)
        command_key = str(body.get('command_key') or '')
        task_id = 'plan_%s_test_task_%s_%s' % (
            sup.plan_id, task.id,
            hashlib.sha256(command_key.encode()).hexdigest()[:16])
        payload = {
            'contract': _PLAN_AGENT_TASK_CONTRACT,
            'test_plan_id': sup.plan_id,
            'test_task_id': task.id,
            'mission_id': sup.mission_id,
            'mission_stage_id': stage.id,
            'stage_key': stage.stage_key,
            'objective': task.name,
            'description': task.description or '',
            'instruction': instruction,
            'priority': task.priority,
            'task_type': task.task_type,
            'acceptance': {
                'result_contract': 'ordinary_agent_task',
                'report_to_hub': True,
            },
        }
        agent_task = AgentTask(
            task_id=task_id,
            claw_id=task.assignee_claw_id,
            task_type='test_plan_agent_task',
            command=instruction or (
                '执行测试计划 #%s 的任务 #%s：%s。完成后返回结构化结论、'
                'outputs 与 evidence。' % (sup.plan_id, task.id, task.name)),
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            status='pending',
            retry_max=retry_max,
        )
        db.session.add(agent_task)
        db.session.flush()
        stage.state = 'dispatched'
        stage.last_reason_code = 'ordinary_agent_task_dispatched'
        stage.fencing_token = int(stage.fencing_token or 0) + 1
        stage.version = int(stage.version or 1) + 1
        add_event(sup.plan_id, 'task_agent_dispatched', [
            'task-agent-dispatched', task.id, agent_task.task_id,
        ], {
            'task_id': task.id,
            'stage_key': stage.stage_key,
            'executor_claw_id': task.assignee_claw_id,
            'agent_task_id': agent_task.task_id,
        })
        return {
            'dispatched': True,
            'execution_mode': 'ordinary_agent_task',
            'test_task_id': task.id,
            'stage_key': stage.stage_key,
            'agent_task': agent_task.to_dict(),
            'wake_claw_id': task.assignee_claw_id,
        }

    return receipt(sup, 'dispatch_agent_task', body, apply)


def record_agent_task_claim(agent_task, now=None):
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    sup = locked(link['plan_id'])
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != task.assignee_claw_id
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID', 'AgentTask 与计划阶段绑定不一致', 409)
    if task.status in ('assigned', 'pending'):
        task.status = 'in_progress'
        task.progress = max(1, int(task.progress or 0))
    stage.state = 'running'
    stage.last_reason_code = 'ordinary_agent_task_claimed'
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_claimed', [
        'ordinary-task-claimed', agent_task.task_id,
        int(agent_task.attempt_no or 0),
    ], {
        'task_id': task.id,
        'mission_id': sup.mission_id,
        'agent_task_id': agent_task.task_id,
        'executor_claw_id': agent_task.claw_id,
        'fencing_token': int(agent_task.fencing_token or 0),
    }, now)
    return task


def record_agent_task_retry_pending(agent_task, now=None):
    """Project an expired attempt that has been requeued back to pending.

    Team membership remains durable; this only reflects that no Worker owns
    the current execution attempt while the same AgentTask waits for retry.
    """
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    sup = locked(link['plan_id'])
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != task.assignee_claw_id
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID',
             'AgentTask 与计划阶段绑定不一致', 409)
    if task.status in ('completed', 'skipped'):
        return task
    changed = task.status != 'pending' or stage.state != 'dispatched'
    task.status = 'pending'
    stage.state = 'dispatched'
    stage.last_reason_code = 'ordinary_agent_task_retry_pending'
    if changed:
        stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_agent_retry_pending', [
        'task-agent-retry-pending', agent_task.task_id,
        int(agent_task.attempt_no or 0), int(agent_task.retry_count or 0),
    ], {
        'task_id': task.id,
        'agent_task_id': agent_task.task_id,
        'executor_claw_id': agent_task.claw_id,
        'retry_count': int(agent_task.retry_count or 0),
        'retry_max': int(agent_task.retry_max or 0),
    }, now)
    return task


def record_agent_task_terminal(agent_task, result, status, now=None):
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    sup = locked(link['plan_id'])
    if not task or not stage or not sup:
        fail('PLAN_AGENT_TASK_LINK_INVALID', 'AgentTask 关联计划已不存在', 409)
    result = result if isinstance(result, dict) else {}
    provider_status = str(result.get('status') or '').lower()
    summary = str(result.get('summary') or result.get('reason') or '')[:8000]
    if status == 'completed' and provider_status == 'skipped':
        task.status, stage.state = 'skipped', 'skipped'
    elif status == 'completed':
        task.status, stage.state = 'completed', 'completed'
        task.progress = 100
    elif status == 'blocked':
        task.status, stage.state = 'blocked', 'blocked'
    else:
        task.status, stage.state = 'blocked', 'failed'
    task.result_summary = summary
    stage.last_reason_code = str(
        result.get('error_code') or status or 'agent_task_terminal')[:80]
    evidence = result.get('evidence')
    if evidence:
        refs = list(stage.evidence_refs_json or [])
        refs.append({
            'type': 'ordinary_agent_task_result',
            'agent_task_id': agent_task.task_id,
            'sha256': digest(result),
        })
        stage.evidence_refs_json = refs[-100:]
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_agent_terminal', [
        'task-agent-terminal', agent_task.task_id,
        int(agent_task.attempt_no or 0), status,
    ], {
        'task_id': task.id,
        'agent_task_id': agent_task.task_id,
        'status': status,
        'test_task_status': task.status,
        'summary': summary,
    }, now)
    return task


def reconcile_ordinary_task_truth(sup, now=None):
    """Repair TestTask/Stage projections from durable ordinary AgentTasks."""
    if not sup or not sup.mission_id:
        return 0
    now = now or _now()
    ordinary_tasks = _plan_agent_tasks(sup)
    count = 0
    for stage in MissionStage.query.filter_by(
            mission_id=sup.mission_id).order_by(MissionStage.id).all():
        agent_task = ordinary_tasks.get(stage.id)
        task_id = _stage_task_id(stage)
        task = db.session.get(TestTask, task_id) if task_id else None
        if not agent_task or not task:
            continue
        if agent_task.status == 'pending':
            if task.status == 'in_progress' or stage.state == 'running':
                record_agent_task_retry_pending(agent_task, now=now)
                count += 1
            continue
        if agent_task.status == 'running':
            if (agent_task.lease_expires_at
                    and agent_task.lease_expires_at <= now):
                continue
            if task.status != 'in_progress' or stage.state != 'running':
                record_agent_task_claim(agent_task, now=now)
                count += 1
            continue
        terminal = {
            'completed': ('completed', 'completed', 'completed'),
            'blocked': ('blocked', 'blocked', 'blocked'),
            'failed': ('failed', 'blocked', 'failed'),
            'cancelled': ('failed', 'blocked', 'failed'),
        }.get(agent_task.status)
        if not terminal:
            continue
        projected_status, task_status, stage_state = terminal
        if task.status == task_status and stage.state == stage_state:
            continue
        try:
            result = json.loads(agent_task.result or '{}')
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {'summary': str(result)}
        result.setdefault('status', projected_status)
        result.setdefault('error_code', agent_task.terminal_reason or '')
        result.setdefault('reason', agent_task.error or '')
        record_agent_task_terminal(
            agent_task, result, projected_status, now=now)
        count += 1
    return count


def recovery_snapshot(sup):
    """Expose stable Run recovery facts without asking the manager to infer."""
    empty = {
        'recovery_state': 'none',
        'reconciliation_owner': None,
        'conflicting_run_id': None,
        'allowed_actions': [],
        'next_check_at': (
            sup.next_check_at.isoformat() + '+08:00'
            if sup and sup.next_check_at else None),
    }
    if not sup or not sup.mission_id:
        return empty
    stages = MissionStage.query.filter_by(
        mission_id=sup.mission_id, state='ready').order_by(
            MissionStage.id.desc()).all()
    recovery_stage = next((stage for stage in stages
        if (stage.input_snapshot_json or {}).get('kind') ==
           'workflow_execution_reconciliation'), None)
    if recovery_stage:
        snapshot = recovery_stage.input_snapshot_json or {}
        owner = db.session.get(
            OpenClawInstance, recovery_stage.assigned_claw_id)
        return {
            'recovery_state': 'reconciliation_required',
            'reconciliation_owner': {
                'claw_id': recovery_stage.assigned_claw_id,
                'name': owner.name if owner else '',
            },
            'conflicting_run_id': snapshot.get('workflow_run_id'),
            'allowed_actions': ['wait', 'submit_reconciliation_receipt'],
            'next_check_at': empty['next_check_at'],
            'reconciliation_stage': recovery_stage.to_dict(
                include_input=True),
        }
    return empty


def decide(sup, claw_id, body, now=None):
    now = now or _now()
    def apply():
        require_lease(sup, claw_id, body, now)
        if type(body.get('cursor')) is not int or body['cursor'] != sup.lease_cursor:
            fail('PLAN_CURSOR_CONFLICT', '必须确认本 Turn 领取时的事件游标')
        outcome = body.get('outcome')
        if outcome not in ('wait', 'degraded', 'retryable', 'blocked'):
            fail('PLAN_DECISION_INVALID',
                 'outcome 仅支持 wait/degraded/retryable/blocked', 400)
        block_scope = body.get('block_scope')
        if block_scope is not None and (
                outcome != 'blocked' or block_scope not in ('stage', 'plan')):
            fail('PLAN_DECISION_INVALID',
                 'block_scope 仅可在 blocked 时设为 stage/plan', 400)
        summary = body.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            fail('PLAN_DECISION_INVALID', '必须提供不超过 4000 字的决策摘要', 400)
        remaining = undispatched_stages(sup)
        local_block = bool(
            outcome == 'blocked' and block_scope != 'plan' and remaining)
        effective_outcome = 'wait' if local_block else outcome
        effective_scope = (
            'stage' if local_block else
            ('plan' if outcome == 'blocked' else None))
        check = None
        if local_block:
            # End this fenced Turn, then immediately create a fresh wake so the
            # main Agent can decide which remaining Stage is actually safe to
            # dispatch.  No Worker is auto-selected by Hub.
            if body.get('next_check_at') is not None:
                fail('PLAN_DECISION_INVALID',
                     '阶段级阻断由 Hub 立即续调度，不接受 next_check_at', 400)
            check = now
        elif outcome in ('wait', 'degraded', 'retryable'):
            if body.get('next_check_at') is None and outcome in ('degraded', 'retryable'):
                check = now + timedelta(seconds=60)
            else:
                check = parse_time(body.get('next_check_at'))
            if not now < check < ends_at(db.session.get(TestPlan, sup.plan_id)):
                fail('PLAN_SCHEDULE_OUT_OF_RANGE', '下一次检查须在未来且不晚于计划结束')
            if (body.get('resume_condition') not in (None, 'timer_or_event')
                    or (outcome == 'wait'
                        and body.get('resume_condition') != 'timer_or_event')):
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
        sup.last_decision_json = {
            'outcome': effective_outcome,
            'requested_outcome': outcome,
            'block_scope': effective_scope,
            'summary': summary,
            'cursor': sup.lease_cursor,
            'at': now.isoformat() + '+08:00',
        }
        msg = db.session.get(ClawMessage, sup.wake_message_id)
        if msg:
            msg.status, msg.done_at = 'done', now
        sup.wake_message_id = None
        if effective_outcome == 'blocked':
            sup.status = 'blocked_owner_gate'
        elif outcome in ('degraded', 'retryable'):
            sup.status = outcome
        else:
            sup.status = 'waiting'
        sup.next_check_at = check
        sup.resume_condition = 'timer_or_event' if check else 'manual'
        sup.lease_owner = None
        sup.lease_expires_at = None
        sup.turn_deadline_at = None
        sup.expired_turns = 0
        if local_block:
            add_event(sup.plan_id, 'stage_blocked_continuation', [
                'stage-blocked-continuation', body.get('command_key'),
            ], {
                'summary': summary,
                'undispatched_stage_keys': [row['stage_key'] for row in remaining],
                'rule': 'manager_decides_next_stage',
            }, now)
        return {
            'scheduled': bool(check),
            'requested_outcome': outcome,
            'effective_outcome': effective_outcome,
            'supervisor_status': sup.status,
            'block_scope': effective_scope,
            'continue_supervision': local_block,
            'undispatched_stages': remaining,
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
    ordinary_tasks = _plan_agent_tasks(sup)
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
        ordinary = ordinary_tasks.get(stage.id)
        ordinary_claimed = bool(
            ordinary and ordinary.assigned_at
            and ordinary.status != 'pending')
        items.append({
            'task_id': task_id,
            'stage_key': stage.stage_key,
            'stage_state': stage.state,
            'executor_claw_id': stage.assigned_claw_id,
            'workflow_run_id': stage.workflow_run_id,
            'agent_task_id': ordinary.task_id if ordinary else None,
            'agent_task_status': ordinary.status if ordinary else None,
            'execution_mode': (
                'workflow' if stage.workflow_run_id else
                'ordinary_agent_task' if ordinary else None),
            'claimed': bool(claim) or ordinary_claimed,
            'claimed_by': (
                claim.claimed_by if claim else
                ('claw:%s' % ordinary.claw_id if ordinary_claimed else '')),
            'claimed_at': (
                str(claim.claimed_at) if claim and claim.claimed_at else
                str(ordinary.assigned_at)
                if ordinary_claimed and ordinary.assigned_at else None),
            'claim_fencing_token': (
                int(claim.claim_fencing_token or 0) if claim else
                int(ordinary.fencing_token or 0)
                if ordinary_claimed else None),
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
    if (not receipt or not receipt['claimed']
            or not (receipt['workflow_run_id'] or receipt['agent_task_id'])):
        fail('TEST_TASK_DISPATCH_RECEIPT_REQUIRED',
             '任务仅为 pending/assigned；须先取得 Child Run 或 AgentTask 的 Worker claim 回执', 409)
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
    if stage.state in ('ready', 'dispatched'):
        stage.state = 'running'
        stage.last_reason_code = 'workflow_claimed'
        stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_claimed',
              ['task-claimed', task.id, run_id, claw_id, worker_id], {
                  'task_id': task.id,
                  'mission_id': sup.mission_id,
                  'run_id': run_id,
                  'executor_claw_id': claw_id,
                  'worker_id': worker_id,
              }, now or _now())
    return task


def record_workflow_terminal(run_id, now=None):
    """Project one terminal Child Run into its Stage and TestTask exactly once."""
    now = now or _now()
    run = db.session.get(WorkflowRun, run_id)
    if not run or run.status in RUN_ACTIVE_STATUSES:
        return None
    stage = MissionStage.query.filter_by(workflow_run_id=run_id).with_for_update().first()
    if not stage:
        return None
    task_id = _stage_task_id(stage)
    sup = PlanSupervisor.query.filter_by(
        mission_id=stage.mission_id).with_for_update().first()
    task = db.session.get(TestTask, task_id) if task_id else None
    if not sup or not task or task.plan_id != sup.plan_id:
        return None
    mapping = {
        'succeeded': ('completed', 'completed', 100),
        'completed': ('completed', 'completed', 100),
        'skipped': ('skipped', 'skipped', int(task.progress or 0)),
        'cancelled': ('cancelled', 'blocked', int(task.progress or 0)),
        'blocked': ('blocked', 'blocked', int(task.progress or 0)),
        'failed': ('failed', 'blocked', int(task.progress or 0)),
    }
    stage_state, task_status, progress = mapping.get(
        run.status, ('failed', 'blocked', int(task.progress or 0)))
    changed = (stage.state != stage_state or task.status != task_status
               or (progress == 100 and int(task.progress or 0) != 100))
    stage.state = stage_state
    stage.last_reason_code = ('workflow_run_%s' % run.status)[:80]
    if changed:
        stage.version = int(stage.version or 1) + 1
    task.status = task_status
    task.progress = progress
    if run.summary:
        task.result_summary = str(run.summary)[:8000]
    refs = list(stage.evidence_refs_json or [])
    marker = {'type': 'workflow_run_terminal', 'run_id': run.id,
              'status': run.status}
    if not any(row.get('type') == marker['type'] and
               row.get('run_id') == run.id and row.get('status') == run.status
               for row in refs if isinstance(row, dict)):
        refs.append(marker)
        stage.evidence_refs_json = refs[-100:]
    add_event(sup.plan_id, 'workflow_terminal_projected', [
        'workflow-terminal-projected', run.id, run.status,
    ], {
        'run_id': run.id, 'stage_key': stage.stage_key,
        'stage_state': stage.state, 'task_id': task.id,
        'test_task_status': task.status,
    }, now)
    return task


def reconcile_plan_truth(sup, now=None):
    """Repair Run/Stage/Task drift after missed callbacks or process restarts."""
    if not sup or not sup.mission_id:
        return 0
    runs = (WorkflowRun.query.join(
        WorkflowMissionDispatch,
        WorkflowMissionDispatch.workflow_run_id == WorkflowRun.id).filter(
            WorkflowMissionDispatch.mission_id == sup.mission_id).all())
    count = 0
    for run in runs:
        if run.status not in RUN_ACTIVE_STATUSES:
            before = digest(task_dispatch_receipts(sup))
            record_workflow_terminal(run.id, now)
            after = digest(task_dispatch_receipts(sup))
            count += int(before != after)
    return count


def stage_truth_snapshot(sup):
    if not sup or not sup.mission_id:
        return {'independent_stage_status': [], 'actionable_gaps': [],
                'allowed_actions': [], 'next_check_at': None}
    stages = MissionStage.query.filter_by(mission_id=sup.mission_id).order_by(
        MissionStage.id).all()
    rows, gaps = [], []
    for stage in stages:
        task_id = _stage_task_id(stage)
        task = db.session.get(TestTask, task_id) if task_id else None
        item = {
            'stage_key': stage.stage_key, 'state': stage.state,
            'test_task_id': task_id,
            'test_task_status': task.status if task else None,
            'workflow_run_id': stage.workflow_run_id,
            'executor_claw_id': stage.assigned_claw_id,
            'reason_code': stage.last_reason_code or '',
        }
        rows.append(item)
        if stage.state in ('blocked', 'failed', 'cancelled'):
            gaps.append(item)
    actions = ['wait', 'dispatch_ready_stage', 'recover_run']
    if sup.status in OWNER_GATE_STATUSES:
        actions = ['wait_for_owner_resume']
    return {
        'independent_stage_status': rows,
        'actionable_gaps': gaps,
        'allowed_actions': actions,
        'next_check_at': (sup.next_check_at.isoformat() + '+08:00'
                          if sup.next_check_at else None),
    }


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
        PlanSupervisor.status.notin_(['expired', 'stopped',
                                      'blocked_owner_gate'])).order_by(
            PlanSupervisor.plan_id).all()]
    # One small transaction per Plan. The caller's scheduler already serializes scans.
    for plan_id in ids:
        sup = locked(plan_id)
        plan = db.session.get(TestPlan, plan_id)
        repair_recoverable_state(sup, now)
        # Ordinary AgentTasks can become terminal in the timeout watcher rather
        # than through the result endpoint.  Reconcile them even while the
        # supervisor is waiting/blocked so stale "running" UI never persists.
        reconcile_ordinary_task_truth(sup, now)
        if available(sup, now):
            ensure_manager_tenure(sup, now)
            enqueue_schedule_ticks(sup, now)
            reconcile_plan_truth(sup, now)
            observations = dict(sup.observations_json or {})
            for task in TestTask.query.filter_by(plan_id=plan_id).all():
                payload = {'task_id': task.id, 'status': task.status}
                add_event(plan_id, 'task_changed', ['task', task.id, task.status, task.updated_at], payload, now)
            runs = (WorkflowRun.query.join(WorkflowMissionDispatch,
                WorkflowMissionDispatch.workflow_run_id == WorkflowRun.id).filter(
                    WorkflowMissionDispatch.mission_id == sup.mission_id).all()) if sup.mission_id else []
            for run in runs:
                terminal = run.status not in RUN_ACTIVE_STATUSES
                kind = 'run_terminal' if terminal else 'run_changed'
                add_event(plan_id, kind, ['run', run.id, run.status, run.current_step_id, run.updated_at],
                          {'run_id': run.id, 'status': run.status}, now)
                if terminal:
                    record_workflow_terminal(run.id, now)
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
