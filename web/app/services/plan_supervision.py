"""Database-driven Plan supervision. No model calls and no implicit Flow grants."""
import copy
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app
from app import db
from app.models import (AgentTask, AgentTeam, AgentTeamMember,
                        AgentTeamMemberStatus,
                        AgentTeamMemberTask, AgentTeamMission,
                        AnalysisRefreshBatch, AuditLog, CapabilityGap,
                        ClawMessage, ClawSidecarConfig, MissionStage,
                        ChatRoomMember,
                        OpenClawInstance,
                        RequirementItem, TestIteration, TestPlan, TestReport,
                        TestPlanReport, TestTask, TestTaskOccurrence, WorkflowMission,
                        WorkflowMissionDispatch,
                        WorkflowApproval, WorkflowDefinition, WorkflowRun,
                        WorkflowRunStep, _now)
from app.models_plan_supervision import PlanSupervisor, PlanSupervisorEvent, PlanSupervisorReceipt
from app.services.test_task_references import serialize_task_references


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
TASK_RECURRENCES = {'once', 'daily', 'weekly'}
OCCURRENCE_TERMINAL = {
    'completed', 'blocked', 'failed', 'skipped', 'cancelled',
    'blocked_terminal', 'analysis_incomplete',
}
MISSION_TERMINAL_STAGE_STATES = {
    'completed', 'skipped', 'cancelled', 'blocked', 'failed',
}
MISSION_CLOSEOUT_CONTRACT = 'hub.plan_supervision.closeout.v2'
PLAN_BLOCK_REASON_CODES = frozenset({
    'owner_cancelled', 'global_environment_unavailable',
    'global_release_invalid', 'global_security_or_compliance',
    'global_resource_unavailable', 'plan_prerequisite_missing',
})
PLAN_SCOPE_CONFLICT_RE = re.compile(
    r'(?:局部|阶段级|单(?:个|项)|不(?:升级|属于|是).{0,8}计划级|'
    r'不(?:影响|阻断).{0,12}(?:其他|独立)|其他.{0,12}(?:继续|可继续)|'
    r'独立.{0,12}(?:继续|可继续))', re.I)


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
                'terminal_projection_v1', 'authoritative_execution_v1',
                'execution_goal_resume_v1', 'condition_probe_v1',
                'control_action_dispatch_v1', 'fact_freshness_v1',
                'manager_goal_v1', 'manager_stage_actions_v1',
                'manager_recovery_actions_v1'],
            'start_requires': ['team_id', 'orchestrator_claw_id', 'command_key'],
            'supervisor_api': '/api/v1/test-plans/{plan_id}/supervision',
            'direct_task_dispatch_api': (
                '/api/v1/test-plans/{plan_id}/supervision/agent-tasks'),
            'manager_recovery_apis': {
                'new_attempt': ('/api/v1/test-plans/{plan_id}/supervision/'
                                'tasks/{test_task_id}/new-attempt'),
                'reassign_ready_stage': (
                    '/api/v1/test-plans/{plan_id}/supervision/tasks/'
                    '{test_task_id}/reassign'),
            },
            'condition_event_apis': {
                'occurrence': ('/api/v1/test-plans/{plan_id}/supervision/'
                               'occurrences/{occurrence_id}/condition-events'),
                'one_off_task': ('/api/v1/test-plans/{plan_id}/supervision/'
                                 'tasks/{test_task_id}/condition-events'),
            },
            'worker_lease_receipt_required': True, 'auto_start': bool(team_enabled(team_id)),
            'persistent_schedule': schedule,
            'decision_outcomes': [
                'wait', 'degraded', 'retryable', 'blocked'],
            'manager_decision_contract': {
                'name': 'hub.manager_goal.v1',
                'stage_actions': ['dispatch', 'defer', 'block', 'complete'],
                'tool_receipt_fields': ['stage_decisions', 'action_receipts'],
                'modes': {
                    'tool_first': 'AI 调用受控 Hub API 后提交动作回执',
                    'atomic_decision': 'AI 在 decision.stage_actions 中由 Hub 原子执行',
                },
                'safety_boundary': (
                    'Hub validates identity, plan scope, fencing, idempotency '
                    'and audit only; the manager Agent chooses the business action.'
                ),
            },
            'supervisor_states': [
                'waiting', 'pending', 'leased', 'degraded', 'retryable',
                'blocked_owner_gate', 'stopped', 'expired'],
            'blocking_policy': {
                'default_scope': 'stage',
                'plan_scope_requires': {
                    'block_scope': 'plan',
                    'plan_block_reason_code': sorted(PLAN_BLOCK_REASON_CODES),
                    'plan_block_evidence': 'non_empty_object_array',
                    'summary_must_not_claim_local_scope': True,
                },
                'continue_when_undispatched_stages_remain': True,
                'manager_auto_recovers_invalid_owner_gate': True,
                'note': ('单个 Child Run/Stage 阻断不得暂停整个计划；主 Agent 应继续判断并派发'
                         '无依赖、无资源冲突的 Stage。只有团队级问题才显式使用 block_scope=plan。'),
            },
            'owner_notification_policy': {
                'enabled': True,
                'delivery': 'hub_dual_outbox',
                'channels': ['agent_wecom_outbox', 'team_chat'],
                'fallback': 'independent_channel_retry',
                'notify_on': [
                    'blocked', 'run_terminal', 'heartbeat_anomaly', 'stale',
                    'occurrence_owner_gate', 'occurrence_due_unresolved',
                    'task_owner_gate', 'task_due_unresolved',
                    'supervision_report_updated', 'control_action_terminal'],
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


def final_day_close_reached(sup, now=None):
    """Whether the last planned day has reached its explicit closeout tick."""
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id) if sup else None
    if not plan or now.date() != plan.end_date:
        return False
    return now >= _clock_at(plan.end_date, schedule_policy(sup)['day_close'])


def _task_clock(value, default='00:00'):
    """Parse persisted HH:MM values; invalid legacy values fail safely."""
    raw = str(value or default)
    try:
        hour, minute = (int(part) for part in raw.split(':', 1))
        if hour not in range(24) or minute not in range(60):
            raise ValueError()
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return _task_clock(default, '00:00') if raw != default else time.min


def _task_occurs_on(task, day):
    if not task.schedule_enabled:
        return False
    start = task.schedule_effective_from or task.start_date or day
    end = task.end_date or start
    if day < start or day > end:
        return False
    recurrence = task.recurrence_type or 'once'
    if recurrence == 'once':
        return day == start
    if recurrence == 'daily':
        return True
    if recurrence == 'weekly':
        weekdays = task.recurrence_weekdays_json or [start.weekday()]
        return day.weekday() in {
            value for value in weekdays if type(value) is int and 0 <= value <= 6}
    return False


def materialize_task_occurrences(sup, now=None):
    """Create today's immutable occurrences once, without dispatching early."""
    if not sup:
        return []
    now = now or _now()
    day = now.date()
    created = []
    tasks = TestTask.query.filter_by(plan_id=sup.plan_id).order_by(TestTask.id).all()
    for task in tasks:
        if not _task_occurs_on(task, day) or not task.assignee_claw_id:
            continue
        occurrence = TestTaskOccurrence.query.filter_by(
            plan_id=sup.plan_id, test_task_id=task.id,
            occurrence_date=day).first()
        if occurrence:
            continue
        not_before = datetime.combine(
            day, _task_clock(task.not_before_time, '00:00'))
        due_at = (datetime.combine(day, _task_clock(task.due_time, '23:59'))
                  if task.due_time else datetime.combine(day, time(23, 59)))
        if due_at < not_before:
            due_at = datetime.combine(day, time(23, 59))
        occurrence = TestTaskOccurrence(
            plan_id=sup.plan_id, test_task_id=task.id,
            occurrence_date=day,
            timezone=task.schedule_timezone or 'Asia/Shanghai',
            not_before_at=not_before, due_at=due_at,
            status='scheduled', assignee_claw_id=task.assignee_claw_id,
            execution_mode=(
                'workflow' if task.workflow_definition_id else
                task.execution_mode or 'ordinary_agent_task'),
            workflow_definition_id=task.workflow_definition_id,
            workflow_start_vars_json=copy.deepcopy(
                task.workflow_start_vars_json or {}),
            allowed_fallback_claw_ids_json=list(
                task.allowed_fallback_claw_ids_json or []),
            required_capabilities_json=list(
                task.required_capabilities_json or []),
            required_resources_json=copy.deepcopy(
                task.required_resources_json or {}),
            reference_skill_ids_json=list(
                task.reference_skill_ids_json or []),
            reference_knowledge_ids_json=list(
                task.reference_knowledge_ids_json or []),
            reference_report_ids_json=list(
                task.reference_report_ids_json or []),
            next_action='wait_not_before', next_check_at=not_before,
            execution_goal_json={
                'objective': task.name,
                'description': task.description or '',
                'occurrence_date': day.isoformat(),
                'due_at': due_at.isoformat() + '+08:00',
                'closeout_policy': 'publish_completed_and_missing_scope',
            },
            checkpoint_json={
                'completed_scope': [], 'remaining_steps': [],
                'side_effect_receipts': [],
            },
            resume_contract_json={}, condition_state='',
            evidence_refs_json=[])
        db.session.add(occurrence)
        db.session.flush()
        add_event(sup.plan_id, 'task_occurrence_created', [
            'task-occurrence', task.id, day.isoformat(),
        ], {
            'test_task_id': task.id,
            'occurrence_id': occurrence.id,
            'occurrence_date': day.isoformat(),
            'not_before_at': not_before.isoformat() + '+08:00',
            'assignee_claw_id': task.assignee_claw_id,
        }, now)
        created.append(occurrence)
    return created


def migrate_schedule_templates(sup, body, now=None):
    """Idempotently activate existing tasks as dated schedule templates.

    This deliberately changes only scheduling metadata. Historical TestTask
    status, reports and already materialized occurrences remain immutable.
    """
    now = now or _now()
    templates = body.get('templates')
    if not isinstance(templates, list) or not templates:
        fail('PLAN_SCHEDULE_TEMPLATES_REQUIRED',
             'templates 必须为非空数组', 400)
    raw_effective = body.get('effective_from')
    try:
        effective_from = (date.fromisoformat(str(raw_effective))
                          if raw_effective else now.date() + timedelta(days=1))
    except (TypeError, ValueError):
        fail('PLAN_SCHEDULE_EFFECTIVE_DATE_INVALID',
             'effective_from 必须为 YYYY-MM-DD', 400)
    plan = db.session.get(TestPlan, sup.plan_id)
    if not plan or effective_from < now.date() or (
            plan.end_date and effective_from > plan.end_date):
        fail('PLAN_SCHEDULE_EFFECTIVE_DATE_INVALID',
             '生效日期不得早于今天，且必须位于计划周期内', 400)
    report_id = body.get('report_id')
    if report_id is not None:
        if isinstance(report_id, bool) or not isinstance(report_id, int):
            fail('PLAN_REPORT_INVALID', 'report_id 必须为正整数', 400)
        report = db.session.get(TestReport, report_id)
        if (not report or report.is_deleted
                or report.project_id != plan.project_id):
            fail('PLAN_REPORT_INVALID', '质量看板报告不存在或不属于当前项目', 400)

    seen = set()
    normalized = []
    for row in templates:
        if not isinstance(row, dict):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID', '模板项必须为对象', 400)
        task_id = row.get('test_task_id')
        if (isinstance(task_id, bool) or not isinstance(task_id, int)
                or task_id <= 0 or task_id in seen):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'test_task_id 必须为不重复的正整数', 400)
        seen.add(task_id)
        task = TestTask.query.filter_by(
            id=task_id, plan_id=sup.plan_id).with_for_update().first()
        if not task:
            fail('TEST_TASK_NOT_FOUND', '测试任务 #%s 不存在' % task_id, 404)
        recurrence = str(row.get('recurrence_type') or 'daily').strip()
        if recurrence not in ('daily', 'weekly'):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 '迁移模板仅支持 daily/weekly', 400)
        not_before = str(row.get('not_before_time') or '00:00')
        due_time = str(row.get('due_time') or '23:59')
        if (not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', not_before)
                or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', due_time)):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'not_before_time/due_time 必须为 HH:MM', 400)
        if _task_clock(due_time) < _task_clock(not_before):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'due_time 不得早于 not_before_time', 400)
        execution_role = str(row.get('execution_role') or 'member_work')
        if execution_role not in ('member_work', 'manager_work'):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'execution_role 仅支持 member_work/manager_work', 400)
        raw_definition_id = row.get('workflow_definition_id')
        workflow_definition_id = None
        workflow_start_vars = row.get('workflow_start_vars') or {}
        if raw_definition_id not in (None, ''):
            if isinstance(raw_definition_id, bool):
                fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                     'workflow_definition_id 必须为正整数', 400)
            try:
                workflow_definition_id = int(raw_definition_id)
            except (TypeError, ValueError):
                fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                     'workflow_definition_id 必须为正整数', 400)
            definition = db.session.get(
                WorkflowDefinition, workflow_definition_id)
            if (workflow_definition_id <= 0 or not definition
                    or definition.status != 'active'
                    or int(definition.project_id or 0) != int(plan.project_id or 0)):
                fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                     'workflow_definition_id 必须是同项目的有效 Flow', 400)
        if not isinstance(workflow_start_vars, dict):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'workflow_start_vars 必须为 JSON 对象', 400)
        if workflow_start_vars and not workflow_definition_id:
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'workflow_start_vars 仅可用于已绑定 Flow 的周期任务', 400)
        execution_mode = str(row.get(
            'execution_mode') or (
                'workflow' if workflow_definition_id
                else 'ordinary_agent_task')).strip().lower()
        if execution_mode not in ('ordinary_agent_task', 'workflow'):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'execution_mode 仅支持 ordinary_agent_task/workflow', 400)
        if ((workflow_definition_id is not None) !=
                (execution_mode == 'workflow')):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'workflow 模式与 workflow_definition_id 必须同时配置', 400)
        raw_fallbacks = row.get('allowed_fallback_claw_ids') or []
        if (not isinstance(raw_fallbacks, list)
                or any(isinstance(item, bool) for item in raw_fallbacks)):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'allowed_fallback_claw_ids 必须为正整数数组', 400)
        try:
            fallback_ids = sorted(set(int(item) for item in raw_fallbacks))
        except (TypeError, ValueError):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'allowed_fallback_claw_ids 必须为正整数数组', 400)
        if any(item <= 0 for item in fallback_ids):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'allowed_fallback_claw_ids 必须为正整数数组', 400)
        if fallback_ids:
            valid_fallbacks = {item.id for item in OpenClawInstance.query.filter(
                OpenClawInstance.id.in_(fallback_ids),
                OpenClawInstance.project_id == plan.project_id,
                OpenClawInstance.status != 'deleted').all()}
            if valid_fallbacks != set(fallback_ids):
                fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                     'allowed_fallback_claw_ids 必须属于当前项目且未删除', 400)
        required_capabilities = row.get('required_capabilities') or []
        if (not isinstance(required_capabilities, list)
                or len(required_capabilities) > 50
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.strip()) > 120
                       for item in required_capabilities)):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'required_capabilities 必须为非空字符串数组', 400)
        required_capabilities = sorted(set(
            item.strip() for item in required_capabilities))
        required_resources = row.get('required_resources') or {}
        if (not isinstance(required_resources, dict)
                or len(json.dumps(
                    required_resources, ensure_ascii=False)) > 16000):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'required_resources 必须为 JSON 对象', 400)
        weekdays = row.get('recurrence_weekdays') or []
        if recurrence == 'weekly' and (not isinstance(weekdays, list)
                or not weekdays or any(type(value) is not int or value < 0
                                       or value > 6 for value in weekdays)):
            fail('PLAN_SCHEDULE_TEMPLATE_INVALID',
                 'weekly 模板必须提供 0..6 的 recurrence_weekdays', 400)
        normalized.append((task, {
            'recurrence_type': recurrence,
            'recurrence_weekdays': sorted(set(weekdays)),
            'not_before_time': not_before,
            'due_time': due_time,
            'auto_dispatch': row.get('auto_dispatch') is not False,
            'execution_role': execution_role,
            'execution_mode': execution_mode,
            'workflow_definition_id': workflow_definition_id,
            'workflow_start_vars': workflow_start_vars,
            'allowed_fallback_claw_ids': fallback_ids,
            'required_capabilities': required_capabilities,
            'required_resources': required_resources,
        }))

    def apply():
        changed = []
        for task, config in normalized:
            before = {
                'schedule_enabled': bool(task.schedule_enabled),
                'recurrence_type': task.recurrence_type or 'once',
                'schedule_effective_from': str(task.schedule_effective_from)
                    if task.schedule_effective_from else None,
            }
            task.schedule_enabled = True
            task.recurrence_type = config['recurrence_type']
            task.recurrence_weekdays_json = config['recurrence_weekdays']
            task.schedule_timezone = 'Asia/Shanghai'
            task.schedule_effective_from = effective_from
            task.not_before_time = config['not_before_time']
            task.due_time = config['due_time']
            task.auto_dispatch = config['auto_dispatch']
            task.execution_role = config['execution_role']
            task.execution_mode = config['execution_mode']
            task.workflow_definition_id = config['workflow_definition_id']
            task.workflow_start_vars_json = config['workflow_start_vars']
            task.allowed_fallback_claw_ids_json = config[
                'allowed_fallback_claw_ids']
            task.required_capabilities_json = config[
                'required_capabilities']
            task.required_resources_json = config['required_resources']
            changed.append({
                'test_task_id': task.id, 'before': before,
                'after': {
                    **config, 'schedule_enabled': True,
                    'schedule_effective_from': effective_from.isoformat(),
                },
            })
        observations = dict(sup.observations_json or {})
        if report_id is not None:
            observations['supervision_report_id'] = report_id
        observations['schedule_migration'] = {
            'effective_from': effective_from.isoformat(),
            'task_ids': sorted(seen), 'at': now.isoformat() + '+08:00',
        }
        sup.observations_json = observations
        add_event(sup.plan_id, 'schedule_templates_migrated', [
            'schedule-templates-migrated', body.get('command_key'),
        ], {
            'effective_from': effective_from.isoformat(),
            'task_ids': sorted(seen), 'report_id': report_id,
        }, now)
        return {'effective_from': effective_from.isoformat(),
                'templates': changed, 'report_id': report_id}

    return receipt(sup, 'migrate_schedule_templates', body, apply)


def next_schedule_at(sup, now=None):
    """Compute the next Hub-owned daily tick independent of a model turn."""
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id) if sup else None
    if not plan:
        return None
    plan_end = ends_at(plan)
    policy = schedule_policy(sup)
    clocks = [policy['morning_check'], *policy['progress_summaries'],
              policy['day_close']]
    candidates = [_clock_at(now.date(), value) for value in clocks]
    candidates += [_clock_at(now.date() + timedelta(days=1), value)
                   for value in clocks]
    candidates = [value for value in candidates
                  if now < value < plan_end]
    return min(candidates) if candidates else None


def enqueue_schedule_ticks(sup, now=None):
    """Persist every due daily slot once; late watchdog runs still catch up."""
    now = now or _now()
    policy = schedule_policy(sup)
    slots = [('morning_version_check', policy['morning_check'])]
    slots += [('progress_summary_%s' % index, value)
              for index, value in enumerate(policy['progress_summaries'], 1)]
    slots.append(('day_close', policy['day_close']))
    plan = db.session.get(TestPlan, sup.plan_id) if sup else None
    plan_end = ends_at(plan) if plan else now
    for kind, value in slots:
        due_at = _clock_at(now.date(), value)
        if due_at <= now and due_at < plan_end:
            add_event(sup.plan_id, 'schedule_tick', [
                'schedule', sup.plan_id, now.date().isoformat(), kind,
            ], {
                'schedule_kind': kind,
                'scheduled_at': due_at.isoformat() + '+08:00',
                'timezone': 'Asia/Shanghai',
            }, now)
    scheduled = next_schedule_at(sup, now)
    if sup.next_check_at and sup.next_check_at >= plan_end:
        sup.next_check_at = None
    if scheduled and (not sup.next_check_at or sup.next_check_at > scheduled):
        sup.next_check_at = scheduled
    return scheduled


def _freshness(observed_at, now, threshold_seconds):
    if not observed_at:
        return {
            'status': 'unavailable', 'observed_at': None,
            'age_seconds': None, 'threshold_seconds': threshold_seconds,
            'usable_for_current_decision': False,
        }
    if isinstance(observed_at, str):
        try:
            observed_at = datetime.fromisoformat(observed_at)
        except ValueError:
            observed_at = None
    if not observed_at:
        return _freshness(None, now, threshold_seconds)
    if observed_at.tzinfo is not None:
        observed_at = observed_at.astimezone(
            timezone(timedelta(hours=8))).replace(tzinfo=None)
    age = max(0, int((now - observed_at).total_seconds()))
    status = 'fresh' if age <= threshold_seconds else 'stale'
    return {
        'status': status,
        'observed_at': observed_at.isoformat() + '+08:00',
        'age_seconds': age,
        'threshold_seconds': threshold_seconds,
        'usable_for_current_decision': status == 'fresh',
    }


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
    member_freshness = []
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
            member_freshness.append({
                'claw_id': claw_id,
                **_freshness(status.reported_at if status else None,
                             now, 120),
            })
    code_freshness = _freshness(
        recent_code.updated_at if recent_code else None, now, 86400)
    report_freshness = _freshness(
        recent_reports[0].updated_at if recent_reports else None,
        now, 86400)
    iteration_freshness = _freshness(
        active_iteration.updated_at if active_iteration else None,
        now, 7 * 86400)
    workflow_freshness = _freshness(
        max((row.updated_at for row in runs if row.updated_at), default=None),
        now, 900)
    freshness = {
        'code_change': code_freshness,
        'version_iteration': iteration_freshness,
        'reports': report_freshness,
        'workflow_runs': workflow_freshness,
        'team_members': member_freshness,
        'device_status': _freshness(None, now, 120),
    }
    stale_sources = [
        key for key, value in freshness.items()
        if key != 'team_members' and value['status'] != 'fresh']
    if any(row['status'] != 'fresh' for row in member_freshness):
        stale_sources.append('team_members')
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
        'freshness': freshness,
        'stale_sources': sorted(set(stale_sources)),
        'decision_guard': {
            'stale_facts_must_not_be_used_as_current': True,
            'refresh_required': bool(stale_sources),
            'prohibited_claims': (
                ['current_version_risk_is_known'] if code_freshness['status'] != 'fresh'
                else []),
        },
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
    is_manager = task.assignee_claw_id in (
        team.primary_manager_claw_id, team.backup_manager_claw_id)
    if task.execution_role == 'manager_work':
        if is_manager:
            return {
                'role_key': 'test_manager', 'specialty': 'analysis',
                'required_capabilities': list(
                    task.required_capabilities_json or []),
            }
        return None
    # Manager authority and member execution are independent roles.  A backup
    # manager explicitly listed as a project assistant/analyst/executor may
    # still receive member_work through that member role.  Merely being a
    # manager never grants member execution authority.
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
        return {
            'role_key': member.role_key, 'specialty': None,
            'required_capabilities': list(
                task.required_capabilities_json or []),
        }
    specialties = sorted(set(member.specialties_json or []))
    required = sorted(set(task.required_capabilities_json or []))
    if required and not set(required).issubset(set(specialties)):
        return None
    choices = []
    if task.task_type == 'performance':
        choices.append('client_performance')
    if re.search(r'android|ios|adb|手机|微信|包', text, re.I):
        choices.append('mobile_package')
    if task.task_type == 'automation' or re.search(r'unity|编辑器|flow\s*#?12', text, re.I):
        choices.append('editor')
    inferred = sorted(set(
        item for item in choices if item in specialties))
    effective_required = required or inferred
    specialty = next(
        (item for item in effective_required if item in specialties), None)
    specialty = specialty or (
        specialties[0] if specialties else None)
    return {
        'role_key': 'test_executor', 'specialty': specialty,
        'required_capabilities': effective_required,
    }


_TASK_DEPENDENCY_SUCCESS = frozenset({'completed', 'skipped'})


def task_dependency_state(task):
    """Return deterministic same-plan dependency truth for one TestTask."""
    raw_ids = list(task.depends_on_task_ids_json or []) if task else []
    dependency_ids = sorted({int(item) for item in raw_ids
                             if type(item) is int and item > 0})
    if not dependency_ids:
        return {
            'depends_on_task_ids': [], 'satisfied_task_ids': [],
            'pending_task_ids': [], 'missing_task_ids': [], 'ready': True,
        }
    rows = TestTask.query.filter(
        TestTask.plan_id == task.plan_id,
        TestTask.id.in_(dependency_ids)).all()
    by_id = {row.id: row for row in rows}
    missing = [item for item in dependency_ids if item not in by_id]
    satisfied = [item for item in dependency_ids
                 if item in by_id and by_id[item].status in _TASK_DEPENDENCY_SUCCESS]
    pending = [item for item in dependency_ids
               if item in by_id and by_id[item].status not in _TASK_DEPENDENCY_SUCCESS]
    return {
        'depends_on_task_ids': dependency_ids,
        'satisfied_task_ids': satisfied,
        'pending_task_ids': pending,
        'missing_task_ids': missing,
        'ready': not missing and not pending,
    }


def _apply_task_dependency_state(sup, task, stage, occurrence=None, now=None):
    """Project dependency truth onto a not-yet-dispatched Mission Stage."""
    now = now or _now()
    dependency = task_dependency_state(task)
    snapshot = dict(stage.input_snapshot_json or {})
    before_dependency = snapshot.get('dependencies')
    snapshot['depends_on_task_ids'] = dependency['depends_on_task_ids']
    snapshot['dependencies'] = dependency
    if before_dependency != dependency:
        stage.input_snapshot_json = snapshot
    if not dependency['depends_on_task_ids']:
        return True, False
    mutable_states = {'ready', 'scheduled', 'waiting_dependency'}
    if stage.state not in mutable_states:
        return dependency['ready'], before_dependency != dependency
    changed = before_dependency != dependency
    if not dependency['ready']:
        if stage.state != 'waiting_dependency':
            stage.state = 'waiting_dependency'
            stage.last_reason_code = 'task_dependency_waiting'
            changed = True
            add_event(sup.plan_id, 'task_dependency_waiting', [
                'task-dependency-waiting', stage.id,
                digest(dependency),
            ], {
                'task_id': task.id,
                'stage_key': stage.stage_key,
                **dependency,
            }, now)
        if occurrence:
            occurrence.status = 'ready'
            occurrence.next_action = 'wait_dependencies'
            occurrence.next_check_at = None
        elif task.status in ('assigned', 'pending', 'blocked'):
            task.status = 'pending'
            task.condition_state = 'waiting_dependency'
            task.recommended_action = 'wait_dependencies'
            task.next_action = 'wait_dependencies'
            task.next_check_at = None
        if changed:
            stage.version = int(stage.version or 1) + 1
        return False, changed
    if stage.state == 'waiting_dependency':
        next_state = (
            'scheduled' if occurrence and occurrence.not_before_at > now
            else 'ready')
        stage.state = next_state
        stage.last_reason_code = 'task_dependency_satisfied'
        stage.version = int(stage.version or 1) + 1
        changed = True
        if occurrence:
            occurrence.status = next_state
            occurrence.next_action = (
                'auto_dispatch' if next_state == 'ready' and task.auto_dispatch
                else 'manager_dispatch' if next_state == 'ready'
                else 'wait_not_before')
            occurrence.next_check_at = (
                now if next_state == 'ready' else occurrence.not_before_at)
        elif task.condition_state == 'waiting_dependency':
            task.status = 'pending'
            task.condition_state = ''
            task.recommended_action = 'dispatch'
            task.next_action = 'manager_dispatch'
            task.next_check_at = now
        add_event(sup.plan_id, 'task_dependency_ready', [
            'task-dependency-ready', stage.id,
            digest(dependency),
        ], {
            'task_id': task.id,
            'stage_key': stage.stage_key,
            **dependency,
        }, now)
    elif changed:
        stage.version = int(stage.version or 1) + 1
    return True, changed


def refresh_task_dependency_stages(sup, now=None):
    """Refresh dependency gates and release newly satisfied stages."""
    if not sup or not sup.mission_id:
        return 0
    now = now or _now()
    changed = 0
    rows = MissionStage.query.filter_by(mission_id=sup.mission_id).all()
    for stage in rows:
        task_id = _stage_task_id(stage)
        task = db.session.get(TestTask, task_id) if task_id else None
        if not task:
            continue
        occurrence_id = _stage_occurrence_id(stage)
        occurrence = (db.session.get(TestTaskOccurrence, occurrence_id)
                      if occurrence_id else None)
        _, updated = _apply_task_dependency_state(
            sup, task, stage, occurrence=occurrence, now=now)
        changed += int(updated)
    return changed


def _stage_snapshot(team, task, assignment, occurrence=None):
    definition_id = (occurrence.workflow_definition_id if occurrence
                     else task.workflow_definition_id)
    start_vars = ((occurrence.workflow_start_vars_json if occurrence
                  else task.workflow_start_vars_json) or {})
    fallback_ids = ((occurrence.allowed_fallback_claw_ids_json if occurrence
                    else task.allowed_fallback_claw_ids_json) or [])
    required_capabilities = (
        (occurrence.required_capabilities_json if occurrence
         else task.required_capabilities_json)
        or assignment.get('required_capabilities')
        or ([assignment['specialty']] if assignment.get('specialty') else []))
    required_resources = ((occurrence.required_resources_json if occurrence
                          else task.required_resources_json) or {})
    execution_mode = (
        occurrence.execution_mode if occurrence else
        'workflow' if definition_id else
        task.execution_mode or 'ordinary_agent_task')
    snapshot = {
        'test_plan_id': task.plan_id,
        'test_task_id': task.id,
        'test_task_name': task.name,
        'test_task_description': task.description or '',
        'test_task_type': task.task_type,
        'test_task_priority': task.priority,
        'execution_mode': execution_mode,
        'workflow_definition_id': definition_id,
        'workflow_start_vars': copy.deepcopy(start_vars),
        'allowed_fallback_claw_ids': list(fallback_ids),
        'required_capabilities': list(required_capabilities),
        'required_resources': copy.deepcopy(required_resources),
        'references': serialize_task_references(task, occurrence),
        'depends_on_task_ids': list(task.depends_on_task_ids_json or []),
        'scheduled_start_date': str(task.start_date) if task.start_date else None,
        'scheduled_end_date': str(task.end_date) if task.end_date else None,
        'team_assignment': {
            'team_id': team.id,
            'team_version': team.version,
            'role_key': assignment['role_key'],
            'specialty': assignment['specialty'],
            'assigned_claw_id': (
                occurrence.assignee_claw_id if occurrence
                else task.assignee_claw_id),
        },
    }
    if occurrence:
        snapshot.update({
            'test_task_occurrence_id': occurrence.id,
            'occurrence_date': str(occurrence.occurrence_date),
            'not_before_at': occurrence.not_before_at.isoformat() + '+08:00',
            'due_at': (occurrence.due_at.isoformat() + '+08:00'
                       if occurrence.due_at else None),
            'recurrence_type': task.recurrence_type or 'once',
            'execution_role': task.execution_role or 'member_work',
        })
    return snapshot


def sync_plan_stages(sup, now=None):
    """Append immutable stages for newly assigned non-terminal TestTasks."""
    if not sup or not sup.mission_id or not sup.team_id:
        return 0
    now = now or _now()
    if mission_expiry_state(sup, now):
        return 0
    team = db.session.get(AgentTeam, sup.team_id)
    mission = db.session.get(WorkflowMission, sup.mission_id)
    binding = db.session.get(AgentTeamMission, sup.mission_id)
    if not team or not mission or not binding:
        fail('PLAN_MISSION_INCOMPLETE', '计划 Mission 缺少团队绑定', 409)
    materialize_task_occurrences(sup, now)
    existing = {}
    existing_occurrences = {}
    for stage in MissionStage.query.filter_by(mission_id=mission.id).all():
        snapshot = stage.input_snapshot_json or {}
        task_id = snapshot.get('test_task_id')
        occurrence_id = snapshot.get('test_task_occurrence_id')
        if type(occurrence_id) is int:
            existing_occurrences[occurrence_id] = stage
        if type(task_id) is int:
            existing[task_id] = stage
    created = 0
    for task in TestTask.query.filter_by(plan_id=sup.plan_id).order_by(TestTask.id).all():
        if task.schedule_enabled:
            continue
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
        stage = MissionStage(
            mission_id=mission.id, stage_key='test_task_%s' % task.id,
            stage_version=1, role_key=assignment['role_key'],
            assigned_claw_id=task.assignee_claw_id, state='ready',
            input_snapshot_json=snapshot, evidence_refs_json=[])
        db.session.add(stage)
        add_event(sup.plan_id, 'task_stage_created',
                  ['task-stage', task.id], {
                      'task_id': task.id,
                      'stage_key': 'test_task_%s' % task.id,
                      'assigned_claw_id': task.assignee_claw_id,
                  })
        created += 1
    occurrences = (TestTaskOccurrence.query.filter_by(plan_id=sup.plan_id)
                   .order_by(TestTaskOccurrence.occurrence_date,
                             TestTaskOccurrence.id).all())
    for occurrence in occurrences:
        if occurrence.id in existing_occurrences:
            occurrence.mission_stage_id = existing_occurrences[occurrence.id].id
            continue
        task = db.session.get(TestTask, occurrence.test_task_id)
        if not task:
            continue
        assignment = _task_assignment(team, task)
        if not assignment:
            reason = ('manager_work_required'
                      if task.assignee_claw_id in (
                          team.primary_manager_claw_id,
                          team.backup_manager_claw_id)
                      else 'missing_or_invalid_team_member')
            add_event(sup.plan_id, 'task_assignment_gap', [
                'task-occurrence-assignment-gap', occurrence.id,
                task.assignee_claw_id,
            ], {
                'task_id': task.id, 'occurrence_id': occurrence.id,
                'assignee_claw_id': task.assignee_claw_id, 'reason': reason,
            })
            continue
        if not occurrence.required_capabilities_json:
            occurrence.required_capabilities_json = list(
                assignment.get('required_capabilities') or [])
        snapshot = _stage_snapshot(team, task, assignment, occurrence)
        stage = MissionStage(
            mission_id=mission.id,
            stage_key='test_task_%s_occ_%s' % (
                task.id, occurrence.occurrence_date.strftime('%Y%m%d')),
            stage_version=1, role_key=assignment['role_key'],
            assigned_claw_id=occurrence.assignee_claw_id,
            state=('ready' if occurrence.not_before_at <= now else 'scheduled'),
            input_snapshot_json=snapshot, evidence_refs_json=[])
        db.session.add(stage)
        db.session.flush()
        occurrence.mission_stage_id = stage.id
        if stage.state == 'ready':
            occurrence.status = 'ready'
            occurrence.next_action = (
                'auto_dispatch' if task.auto_dispatch else 'manager_dispatch')
            occurrence.next_check_at = now
        add_event(sup.plan_id, 'task_stage_created', [
            'task-occurrence-stage', occurrence.id,
        ], {
            'task_id': task.id, 'occurrence_id': occurrence.id,
            'stage_key': stage.stage_key,
            'state': stage.state,
            'assigned_claw_id': occurrence.assignee_claw_id,
        })
        created += 1
    db.session.flush()
    dependency_changes = refresh_task_dependency_stages(sup, now=now)
    if created or dependency_changes:
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
        context['test_task_ids'] = sorted({
            (stage.input_snapshot_json or {}).get('test_task_id')
            for stage in stages
            if type((stage.input_snapshot_json or {}).get('test_task_id')) is int})
        context['stage_count'] = len(stages)
        mission.context_json = context
    return created


def _reconcile_team_mission(sup, mission, team, plan, now=None):
    """Keep a durable Mission aligned with the active Plan and team roster."""
    now = now or _now()
    changed = []
    expected = {
        'main_claw_id': sup.orchestrator_claw_id,
        'status': ('active' if (plan.status == 'active'
                                and now < ends_at(plan)
                                and mission.status in ('active', 'expired'))
                   else mission.status),
        'expires_at': ends_at(plan),
        'allowed_definition_ids_json': sorted(set(
            (team.policy_json or {}).get('allowed_definition_ids') or [])),
        'allowed_worker_claw_ids_json': sorted({
            row.claw_id for row in team.members}),
    }
    for field, value in expected.items():
        if getattr(mission, field) != value:
            setattr(mission, field, value)
            changed.append(field)
    context = dict(mission.context_json or {})
    desired_context = {
        'plan_supervision_id': plan.id,
        'test_plan_id': plan.id,
        'team_id': team.id,
        'manager_claw_id': sup.orchestrator_claw_id,
    }
    if any(context.get(key) != value
           for key, value in desired_context.items()):
        context.update(desired_context)
        mission.context_json = context
        changed.append('context_json')

    binding = db.session.get(AgentTeamMission, mission.id)
    if not binding:
        fail('PLAN_MISSION_INCOMPLETE', '团队 Mission 缺少绑定快照', 409)
    desired_snapshot = {
        'primary_manager_claw_id': team.primary_manager_claw_id,
        'backup_manager_claw_id': team.backup_manager_claw_id,
        'policy': copy.deepcopy(team.policy_json or {}),
        'members': [row.to_dict() for row in team.members],
        'manager_epoch_at_creation': team.manager_epoch,
        'reconciled_at': now.isoformat() + '+08:00',
    }
    stable_snapshot = dict(binding.snapshot_json or {})
    stable_snapshot.pop('reconciled_at', None)
    desired_stable = dict(desired_snapshot)
    desired_stable.pop('reconciled_at', None)
    if (binding.team_version != team.version
            or stable_snapshot != desired_stable):
        binding.team_version = team.version
        binding.snapshot_json = desired_snapshot
        changed.append('team_binding')
    if changed:
        mission.version = int(mission.version or 1) + 1
        add_event(sup.plan_id, 'mission_reconciled', [
            'mission-reconciled', mission.id, mission.version,
        ], {
            'mission_id': mission.id,
            'changed_fields': changed,
            'main_claw_id': mission.main_claw_id,
            'expires_at': mission.expires_at.isoformat() + '+08:00',
        }, now)
    return changed


def ensure_team_mission(sup, now=None):
    """Create/recover the one durable Mission owned by a team Plan."""
    now = now or _now()
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
        binding = db.session.get(AgentTeamMission, mission.id)
        if (mission.project_id != plan.project_id or not binding
                or binding.team_id != team.id):
            fail('PLAN_MISSION_CONFLICT', '监督 Mission 与计划团队不兼容', 409)
        _reconcile_team_mission(sup, mission, team, plan, now)
        sync_plan_stages(sup, now=now)
        return mission
    mission_key = 'plan-supervisor-%s' % plan.id
    mission = WorkflowMission.query.filter_by(mission_key=mission_key).first()
    if mission:
        binding = db.session.get(AgentTeamMission, mission.id)
        if (mission.project_id != plan.project_id
                or not binding or binding.team_id != team.id):
            fail('PLAN_MISSION_CONFLICT', '稳定 Mission key 已被不兼容记录占用', 409)
    else:
        # A manager may also hold an explicit member role.  Only the member
        # roster grants execution authority; do not erase that authority just
        # because the same Claw is configured as backup manager.
        allowed_workers = sorted({row.claw_id for row in team.members})
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
            allow_external_mutations=False,
            context_json={
                'plan_supervision_id': plan.id,
                'test_plan_id': plan.id,
                'team_id': team.id,
                'manager_claw_id': sup.orchestrator_claw_id,
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
    _reconcile_team_mission(sup, mission, team, plan, now)
    db.session.flush()
    sync_plan_stages(sup, now=now)
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
        manager_goal_snapshot(sup, persist=True, now=now)
        manager = ensure_manager_tenure(sup, now)
        mission = ensure_team_mission(sup, now=now) if team_id else None
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
    mission = (db.session.get(WorkflowMission, sup.mission_id)
               if sup and sup.mission_id else None)
    mission_available = bool(
        not sup.mission_id
        or (mission and mission.effective_status(now) == 'active'))
    return bool(plan and plan.status == 'active' and plan.project_id
                and team_binding_valid(sup.team_id, plan.project_id, sup.orchestrator_claw_id)
                and claw and claw.status != 'deleted' and claw.project_id == plan.project_id
                and mission_available
                and sup.status not in ('stopped', 'expired', 'blocked_owner_gate')
                and datetime.combine(plan.start_date, time.min) <= now < ends_at(plan))


def mission_expiry_state(sup, now=None):
    """Return the authoritative Mission/Plan expiry boundary, if crossed."""
    if not sup:
        return None
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    mission = (db.session.get(WorkflowMission, sup.mission_id)
               if sup.mission_id else None)
    plan_end = ends_at(plan) if plan else None
    mission_end = mission.expires_at if mission else None
    reason = None
    boundary = None
    if mission and mission.status == 'expired':
        reason = 'mission_status_expired'
        boundary = mission_end or plan_end or now
    elif mission and mission.effective_status(now) == 'expired':
        reason = 'mission_expired'
        boundary = mission_end
    elif plan_end and now >= plan_end:
        reason = 'plan_expired'
        boundary = plan_end
    if not reason:
        return None
    return {
        'reason': reason,
        'boundary_at': boundary,
        'plan_ends_at': plan_end,
        'mission_expires_at': mission_end,
        'mission': mission,
    }


def require_mission_not_expired(sup, now=None):
    """Fence every explicit manager dispatch/recovery path after expiry."""
    now = now or _now()
    state = mission_expiry_state(sup, now)
    if state:
        fail('PLAN_MISSION_EXPIRED',
             '计划 Mission 已到期并进入收口，禁止继续派发或创建恢复阶段', 409)
    mission = (db.session.get(WorkflowMission, sup.mission_id)
               if sup and sup.mission_id else None)
    if mission and mission.effective_status(now) != 'active':
        fail('PLAN_MISSION_INACTIVE', '计划 Mission 当前不可调度', 409)
    return mission


def valid_plan_owner_gate(sup):
    """Return whether the stored decision is a fully evidenced global gate."""
    decision = (sup.last_decision_json or {}) if sup else {}
    evidence = decision.get('plan_block_evidence')
    summary = str(decision.get('summary') or '')
    return bool(
        decision.get('requested_outcome', decision.get('outcome')) == 'blocked'
        and decision.get('block_scope') == 'plan'
        and decision.get('plan_block_reason_code') in PLAN_BLOCK_REASON_CODES
        and isinstance(evidence, list) and evidence
        and all(isinstance(item, dict) and item for item in evidence)
        and not PLAN_SCOPE_CONFLICT_RE.search(summary)
    )


def manager_auto_resume_allowed(sup, now=None):
    """A durable test manager may recover invalid/local legacy owner gates."""
    return bool(
        sup and sup.status in ('blocked', 'blocked_owner_gate')
        and (manager_lease_payload(sup, now) or {}).get('active')
        and not valid_plan_owner_gate(sup)
    )


def repair_recoverable_state(sup, now=None):
    """Repair legacy/local blocks without crossing a validated global gate."""
    now = now or _now()
    if sup.status in ('blocked', 'blocked_owner_gate'):
        if valid_plan_owner_gate(sup):
            sup.status = 'blocked_owner_gate'
            return False
        if not manager_auto_resume_allowed(sup, now):
            return False
        previous = sup.status
        cancel_wake(sup)
        sup.status = 'retryable'
        sup.next_check_at = min(sup.next_check_at or now, now)
        sup.resume_condition = 'timer_or_event'
        sup.lease_owner = None
        sup.lease_expires_at = None
        sup.turn_deadline_at = None
        add_event(sup.plan_id, 'supervisor_auto_repaired', [
            'manager-auto-repair', sup.plan_id, sup.fencing_token,
            (sup.last_decision_json or {}).get('at'),
        ], {
            'from_status': previous,
            'to_status': 'retryable',
            'authority': 'team_test_manager',
            'reason': 'invalid_or_local_owner_gate',
        }, now)
        return True
    return False


def add_event(plan_id, kind, key, payload=None, now=None):
    """Same transaction as the source state; unique outbox key survives retries."""
    table = PlanSupervisorEvent.__table__
    statement = table.insert().values(plan_id=plan_id, event_key=digest(key), kind=kind,
                                     payload_json=payload or {}, created_at=now or _now())
    try:
        dialect = db.session.get_bind().dialect.name
    except TypeError:
        # Flask-SQLAlchemy 2.x delegates a legacy ``bind`` keyword to older
        # SQLAlchemy sessions. Production still carries that combination.
        dialect = db.engine.dialect.name
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


def _mission_plan(mission, sup=None):
    """Resolve a Mission's owning Plan without trusting a single legacy link."""
    if sup:
        return db.session.get(TestPlan, sup.plan_id)
    context = mission.context_json if mission and isinstance(
        mission.context_json, dict) else {}
    for key in ('test_plan_id', 'plan_id'):
        try:
            plan_id = int(context.get(key) or 0)
        except (TypeError, ValueError):
            plan_id = 0
        plan = db.session.get(TestPlan, plan_id) if plan_id else None
        if plan and (not plan.project_id or plan.project_id == mission.project_id):
            return plan
    if not mission:
        return None
    for stage in MissionStage.query.filter_by(
            mission_id=mission.id).order_by(MissionStage.id).all():
        task_id = _stage_task_id(stage)
        task = db.session.get(TestTask, task_id) if task_id else None
        if task and task.plan_id:
            return db.session.get(TestPlan, task.plan_id)
    return None


def _mission_agent_task_payload(task):
    try:
        value = json.loads(task.payload or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _cancel_expired_mission_run(run, now):
    """Terminalize unfinished Run state while preserving all prior evidence."""
    if not run or run.status not in RUN_ACTIVE_STATUSES:
        return False
    run.status = 'cancelled'
    run.current_step_id = ''
    run.finished_at = now
    run.summary = run.summary or 'Mission 已到期，Hub 已终止未完成执行'
    blocker = dict(run.blocker_json or {})
    blocker.update({
        'code': 'MISSION_EXPIRED',
        'reason': 'mission_lifecycle_closeout',
        'closed_at': now.isoformat() + '+08:00',
    })
    run.blocker_json = blocker
    if not run.automation_conclusion:
        run.automation_conclusion = 'CANCELLED_BY_MISSION_EXPIRY'
    for step in WorkflowRunStep.query.filter_by(
            run_id=run.id).with_for_update().all():
        if step.status not in RUN_ACTIVE_STATUSES:
            continue
        step.status = 'skipped'
        step.summary = step.summary or 'Mission 已到期，未完成步骤已跳过'
        step.finished_at = step.finished_at or now
        step.updated_by = 'mission-lifecycle-watchdog'
        step.health_status = 'idle'
        step.health_checked_at = now
        step.progress_at = now
        step.progress_by = 'mission-lifecycle-watchdog'
        step.progress_phase = 'cancelled'
        step.progress_message = step.summary
        step.claimed_by = ''
        step.claimed_at = None
        step.claimed_claw_id = None
        step.claim_expires_at = None
        step.claim_lease_seconds = None
        step.claim_fencing_token = int(step.claim_fencing_token or 0) + 1
    for approval in WorkflowApproval.query.filter_by(
            run_id=run.id, status='pending').with_for_update().all():
        approval.status = 'skipped'
        approval.approver = 'mission-lifecycle-watchdog'
        approval.comment = approval.comment or 'Mission 已到期，审批自动关闭'
        approval.approved_at = approval.approved_at or now
    return True


def close_expired_mission_lifecycle(mission, sup=None, reason=None,
                                    boundary=None, now=None):
    """Atomically close an expired Mission, its Plan and unfinished work.

    This is Mission-scoped rather than Supervisor-scoped: legacy Missions with
    an already-expired Supervisor, or with no Supervisor at all, are fenced by
    the same deterministic watchdog. Historical Runs and evidence are retained.
    """
    now = now or _now()
    plan = _mission_plan(mission, sup)
    boundary = boundary or (mission.expires_at if mission else None) or (
        ends_at(plan) if plan else now)
    reason = reason or 'mission_expired'
    mission_context = dict(mission.context_json or {}) if mission else {}
    previous_closeout = mission_context.get('lifecycle_closeout')
    if (isinstance(previous_closeout, dict)
            and previous_closeout.get('contract') == MISSION_CLOSEOUT_CONTRACT):
        return dict(previous_closeout, replayed=True)

    plan_id = plan.id if plan else (sup.plan_id if sup else None)
    command_key = 'mission-lifecycle:%s:%s' % (
        mission.id if mission else 'none', boundary.isoformat())
    old = (PlanSupervisorReceipt.query.filter_by(
        plan_id=plan_id, action='mission_lifecycle_closeout',
        command_key=command_key).first() if plan_id else None)
    if old:
        return dict(old.response_json, replayed=True)

    previous_plan_status = plan.status if plan else None
    previous_supervisor_status = sup.status if sup else None
    previous_mission_status = mission.status if mission else None
    cancelled_stage_ids = []
    preserved_stage_ids = []
    cleared_occurrence_ids = []
    cleared_task_ids = set()
    stages = (MissionStage.query.filter_by(mission_id=mission.id)
              .order_by(MissionStage.id).with_for_update().all()
              if mission else [])
    for stage in stages:
        task_id = _stage_task_id(stage)
        if task_id:
            cleared_task_ids.add(task_id)
        if stage.state in MISSION_TERMINAL_STAGE_STATES:
            preserved_stage_ids.append(stage.id)
            continue
        stage.state = 'cancelled'
        stage.last_reason_code = 'mission_expired'
        stage.fencing_token = int(stage.fencing_token or 0) + 1
        stage.version = int(stage.version or 1) + 1
        cancelled_stage_ids.append(stage.id)

    stage_ids = [stage.id for stage in stages]
    if stage_ids:
        occurrences = TestTaskOccurrence.query.filter(
            TestTaskOccurrence.mission_stage_id.in_(stage_ids)
        ).with_for_update().all()
        for occurrence in occurrences:
            if occurrence.status not in OCCURRENCE_TERMINAL:
                occurrence.status = 'cancelled'
            occurrence.next_action = 'mission_expired'
            occurrence.recommended_action = 'plan_closed'
            occurrence.next_check_at = None
            occurrence.next_probe_at = None
            occurrence.condition_state = 'mission_expired'
            occurrence.resume_fencing_token = int(
                occurrence.resume_fencing_token or 0) + 1
            cleared_occurrence_ids.append(occurrence.id)
        for task in TestTask.query.filter(
                TestTask.id.in_(sorted(cleared_task_ids))).all():
            task.next_check_at = None
            task.next_probe_at = None
            task.next_action = 'mission_expired'
            task.recommended_action = 'plan_closed'
            task.condition_state = 'mission_expired'

    dispatches = (WorkflowMissionDispatch.query.filter_by(
        mission_id=mission.id).order_by(WorkflowMissionDispatch.id)
        .with_for_update().all() if mission else [])
    run_ids = sorted({row.workflow_run_id for row in dispatches})
    cancelled_run_ids = []
    for run in (WorkflowRun.query.filter(WorkflowRun.id.in_(run_ids))
                .with_for_update().all() if run_ids else []):
        if _cancel_expired_mission_run(run, now):
            cancelled_run_ids.append(run.id)
    for dispatch in dispatches:
        if dispatch.workflow_run_id in cancelled_run_ids:
            dispatch.status = 'cancelled'

    cancelled_agent_task_ids = []
    active_agent_tasks = AgentTask.query.filter(
        AgentTask.status.in_(('pending', 'running', 'waiting_condition'))
    ).with_for_update().all()
    run_id_set = set(run_ids)
    for task in active_agent_tasks:
        payload = _mission_agent_task_payload(task)
        try:
            payload_mission_id = int(payload.get('mission_id') or 0)
        except (TypeError, ValueError):
            payload_mission_id = 0
        try:
            payload_run_id = int(payload.get('run_id') or 0)
        except (TypeError, ValueError):
            payload_run_id = 0
        if not ((mission and payload_mission_id == mission.id)
                or payload_run_id in run_id_set):
            continue
        task.status = 'cancelled'
        task.error = 'Mission expired; unfinished AgentTask cancelled by Hub'
        task.terminal_reason = 'mission_expired'
        task.completed_at = now
        task.claim_token = None
        task.lease_expires_at = None
        task.fencing_token = int(task.fencing_token or 0) + 1
        task.version = int(task.version or 0) + 1
        cancelled_agent_task_ids.append(task.id)

    if mission and mission.status == 'active':
        mission.status = 'expired'
        mission.version = int(mission.version or 1) + 1
    if plan and plan.status == 'active':
        plan.status = 'completed'
    if sup:
        stop(sup, 'expired')
        sup.resume_condition = 'mission_expired'

    request_payload = {
        'contract': MISSION_CLOSEOUT_CONTRACT,
        'plan_id': plan_id,
        'mission_id': mission.id if mission else None,
        'reason': reason,
        'boundary_at': boundary.isoformat(),
    }
    record = None
    if plan_id:
        record = PlanSupervisorReceipt(
            plan_id=plan_id, action='mission_lifecycle_closeout',
            command_key=command_key, request_hash=digest(request_payload),
            response_json={}, created_at=now)
        db.session.add(record)
        db.session.flush()
    result = {
        'contract': MISSION_CLOSEOUT_CONTRACT,
        'receipt_id': record.id if record else None,
        'replayed': False,
        'plan_id': plan_id,
        'mission_id': mission.id if mission else None,
        'reason': reason,
        'closed_at': now.isoformat() + '+08:00',
        'boundary_at': boundary.isoformat() + '+08:00',
        'previous_plan_status': previous_plan_status,
        'plan_status': plan.status if plan else None,
        'previous_supervisor_status': previous_supervisor_status,
        'supervisor_status': sup.status if sup else None,
        'previous_mission_status': previous_mission_status,
        'mission_status': mission.status if mission else None,
        'next_check_at_cleared': not sup or sup.next_check_at is None,
        'cancelled_stage_ids': cancelled_stage_ids,
        'preserved_terminal_stage_ids': preserved_stage_ids,
        'cancelled_workflow_run_ids': sorted(cancelled_run_ids),
        'cancelled_agent_task_ids': sorted(cancelled_agent_task_ids),
        'cleared_occurrence_ids': cleared_occurrence_ids,
        'cleared_test_task_ids': sorted(cleared_task_ids),
    }
    if record:
        record.response_json = result
    if mission:
        mission_context['lifecycle_closeout'] = dict(result)
        mission.context_json = mission_context
    if sup:
        observations = dict(sup.observations_json or {})
        observations['mission_closeout'] = dict(result)
        sup.observations_json = observations
    if plan_id:
        add_event(plan_id, 'mission_supervision_closed', [
            'mission-lifecycle-closed', mission.id if mission else None,
            boundary.isoformat(),
        ], {
            'receipt_id': record.id if record else None,
            'contract': MISSION_CLOSEOUT_CONTRACT,
            'reason': reason,
            'cancelled_stage_count': len(cancelled_stage_ids),
            'cancelled_workflow_run_count': len(cancelled_run_ids),
            'cancelled_agent_task_count': len(cancelled_agent_task_ids),
        }, now)
    db.session.add(AuditLog(
        action='mission_lifecycle_closeout',
        resource_type='workflow_mission',
        resource_id=mission.id if mission else None,
        resource_name=mission.mission_key if mission else 'plan:%s' % plan_id,
        operator='mission-lifecycle-watchdog',
        detail=json.dumps(result, ensure_ascii=False, sort_keys=True),
    ))
    return result


def close_expired_mission_supervision(sup, now=None):
    """Compatibility entry point for one supervised Mission/Plan."""
    now = now or _now()
    state = mission_expiry_state(sup, now)
    if not state:
        return None
    return close_expired_mission_lifecycle(
        state['mission'], sup=sup, reason=state['reason'],
        boundary=state['boundary_at'], now=now)


def close_all_expired_missions(now=None):
    """Close expired Missions even when their Supervisor is absent/terminal."""
    now = now or _now()
    mission_ids = [row.id for row in WorkflowMission.query.filter(
        WorkflowMission.status.in_(('active', 'expired')),
        WorkflowMission.expires_at <= now,
    ).order_by(WorkflowMission.id).limit(200).all()]
    results = []
    for mission_id in mission_ids:
        mission = (WorkflowMission.query.filter_by(id=mission_id)
                   .with_for_update().first())
        if not mission:
            continue
        context = mission.context_json if isinstance(
            mission.context_json, dict) else {}
        existing = context.get('lifecycle_closeout')
        if (isinstance(existing, dict)
                and existing.get('contract') == MISSION_CLOSEOUT_CONTRACT):
            continue
        sup = (PlanSupervisor.query.filter_by(mission_id=mission.id)
               .with_for_update().first())
        results.append(close_expired_mission_lifecycle(
            mission, sup=sup, reason='mission_expired',
            boundary=mission.expires_at, now=now))
        db.session.commit()
    return results


def ingest(sup):
    # Query unsequenced rows, NOT id > cursor: a lower ID may commit later.
    rows = PlanSupervisorEvent.query.filter_by(plan_id=sup.plan_id, sequence=None).order_by(
        PlanSupervisorEvent.id).limit(200).all()
    for row in rows:
        sup.cursor += 1
        row.sequence = sup.cursor
        if row.kind in ('task_changed', 'task_blocked', 'run_changed', 'step_progress', 'run_created'):
            sup.last_progress_at = max(sup.last_progress_at or row.created_at, row.created_at)
    return rows


def pump(sup, now=None):
    """Create at most one durable wake. SSE is only an optimization after commit."""
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    if close_expired_mission_supervision(sup, now):
        return None
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
        PlanSupervisorEvent.kind.in_(['task_blocked', 'run_terminal', 'heartbeat_anomaly', 'stale',
                                      'supervisor_lease_expired', 'plan_started',
                                      'occurrence_owner_gate',
                                      'occurrence_recovery_failed',
                                      'occurrence_due_unresolved',
                                      'task_dependency_ready',
                                      'supervision_report_updated',
                                      'control_action_terminal',
                                      'run_recovery_required',
                                      'run_reconciliation_resolved',
                                      'task_agent_terminal'])).first()
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
                           'block_scope=plan，并提供白名单 plan_block_reason_code 与结构化 '
                           'plan_block_evidence；摘要若声明局部阻断或其他任务可继续，Hub 将拒绝。'
                           '无需 Flow 的独立测试任务由你决策后调用 '
                           '/api/v1/test-plans/{plan_id}/supervision/agent-tasks，'
                           '提交 test_task_id、稳定 command_key 和可选 instruction；'
                           'Hub 将创建可租约、可回写的普通 AgentTask。禁止用 Todo 代替正式派工。'
                           '终态普通任务需要重试时，使用态势 actionable_gaps 提供的 '
                           'new-attempt 端点；ready 阶段需要换人时，使用 available_actions '
                           '提供的 reassign 端点和 expected_stage_version。不得靠自然语言假装已恢复。'
                           '普通任务 blocked/failed 事件必须立即核对原因并提交可回读动作：'
                           '自救、规范转派给已验证具备能力的成员，或请求人工协助；'
                           '不能仅回复 wait 让该阻断静默悬置。'
                           '计划最终日到达 day_close 后，完成日终收口时省略 next_check_at，'
                           '并提交 resume_condition=end_of_plan；Hub 会在计划边界自动终止监督。'
                           '禁止模型轮询；正常心跳和无变化检查不通知 Owner。'
                           '阻断通知由 Hub 分别写入企微和团队聊天室持久队列；'
                           '你只需在自救恢复、换人重派、请求 Owner 协助中选择并执行动作，'
                           '不要重复发送通知，也不得调用 Hub /wecom/send 或 SendRTXInfo。'}, ensure_ascii=False))
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
        # Existing supervisors created before manager_goal.v1 are upgraded on
        # their next real lease acquisition, without mutating a read-only GET.
        manager_goal_snapshot(sup, persist=True, now=now)
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
        mission_id=sup.mission_id, state='ready',
    ).order_by(MissionStage.id).all()
    for stage in rows:
        snapshot = stage.input_snapshot_json or {}
        control_kind = snapshot.get('kind')
        if (stage.workflow_run_id is not None
                and control_kind not in (
                    'workflow_execution_reconciliation',
                    'environment_repair', 'evidence_review',
                    'manager_review')):
            continue
        items.append({
            'stage_key': stage.stage_key,
            'test_task_id': _stage_task_id(stage),
            'test_task_occurrence_id': _stage_occurrence_id(stage),
            'test_task_name': snapshot.get('test_task_name') or '',
            'executor_claw_id': stage.assigned_claw_id,
            'role_key': stage.role_key,
            'control_action': control_kind or None,
            'depends_on_task_ids': snapshot.get('depends_on_task_ids') or [],
            'dependencies': snapshot.get('dependencies') or {},
            'agent_task_dispatch_api': (
                '/api/v1/test-plans/%s/supervision/agent-tasks'
                % sup.plan_id),
        })
    return items


MANAGER_DECISION_CONTRACT = 'hub.manager_goal.v1'
MANAGER_STAGE_ACTIONS = frozenset({'dispatch', 'defer', 'block', 'complete'})


def manager_goal_snapshot(sup, persist=False, now=None):
    """Return the durable manager objective without creating a second engine.

    The Supervisor row already is the plan-scoped durable aggregate.  Keeping
    the goal and bounded commitment ledger in ``observations_json`` makes the
    extension deployable without changing existing Flow/Worker tables while
    preserving every security fence around the actual effects.
    """
    now = now or _now()
    observations = dict((sup.observations_json or {}) if sup else {})
    goal = observations.get('manager_goal')
    plan = db.session.get(TestPlan, sup.plan_id) if sup else None
    team = db.session.get(AgentTeam, sup.team_id) if sup and sup.team_id else None
    if not isinstance(goal, dict):
        goal = {
            'schema_version': 1,
            'contract': MANAGER_DECISION_CONTRACT,
            'goal_id': 'plan-manager:%s' % (sup.plan_id if sup else 'unknown'),
            'version': 1,
            'status': 'active',
            'objective': (
                '持续推进测试计划「%s」，完成派发、跟进、异常恢复、复核与收口。'
                % (plan.name if plan else '')),
            'success_criteria': [
                '所有到期任务均有执行结果、明确延期条件或可审计阻断',
                '关键结论与证据已回写 Hub，Owner 只处理必要授权和产品取舍',
            ],
            'stop_conditions': ['plan_completed', 'plan_archived', 'owner_stopped'],
            'current_priorities': [],
            'next_review_at': None,
            'created_at': now.isoformat() + '+08:00',
        }
        if persist and sup:
            observations['manager_goal'] = goal
            sup.observations_json = observations
    commitments = observations.get('manager_commitments')
    if not isinstance(commitments, list):
        commitments = []
    latest_commitments = {}
    for item in commitments[-100:]:
        if not isinstance(item, dict):
            continue
        key = item.get('stage_id') or item.get('stage_key')
        if isinstance(key, str) and key:
            latest_commitments[key] = item
    stage_states = {
        row.stage_key: row.state for row in MissionStage.query.filter_by(
            mission_id=sup.mission_id).all()
    } if sup and sup.mission_id else {}
    return {
        **goal,
        'plan_id': sup.plan_id if sup else None,
        'team_id': sup.team_id if sup else None,
        'team_name': team.name if team else '',
        'pending_commitments': [
            item for key, item in latest_commitments.items()
            if item.get('status') == 'pending'
            and stage_states.get(key) == 'ready'
        ],
    }


def manager_brief(sup):
    """Small, trusted situation summary for a manager model turn."""
    plan = db.session.get(TestPlan, sup.plan_id) if sup else None
    remaining = undispatched_stages(sup)
    status_counts = {}
    if sup and sup.mission_id:
        for state, count in (db.session.query(
                MissionStage.state, db.func.count(MissionStage.id))
                .filter_by(mission_id=sup.mission_id)
                .group_by(MissionStage.state).all()):
            status_counts[state] = int(count)
    return {
        'schema_version': 1,
        'plan_id': sup.plan_id if sup else None,
        'plan_name': plan.name if plan else '',
        'plan_status': plan.status if plan else None,
        'stage_status_counts': status_counts,
        'ready_stages': [
            {
                'stage_id': item['stage_key'],
                'stage_key': item['stage_key'],
                'test_task_id': item.get('test_task_id'),
                'test_task_name': item.get('test_task_name') or '',
                'executor_claw_id': item.get('executor_claw_id'),
                'control_action': item.get('control_action'),
            }
            for item in remaining
        ],
        'ready_stage_keys': [item['stage_key'] for item in remaining],
        'ready_count': len(remaining),
        'last_decision': (sup.last_decision_json or {}) if sup else {},
        'next_check_at': (
            sup.next_check_at.isoformat() + '+08:00'
            if sup and sup.next_check_at else None),
    }


def _manager_decision_audit(sup, body, now):
    """Validate manager receipts without second-guessing business choices."""
    decisions = body.get('stage_decisions')
    receipts = body.get('action_receipts')
    if decisions is None and receipts is None:
        return {}, []
    if (not isinstance(decisions, list) or len(decisions) > 256
            or any(not isinstance(item, dict) for item in decisions)
            or not isinstance(receipts, list) or len(receipts) > 256
            or any(not isinstance(item, dict) for item in receipts)):
        fail('PLAN_MANAGER_DECISION_INVALID',
             'stage_decisions/action_receipts 须为有界对象数组', 400)
    allowed_stages = {
        row.stage_key: row for row in MissionStage.query.filter_by(
            mission_id=sup.mission_id).all()
    }
    seen = set()
    normalized = []
    commitments = []
    dispositions = {
        'dispatched', 'in_progress', 'completed', 'deferred', 'blocked',
    }
    for item in decisions:
        stage_id = str(item.get('stage_id') or '').strip()
        disposition = str(item.get('disposition') or '').strip()
        reason = str(item.get('reason') or '').strip()
        if (stage_id not in allowed_stages or stage_id in seen
                or disposition not in dispositions
                or not reason or len(reason) > 2000):
            fail('PLAN_MANAGER_DECISION_INVALID',
                 'Stage 决策必须属于当前 Mission、不可重复，并提供有效处置与理由', 409)
        receipt_value = item.get('receipt')
        if disposition in ('dispatched', 'in_progress', 'completed'):
            if not isinstance(receipt_value, dict) or not receipt_value:
                fail('PLAN_MANAGER_RECEIPT_REQUIRED',
                     '已执行处置必须附带稳定 Hub 动作回执', 409)
        normalized_item = {
            'stage_id': stage_id,
            'disposition': disposition,
            'reason': reason,
        }
        if isinstance(receipt_value, dict):
            normalized_item['receipt'] = receipt_value
        normalized.append(normalized_item)
        commitments.append({
            **normalized_item,
            'status': ('pending' if disposition in ('deferred', 'blocked')
                       else 'fulfilled'),
            'decided_at': now.isoformat() + '+08:00',
            'decision_command_key': body.get('command_key'),
        })
        seen.add(stage_id)
    normalized_receipts = []
    for item in receipts:
        if (not item or len(item) > 16
                or not any(item.get(key) not in (None, '') for key in (
                    'receipt_id', 'operation_id', 'mission_id', 'task_id',
                    'run_id'))):
            fail('PLAN_MANAGER_RECEIPT_INVALID',
                 '动作回执必须包含稳定 Hub 资源标识', 400)
        normalized_receipts.append(item)
    observations = dict(sup.observations_json or {})
    history = observations.get('manager_commitments')
    history = history if isinstance(history, list) else []
    observations['manager_commitments'] = (history + commitments)[-100:]
    sup.observations_json = observations
    return {
        'stage_decisions': normalized,
        'action_receipts': normalized_receipts,
    }, commitments


_PLAN_AGENT_TASK_CONTRACT = 'hub.plan_test_task.agent_task.v1'
_PLAN_CONTROL_TASK_CONTRACT = 'hub.plan_control_action.agent_task.v1'
RESUME_CONDITION_TYPES = {
    'build_changed', 'login_session_ready', 'device_ready', 'wda_ready',
    'account_available', 'server_ready', 'resource_released',
    'unity_ready', 'bridge_ready', 'custom_ready',
}


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
        link = {
            'plan_id': int(payload['test_plan_id']),
            'test_task_id': int(payload['test_task_id']),
            'mission_id': int(payload['mission_id']),
            'mission_stage_id': int(payload['mission_stage_id']),
            'stage_key': str(payload['stage_key']),
        }
        occurrence_id = payload.get('test_task_occurrence_id')
        link['occurrence_id'] = (
            int(occurrence_id) if occurrence_id is not None else None)
        return link
    except (KeyError, TypeError, ValueError):
        return None


def _agent_task_control_link(agent_task):
    try:
        payload = json.loads(agent_task.payload or '{}')
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict)
            or payload.get('contract') != _PLAN_CONTROL_TASK_CONTRACT):
        return None
    try:
        return {
            'plan_id': int(payload['test_plan_id']),
            'mission_id': int(payload['mission_id']),
            'mission_stage_id': int(payload['mission_stage_id']),
            'stage_key': str(payload['stage_key']),
            'action_kind': str(payload['action_kind']),
            'source_occurrence_id': (
                int(payload['source_occurrence_id'])
                if payload.get('source_occurrence_id') is not None else None),
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


def _plan_control_tasks(sup):
    if not sup or not sup.mission_id:
        return {}
    rows = (AgentTask.query.filter(
        AgentTask.task_type == 'plan_control_action',
        AgentTask.task_id.like('plan_%s_control_%%' % sup.plan_id))
        .order_by(AgentTask.id.desc()).limit(1000).all())
    indexed = {}
    for row in rows:
        link = _agent_task_control_link(row)
        if (link and link['plan_id'] == sup.plan_id
                and link['mission_id'] == sup.mission_id):
            indexed.setdefault(link['mission_stage_id'], row)
    return indexed


def pump_agent_task_plan(agent_task, now=None):
    """After result commit, promptly wake the manager for meaningful changes."""
    link = (_agent_task_plan_link(agent_task)
            or _agent_task_control_link(agent_task))
    if not link:
        return None
    sup = locked(link['plan_id'])
    if not sup:
        return None
    refresh_supervision_report(sup, now=now)
    target = pump(sup, now=now)
    db.session.commit()
    if target:
        wake(target)
    return target


def _create_control_agent_task(sup, stage, action_kind, instruction,
                               source_occurrence_id=None):
    if not stage.assigned_claw_id:
        fail('PLAN_CONTROL_OWNER_REQUIRED',
             '控制阶段缺少明确执行 Agent', 409)
    seed = '%s:%s:%s:%s' % (
        sup.plan_id, stage.id, action_kind, int(stage.fencing_token or 0) + 1)
    task_id = 'plan_%s_control_%s_%s' % (
        sup.plan_id, stage.id,
        hashlib.sha256(seed.encode()).hexdigest()[:16])
    # Control tasks are advisory/read-only unless a future, separately
    # authenticated Owner approval binds an exact mutation manifest. A broad
    # phrase such as "environment repair" must never become repository,
    # deployment, or shared-environment write authority.
    allowed_operations = {
        'manager_review': ['inspect_hub_state', 'submit_decision'],
        'environment_repair': [
            'inspect_environment', 'diagnose', 'propose_patch'],
        'evidence_review': ['inspect_evidence', 'propose_findings'],
        'workflow_execution_reconciliation': [
            'inspect_execution', 'submit_reconciliation_receipt'],
    }.get(action_kind, ['inspect_hub_state'])
    side_effect_policy = {
        'mode': 'read_only',
        'external_mutations_allowed': False,
        'owner_approval_required': True,
        'git_commit_allowed': False,
        'git_push_allowed': False,
        'deployment_allowed': False,
        'shared_api_mutation_allowed': False,
        'denial_code': 'EXTERNAL_MUTATION_APPROVAL_REQUIRED',
    }
    payload = {
        'contract': _PLAN_CONTROL_TASK_CONTRACT,
        'test_plan_id': sup.plan_id,
        'mission_id': sup.mission_id,
        'mission_stage_id': stage.id,
        'stage_key': stage.stage_key,
        'action_kind': action_kind,
        'source_occurrence_id': source_occurrence_id,
        'input_snapshot': stage.input_snapshot_json or {},
        'instruction': instruction,
        'allowed_operations': allowed_operations,
        'allowed_paths': [],
        'allowed_repositories': [],
        'side_effect_policy': side_effect_policy,
        'acceptance': {
            'result_contract': 'plan_control_action',
            'evidence_required': True,
        },
    }
    task = AgentTask(
        task_id=task_id, claw_id=stage.assigned_claw_id,
        task_type='plan_control_action', command=instruction,
        payload=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        status='pending', retry_max=2)
    db.session.add(task)
    db.session.flush()
    stage.state = 'dispatched'
    stage.last_reason_code = 'plan_control_task_dispatched'
    stage.fencing_token = int(stage.fencing_token or 0) + 1
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'control_action_dispatched', [
        'control-action-dispatched', stage.id, task.task_id,
    ], {
        'stage_key': stage.stage_key, 'action_kind': action_kind,
        'agent_task_id': task.task_id,
        'executor_claw_id': task.claw_id,
        'source_occurrence_id': source_occurrence_id,
        'side_effect_policy': side_effect_policy,
    })
    return task


def _linked_occurrence(link):
    occurrence_id = link.get('occurrence_id') if link else None
    if occurrence_id is None:
        return None
    occurrence = db.session.get(TestTaskOccurrence, occurrence_id)
    if (not occurrence or occurrence.plan_id != link['plan_id']
            or occurrence.test_task_id != link['test_task_id']
            or occurrence.mission_stage_id != link['mission_stage_id']):
        fail('PLAN_AGENT_TASK_LINK_INVALID',
             'AgentTask 与周期执行实例绑定不一致', 409)
    return occurrence


def _create_plan_agent_task(sup, task, stage, command_key, instruction,
                            retry_max, occurrence=None):
    """Create one bounded ordinary AgentTask and project dispatch truth."""
    executor_claw_id = (
        occurrence.assignee_claw_id if occurrence else task.assignee_claw_id)
    suffix_seed = '%s:%s' % (command_key, occurrence.id if occurrence else '')
    task_id = 'plan_%s_test_task_%s_%s' % (
        sup.plan_id, task.id,
        hashlib.sha256(suffix_seed.encode()).hexdigest()[:16])
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
        'execution_role': task.execution_role or 'member_work',
        'references': serialize_task_references(task, occurrence),
        'acceptance': {
            'result_contract': 'ordinary_agent_task',
            'report_to_hub': True,
        },
    }
    if occurrence:
        payload.update({
            'test_task_occurrence_id': occurrence.id,
            'occurrence_date': str(occurrence.occurrence_date),
            'not_before_at': occurrence.not_before_at.isoformat() + '+08:00',
            'due_at': (occurrence.due_at.isoformat() + '+08:00'
                       if occurrence.due_at else None),
            'execution_goal': occurrence.execution_goal_json or {},
            'resume_fencing_token': int(
                occurrence.resume_fencing_token or 0),
        })
        # Worker treats the *presence* of resume_contract as a signed request
        # to enable deterministic condition probing and validates it before a
        # Provider can run.  A fresh occurrence has no such contract; sending
        # an empty object makes Worker correctly fail closed with
        # resume_contract_invalid.  Only resumed work may carry the checkpoint
        # and contract that Hub previously accepted and persisted.
        if occurrence.resume_contract_json:
            payload['resume_contract'] = occurrence.resume_contract_json
            payload['checkpoint'] = occurrence.checkpoint_json or {}
    elif task.resume_contract_json:
        payload['resume_fencing_token'] = int(
            task.resume_fencing_token or 0)
        payload['resume_contract'] = task.resume_contract_json
        payload['checkpoint'] = task.checkpoint_json or {}
    agent_task = AgentTask(
        task_id=task_id,
        claw_id=executor_claw_id,
        task_type='test_plan_agent_task',
        command=instruction or (
            ('以测试经理身份完成分析/复查，不执行成员型测试动作：'
             if task.execution_role == 'manager_work' else
             '执行固定分配的测试任务：')
            + ('测试计划 #%s，任务 #%s「%s」。完成后返回结构化结论、'
               'outputs 与 evidence。开始前必须读取 payload.references 中'
               '绑定的 Skill、知识库和报告；任何必需资料不可读时应阻断并'
               '报告，不能静默忽略。' % (sup.plan_id, task.id, task.name))),
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
    if occurrence:
        occurrence.status = 'dispatched'
        occurrence.agent_task_id = agent_task.id
        occurrence.attempt_count = max(
            int(occurrence.attempt_count or 0), 1)
        occurrence.next_action = 'await_claim'
        occurrence.next_check_at = agent_task.created_at or _now()
    add_event(sup.plan_id, 'task_agent_dispatched', [
        'task-agent-dispatched', task.id,
        occurrence.id if occurrence else 0, agent_task.task_id,
    ], {
        'task_id': task.id,
        'occurrence_id': occurrence.id if occurrence else None,
        'stage_key': stage.stage_key,
        'executor_claw_id': executor_claw_id,
        'agent_task_id': agent_task.task_id,
    })
    return agent_task


def promote_and_dispatch_due_occurrences(sup, now=None):
    """Promote due instances and auto-dispatch only their fixed assignee."""
    if not sup or not sup.mission_id:
        return []
    now = now or _now()
    if mission_expiry_state(sup, now):
        return []
    wake_ids = []
    rows = (TestTaskOccurrence.query.filter(
        TestTaskOccurrence.plan_id == sup.plan_id,
        TestTaskOccurrence.status.in_(('scheduled', 'ready')),
        TestTaskOccurrence.not_before_at <= now,
    ).order_by(TestTaskOccurrence.id).with_for_update().all())
    ordinary = _plan_agent_tasks(sup)
    for occurrence in rows:
        task = db.session.get(TestTask, occurrence.test_task_id)
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        if not task or not stage or stage.mission_id != sup.mission_id:
            continue
        dependency_ready, _ = _apply_task_dependency_state(
            sup, task, stage, occurrence=occurrence, now=now)
        if not dependency_ready:
            continue
        if occurrence.status == 'scheduled':
            occurrence.status = 'ready'
            occurrence.next_action = (
                'auto_dispatch' if task.auto_dispatch else 'manager_dispatch')
            occurrence.next_check_at = now
            if stage.state == 'scheduled':
                stage.state = 'ready'
                stage.last_reason_code = 'occurrence_not_before_reached'
                stage.version = int(stage.version or 1) + 1
            add_event(sup.plan_id, 'task_occurrence_due', [
                'task-occurrence-due', occurrence.id,
            ], {'task_id': task.id, 'occurrence_id': occurrence.id}, now)
        if not task.auto_dispatch or stage.state != 'ready':
            continue
        mode = occurrence.execution_mode or (
            'workflow' if occurrence.workflow_definition_id
            else 'ordinary_agent_task')
        if mode == 'workflow':
            if not occurrence.workflow_definition_id:
                fail('PLAN_OCCURRENCE_FLOW_BINDING_REQUIRED',
                     '周期实例已冻结为 workflow 模式，但缺少 Flow 绑定', 409)
            _dispatch_scheduled_workflow(sup, task, stage, occurrence, now)
            wake_ids.append(occurrence.assignee_claw_id)
            continue
        if occurrence.workflow_definition_id:
            fail('PLAN_OCCURRENCE_EXECUTION_MODE_CONFLICT',
                 '普通 AgentTask 实例不得携带 Flow 绑定', 409)
        active = ordinary.get(stage.id)
        if active and active.status in ('pending', 'running'):
            continue
        agent_task = _create_plan_agent_task(
            sup, task, stage,
            'auto-occurrence:%s' % occurrence.id,
            '', 2, occurrence=occurrence)
        ordinary[stage.id] = agent_task
        wake_ids.append(occurrence.assignee_claw_id)
    return sorted(set(wake_ids))


def _dispatch_scheduled_workflow(sup, task, stage, occurrence, now=None):
    """Create exactly one Mission Run for a due, explicitly Flow-bound task."""
    now = now or _now()
    if stage.workflow_run_id or occurrence.workflow_run_id:
        return db.session.get(
            WorkflowRun, stage.workflow_run_id or occurrence.workflow_run_id)
    mission = db.session.get(WorkflowMission, sup.mission_id)
    team = db.session.get(AgentTeam, sup.team_id)
    binding = db.session.get(AgentTeamMission, sup.mission_id)
    definition = db.session.get(
        WorkflowDefinition, occurrence.workflow_definition_id)
    worker = db.session.get(OpenClawInstance, occurrence.assignee_claw_id)
    if not mission or not team or not binding:
        fail('PLAN_MISSION_INCOMPLETE', '周期 Flow 派发缺少团队 Mission 绑定', 409)
    if (not definition or definition.status != 'active'
            or int(definition.project_id or 0) != int(mission.project_id or 0)):
        fail('PLAN_SCHEDULED_FLOW_INVALID', '周期任务绑定的 Flow 不存在或不属于本项目', 409)
    if definition.id not in {
            int(item) for item in (team.policy_json or {}).get(
                'allowed_definition_ids', [])}:
        fail('TEAM_FLOW_NOT_ALLOWED', '周期任务绑定的 Flow 不在团队授权范围内', 403)
    if (not worker or worker.status == 'deleted'
            or int(worker.project_id or 0) != int(mission.project_id or 0)):
        fail('MISSION_WORKER_SCOPE_INVALID', '周期 Flow 的固定执行 Agent 无效', 403)
    from app.api.workflow_missions import (
        _allowed_worker_ids, _claw_can_execute_definition, _dispatch_key,
        _mission_definition_allowed, _request_hash, _trusted_worker_runtime,
        create_mission_dispatch_record,
    )
    if (worker.id != mission.main_claw_id
            and worker.id not in _allowed_worker_ids(mission)):
        fail('MISSION_WORKER_NOT_ALLOWED', '周期 Flow 执行 Agent 不在 Mission 白名单', 403)
    if not _mission_definition_allowed(mission, definition):
        fail('MISSION_DEFINITION_NOT_ALLOWED', '周期 Flow 不在 Mission 定义白名单', 403)
    runtime = _trusted_worker_runtime(worker.id)
    if not runtime:
        fail('MISSION_WORKER_RUNTIME_REQUIRED', '周期 Flow 执行 Agent 未注册可信 Runtime', 409)
    from app.services.worker_runtime import workflow_runtime_compatibility
    compatibility = workflow_runtime_compatibility(
        definition.definition_json or {}, runtime.get('worker_runtime'))
    if not compatibility['compatible']:
        fail(
            'WORKER_RUNTIME_INCOMPATIBLE',
            '周期 Flow 执行 Agent 的运行模式与 Workflow 不兼容', 409)
    if not _claw_can_execute_definition(worker.id, definition):
        fail('MISSION_WORKER_EXECUTE_FORBIDDEN', '周期 Flow 执行 Agent 没有执行权限', 403)
    if int(mission.child_run_count or 0) >= int(mission.max_child_runs or 20):
        fail('MISSION_CHILD_RUN_BUDGET_EXHAUSTED', 'Mission Child Run 预算已耗尽', 409)
    start_vars = copy.deepcopy(occurrence.workflow_start_vars_json or {})
    start_vars['worker_claw_id'] = worker.id
    decision_key = 'plan:%s:occurrence:%s:flow:%s' % (
        sup.plan_id, occurrence.id, definition.id)
    raw_context = {
        'test_plan_id': sup.plan_id,
        'test_task_id': task.id,
        'test_task_occurrence_id': occurrence.id,
        'test_task_description': task.description or '',
        'business_date': occurrence.occurrence_date.isoformat(),
        'task_references': serialize_task_references(task, occurrence),
    }
    data = {
        'workflow_definition_id': definition.id,
        'stage_key': stage.stage_key,
        'decision_key': decision_key,
        'start_vars': start_vars,
        'context': raw_context,
        'reason': 'Hub 到点补派周期测试任务 #%s / occurrence #%s' % (
            task.id, occurrence.id),
    }
    hash_payload = {
        'mission_id': mission.id,
        'workflow_definition_id': definition.id,
        'worker_claw_id': worker.id,
        'start_vars': start_vars,
        'context': raw_context,
        'run_name': '',
        'reason': data['reason'],
        'decision_key': decision_key,
        'stage_key': stage.stage_key,
    }
    request_hash = _request_hash(hash_payload)
    idempotency_key = _dispatch_key(mission.id, data, request_hash)
    existing = WorkflowMissionDispatch.query.filter_by(
        mission_id=mission.id, idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_hash != request_hash:
            fail('MISSION_IDEMPOTENCY_CONFLICT', '周期 Flow 幂等键发生请求冲突', 409)
        stage.workflow_run_id = existing.workflow_run_id
        occurrence.workflow_run_id = existing.workflow_run_id
        occurrence.status = 'dispatched'
        occurrence.next_action = 'await_claim'
        occurrence.next_check_at = now
        return db.session.get(WorkflowRun, existing.workflow_run_id)
    manager = db.session.get(OpenClawInstance, sup.orchestrator_claw_id)
    try:
        dispatch = create_mission_dispatch_record(
            mission=mission, definition=definition, stage=stage,
            worker_claw_id=worker.id, actor_id=sup.orchestrator_claw_id,
            actor_name=(manager.name if manager and manager.name
                        else 'Hub Plan Supervisor'),
            data=data, start_vars=start_vars, raw_context=raw_context,
            reason=data['reason'], decision_key=decision_key,
            idempotency_key=idempotency_key, request_hash=request_hash,
            team=team, binding=binding, ip_address='hub-plan-supervisor',
            commit=False)
    except Exception as exc:
        from app.services.workflows import WorkflowStartVarsValidationError
        if isinstance(exc, WorkflowStartVarsValidationError):
            fail(exc.code, str(exc), 422)
        raise
    occurrence.workflow_run_id = dispatch.workflow_run_id
    occurrence.status = 'dispatched'
    occurrence.attempt_count = max(int(occurrence.attempt_count or 0), 1)
    occurrence.next_action = 'await_claim'
    occurrence.next_check_at = now
    add_event(sup.plan_id, 'task_workflow_dispatched', [
        'task-workflow-dispatched', occurrence.id, definition.id,
    ], {
        'test_task_id': task.id,
        'occurrence_id': occurrence.id,
        'workflow_definition_id': definition.id,
        'workflow_run_id': dispatch.workflow_run_id,
        'executor_claw_id': worker.id,
    }, now)
    return db.session.get(WorkflowRun, dispatch.workflow_run_id)


def dispatch_test_task(sup, manager_claw_id, test_task_id, body):
    """Dispatch a non-Flow TestTask through the ordinary AgentTask channel.

    This is intentionally independent from the Todo worker and from a blocked
    Plan Supervisor turn.  The durable team manager assignment is the authority;
    the command receipt supplies idempotency while the AgentTask lease supplies
    execution fencing.
    """
    if not sup or not sup.team_id or not sup.mission_id:
        fail('PLAN_MISSION_INCOMPLETE', '计划尚未建立团队 Mission', 409)
    require_mission_not_expired(sup)
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
    if task.workflow_definition_id:
        fail('TEST_TASK_FLOW_DISPATCH_REQUIRED',
             '该周期任务已绑定 Flow，由 Hub 到点派发或管理员恢复监督补派', 409)
    if (not task.schedule_enabled
            and task.status not in ('assigned', 'pending')):
        fail('TEST_TASK_NOT_DISPATCHABLE', '仅新分配或待开始任务可直接派发', 409)
    dependency = task_dependency_state(task)
    if not dependency['ready']:
        fail('TEST_TASK_DEPENDENCY_NOT_READY',
             '前置任务尚未完成：%s' % ', '.join(
                 '#%s' % item for item in (
                     dependency['pending_task_ids']
                     + dependency['missing_task_ids'])), 409)
    occurrence = None
    if task.schedule_enabled:
        occurrence_id = body.get('occurrence_id')
        if occurrence_id is None:
            occurrence = (TestTaskOccurrence.query.filter_by(
                plan_id=sup.plan_id, test_task_id=task.id,
                occurrence_date=_now().date()).with_for_update().first())
        elif type(occurrence_id) is int:
            occurrence = TestTaskOccurrence.query.filter_by(
                id=occurrence_id, plan_id=sup.plan_id,
                test_task_id=task.id).with_for_update().first()
        if not occurrence:
            fail('TEST_TASK_OCCURRENCE_REQUIRED', '周期任务须指定有效执行实例', 409)
        if occurrence.not_before_at > _now():
            fail('TEST_TASK_NOT_DUE', '任务尚未到允许派发时间', 409)
        if occurrence.status in OCCURRENCE_TERMINAL:
            fail('TEST_TASK_OCCURRENCE_TERMINAL', '该执行实例已经结束', 409)
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
    else:
        stage = MissionStage.query.filter_by(
            mission_id=sup.mission_id,
            stage_key='test_task_%s' % task.id,
        ).order_by(MissionStage.stage_version.desc()).with_for_update().first()
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not stage or stage.assigned_claw_id != expected_assignee
            or not expected_assignee):
        fail('TEST_TASK_STAGE_INVALID', '任务缺少有效的团队执行阶段或执行 Agent', 409)
    if stage.workflow_run_id:
        fail('TEST_TASK_ALREADY_FLOW_DISPATCHED', '任务已经通过 Workflow 派发', 409)

    if (occurrence and occurrence.status == 'scheduled'
            and occurrence.not_before_at <= _now()):
        occurrence.status = 'ready'
        occurrence.next_action = (
            'auto_dispatch' if task.auto_dispatch else 'manager_dispatch')
        occurrence.next_check_at = _now()
        if stage.state == 'scheduled':
            stage.state = 'ready'
            stage.last_reason_code = 'manager_dispatch_due_occurrence'
            stage.version = int(stage.version or 1) + 1
        add_event(sup.plan_id, 'task_occurrence_due', [
            'task-occurrence-manager-due', occurrence.id,
        ], {'task_id': task.id, 'occurrence_id': occurrence.id})

    def apply():
        active = _plan_agent_tasks(sup).get(stage.id)
        if active and active.status in ('pending', 'running'):
            fail('TEST_TASK_ALREADY_DISPATCHED', '任务已有待领取或执行中的 AgentTask', 409)
        if stage.state != 'ready':
            fail('TEST_TASK_STAGE_NOT_READY', '任务阶段已派发或不再可执行', 409)
        instruction = str(body.get('instruction') or '').strip()
        if len(instruction) > 4000:
            fail('TEST_TASK_INSTRUCTION_INVALID', '执行说明不能超过 4000 字', 400)
        agent_task = _create_plan_agent_task(
            sup, task, stage, str(body.get('command_key') or ''),
            instruction, retry_max, occurrence=occurrence)
        return {
            'dispatched': True,
            'execution_mode': 'ordinary_agent_task',
            'test_task_id': task.id,
            'occurrence_id': occurrence.id if occurrence else None,
            'stage_key': stage.stage_key,
            'agent_task': agent_task.to_dict(),
            'wake_claw_id': expected_assignee,
        }

    return receipt(sup, 'dispatch_agent_task', body, apply)


def create_terminal_task_attempt(sup, manager_claw_id, test_task_id, body,
                                 now=None):
    """Replace one terminal/zombie Stage with a fenced, auditable attempt.

    Historical Stages and Runs remain immutable.  Recovery creates the next
    stage_version, fences the old Stage, and wakes the manager to dispatch the
    new attempt through the normal AgentTask or Mission path.
    """
    now = now or _now()
    if not sup or not sup.team_id or not sup.mission_id:
        fail('PLAN_MISSION_INCOMPLETE', '计划尚未建立团队 Mission', 409)
    require_mission_not_expired(sup, now)
    team = db.session.get(AgentTeam, sup.team_id)
    plan = db.session.get(TestPlan, sup.plan_id)
    if (not team or team.status != 'active' or not plan
            or plan.status != 'active'
            or team.primary_manager_claw_id != manager_claw_id
            or sup.orchestrator_claw_id != manager_claw_id):
        fail('PLAN_MANAGER_REQUIRED', '仅当前团队主测试经理可以创建恢复尝试', 403)
    task = TestTask.query.filter_by(
        id=test_task_id, plan_id=sup.plan_id).with_for_update().first()
    if not task:
        fail('TEST_TASK_NOT_FOUND', '测试任务不存在', 404)
    if task.schedule_enabled:
        fail('TEST_TASK_OCCURRENCE_REQUIRED', '周期任务须恢复具体 occurrence', 409)
    stage_key = 'test_task_%s' % task.id
    previous = (MissionStage.query.filter_by(
        mission_id=sup.mission_id, stage_key=stage_key)
        .order_by(MissionStage.stage_version.desc())
        .with_for_update().first())
    if not previous:
        fail('TEST_TASK_STAGE_INVALID', '任务没有可恢复的 Mission Stage', 409)
    active = _plan_agent_tasks(sup).get(previous.id)
    if active and active.status in ('pending', 'running', 'waiting_condition'):
        fail('TEST_TASK_ATTEMPT_ACTIVE', '当前任务仍有活动执行，不能创建新尝试', 409)
    old_run = (db.session.get(WorkflowRun, previous.workflow_run_id)
               if previous.workflow_run_id else None)
    if old_run and old_run.status in RUN_ACTIVE_STATUSES:
        fail('TEST_TASK_ATTEMPT_ACTIVE', '当前任务仍有关联的活动 Run', 409)
    recoverable_stage_states = {
        'ready', 'dispatched', 'blocked', 'failed', 'cancelled'}
    if previous.state not in recoverable_stage_states:
        fail('TEST_TASK_STAGE_NOT_RECOVERABLE', '当前 Stage 状态不能创建恢复尝试', 409)
    reason = str(body.get('reason') or '').strip()
    if not reason or len(reason) > 1000:
        fail('TEST_TASK_RECOVERY_REASON_REQUIRED',
             '创建恢复尝试须填写不超过 1000 字的原因', 400)
    expected = body.get('expected_stage_version')
    if (expected is not None
            and (type(expected) is not int
                 or expected != int(previous.stage_version or 1))):
        fail('TEST_TASK_STAGE_VERSION_CONFLICT', 'Stage 已变化，请回读后重试', 409)
    assignee_claw_id = body.get('assignee_claw_id', task.assignee_claw_id)
    if type(assignee_claw_id) is not int or assignee_claw_id <= 0:
        fail('TEST_TASK_ASSIGNEE_INVALID', '恢复尝试须指定有效执行者', 400)
    capability_match = None
    if assignee_claw_id != task.assignee_claw_id:
        member = AgentTeamMember.query.filter_by(
            team_id=team.id,
            claw_id=assignee_claw_id,
            role_key=previous.role_key,
        ).first()
        claw = db.session.get(OpenClawInstance, assignee_claw_id)
        if (not member or not claw or claw.status == 'deleted'
                or claw.project_id != plan.project_id):
            fail('TEST_TASK_ASSIGNEE_NOT_ALLOWED',
                 '目标执行者不是同项目同岗位的有效团队成员', 409)
        matched, capability_match = _candidate_assignment_match(
            sup, previous, None, member, claw)
        if not matched:
            fail('TEST_TASK_ASSIGNEE_NOT_EQUIVALENT',
                 '目标执行者不满足能力或资源约束：%s'
                 % capability_match.get('reason'), 409)

    def apply():
        previous.state = 'cancelled'
        previous.last_reason_code = 'superseded_by_controlled_attempt'
        previous.version = int(previous.version or 1) + 1
        snapshot = copy.deepcopy(previous.input_snapshot_json or {})
        snapshot['recovery_attempt'] = {
            'previous_stage_id': previous.id,
            'previous_stage_version': int(previous.stage_version or 1),
            'previous_workflow_run_id': previous.workflow_run_id,
            'reason': reason,
            'created_at': now.isoformat() + '+08:00',
            'created_by_claw_id': manager_claw_id,
            'assigned_claw_id': assignee_claw_id,
            'reassigned_from_claw_id': (
                task.assignee_claw_id
                if assignee_claw_id != task.assignee_claw_id else None),
            'capability_match': capability_match,
        }
        new_stage = MissionStage(
            mission_id=sup.mission_id,
            stage_key=stage_key,
            stage_version=int(previous.stage_version or 1) + 1,
            role_key=previous.role_key,
            assigned_claw_id=assignee_claw_id,
            state='ready',
            input_snapshot_json=snapshot,
            evidence_refs_json=list(previous.evidence_refs_json or []),
            last_reason_code='controlled_recovery_attempt',
            fencing_token=int(previous.fencing_token or 0) + 1,
            retry_count=int(previous.retry_count or 0) + 1,
        )
        db.session.add(new_stage)
        db.session.flush()
        binding = db.session.get(AgentTeamMission, sup.mission_id)
        mission = db.session.get(WorkflowMission, sup.mission_id)
        if binding and mission:
            stages = MissionStage.query.filter_by(
                mission_id=sup.mission_id).order_by(
                    MissionStage.stage_key,
                    MissionStage.stage_version).all()
            binding.plan_sha256 = digest([{
                'stage_key': row.stage_key,
                'stage_version': int(row.stage_version or 1),
                'role_key': row.role_key,
                'assigned_claw_id': row.assigned_claw_id,
                'input_snapshot': row.input_snapshot_json or {},
            } for row in stages])
            mission.version = int(mission.version or 1) + 1
        previous_assignee = task.assignee_claw_id
        task.assignee_claw_id = assignee_claw_id
        task.status = 'pending'
        task.condition_state = 'recovery_ready'
        task.next_action = 'manager_dispatch'
        task.recommended_action = 'controlled_new_attempt'
        task.next_check_at = now
        task.owner_gate = False
        task.resume_fencing_token = int(task.resume_fencing_token or 0) + 1
        add_event(sup.plan_id, 'task_recovery_attempt_created', [
            'task-recovery-attempt', task.id, new_stage.stage_version,
        ], {
            'test_task_id': task.id,
            'previous_stage_id': previous.id,
            'previous_workflow_run_id': previous.workflow_run_id,
            'stage_id': new_stage.id,
            'stage_key': new_stage.stage_key,
            'stage_version': new_stage.stage_version,
            'reason': reason,
            'from_claw_id': previous_assignee,
            'to_claw_id': assignee_claw_id,
        }, now)
        sup.next_check_at = now
        return {
            'created': True,
            'test_task_id': task.id,
            'previous_stage_id': previous.id,
            'previous_workflow_run_id': previous.workflow_run_id,
            'stage': new_stage.to_dict(include_input=True),
            'wake_claw_id': sup.orchestrator_claw_id,
        }

    return receipt(sup, 'create_terminal_task_attempt', body, apply)


def reassign_ready_task_stage(sup, manager_claw_id, test_task_id, body,
                              now=None):
    """Move one ready one-off Stage to a capability-equivalent team member."""
    now = now or _now()
    if not sup or not sup.team_id or not sup.mission_id:
        fail('PLAN_MISSION_INCOMPLETE', '计划尚未建立团队 Mission', 409)
    require_mission_not_expired(sup, now)
    team = db.session.get(AgentTeam, sup.team_id)
    plan = db.session.get(TestPlan, sup.plan_id)
    if (not team or team.status != 'active' or not plan
            or plan.status != 'active'
            or team.primary_manager_claw_id != manager_claw_id
            or sup.orchestrator_claw_id != manager_claw_id):
        fail('PLAN_MANAGER_REQUIRED', '仅当前团队主测试经理可以更换执行者', 403)
    task = TestTask.query.filter_by(
        id=test_task_id, plan_id=sup.plan_id).with_for_update().first()
    if not task:
        fail('TEST_TASK_NOT_FOUND', '测试任务不存在', 404)
    if task.schedule_enabled:
        fail('TEST_TASK_OCCURRENCE_REQUIRED', '周期任务须在具体 occurrence 上换人', 409)
    stage = (MissionStage.query.filter_by(
        mission_id=sup.mission_id, stage_key='test_task_%s' % task.id)
        .order_by(MissionStage.stage_version.desc())
        .with_for_update().first())
    if not stage or stage.state != 'ready':
        fail('TEST_TASK_STAGE_NOT_READY', '仅 ready 且尚未派发的 Stage 可以换人', 409)
    active = _plan_agent_tasks(sup).get(stage.id)
    if active and active.status in ('pending', 'running', 'waiting_condition'):
        fail('TEST_TASK_ALREADY_DISPATCHED', '任务已有活动 AgentTask，不能直接换人', 409)
    expected = body.get('expected_stage_version')
    if (type(expected) is not int
            or expected != int(stage.stage_version or 1)):
        fail('TEST_TASK_STAGE_VERSION_CONFLICT', 'Stage 已变化，请回读后重试', 409)
    assignee_claw_id = body.get('assignee_claw_id')
    if (type(assignee_claw_id) is not int or assignee_claw_id <= 0
            or assignee_claw_id == stage.assigned_claw_id):
        fail('TEST_TASK_ASSIGNEE_INVALID', '须指定不同的有效团队执行者', 400)
    member = AgentTeamMember.query.filter_by(
        team_id=team.id, claw_id=assignee_claw_id,
        role_key=stage.role_key).first()
    claw = db.session.get(OpenClawInstance, assignee_claw_id)
    if (not member or not claw or claw.status == 'deleted'
            or claw.project_id != plan.project_id):
        fail('TEST_TASK_ASSIGNEE_NOT_ALLOWED', '目标执行者不是同项目同岗位的有效团队成员', 409)
    matched, match = _candidate_assignment_match(
        sup, stage, None, member, claw)
    if not matched:
        fail('TEST_TASK_ASSIGNEE_NOT_EQUIVALENT',
             '目标执行者不满足能力或资源约束：%s' % match.get('reason'), 409)
    reason = str(body.get('reason') or '').strip()
    if not reason or len(reason) > 1000:
        fail('TEST_TASK_REASSIGN_REASON_REQUIRED', '换人须填写不超过 1000 字的原因', 400)

    def apply():
        previous_claw_id = stage.assigned_claw_id
        stage.assigned_claw_id = assignee_claw_id
        stage.fencing_token = int(stage.fencing_token or 0) + 1
        stage.version = int(stage.version or 1) + 1
        stage.last_reason_code = 'manager_reassigned_ready_stage'
        snapshot = copy.deepcopy(stage.input_snapshot_json or {})
        assignment = dict(snapshot.get('team_assignment') or {})
        assignment.update({
            'assigned_claw_id': assignee_claw_id,
            'reassigned_from_claw_id': previous_claw_id,
            'reassigned_by_claw_id': manager_claw_id,
            'reason': reason,
            'capability_match': match,
            'reassigned_at': now.isoformat() + '+08:00',
        })
        snapshot['team_assignment'] = assignment
        stage.input_snapshot_json = snapshot
        task.assignee_claw_id = assignee_claw_id
        task.status = 'pending'
        task.next_action = 'manager_dispatch'
        task.recommended_action = 'dispatch_reassigned_stage'
        task.next_check_at = now
        mission = db.session.get(WorkflowMission, sup.mission_id)
        if mission:
            mission.version = int(mission.version or 1) + 1
        add_event(sup.plan_id, 'task_stage_reassigned', [
            'task-stage-reassigned', stage.id, stage.fencing_token,
        ], {
            'test_task_id': task.id,
            'stage_id': stage.id,
            'stage_version': stage.stage_version,
            'from_claw_id': previous_claw_id,
            'to_claw_id': assignee_claw_id,
            'reason': reason,
            'capability_match': match,
        }, now)
        sup.next_check_at = now
        return {
            'reassigned': True,
            'test_task_id': task.id,
            'stage': stage.to_dict(include_input=True),
            'from_claw_id': previous_claw_id,
            'to_claw_id': assignee_claw_id,
            'wake_claw_id': sup.orchestrator_claw_id,
        }

    return receipt(sup, 'reassign_ready_task_stage', body, apply)


def _record_control_claim(agent_task, link, now):
    sup = locked(link['plan_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    if (not sup or not stage or stage.mission_id != link['mission_id']
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_CONTROL_TASK_LINK_INVALID',
             '控制 AgentTask 与阶段绑定不一致', 409)
    stage.state = 'running'
    stage.last_reason_code = 'plan_control_task_claimed'
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'control_action_claimed', [
        'control-action-claimed', agent_task.task_id,
        int(agent_task.attempt_no or 0),
    ], {
        'stage_key': stage.stage_key,
        'action_kind': link['action_kind'],
        'agent_task_id': agent_task.task_id,
        'executor_claw_id': agent_task.claw_id,
    }, now)
    return stage


MANAGER_REVIEW_DECISIONS = frozenset({
    'publish_partial_closeout', 'create_environment_repair',
    'wait_condition', 'retry_same_executor', 'dispatch_workflow',
    'owner_action_required',
})


def _apply_manager_review_decision(sup, agent_task, source, result, now):
    """Apply one fenced manager decision to the source occurrence."""
    outputs = result.get('outputs') if isinstance(result.get('outputs'), dict) else {}
    decision = str(result.get('decision') or outputs.get('decision') or '').strip()
    if decision not in MANAGER_REVIEW_DECISIONS:
        return False, None, 'PLAN_MANAGER_DECISION_INVALID'
    evidence = result.get('evidence') or outputs.get('evidence')
    if not isinstance(evidence, (list, dict)) or not evidence:
        return False, None, 'PLAN_MANAGER_EVIDENCE_REQUIRED'
    original = db.session.get(MissionStage, source.mission_stage_id)
    task = db.session.get(TestTask, source.test_task_id)
    if not original or not task or original.mission_id != sup.mission_id:
        return False, None, 'PLAN_MANAGER_SOURCE_INVALID'
    receipt = {
        'decision': decision,
        'agent_task_id': agent_task.task_id,
        'manager_claw_id': agent_task.claw_id,
        'decided_at': now.isoformat() + '+08:00',
        'evidence_digest': digest(evidence),
    }
    metadata = dict(source.action_metadata_json or {})
    metadata['manager_review_receipt'] = receipt
    source.action_metadata_json = metadata
    source.owner_gate = False
    wake_id = None
    if decision == 'publish_partial_closeout':
        source.status = 'analysis_incomplete'
        source.condition_state = 'manager_closed_partial'
        source.recommended_action = 'publish_partial_closeout'
        source.next_action = 'none'
        source.next_check_at = None
        original.state = 'completed'
        original.last_reason_code = 'manager_partial_closeout'
    elif decision == 'create_environment_repair':
        source.status = 'blocked'
        source.recommended_action = decision
        source.next_action = decision
        source.next_check_at = now
        original.state = 'blocked'
        original.last_reason_code = 'manager_requested_environment_repair'
    elif decision == 'wait_condition':
        contract, checkpoint, next_probe_at = _normalize_resume_contract(
            result, source, now)
        source.status = 'waiting_condition'
        source.condition_state = 'waiting_condition'
        source.resume_contract_json = contract
        source.checkpoint_json = checkpoint
        source.next_probe_at = next_probe_at
        source.next_check_at = next_probe_at
        source.owner_gate = bool(contract['owner_gate'])
        source.recommended_action = (
            'owner_action_then_auto_resume' if source.owner_gate
            else 'local_probe_then_auto_resume')
        source.next_action = (
            'wait_owner_and_probe' if source.owner_gate
            else 'wait_condition_probe')
        original.state = 'waiting_condition'
        original.last_reason_code = 'manager_wait_condition'
    elif decision == 'owner_action_required':
        source.status = 'blocked'
        source.owner_gate = True
        source.recommended_action = decision
        source.next_action = decision
        source.next_check_at = None
        original.state = 'blocked'
        original.last_reason_code = 'manager_owner_action_required'
    elif decision in ('retry_same_executor', 'dispatch_workflow'):
        if source.due_at and source.due_at <= now:
            return False, None, 'PLAN_MANAGER_DECISION_AFTER_DUE'
        mode = source.execution_mode or 'ordinary_agent_task'
        if decision == 'dispatch_workflow' and mode != 'workflow':
            return False, None, 'PLAN_MANAGER_FLOW_BINDING_REQUIRED'
        if mode == 'workflow' and source.workflow_run_id:
            return False, None, 'PLAN_MANAGER_EXISTING_RUN_RECONCILIATION_REQUIRED'
        source.status = 'ready'
        source.condition_state = 'manager_retry_approved'
        source.next_action = 'auto_dispatch'
        source.next_check_at = now
        source.resume_fencing_token = int(source.resume_fencing_token or 0) + 1
        original.state = 'ready'
        original.last_reason_code = 'manager_retry_approved'
        if mode == 'workflow':
            _dispatch_scheduled_workflow(sup, task, original, source, now)
            wake_id = source.assignee_claw_id
        else:
            dispatched = _create_plan_agent_task(
                sup, task, original,
                'manager-retry:%s:%s' % (
                    source.id, source.resume_fencing_token),
                '测试经理已完成复核；沿用当前能力与资源合同重试同一执行者。',
                2, occurrence=source)
            wake_id = dispatched.claw_id
    original.version = int(original.version or 1) + 1
    add_event(sup.plan_id, 'manager_review_decided', [
        'manager-review-decided', source.id, agent_task.task_id, decision,
    ], {
        'occurrence_id': source.id,
        'test_task_id': source.test_task_id,
        **receipt,
        'wake_claw_id': wake_id,
    }, now)
    return True, wake_id, None


def _record_control_terminal(agent_task, link, result, status, now):
    sup = locked(link['plan_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    if (not sup or not stage or stage.mission_id != link['mission_id']
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_CONTROL_TASK_LINK_INVALID',
             '控制 AgentTask 与阶段绑定不一致', 409)
    result = result if isinstance(result, dict) else {}
    completed = status == 'completed'
    manager_wake_id = None
    manager_error = None
    source_id = link.get('source_occurrence_id')
    source = (db.session.get(TestTaskOccurrence, source_id)
              if source_id else None)
    if (link['action_kind'] == 'manager_review' and completed):
        if source:
            completed, manager_wake_id, manager_error = (
                _apply_manager_review_decision(
                    sup, agent_task, source, result, now))
        else:
            completed, manager_error = False, 'PLAN_MANAGER_SOURCE_INVALID'
    if (link['action_kind'] == 'manager_review' and not completed and source):
        attempts = int(source.action_attempt_count or 0)
        source.status = 'blocked'
        source.condition_state = 'manager_decision_rejected'
        source.recommended_action = 'manager_review'
        if attempts >= 3:
            source.owner_gate = True
            source.next_action = 'owner_action_required'
            source.next_check_at = None
        else:
            source.owner_gate = False
            source.next_action = 'manager_review'
            source.next_check_at = now
    if (link['action_kind'] == 'workflow_execution_reconciliation'
            and completed and stage.state != 'completed'):
        # The dedicated resolve API is the canonical verifier. A model result
        # that merely repeats the three assertions is not a trusted receipt.
        completed = False
        stage.last_reason_code = 'RECONCILIATION_RECEIPT_MISSING'
    if completed:
        stage.state = 'completed'
        stage.last_reason_code = 'plan_control_task_completed'
    else:
        stage.state = 'blocked'
        stage.last_reason_code = str(
            manager_error or result.get('error_code') or stage.last_reason_code
            or status or 'plan_control_task_failed')[:80]
    stage.version = int(stage.version or 1) + 1
    wake_id = manager_wake_id
    if completed and source and link['action_kind'] == 'environment_repair':
        original = db.session.get(MissionStage, source.mission_stage_id)
        if original:
            # The control task is only a read-only diagnosis/proposal. It is
            # never evidence that a shared environment was mutated, so it may
            # not automatically resume the source occurrence.
            metadata = dict(source.action_metadata_json or {})
            metadata['environment_repair_proposal'] = {
                'agent_task_id': agent_task.task_id,
                'executor_claw_id': agent_task.claw_id,
                'recorded_at': now.isoformat() + '+08:00',
                'result_digest': digest(result),
                'approval_required': True,
                'denial_code': 'EXTERNAL_MUTATION_APPROVAL_REQUIRED',
            }
            source.action_metadata_json = metadata
            source.status = 'blocked'
            source.condition_state = 'environment_repair_proposed'
            source.owner_gate = True
            source.next_action = 'owner_action_required'
            source.next_check_at = None
            source.recommended_action = 'owner_action_required'
            original.state = 'blocked'
            original.last_reason_code = 'environment_repair_approval_required'
            original.version = int(original.version or 1) + 1
    add_event(sup.plan_id, 'control_action_terminal', [
        'control-action-terminal', agent_task.task_id,
        int(agent_task.attempt_no or 0), status,
    ], {
        'stage_key': stage.stage_key,
        'action_kind': link['action_kind'],
        'agent_task_id': agent_task.task_id,
        'status': stage.state,
        'source_occurrence_id': source_id,
        'decision': (result.get('decision') or (
            result.get('outputs') or {}).get('decision')
            if isinstance(result.get('outputs'), dict)
            else result.get('decision')),
    }, now)
    return {'stage': stage, 'wake_claw_id': wake_id} if wake_id else stage


def record_agent_task_claim(agent_task, now=None):
    control = _agent_task_control_link(agent_task)
    if control:
        return _record_control_claim(agent_task, control, now or _now())
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    occurrence = _linked_occurrence(link)
    sup = locked(link['plan_id'])
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != expected_assignee
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID', 'AgentTask 与计划阶段绑定不一致', 409)
    if occurrence:
        occurrence.status = 'running'
        occurrence.condition_state = 'running'
        occurrence.agent_task_id = agent_task.id
        occurrence.attempt_count = max(
            int(occurrence.attempt_count or 0),
            int(agent_task.attempt_no or 0), 1)
        occurrence.last_heartbeat_at = (
            agent_task.last_heartbeat_at or now)
        occurrence.next_action = 'await_result'
        occurrence.next_check_at = agent_task.lease_expires_at
    elif task.status in ('assigned', 'pending'):
        task.status = 'in_progress'
        task.progress = max(1, int(task.progress or 0))
        task.condition_state = 'running'
        task.last_heartbeat_at = agent_task.last_heartbeat_at or now
        task.next_action = 'await_result'
        task.next_check_at = agent_task.lease_expires_at
    stage.state = 'running'
    stage.last_reason_code = 'ordinary_agent_task_claimed'
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_claimed', [
        'ordinary-task-claimed', agent_task.task_id,
        int(agent_task.attempt_no or 0),
    ], {
        'task_id': task.id,
        'occurrence_id': occurrence.id if occurrence else None,
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
    control = _agent_task_control_link(agent_task)
    if control:
        stage = _record_control_claim(agent_task, control, now or _now())
        stage.state = 'dispatched'
        stage.last_reason_code = 'plan_control_task_retry_pending'
        return stage
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    occurrence = _linked_occurrence(link)
    sup = locked(link['plan_id'])
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != expected_assignee
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID',
             'AgentTask 与计划阶段绑定不一致', 409)
    if occurrence and occurrence.status in ('completed', 'skipped'):
        return task
    if not occurrence and task.status in ('completed', 'skipped'):
        return task
    changed = ((occurrence.status != 'dispatched' if occurrence else
                task.status != 'pending') or stage.state != 'dispatched')
    if occurrence:
        occurrence.status = 'dispatched'
        occurrence.attempt_count = max(
            int(occurrence.attempt_count or 0),
            int(agent_task.attempt_no or 0), 1)
        occurrence.last_heartbeat_at = None
        occurrence.next_action = 'retry_pending'
        occurrence.next_check_at = now
    else:
        task.status = 'pending'
        task.condition_state = 'retry_pending'
        task.last_heartbeat_at = None
        task.next_action = 'retry_pending'
        task.next_check_at = now
    stage.state = 'dispatched'
    stage.last_reason_code = 'ordinary_agent_task_retry_pending'
    if changed:
        stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_agent_retry_pending', [
        'task-agent-retry-pending', agent_task.task_id,
        int(agent_task.attempt_no or 0), int(agent_task.retry_count or 0),
    ], {
        'task_id': task.id,
        'occurrence_id': occurrence.id if occurrence else None,
        'agent_task_id': agent_task.task_id,
        'executor_claw_id': agent_task.claw_id,
        'retry_count': int(agent_task.retry_count or 0),
        'retry_max': int(agent_task.retry_max or 0),
    }, now)
    return task


def record_agent_task_heartbeat(agent_task, now=None):
    """Refresh occurrence execution truth without emitting noisy events."""
    control = _agent_task_control_link(agent_task)
    if control:
        stage = db.session.get(MissionStage, control['mission_stage_id'])
        if stage and stage.assigned_claw_id == agent_task.claw_id:
            stage.state = 'running'
        return stage
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    occurrence = _linked_occurrence(link)
    if not occurrence:
        task = db.session.get(TestTask, link['test_task_id'])
        stage = db.session.get(MissionStage, link['mission_stage_id'])
        if (not task or not stage
                or task.assignee_claw_id != agent_task.claw_id
                or stage.assigned_claw_id != agent_task.claw_id):
            return None
        task.status = 'in_progress'
        task.condition_state = 'running'
        task.last_heartbeat_at = agent_task.last_heartbeat_at or now
        task.next_action = 'await_result'
        task.next_check_at = agent_task.lease_expires_at
        stage.state = 'running'
        return task
    occurrence.status = 'running'
    occurrence.last_heartbeat_at = agent_task.last_heartbeat_at or now
    occurrence.next_action = 'await_result'
    occurrence.next_check_at = agent_task.lease_expires_at
    occurrence.attempt_count = max(
        int(occurrence.attempt_count or 0),
        int(agent_task.attempt_no or 0), 1)
    return occurrence


def _apply_terminal_action(occurrence, error_code, summary, now):
    text = ('%s %s' % (error_code or '', summary or '')).lower()
    occurrence.owner_gate = False
    if re.search(r'credential|permission|password|login required|账号登录|凭据|权限|人工', text):
        action, owner_gate = 'owner_action_required', True
    elif re.search(r'webdriveragent|\bwda\b|device|adb|设备|手机连接', text):
        action, owner_gate = 'create_environment_repair', False
    elif re.search(r'evidence|manifest|receipt missing|证据|回执缺失', text):
        action, owner_gate = 'create_evidence_review', False
    elif re.search(r'timeout|transport|result_invalid|lease_expired|provider_', text):
        action, owner_gate = 'reassign_stage', False
    else:
        action, owner_gate = 'manager_review', False
    occurrence.recommended_action = action
    occurrence.next_action = action
    occurrence.owner_gate = owner_gate
    occurrence.next_check_at = None if owner_gate else now
    metadata = dict(occurrence.action_metadata_json or {})
    metadata.update({
        'error_code': str(error_code or '')[:128],
        'classified_at': now.isoformat() + '+08:00',
    })
    occurrence.action_metadata_json = metadata
    return action


def _direct_task_due_at(task):
    if not task or not task.end_date:
        return None
    return datetime.combine(
        task.end_date, _task_clock(task.due_time, '23:59'))


def _normalize_resume_contract(result, due_at, now):
    contract = result.get('resume_contract')
    if not isinstance(contract, dict):
        fail('RESUME_CONTRACT_REQUIRED',
             'waiting_condition 必须提供 resume_contract', 400)
    conditions = contract.get('conditions')
    if not isinstance(conditions, list) or not conditions or len(conditions) > 10:
        fail('RESUME_CONTRACT_INVALID',
             'resume_contract.conditions 必须为 1 到 10 项', 400)
    normalized = []
    for row in conditions:
        if not isinstance(row, dict):
            fail('RESUME_CONTRACT_INVALID', '恢复条件必须为对象', 400)
        condition_type = str(row.get('condition_type') or '').strip()
        scope = row.get('condition_scope')
        operation = str(row.get('probe_operation') or '').strip()
        if condition_type not in RESUME_CONDITION_TYPES:
            fail('RESUME_CONTRACT_INVALID',
                 '不支持的 condition_type: %s' % condition_type, 400)
        if not isinstance(scope, dict) or not scope:
            fail('RESUME_CONTRACT_INVALID',
                 'condition_scope 必须为非空对象', 400)
        if (not operation or len(operation) > 120
                or not re.fullmatch(r'[a-z0-9_.:-]+', operation)):
            fail('RESUME_CONTRACT_INVALID',
                 'probe_operation 必须为 allowlist operation 名称', 400)
        normalized.append({
            'condition_type': condition_type,
            'condition_scope': scope,
            'probe_operation': operation,
            'ready': False,
        })
    interval = contract.get('probe_interval_seconds', 120)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or interval < 60 or interval > 1800):
        fail('RESUME_CONTRACT_INVALID',
             'probe_interval_seconds 必须为 60 到 1800', 400)
    checkpoint = result.get('checkpoint')
    if not isinstance(checkpoint, dict):
        fail('CHECKPOINT_REQUIRED',
             'waiting_condition 必须提供结构化 checkpoint', 400)
    owner_gate = contract.get('owner_gate') is True
    human_action = str(contract.get('human_action') or '').strip()
    if owner_gate and not human_action:
        fail('RESUME_CONTRACT_INVALID',
             'owner_gate=true 时必须说明 human_action', 400)
    next_probe_at = now + timedelta(seconds=interval)
    if due_at and next_probe_at > due_at:
        next_probe_at = due_at
    return ({
        'schema_version': 1,
        'conditions': normalized,
        'probe_interval_seconds': interval,
        'next_probe_at': next_probe_at.isoformat() + '+08:00',
        'resume_from_checkpoint': contract.get('resume_from_checkpoint'),
        'owner_gate': owner_gate,
        'human_action': human_action,
        'due_at': due_at.isoformat() + '+08:00' if due_at else None,
        'timeout_report_policy': str(
            contract.get('timeout_report_policy')
            or 'publish_completed_and_missing_scope'),
    }, checkpoint, next_probe_at)


def record_agent_task_waiting_condition(agent_task, result, now=None):
    """Persist a non-terminal external-condition wait for scheduled or one-off work."""
    link = _agent_task_plan_link(agent_task)
    if not link:
        fail('WAITING_CONDITION_PLAN_TASK_REQUIRED',
             'waiting_condition 仅支持团队计划任务', 409)
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    occurrence = _linked_occurrence(link)
    sup = locked(link['plan_id'])
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != expected_assignee
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID',
             'AgentTask 与计划任务绑定不一致', 409)
    if not occurrence and task.schedule_enabled:
        fail('WAITING_CONDITION_OCCURRENCE_REQUIRED',
             '周期任务的 waiting_condition 必须绑定执行实例', 409)
    contract, checkpoint, next_probe_at = _normalize_resume_contract(
        result, occurrence.due_at if occurrence else _direct_task_due_at(task),
        now)
    target = occurrence or task
    target.status = 'waiting_condition'
    target.condition_state = 'waiting_condition'
    target.resume_contract_json = contract
    target.checkpoint_json = checkpoint
    target.next_probe_at = next_probe_at
    target.next_check_at = next_probe_at
    target.last_condition_event_at = now
    target.owner_gate = bool(contract['owner_gate'])
    target.recommended_action = (
        'owner_action_then_auto_resume' if target.owner_gate
        else 'local_probe_then_auto_resume')
    target.next_action = (
        'wait_owner_and_probe' if target.owner_gate
        else 'wait_condition_probe')
    target.result_summary = str(
        result.get('summary') or result.get('reason') or '')[:8000]
    stage.state = 'waiting_condition'
    stage.last_reason_code = 'waiting_condition'
    stage.version = int(stage.version or 1) + 1
    event_prefix = 'occurrence' if occurrence else 'task'
    event_kind = ('%s_owner_gate' % event_prefix if target.owner_gate
                  else '%s_waiting_condition' % event_prefix)
    add_event(sup.plan_id, event_kind, [
        event_kind, occurrence.id if occurrence else task.id,
        int(target.resume_fencing_token or 0),
    ], {
        'occurrence_id': occurrence.id if occurrence else None,
        'test_task_id': task.id,
        'condition_types': [row['condition_type']
                            for row in contract['conditions']],
        'owner_gate': target.owner_gate,
        'human_action': contract['human_action'],
        'next_probe_at': contract['next_probe_at'],
        'due_at': contract['due_at'],
    }, now)
    return target


def record_condition_probe(sup, claw_id, occurrence_id, body, now=None):
    """Accept one no-LLM probe fact and atomically resume when ready."""
    now = now or _now()
    key = body.get('command_key')
    if isinstance(key, str):
        old = PlanSupervisorReceipt.query.filter_by(
            plan_id=sup.plan_id, action='condition_probe',
            command_key=key).first()
        if old:
            if old.request_hash != digest(body):
                fail('PLAN_COMMAND_CONFLICT',
                     '同一 command_key 不得改变请求')
            return dict(old.response_json, replayed=True)
    occurrence = TestTaskOccurrence.query.filter_by(
        id=occurrence_id, plan_id=sup.plan_id).with_for_update().first()
    if not occurrence:
        fail('TEST_TASK_OCCURRENCE_NOT_FOUND', '周期任务实例不存在', 404)
    if occurrence.assignee_claw_id != claw_id:
        fail('TEST_TASK_OCCURRENCE_FORBIDDEN',
             '仅当前执行 Agent 可上报恢复条件', 403)
    expected = body.get('expected_resume_fencing_token')
    if (isinstance(expected, bool) or not isinstance(expected, int)
            or expected != int(occurrence.resume_fencing_token or 0)):
        fail('RESUME_CONDITION_FENCED', '恢复条件版本已变化', 409)
    condition_type = str(body.get('condition_type') or '').strip()
    ready = body.get('condition_ready')
    if type(ready) is not bool:
        fail('RESUME_CONDITION_INVALID', 'condition_ready 必须为布尔值', 400)
    contract = dict(occurrence.resume_contract_json or {})
    conditions = [dict(row) for row in list(contract.get('conditions') or [])]
    matched = False
    for row in conditions:
        if row.get('condition_type') == condition_type:
            row['ready'] = ready
            row['observed_at'] = now.isoformat() + '+08:00'
            facts = body.get('facts')
            if isinstance(facts, dict):
                row['facts_digest'] = digest(facts)
            matched = True
    if not matched:
        fail('RESUME_CONDITION_INVALID',
             'condition_type 不属于当前 ResumeContract', 400)

    def apply():
        occurrence.resume_contract_json = dict(contract, conditions=conditions)
        occurrence.last_condition_event_at = now
        metadata = dict(occurrence.action_metadata_json or {})
        probe_count = int(metadata.get('probe_count') or 0) + 1
        metadata['probe_count'] = probe_count
        metadata['last_probe_type'] = condition_type
        metadata['last_probe_ready'] = ready
        occurrence.action_metadata_json = metadata
        all_ready = bool(conditions and all(row.get('ready') is True
                                            for row in conditions))
        if not all_ready:
            delays = (60, 120, 300, 600, 1800)
            delay = delays[min(probe_count - 1, len(delays) - 1)]
            next_probe = now + timedelta(seconds=delay)
            if occurrence.due_at and next_probe > occurrence.due_at:
                next_probe = occurrence.due_at
            occurrence.next_probe_at = next_probe
            occurrence.next_check_at = next_probe
            occurrence.resume_contract_json = dict(
                occurrence.resume_contract_json or {},
                next_probe_at=next_probe.isoformat() + '+08:00')
            return {'condition_ready': False,
                    'next_probe_at': next_probe.isoformat() + '+08:00'}
        if occurrence.status != 'waiting_condition':
            fail('RESUME_CONDITION_STATE_CONFLICT',
                 '当前实例不在 waiting_condition', 409)
        task = db.session.get(TestTask, occurrence.test_task_id)
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        if not task or not stage or stage.mission_id != sup.mission_id:
            fail('TEST_TASK_STAGE_INVALID', '恢复目标阶段不存在', 409)
        occurrence.status = 'recovery_ready'
        occurrence.condition_state = 'recovery_ready'
        occurrence.resume_fencing_token = int(
            occurrence.resume_fencing_token or 0) + 1
        occurrence.next_probe_at = None
        occurrence.next_check_at = now
        occurrence.owner_gate = False
        occurrence.next_action = 'auto_resume'
        occurrence.recommended_action = 'resume_from_checkpoint'
        stage.state = 'ready'
        stage.last_reason_code = 'resume_condition_satisfied'
        stage.version = int(stage.version or 1) + 1
        add_event(sup.plan_id, 'occurrence_recovery_ready', [
            'occurrence-recovery-ready', occurrence.id,
            occurrence.resume_fencing_token,
        ], {
            'occurrence_id': occurrence.id,
            'test_task_id': task.id,
            'resume_fencing_token': occurrence.resume_fencing_token,
        }, now)
        agent_task = _create_plan_agent_task(
            sup, task, stage,
            'resume:%s:%s' % (
                occurrence.id, occurrence.resume_fencing_token),
            ('恢复条件已满足。使用 payload.checkpoint 和 '
             'payload.resume_contract，从未完成步骤继续；禁止重复已登记副作用。'),
            2, occurrence=occurrence)
        occurrence.condition_state = 'resuming'
        occurrence.next_action = 'await_resume_claim'
        return {
            'condition_ready': True,
            'resumed': True,
            'resume_fencing_token': occurrence.resume_fencing_token,
            'agent_task': agent_task.to_dict(),
            'wake_claw_id': occurrence.assignee_claw_id,
        }

    return receipt(sup, 'condition_probe', body, apply)


def record_test_task_condition_probe(sup, claw_id, test_task_id, body,
                                     now=None):
    """Resume one non-recurring TestTask after a no-LLM condition probe."""
    now = now or _now()
    key = body.get('command_key')
    if isinstance(key, str):
        old = PlanSupervisorReceipt.query.filter_by(
            plan_id=sup.plan_id, action='test_task_condition_probe',
            command_key=key).first()
        if old:
            if old.request_hash != digest(body):
                fail('PLAN_COMMAND_CONFLICT',
                     '同一 command_key 不得改变请求')
            return dict(old.response_json, replayed=True)
    task = TestTask.query.filter_by(
        id=test_task_id, plan_id=sup.plan_id).with_for_update().first()
    if not task:
        fail('TEST_TASK_NOT_FOUND', '测试任务不存在', 404)
    if task.schedule_enabled:
        fail('TEST_TASK_OCCURRENCE_REQUIRED',
             '周期任务须向 occurrence condition-events 上报', 409)
    if task.assignee_claw_id != claw_id:
        fail('TEST_TASK_CONDITION_FORBIDDEN',
             '仅当前执行 Agent 可上报恢复条件', 403)
    expected = body.get('expected_resume_fencing_token')
    if (isinstance(expected, bool) or not isinstance(expected, int)
            or expected != int(task.resume_fencing_token or 0)):
        fail('RESUME_CONDITION_FENCED', '恢复条件版本已变化', 409)
    condition_type = str(body.get('condition_type') or '').strip()
    ready = body.get('condition_ready')
    if type(ready) is not bool:
        fail('RESUME_CONDITION_INVALID', 'condition_ready 必须为布尔值', 400)
    contract = dict(task.resume_contract_json or {})
    conditions = [dict(row) for row in list(contract.get('conditions') or [])]
    matched = False
    for row in conditions:
        if row.get('condition_type') == condition_type:
            row['ready'] = ready
            row['observed_at'] = now.isoformat() + '+08:00'
            facts = body.get('facts')
            if isinstance(facts, dict):
                row['facts_digest'] = digest(facts)
            matched = True
    if not matched:
        fail('RESUME_CONDITION_INVALID',
             'condition_type 不属于当前 ResumeContract', 400)

    def apply():
        task.resume_contract_json = dict(contract, conditions=conditions)
        task.last_condition_event_at = now
        metadata = dict(task.action_metadata_json or {})
        probe_count = int(metadata.get('probe_count') or 0) + 1
        metadata['probe_count'] = probe_count
        metadata['last_probe_type'] = condition_type
        metadata['last_probe_ready'] = ready
        task.action_metadata_json = metadata
        all_ready = bool(conditions and all(row.get('ready') is True
                                            for row in conditions))
        if not all_ready:
            delays = (60, 120, 300, 600, 1800)
            delay = delays[min(probe_count - 1, len(delays) - 1)]
            next_probe = now + timedelta(seconds=delay)
            due_at = _direct_task_due_at(task)
            if due_at and next_probe > due_at:
                next_probe = due_at
            task.next_probe_at = next_probe
            task.next_check_at = next_probe
            task.resume_contract_json = dict(
                task.resume_contract_json or {},
                next_probe_at=next_probe.isoformat() + '+08:00')
            return {
                'condition_ready': False,
                'next_probe_at': next_probe.isoformat() + '+08:00',
            }
        if (task.status != 'waiting_condition'
                or task.condition_state != 'waiting_condition'):
            fail('RESUME_CONDITION_STATE_CONFLICT',
                 '当前任务不在 waiting_condition', 409)
        stage = MissionStage.query.filter_by(
            mission_id=sup.mission_id,
            stage_key='test_task_%s' % task.id,
        ).order_by(MissionStage.stage_version.desc()).with_for_update().first()
        if (not stage or stage.assigned_claw_id != task.assignee_claw_id):
            fail('TEST_TASK_STAGE_INVALID', '恢复目标阶段不存在', 409)
        task.status = 'pending'
        task.condition_state = 'recovery_ready'
        task.resume_fencing_token = int(task.resume_fencing_token or 0) + 1
        task.next_probe_at = None
        task.next_check_at = now
        task.owner_gate = False
        task.next_action = 'auto_resume'
        task.recommended_action = 'resume_from_checkpoint'
        stage.state = 'ready'
        stage.last_reason_code = 'resume_condition_satisfied'
        stage.version = int(stage.version or 1) + 1
        add_event(sup.plan_id, 'task_recovery_ready', [
            'task-recovery-ready', task.id, task.resume_fencing_token,
        ], {
            'test_task_id': task.id,
            'resume_fencing_token': task.resume_fencing_token,
        }, now)
        agent_task = _create_plan_agent_task(
            sup, task, stage,
            'direct-resume:%s:%s' % (
                task.id, task.resume_fencing_token),
            ('恢复条件已满足。使用 payload.checkpoint 和 '
             'payload.resume_contract，从未完成步骤继续；禁止重复已登记副作用。'),
            2)
        task.condition_state = 'resuming'
        task.next_action = 'await_resume_claim'
        return {
            'condition_ready': True,
            'resumed': True,
            'resume_fencing_token': task.resume_fencing_token,
            'agent_task': agent_task.to_dict(),
            'wake_claw_id': task.assignee_claw_id,
        }

    return receipt(sup, 'test_task_condition_probe', body, apply)


def guard_execution_goals(sup, now=None):
    """Close overdue goals and expose stalled probes without invoking an LLM."""
    if not sup:
        return 0
    now = now or _now()
    changed = 0
    rows = (TestTaskOccurrence.query.filter(
        TestTaskOccurrence.plan_id == sup.plan_id,
        TestTaskOccurrence.status.in_((
            'waiting_condition', 'recovery_ready', 'dispatched', 'running')),
    ).order_by(TestTaskOccurrence.id).all())
    for occurrence in rows:
        if occurrence.due_at and occurrence.due_at <= now:
            stage = db.session.get(MissionStage, occurrence.mission_stage_id)
            latest_task = (_plan_agent_tasks(sup).get(stage.id)
                           if stage else None)
            if latest_task and latest_task.status == 'waiting_condition':
                latest_task.status = 'blocked'
                latest_task.terminal_reason = 'occurrence_due_elapsed'
                latest_task.completed_at = now
                latest_task.version = int(latest_task.version or 0) + 1
            occurrence.status = 'analysis_incomplete'
            occurrence.condition_state = 'due_elapsed'
            occurrence.next_probe_at = None
            occurrence.next_check_at = None
            occurrence.owner_gate = False
            occurrence.recommended_action = 'publish_partial_closeout'
            occurrence.next_action = 'none'
            if stage:
                stage.state = 'blocked'
                stage.last_reason_code = 'occurrence_due_elapsed'
                stage.version = int(stage.version or 1) + 1
            add_event(sup.plan_id, 'occurrence_due_unresolved', [
                'occurrence-due-unresolved', occurrence.id,
                occurrence.due_at,
            ], {
                'occurrence_id': occurrence.id,
                'test_task_id': occurrence.test_task_id,
                'checkpoint': occurrence.checkpoint_json or {},
                'resume_contract': occurrence.resume_contract_json or {},
                'conclusion': 'ANALYSIS_INCOMPLETE',
            }, now)
            changed += 1
            continue
        if (occurrence.status == 'waiting_condition'
                and occurrence.next_probe_at
                and occurrence.next_probe_at <= now):
            occurrence.recommended_action = (
                'owner_action_then_auto_resume' if occurrence.owner_gate
                else 'worker_probe_required')
            occurrence.next_action = (
                'wait_owner_and_probe' if occurrence.owner_gate
                else 'await_probe_event')
            add_event(sup.plan_id, 'occurrence_probe_overdue', [
                'occurrence-probe-overdue', occurrence.id,
                occurrence.next_probe_at,
            ], {
                'occurrence_id': occurrence.id,
                'test_task_id': occurrence.test_task_id,
                'next_probe_at': occurrence.next_probe_at.isoformat() + '+08:00',
                'owner_gate': bool(occurrence.owner_gate),
            }, now)
    ordinary = _plan_agent_tasks(sup)
    direct_rows = (TestTask.query.filter_by(
        plan_id=sup.plan_id, schedule_enabled=False,
        status='waiting_condition').order_by(TestTask.id).all())
    for task in direct_rows:
        stage = MissionStage.query.filter_by(
            mission_id=sup.mission_id,
            stage_key='test_task_%s' % task.id,
        ).order_by(MissionStage.stage_version.desc()).first()
        due_at = _direct_task_due_at(task)
        if due_at and due_at <= now:
            latest_task = ordinary.get(stage.id) if stage else None
            if latest_task and latest_task.status == 'waiting_condition':
                latest_task.status = 'blocked'
                latest_task.terminal_reason = 'test_task_due_elapsed'
                latest_task.completed_at = now
                latest_task.version = int(latest_task.version or 0) + 1
            task.status = 'blocked'
            task.condition_state = 'due_elapsed'
            task.next_probe_at = None
            task.next_check_at = None
            task.owner_gate = False
            task.recommended_action = 'publish_partial_closeout'
            task.next_action = 'none'
            if stage:
                stage.state = 'blocked'
                stage.last_reason_code = 'test_task_due_elapsed'
                stage.version = int(stage.version or 1) + 1
            add_event(sup.plan_id, 'task_due_unresolved', [
                'task-due-unresolved', task.id, due_at,
            ], {
                'test_task_id': task.id,
                'checkpoint': task.checkpoint_json or {},
                'resume_contract': task.resume_contract_json or {},
                'conclusion': 'ANALYSIS_INCOMPLETE',
            }, now)
            changed += 1
            continue
        if task.next_probe_at and task.next_probe_at <= now:
            task.recommended_action = (
                'owner_action_then_auto_resume' if task.owner_gate
                else 'worker_probe_required')
            task.next_action = (
                'wait_owner_and_probe' if task.owner_gate
                else 'await_probe_event')
            add_event(sup.plan_id, 'task_probe_overdue', [
                'task-probe-overdue', task.id, task.next_probe_at,
            ], {
                'test_task_id': task.id,
                'next_probe_at': task.next_probe_at.isoformat() + '+08:00',
                'owner_gate': bool(task.owner_gate),
            }, now)
    return changed


def _candidate_assignment_match(sup, stage, occurrence, member, claw):
    """Validate one replacement against the frozen occurrence contract."""
    snapshot = stage.input_snapshot_json or {}
    assignment = snapshot.get('team_assignment') or {}
    required = set(
        (occurrence.required_capabilities_json if occurrence else None)
        or snapshot.get('required_capabilities') or
        ([assignment.get('specialty')] if assignment.get('specialty') else []))
    specialties = set(member.specialties_json or [])
    fallback_ids = set(
        (occurrence.allowed_fallback_claw_ids_json if occurrence else None)
        or snapshot.get('allowed_fallback_claw_ids') or [])
    resources = dict(
        (occurrence.required_resources_json if occurrence else None)
        or snapshot.get('required_resources') or {})
    if member.role_key != stage.role_key:
        return False, {'reason': 'role_mismatch'}
    if fallback_ids and claw.id not in fallback_ids:
        return False, {'reason': 'fallback_not_allowed'}
    if not required.issubset(specialties):
        return False, {
            'reason': 'capability_mismatch',
            'required_capabilities': sorted(required),
            'candidate_capabilities': sorted(specialties),
        }
    explicit_ids = resources.get('allowed_claw_ids') or resources.get(
        'executor_claw_ids') or []
    if explicit_ids and claw.id not in {
            int(item) for item in explicit_ids
            if not isinstance(item, bool) and str(item).isdigit()}:
        return False, {'reason': 'resource_executor_mismatch'}
    bound_claw = resources.get('claw_id')
    if bound_claw not in (None, ''):
        try:
            bound_claw_id = int(bound_claw)
        except (TypeError, ValueError):
            return False, {'reason': 'resource_bound_claw_invalid'}
        if bound_claw_id != claw.id:
            return False, {'reason': 'resource_bound_to_other_claw'}
    nonportable = set(resources).intersection({
        'device_id', 'device_ids', 'account_id', 'account_ids',
        'resource_lease_id', 'resource_lease_ids',
    })
    if nonportable and not explicit_ids and resources.get('portable') is not True:
        return False, {'reason': 'resource_binding_not_portable'}
    worker_platform = str(resources.get('worker_platform') or '').lower()
    if worker_platform:
        sidecar = db.session.get(ClawSidecarConfig, claw.id)
        actual_platform = str(
            (sidecar.runtime_config_json or {}).get('platform')
            if sidecar else '').lower()
        if actual_platform != worker_platform:
            return False, {
                'reason': 'worker_platform_mismatch',
                'required_platform': worker_platform,
                'candidate_platform': actual_platform,
            }
    return True, {
        'role_key': member.role_key,
        'required_capabilities': sorted(required),
        'candidate_capabilities': sorted(specialties),
        'required_resources': resources,
    }


def _alternate_executor(sup, stage, current_claw_id, occurrence=None):
    if not sup.team_id:
        return None, {'reason': 'team_missing'}
    rows = AgentTeamMember.query.filter_by(
        team_id=sup.team_id, role_key=stage.role_key).order_by(
            AgentTeamMember.claw_id).all()
    members = {row.claw_id: row for row in rows
               if row.claw_id != current_claw_id}
    candidates = sorted(members)
    plan = db.session.get(TestPlan, sup.plan_id)
    if not plan:
        return None, {'reason': 'plan_missing'}
    claws = {row.id: row for row in OpenClawInstance.query.filter(
        OpenClawInstance.id.in_(candidates),
        OpenClawInstance.status != 'deleted',
        OpenClawInstance.project_id == plan.project_id).all()} if candidates else {}
    rejected = []
    for claw_id in candidates:
        claw = claws.get(claw_id)
        if not claw:
            rejected.append({'claw_id': claw_id, 'reason': 'claw_unavailable'})
            continue
        matched, detail = _candidate_assignment_match(
            sup, stage, occurrence, members[claw_id], claw)
        if matched:
            return claw_id, detail
        rejected.append({'claw_id': claw_id, **detail})
    return None, {'reason': 'no_capability_equivalent_executor',
                  'rejected_candidates': rejected}


def advance_occurrence_actions(sup, now=None):
    """Execute deterministic recovery actions after ordinary retries exhaust."""
    if not sup or not sup.mission_id:
        return []
    now = now or _now()
    if mission_expiry_state(sup, now):
        return []
    wake_ids = []
    rows = (TestTaskOccurrence.query.filter(
        TestTaskOccurrence.plan_id == sup.plan_id,
        TestTaskOccurrence.status.in_(('blocked', 'failed')),
        TestTaskOccurrence.owner_gate.is_(False),
        TestTaskOccurrence.next_check_at.isnot(None),
        TestTaskOccurrence.next_check_at <= now,
    ).order_by(TestTaskOccurrence.id).with_for_update().all())
    for occurrence in rows:
        action = occurrence.next_action or occurrence.recommended_action
        task = db.session.get(TestTask, occurrence.test_task_id)
        stage = db.session.get(MissionStage, occurrence.mission_stage_id)
        if not task or not stage:
            continue
        if action == 'reassign_stage':
            previous_claw_id = occurrence.assignee_claw_id
            alternate, match = _alternate_executor(
                sup, stage, previous_claw_id, occurrence)
            if not alternate:
                occurrence.owner_gate = True
                occurrence.recommended_action = 'owner_select_executor'
                occurrence.next_action = 'owner_select_executor'
                occurrence.next_check_at = None
                add_event(sup.plan_id, 'occurrence_owner_gate', [
                    'occurrence-owner-gate-no-executor', occurrence.id,
                ], {
                    'occurrence_id': occurrence.id,
                    'test_task_id': task.id,
                    'reason': 'no_capability_equivalent_executor',
                    'capability_match': match,
                }, now)
                continue
            occurrence.action_attempt_count = int(
                occurrence.action_attempt_count or 0) + 1
            occurrence.assignee_claw_id = alternate
            occurrence.status = 'ready'
            occurrence.next_action = 'auto_dispatch'
            occurrence.next_check_at = now
            stage.assigned_claw_id = alternate
            snapshot = dict(stage.input_snapshot_json or {})
            team_assignment = dict(snapshot.get('team_assignment') or {})
            team_assignment.update({
                'assigned_claw_id': alternate,
                'reassigned_from_claw_id': previous_claw_id,
                'matched_capabilities': match.get(
                    'candidate_capabilities', []),
            })
            snapshot['team_assignment'] = team_assignment
            stage.input_snapshot_json = snapshot
            stage.state = 'ready'
            stage.last_reason_code = 'occurrence_reassigned'
            stage.version = int(stage.version or 1) + 1
            if occurrence.execution_mode == 'workflow':
                dispatched = _dispatch_scheduled_workflow(
                    sup, task, stage, occurrence, now)
                wake_ids.append(alternate)
                execution_ref = {'workflow_run_id': dispatched.id}
            else:
                dispatched = _create_plan_agent_task(
                    sup, task, stage,
                    'reassign:%s:%s' % (
                        occurrence.id, occurrence.action_attempt_count),
                    '前一执行者已耗尽有界重试。继续同一当日目标，并保留已有证据。',
                    2, occurrence=occurrence)
                wake_ids.append(dispatched.claw_id)
                execution_ref = {'agent_task_id': dispatched.task_id}
            metadata = dict(occurrence.action_metadata_json or {})
            metadata['last_reassignment'] = {
                'from_claw_id': previous_claw_id,
                'to_claw_id': alternate,
                'reason': 'bounded_retry_exhausted',
                'capability_match': match,
                'fencing_token': int(stage.fencing_token or 0),
                **execution_ref,
            }
            occurrence.action_metadata_json = metadata
            add_event(sup.plan_id, 'occurrence_reassigned', [
                'occurrence-reassigned', occurrence.id,
                previous_claw_id, alternate,
                int(stage.fencing_token or 0),
            ], {
                'occurrence_id': occurrence.id,
                'test_task_id': task.id,
                **metadata['last_reassignment'],
            }, now)
            continue
        if action == 'manager_review':
            key = 'occurrence_%s_manager_review' % occurrence.id
            control = MissionStage.query.filter_by(
                mission_id=sup.mission_id, stage_key=key,
                stage_version=1).first()
            if not control:
                control = MissionStage(
                    mission_id=sup.mission_id, stage_key=key,
                    stage_version=1, role_key='test_manager',
                    assigned_claw_id=sup.orchestrator_claw_id,
                    state='ready',
                    input_snapshot_json={
                        'kind': 'manager_review',
                        'source_stage_id': stage.id,
                        'source_occurrence_id': occurrence.id,
                        'test_task_id': task.id,
                        'test_task_name': task.name,
                        'current_occurrence': occurrence.to_dict(),
                        'failure': occurrence.action_metadata_json or {},
                        'allowed_decisions': sorted(
                            MANAGER_REVIEW_DECISIONS),
                        'decision_contract': {
                            'decision': 'required',
                            'evidence': 'required',
                            'resume_contract': (
                                'required_when_decision_is_wait_condition'),
                            'checkpoint': (
                                'required_when_decision_is_wait_condition'),
                        },
                    },
                    evidence_refs_json=[])
                db.session.add(control)
                db.session.flush()
            active = _plan_control_tasks(sup).get(control.id)
            if active and active.status in (
                    'pending', 'running', 'waiting_condition'):
                occurrence.next_action = 'await_manager_review'
                occurrence.next_check_at = None
                wake_ids.append(active.claw_id)
                continue
            if (active and active.status == 'completed'
                    and (occurrence.action_metadata_json or {}).get(
                        'manager_review_receipt')):
                # A completed, fenced manager decision is final for this
                # occurrence. Never manufacture another review merely because
                # a stale supervisor snapshot still says manager_review.
                occurrence.next_check_at = None
                continue
            occurrence.action_attempt_count = int(
                occurrence.action_attempt_count or 0) + 1
            dispatched = _create_control_agent_task(
                sup, control, 'manager_review',
                '复核当前执行实例，并从 input_snapshot.allowed_decisions '
                '中选择唯一 decision；返回结构化 evidence，禁止用文字替代决策。',
                source_occurrence_id=occurrence.id)
            occurrence.next_action = 'await_manager_review'
            occurrence.next_check_at = None
            metadata = dict(occurrence.action_metadata_json or {})
            metadata['manager_review_control'] = {
                'mission_stage_id': control.id,
                'agent_task_id': dispatched.task_id,
                'manager_claw_id': dispatched.claw_id,
                'created_at': now.isoformat() + '+08:00',
            }
            occurrence.action_metadata_json = metadata
            add_event(sup.plan_id, 'manager_review_requested', [
                'manager-review-requested', occurrence.id,
                dispatched.task_id,
            ], {
                'occurrence_id': occurrence.id,
                'test_task_id': task.id,
                **metadata['manager_review_control'],
            }, now)
            wake_ids.append(dispatched.claw_id)
            continue
        if action not in ('create_environment_repair',
                           'create_evidence_review'):
            continue
        kind = ('environment_repair' if action == 'create_environment_repair'
                else 'evidence_review')
        key = 'occurrence_%s_%s' % (occurrence.id, kind)
        control = MissionStage.query.filter_by(
            mission_id=sup.mission_id, stage_key=key,
            stage_version=1).first()
        if not control:
            control = MissionStage(
                mission_id=sup.mission_id, stage_key=key,
                stage_version=1,
                role_key=stage.role_key,
                assigned_claw_id=occurrence.assignee_claw_id,
                state='ready',
                input_snapshot_json={
                    'kind': kind,
                    'source_stage_id': stage.id,
                    'source_occurrence_id': occurrence.id,
                    'test_task_id': task.id,
                    'checkpoint': occurrence.checkpoint_json or {},
                    'failure': occurrence.action_metadata_json or {},
                    'rule': ('diagnose_then_require_owner_approval'
                             if kind == 'environment_repair'
                             else 'supplement_evidence_without_rerun'),
                },
                evidence_refs_json=[])
            db.session.add(control)
            db.session.flush()
        active = _plan_control_tasks(sup).get(control.id)
        if active and active.status in (
                'pending', 'running', 'waiting_condition'):
            occurrence.next_action = 'await_%s' % kind
            occurrence.next_check_at = None
            wake_ids.append(active.claw_id)
            continue
        if active and active.status == 'completed':
            # Completed repair/evidence proposals are immutable for the source
            # occurrence. An Owner must approve a new exact action instead of
            # the supervisor replaying the same generic control task.
            occurrence.next_check_at = None
            continue
        occurrence.action_attempt_count = int(
            occurrence.action_attempt_count or 0) + 1
        instruction = (
            '只读诊断环境问题并返回候选 diff、目标仓库/分支、影响范围和回滚计划；'
            '禁止修改文件、commit、push、发布、部署或调用共享写接口。'
            if kind == 'environment_repair' else
            '仅补齐或复核缺失证据，不重跑已通过范围。')
        dispatched = _create_control_agent_task(
            sup, control, kind, instruction,
            source_occurrence_id=occurrence.id)
        occurrence.next_action = 'await_%s' % kind
        occurrence.next_check_at = dispatched.created_at or now
        wake_ids.append(dispatched.claw_id)
    return sorted(set(wake_ids))


def dispatch_ready_control_stages(sup, now=None):
    """Make recovery stages real work instead of data-only ready rows."""
    if not sup or not sup.mission_id:
        return []
    now = now or _now()
    if mission_expiry_state(sup, now):
        return []
    active = _plan_control_tasks(sup)
    wake_ids = []
    rows = MissionStage.query.filter_by(
        mission_id=sup.mission_id, state='ready').order_by(
            MissionStage.id).with_for_update().all()
    for stage in rows:
        snapshot = stage.input_snapshot_json or {}
        kind = snapshot.get('kind')
        if kind not in ('workflow_execution_reconciliation',
                        'environment_repair', 'evidence_review',
                        'manager_review'):
            continue
        current = active.get(stage.id)
        if current and current.status in (
                'pending', 'running', 'waiting_condition'):
            continue
        instruction = {
            'workflow_execution_reconciliation': (
                '核对执行已停止、副作用已对账，并按 input_snapshot.resolve_api '
                '提交 execution_stopped、side_effects_reconciled、receipt_ref；'
                '成功后再调用 recover_api。'),
            'environment_repair': (
                '只读诊断环境问题并返回候选 diff、目标仓库/分支、影响范围和回滚计划；'
                '禁止修改文件、commit、push、发布、部署或调用共享写接口。'),
            'evidence_review': '补证或复核，不重跑已通过范围。',
            'manager_review': (
                '复核来源 occurrence，并从 allowed_decisions 中返回唯一 '
                'decision 与结构化 evidence。'),
        }[kind]
        task = _create_control_agent_task(
            sup, stage, kind, instruction,
            source_occurrence_id=snapshot.get('source_occurrence_id'))
        active[stage.id] = task
        wake_ids.append(task.claw_id)
    return sorted(set(wake_ids))


def _queue_block_notifications(sup, source_key, summary, now=None,
                               test_task_id=None, agent_task_id=None):
    """Persist independent WeCom and Team-chat notifications for a blocker."""
    now = now or _now()
    if not sup or not sup.orchestrator_claw_id:
        return {'wecom_message_id': None, 'chat_message_id': None}
    stable = hashlib.sha256(
        ('%s:%s:%s' % (sup.plan_id, source_key, summary)).encode('utf-8')
    ).hexdigest()[:24]
    notification_id = 'plan-block:%s:%s' % (sup.plan_id, stable)
    text = '计划 #%s 出现执行阻断' % sup.plan_id
    if test_task_id:
        text += '，任务 #%s' % test_task_id
    text += '：%s' % (str(summary or '请经理处理')[:2000])
    payload = json.dumps({
        'schema': 1,
        'notification_id': notification_id,
        'plan_id': sup.plan_id,
        'test_task_id': test_task_id,
        'agent_task_id': agent_task_id,
        'text': text,
    }, ensure_ascii=False, sort_keys=True)
    wecom = ClawMessage.query.filter_by(
        claw_id=sup.orchestrator_claw_id,
        msg_type='owner_notification',
        direction='to_claw',
        content=payload,
    ).first()
    if wecom is None:
        wecom = ClawMessage(
            claw_id=sup.orchestrator_claw_id,
            sender_name='Hub Plan Supervisor',
            msg_type='owner_notification',
            direction='to_claw',
            status='pending',
            content=payload,
        )
        db.session.add(wecom)
        db.session.flush()

    event_key = digest([
        'block-notification', notification_id,
    ])
    add_event(sup.plan_id, 'block_notification_queued', [
        'block-notification', notification_id,
    ], {
        'notification_id': notification_id,
        'wecom_message_id': wecom.id,
        'chat_message_id': None,
        'chat_status': 'pending' if sup.team_id else 'not_applicable',
        'chat_attempts': 0,
        'text': text,
        'test_task_id': test_task_id,
        'agent_task_id': agent_task_id,
        'channels': ['agent_wecom_outbox', 'team_chat'],
    }, now)
    event = PlanSupervisorEvent.query.filter_by(
        plan_id=sup.plan_id, event_key=event_key).first()
    if event and sup.team_id:
        _deliver_block_chat(sup, event, now)
    return {
        'notification_id': notification_id,
        'wecom_message_id': wecom.id,
        'chat_message_id': (event.payload_json or {}).get('chat_message_id') if event else None,
    }


def _deliver_block_chat(sup, event, now=None):
    """Retry one channel independently; a chat failure cannot drop WeCom."""
    now = now or _now()
    payload = dict(event.payload_json or {})
    if payload.get('chat_status') in ('sent', 'not_applicable'):
        return
    attempts = int(payload.get('chat_attempts') or 0)
    retry_at = payload.get('chat_next_attempt_at')
    if retry_at and datetime.fromisoformat(retry_at) > now:
        return
    try:
        with db.session.begin_nested():
            team = db.session.get(AgentTeam, sup.team_id)
            if not team or team.status != 'active':
                raise LookupError('team_not_active')
            from app.services.agent_team_chat import sync_team_room
            from app.services.chat_rooms import post_message
            room = sync_team_room(team)
            sender = ChatRoomMember.query.filter_by(
                room_id=room.id,
                claw_id=sup.orchestrator_claw_id,
                status='active',
            ).first()
            if sender is None:
                raise LookupError('manager_room_member_missing')
            message, _ = post_message(
                room, sender,
                '[Hub阻断通知] ' + str(payload.get('text') or ''),
                payload['notification_id'], [], commit=False,
            )
        payload.update(chat_status='sent', chat_message_id=message.id,
                       chat_attempts=attempts + 1, chat_next_attempt_at=None)
    except Exception as exc:
        payload.update(
            chat_status='retryable', chat_attempts=attempts + 1,
            chat_error=type(exc).__name__,
            chat_next_attempt_at=(now + timedelta(seconds=min(
                900, 30 * (2 ** min(attempts, 5))))).isoformat(),
        )
        current_app.logger.warning(
            'Plan block chat notification deferred: plan_id=%s event_id=%s error=%s',
            sup.plan_id, event.id, type(exc).__name__,
        )
    event.payload_json = payload


def record_agent_task_terminal(agent_task, result, status, now=None):
    control = _agent_task_control_link(agent_task)
    if control:
        return _record_control_terminal(
            agent_task, control, result, status, now or _now())
    link = _agent_task_plan_link(agent_task)
    if not link:
        return None
    now = now or _now()
    task = db.session.get(TestTask, link['test_task_id'])
    stage = db.session.get(MissionStage, link['mission_stage_id'])
    occurrence = _linked_occurrence(link)
    sup = locked(link['plan_id'])
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not task or not stage or not sup
            or task.plan_id != link['plan_id']
            or stage.mission_id != link['mission_id']
            or agent_task.claw_id != expected_assignee
            or stage.assigned_claw_id != agent_task.claw_id):
        fail('PLAN_AGENT_TASK_LINK_INVALID', 'AgentTask 关联计划已不存在', 409)
    result = result if isinstance(result, dict) else {}
    provider_status = str(result.get('status') or '').lower()
    summary = str(result.get('summary') or result.get('reason') or '')[:8000]
    if status == 'completed' and provider_status == 'skipped':
        projected, stage.state = 'skipped', 'skipped'
    elif status == 'completed':
        projected, stage.state = 'completed', 'completed'
    elif status == 'blocked':
        projected, stage.state = 'blocked', 'blocked'
    else:
        projected, stage.state = 'failed', 'failed'
    if occurrence:
        occurrence.status = projected
        occurrence.condition_state = (
            '' if projected in ('completed', 'skipped') else projected)
        occurrence.result_summary = summary
        occurrence.last_heartbeat_at = (
            agent_task.last_heartbeat_at or now)
        if projected in ('completed', 'skipped'):
            occurrence.recommended_action = 'none'
            occurrence.next_action = 'none'
            occurrence.next_check_at = None
            occurrence.owner_gate = False
        else:
            _apply_terminal_action(
                occurrence, result.get('error_code') or status,
                summary, now)
        occurrence.attempt_count = max(
            int(occurrence.attempt_count or 0),
            int(agent_task.attempt_no or 0), 1)
    else:
        task.status = ('blocked' if projected == 'failed' else projected)
        if projected == 'completed':
            task.progress = 100
        task.result_summary = summary
        task.condition_state = (
            '' if projected in ('completed', 'skipped') else projected)
        task.last_heartbeat_at = agent_task.last_heartbeat_at or now
        task.next_probe_at = None
        task.next_check_at = None
        task.owner_gate = False
        task.recommended_action = (
            'none' if projected in ('completed', 'skipped') else 'manager_review')
        task.next_action = (
            'none' if projected in ('completed', 'skipped') else 'manager_review')
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
        if occurrence:
            occurrence.evidence_refs_json = list(stage.evidence_refs_json)
    stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_agent_terminal', [
        'task-agent-terminal', agent_task.task_id,
        int(agent_task.attempt_no or 0), status,
    ], {
        'task_id': task.id,
        'occurrence_id': occurrence.id if occurrence else None,
        'agent_task_id': agent_task.task_id,
        'status': status,
        'test_task_status': task.status,
        'occurrence_status': occurrence.status if occurrence else None,
        'summary': summary,
    }, now)
    if projected not in ('completed', 'skipped'):
        _queue_block_notifications(
            sup,
            'agent-task:%s:attempt:%s' % (
                agent_task.task_id, int(agent_task.attempt_no or 0)),
            summary or stage.last_reason_code,
            now=now,
            test_task_id=task.id,
            agent_task_id=agent_task.task_id,
        )
        # The event is urgent and the next pump must create a fresh manager
        # Turn immediately, independent of the ordinary schedule tick.
        sup.next_check_at = now
    if occurrence and occurrence.owner_gate:
        add_event(sup.plan_id, 'occurrence_owner_gate', [
            'occurrence-owner-gate-terminal', occurrence.id,
            int(agent_task.attempt_no or 0),
        ], {
            'occurrence_id': occurrence.id,
            'test_task_id': task.id,
            'recommended_action': occurrence.recommended_action,
            'summary': summary,
        }, now)
    if projected in ('completed', 'skipped'):
        refresh_task_dependency_stages(sup, now=now)
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
        occurrence_id = _stage_occurrence_id(stage)
        occurrence = (db.session.get(TestTaskOccurrence, occurrence_id)
                      if occurrence_id else None)
        if not agent_task or not task:
            continue
        if agent_task.status == 'pending':
            if ((occurrence and occurrence.status == 'running')
                    or (not occurrence and task.status == 'in_progress')
                    or stage.state == 'running'):
                record_agent_task_retry_pending(agent_task, now=now)
                count += 1
            continue
        if agent_task.status == 'running':
            if (agent_task.lease_expires_at
                    and agent_task.lease_expires_at <= now):
                continue
            if ((occurrence and occurrence.status != 'running')
                    or (not occurrence and task.status != 'in_progress')
                    or stage.state != 'running'):
                record_agent_task_claim(agent_task, now=now)
                count += 1
            continue
        if agent_task.status == 'waiting_condition':
            truth_status = occurrence.status if occurrence else task.status
            if truth_status == 'waiting_condition' and stage.state == 'waiting_condition':
                continue
            try:
                waiting_result = json.loads(agent_task.result or '{}')
            except (TypeError, ValueError):
                waiting_result = {}
            if isinstance(waiting_result, dict):
                record_agent_task_waiting_condition(
                    agent_task, waiting_result, now=now)
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
        truth_status = occurrence.status if occurrence else task.status
        expected_status = projected_status if occurrence else task_status
        if truth_status == expected_status and stage.state == stage_state:
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
    stages = MissionStage.query.filter(
        MissionStage.mission_id == sup.mission_id,
        MissionStage.state.in_((
            'ready', 'dispatched', 'running', 'blocked')),
    ).order_by(MissionStage.id.desc()).all()
    recovery_stage = next((stage for stage in stages
        if (stage.input_snapshot_json or {}).get('kind') ==
           'workflow_execution_reconciliation'), None)
    if recovery_stage:
        snapshot = recovery_stage.input_snapshot_json or {}
        owner = db.session.get(
            OpenClawInstance, recovery_stage.assigned_claw_id)
        control = _plan_control_tasks(sup).get(recovery_stage.id)
        return {
            'recovery_state': (
                'reconciliation_running'
                if recovery_stage.state == 'running'
                else 'reconciliation_dispatched'
                if recovery_stage.state == 'dispatched'
                else 'reconciliation_required'),
            'reconciliation_owner': {
                'claw_id': recovery_stage.assigned_claw_id,
                'name': owner.name if owner else '',
            },
            'conflicting_run_id': snapshot.get('workflow_run_id'),
            'allowed_actions': ['wait', 'submit_reconciliation_receipt'],
            'next_check_at': empty['next_check_at'],
            'reconciliation_stage': recovery_stage.to_dict(
                include_input=True),
            'agent_task': control.to_dict() if control else None,
        }
    return empty


def _manager_stage_actions(sup, claw_id, body, remaining, now):
    """Validate and apply manager-selected effects inside the decision txn.

    Deliberately keep policy broad: Hub does not rank work or interpret the
    manager's reason.  It only proves that every referenced stage belongs to
    this plan and that concrete effects use the existing fenced/idempotent
    APIs.  ``defer`` and stage-local ``block`` remain non-terminal so ordinary
    environmental waits never destroy executable work.
    """
    contract = body.get('manager_contract')
    actions = body.get('stage_actions')
    if contract != MANAGER_DECISION_CONTRACT:
        return [], [], None
    if actions is None:
        actions = []
    if (not isinstance(actions, list) or len(actions) > 100
            or any(not isinstance(item, dict) for item in actions)):
        fail('PLAN_STAGE_ACTIONS_INVALID', 'stage_actions 须为不超过 100 项的对象数组', 400)
    remaining_by_key = {item['stage_key']: item for item in remaining}
    seen = set()
    normalized = []
    for raw in actions:
        stage_key = raw.get('stage_key')
        action = raw.get('action')
        reason = str(raw.get('reason') or '').strip()
        if (not isinstance(stage_key, str) or stage_key not in remaining_by_key
                or stage_key in seen):
            fail('PLAN_STAGE_ACTION_SCOPE_INVALID',
                 'stage_action 必须且只能引用本计划本轮一个未派发 Stage', 409)
        if action not in MANAGER_STAGE_ACTIONS:
            fail('PLAN_STAGE_ACTION_INVALID',
                 'stage action 仅支持 dispatch/defer/block/complete', 400)
        if not reason or len(reason) > 2000:
            fail('PLAN_STAGE_ACTION_REASON_REQUIRED',
                 '每个 stage action 必须提供不超过 2000 字的理由', 400)
        seen.add(stage_key)
        normalized.append((remaining_by_key[stage_key], raw, action, reason))
    if remaining_by_key and set(remaining_by_key) != seen:
        fail('SUPERVISION_ACTIONS_UNCOMMITTED',
             '本轮须为每个 ready Stage 选择 dispatch/defer/block/complete；'
             '可自由延期，但不能静默遗漏', 409)

    receipts = []
    wake_ids = []
    next_checks = []
    commitments = []
    plan = db.session.get(TestPlan, sup.plan_id)
    for stage_info, raw, action, reason in normalized:
        stage_key = stage_info['stage_key']
        stage = MissionStage.query.filter_by(
            mission_id=sup.mission_id, stage_key=stage_key,
        ).with_for_update().first()
        if not stage or stage.state != 'ready':
            fail('PLAN_STAGE_STATE_CONFLICT', 'Stage 已被其他动作推进，请回读后重试', 409)
        action_receipt = {
            'stage_key': stage_key,
            'action': action,
            'reason': reason,
        }
        commitment = {
            **action_receipt,
            'status': 'pending',
            'decided_at': now.isoformat() + '+08:00',
            'decision_command_key': body.get('command_key'),
        }
        if action == 'dispatch':
            task_id = stage_info.get('test_task_id')
            if type(task_id) is not int:
                fail('PLAN_STAGE_DISPATCH_UNSUPPORTED',
                     '该 Stage 不能通过普通 AgentTask 派发；请先调用其受控 Flow/控制动作接口', 409)
            dispatched = dispatch_test_task(sup, claw_id, task_id, {
                'command_key': '%s:%s' % (body['command_key'], stage_key),
                'occurrence_id': stage_info.get('test_task_occurrence_id'),
                'instruction': str(raw.get('instruction') or '')[:4000],
                'retry_max': raw.get('retry_max', 1),
            })
            wake_id = dispatched.pop('wake_claw_id', None)
            if wake_id:
                wake_ids.append(wake_id)
            action_receipt.update({
                'status': 'dispatched',
                'agent_task_id': (dispatched.get('agent_task') or {}).get('task_id'),
                'dispatch_receipt_id': dispatched.get('receipt_id'),
            })
            commitment.update(action_receipt)
            commitment['status'] = 'fulfilled'
        elif action in ('defer', 'block'):
            condition = str(raw.get('resume_condition') or '').strip()
            if not condition or len(condition) > 500:
                fail('PLAN_STAGE_RESUME_CONDITION_REQUIRED',
                     '延期或阶段阻断须提供可观察的 resume_condition', 400)
            raw_check = raw.get('next_check_at')
            check = (parse_time(raw_check) if raw_check is not None
                     else now + timedelta(minutes=30))
            if not now < check < ends_at(plan):
                fail('PLAN_SCHEDULE_OUT_OF_RANGE', 'Stage 复查时间须在未来且不晚于计划结束')
            next_checks.append(check)
            action_receipt.update({
                'status': 'deferred' if action == 'defer' else 'blocked_stage',
                'resume_condition': condition,
                'next_check_at': check.isoformat() + '+08:00',
            })
            commitment.update(action_receipt)
            add_event(sup.plan_id, 'manager_stage_%s' % action, [
                'manager-stage-action', body.get('command_key'), stage_key,
            ], action_receipt, now)
        else:  # complete
            task = db.session.get(TestTask, stage_info.get('test_task_id'))
            occurrence = db.session.get(
                TestTaskOccurrence, stage_info.get('test_task_occurrence_id'))
            verified = bool(
                (occurrence and occurrence.status in OCCURRENCE_TERMINAL)
                or (task and task.status in ('completed', 'skipped')))
            if not verified:
                fail('PLAN_STAGE_COMPLETION_UNVERIFIED',
                     'complete 只能收口已有终态事实，不能代替执行结果', 409)
            stage.state = 'completed'
            stage.last_reason_code = 'manager_verified_terminal_fact'
            stage.version = int(stage.version or 1) + 1
            action_receipt['status'] = 'completed'
            commitment.update(action_receipt)
            commitment['status'] = 'fulfilled'
            add_event(sup.plan_id, 'manager_stage_completed', [
                'manager-stage-completed', body.get('command_key'), stage_key,
            ], action_receipt, now)
        receipts.append(action_receipt)
        commitments.append(commitment)

    observations = dict(sup.observations_json or {})
    history = observations.get('manager_commitments')
    history = history if isinstance(history, list) else []
    observations['manager_commitments'] = (history + commitments)[-100:]
    sup.observations_json = observations
    return receipts, sorted(set(wake_ids)), (min(next_checks) if next_checks else None)


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
        plan_block_reason_code = body.get('plan_block_reason_code')
        plan_block_evidence = body.get('plan_block_evidence')
        if outcome == 'blocked' and block_scope == 'plan':
            if plan_block_reason_code not in PLAN_BLOCK_REASON_CODES:
                fail('PLAN_BLOCK_REASON_REQUIRED',
                     '计划级阻断必须提供受支持的 plan_block_reason_code', 400)
            if (not isinstance(plan_block_evidence, list)
                    or not plan_block_evidence
                    or any(not isinstance(item, dict) or not item
                           for item in plan_block_evidence)):
                fail('PLAN_BLOCK_EVIDENCE_REQUIRED',
                     '计划级阻断必须提供非空结构化 plan_block_evidence', 400)
            if PLAN_SCOPE_CONFLICT_RE.search(summary):
                fail('PLAN_BLOCK_SCOPE_CONFLICT',
                     '摘要声明局部/阶段级阻断或其他任务可继续，不能提交计划级阻断', 409)
        elif plan_block_reason_code is not None or plan_block_evidence is not None:
            fail('PLAN_DECISION_INVALID',
                 'plan_block_reason_code/evidence 仅用于计划级阻断', 400)
        remaining = undispatched_stages(sup)
        stage_action_receipts, wake_claw_ids, stage_next_check = (
            _manager_stage_actions(sup, claw_id, body, remaining, now))
        manager_audit, _manager_commitments = _manager_decision_audit(
            sup, body, now)
        remaining_after_actions = undispatched_stages(sup)
        local_block = bool(
            outcome == 'blocked' and block_scope != 'plan'
            and remaining_after_actions)
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
            closing = final_day_close_reached(sup, now)
            if body.get('next_check_at') is None and closing:
                check = None
            elif body.get('next_check_at') is None and outcome in ('degraded', 'retryable'):
                check = now + timedelta(seconds=60)
            else:
                check = parse_time(body.get('next_check_at'))
            if check is not None and not (
                    now < check < ends_at(db.session.get(TestPlan, sup.plan_id))):
                fail('PLAN_SCHEDULE_OUT_OF_RANGE', '下一次检查须在未来且不晚于计划结束')
            if (body.get('resume_condition') not in (
                    None, 'timer_or_event', 'end_of_plan')
                    or (outcome == 'wait'
                        and body.get('resume_condition') not in (
                            'timer_or_event', 'end_of_plan'))):
                fail('PLAN_RESUME_CONDITION_REQUIRED', '须声明 timer_or_event；重要事件可提前唤醒', 400)
            if check is None and not closing:
                fail('PLAN_SCHEDULE_OUT_OF_RANGE', '非最终日收口必须提供下一次检查时间', 400)
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
            'run_terminal', 'heartbeat_anomaly', 'stale',
            'occurrence_owner_gate', 'occurrence_due_unresolved',
            'supervision_report_updated', 'control_action_terminal')
            if kind in event_kinds)
        notification_receipt = None
        if notification_reasons:
            notification_receipt = _queue_block_notifications(
                sup,
                'manager-decision:%s' % body.get('command_key'),
                summary,
                now=now,
            )
        sup.acknowledged_cursor = sup.lease_cursor
        sup.last_decision_json = {
            'outcome': effective_outcome,
            'requested_outcome': outcome,
            'block_scope': effective_scope,
            'summary': summary,
            'plan_block_reason_code': plan_block_reason_code,
            'plan_block_evidence': plan_block_evidence or [],
            'manager_contract': body.get('manager_contract'),
            'stage_actions': stage_action_receipts,
            **manager_audit,
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
        if stage_next_check and (not sup.next_check_at or stage_next_check < sup.next_check_at):
            sup.next_check_at = stage_next_check
        sup.resume_condition = (
            'timer_or_event' if check else
            'end_of_plan' if final_day_close_reached(sup, now) else 'manual')
        observations = dict(sup.observations_json or {})
        goal = dict(observations.get('manager_goal') or {})
        if goal:
            goal['last_reviewed_at'] = now.isoformat() + '+08:00'
            goal['next_review_at'] = (
                sup.next_check_at.isoformat() + '+08:00'
                if sup.next_check_at else None)
            goal['current_priorities'] = [
                (item.get('stage_id') or item.get('stage_key'))
                for item in (
                    manager_audit.get('stage_decisions', [])
                    or stage_action_receipts)
                if (item.get('disposition') or item.get('action'))
                not in ('completed', 'complete')
            ][:50]
            observations['manager_goal'] = goal
            sup.observations_json = observations
        sup.lease_owner = None
        sup.lease_expires_at = None
        sup.turn_deadline_at = None
        sup.expired_turns = 0
        if local_block:
            add_event(sup.plan_id, 'stage_blocked_continuation', [
                'stage-blocked-continuation', body.get('command_key'),
            ], {
                'summary': summary,
                'undispatched_stage_keys': [
                    row['stage_key'] for row in remaining_after_actions],
                'rule': 'manager_decides_next_stage',
            }, now)
        return {
            'scheduled': bool(check),
            'requested_outcome': outcome,
            'effective_outcome': effective_outcome,
            'supervisor_status': sup.status,
            'block_scope': effective_scope,
            'continue_supervision': local_block,
            'undispatched_stages': remaining_after_actions,
            'stage_action_receipts': stage_action_receipts,
            **manager_audit,
            'wake_claw_ids': wake_claw_ids,
            'owner_notification': {
                'required': bool(notification_reasons),
                'reasons': notification_reasons,
                'event_kinds': event_kinds,
                'delivery': 'hub_dual_outbox',
                'channels': ['agent_wecom_outbox', 'team_chat'],
                'receipts': notification_receipt or {},
                'status': ('queued' if notification_reasons
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
        require_mission_not_expired(sup)
        require_lease(sup, claw_id, lease_credentials(body))
    return sup


def _stage_task_id(stage):
    value = (stage.input_snapshot_json or {}).get('test_task_id') if stage else None
    return value if type(value) is int and value > 0 else None


def _stage_occurrence_id(stage):
    value = ((stage.input_snapshot_json or {}).get(
        'test_task_occurrence_id') if stage else None)
    return value if type(value) is int and value > 0 else None


def task_dispatch_receipts(sup):
    """Read authoritative Mission/Run/claim state for supervised TestTasks."""
    if not sup or not sup.mission_id:
        return []
    ordinary_tasks = _plan_agent_tasks(sup)
    control_tasks = _plan_control_tasks(sup)
    items = []
    for stage in MissionStage.query.filter_by(mission_id=sup.mission_id).order_by(
            MissionStage.id).all():
        snapshot = stage.input_snapshot_json or {}
        control = control_tasks.get(stage.id)
        control_kind = snapshot.get('kind')
        task_id = _stage_task_id(stage)
        if control or control_kind in (
                'workflow_execution_reconciliation',
                'environment_repair', 'evidence_review',
                'manager_review'):
            if not control and control_kind not in (
                    'workflow_execution_reconciliation',
                    'environment_repair', 'evidence_review',
                    'manager_review'):
                continue
            control_claimed = bool(
                control and control.assigned_at
                and control.status != 'pending')
            items.append({
                'task_id': None,
                'source_test_task_id': task_id,
                'occurrence_id': snapshot.get('source_occurrence_id'),
                'stage_key': stage.stage_key,
                'stage_state': stage.state,
                'control_action': control_kind,
                'executor_claw_id': stage.assigned_claw_id,
                'workflow_run_id': stage.workflow_run_id,
                'agent_task_id': control.task_id if control else None,
                'agent_task_status': control.status if control else None,
                'execution_mode': 'plan_control_action' if control else None,
                'claimed': control_claimed,
                'claimed_by': (
                    'claw:%s' % control.claw_id if control_claimed else ''),
                'claimed_at': (
                    str(control.assigned_at)
                    if control_claimed and control.assigned_at else None),
                'claim_fencing_token': (
                    int(control.fencing_token or 0)
                    if control_claimed else None),
            })
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
            'occurrence_id': _stage_occurrence_id(stage),
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
    rows = [row for row in task_dispatch_receipts(sup)
            if row['task_id'] == task.id]
    return rows[-1] if rows else None


def require_task_dispatch_receipt(task):
    """A supervised TestTask becomes running only after a real Worker claim."""
    sup = locked(task.plan_id)
    if not sup:
        return None
    rows = [row for row in task_dispatch_receipts(sup)
            if row['task_id'] == task.id]
    receipt = rows[-1] if rows else None
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
    occurrence_id = _stage_occurrence_id(stage)
    occurrence = (db.session.get(TestTaskOccurrence, occurrence_id)
                  if occurrence_id else None)
    if not task_id:
        return None
    sup = PlanSupervisor.query.filter_by(mission_id=stage.mission_id).with_for_update().first()
    task = db.session.get(TestTask, task_id)
    expected_assignee = (
        occurrence.assignee_claw_id if occurrence else
        task.assignee_claw_id if task else None)
    if (not sup or not task or task.plan_id != sup.plan_id
            or expected_assignee != claw_id):
        return None
    if occurrence:
        occurrence.status = 'running'
        occurrence.workflow_run_id = run_id
        occurrence.attempt_count = max(
            int(occurrence.attempt_count or 0), 1)
        occurrence.last_heartbeat_at = now or _now()
        occurrence.next_action = 'await_result'
        occurrence.next_check_at = None
    elif task.status in ('assigned', 'pending'):
        task.status = 'in_progress'
        task.progress = max(1, int(task.progress or 0))
    if stage.state in ('ready', 'dispatched'):
        stage.state = 'running'
        stage.last_reason_code = 'workflow_claimed'
        stage.version = int(stage.version or 1) + 1
    add_event(sup.plan_id, 'task_claimed',
              ['task-claimed', task.id, run_id, claw_id, worker_id], {
                  'task_id': task.id,
                  'occurrence_id': occurrence.id if occurrence else None,
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
    occurrence_id = _stage_occurrence_id(stage)
    occurrence = (db.session.get(TestTaskOccurrence, occurrence_id)
                  if occurrence_id else None)
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
    changed = (stage.state != stage_state
               or (occurrence.status != stage_state if occurrence else
                   task.status != task_status)
               or (not occurrence and progress == 100
                   and int(task.progress or 0) != 100))
    stage.state = stage_state
    stage.last_reason_code = ('workflow_run_%s' % run.status)[:80]
    if changed:
        stage.version = int(stage.version or 1) + 1
    if occurrence:
        occurrence.status = stage_state
        occurrence.workflow_run_id = run.id
        if stage_state in ('completed', 'skipped'):
            occurrence.recommended_action = 'none'
            occurrence.next_action = 'none'
            occurrence.next_check_at = None
            occurrence.owner_gate = False
        else:
            _apply_terminal_action(
                occurrence, 'workflow_run_%s' % run.status,
                str(run.summary or ''), now)
        occurrence.last_heartbeat_at = now
        if run.summary:
            occurrence.result_summary = str(run.summary)[:8000]
    else:
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
        if occurrence:
            occurrence.evidence_refs_json = list(stage.evidence_refs_json)
    add_event(sup.plan_id, 'workflow_terminal_projected', [
        'workflow-terminal-projected', run.id, run.status,
    ], {
        'run_id': run.id, 'stage_key': stage.stage_key,
        'stage_state': stage.state, 'task_id': task.id,
        'test_task_status': task.status,
        'occurrence_id': occurrence.id if occurrence else None,
        'occurrence_status': occurrence.status if occurrence else None,
    }, now)
    if occurrence and occurrence.owner_gate:
        add_event(sup.plan_id, 'occurrence_owner_gate', [
            'workflow-owner-gate', run.id, occurrence.id,
        ], {
            'run_id': run.id,
            'occurrence_id': occurrence.id,
            'test_task_id': task.id,
            'recommended_action': occurrence.recommended_action,
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


def refresh_supervision_report(sup, now=None):
    """Refresh the deterministic Plan dashboard only when truth changed."""
    if not sup:
        return False
    now = now or _now()
    plan = db.session.get(TestPlan, sup.plan_id)
    if not plan:
        return False
    rows = (TestTaskOccurrence.query.filter_by(plan_id=sup.plan_id)
            .order_by(TestTaskOccurrence.occurrence_date,
                      TestTaskOccurrence.id).all())
    today = [row for row in rows if row.occurrence_date == now.date()]
    if not today:
        return False
    status_counts = {}
    for row in today:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    gaps = [{
        'occurrence_id': row.id,
        'task_id': row.test_task_id,
        'status': row.status,
        'recommended_action': row.recommended_action or '',
        'next_action': row.next_action or '',
        'next_check_at': str(row.next_check_at) if row.next_check_at else None,
        'owner_gate': bool(row.owner_gate),
    } for row in today if row.status not in ('completed', 'skipped')]
    report_truth = {
        'date': now.date().isoformat(),
        'plan_id': plan.id,
        'status_counts': status_counts,
        'gaps': gaps,
        'dispatches': task_dispatch_receipts(sup),
    }
    report_digest = digest(report_truth)
    observations = dict(sup.observations_json or {})
    if observations.get('supervision_report_digest') == report_digest:
        return False
    lines = [
        '<!-- PLAN_SUPERVISION_AUTOGEN_START -->',
        '## %s 自动监督快照' % now.date().isoformat(),
        '',
        '- 计划：#%s %s' % (plan.id, plan.name),
        '- 生成时间：%s +08:00' % now.strftime('%Y-%m-%d %H:%M:%S'),
        '- 当日实例：%s' % len(today),
        '- 状态：%s' % (', '.join(
            '%s=%s' % item for item in sorted(status_counts.items())) or '暂无'),
        '',
        '### 待处理项',
    ]
    if gaps:
        for row in gaps:
            lines.append(
                '- occurrence #{occurrence_id} / task #{task_id}：{status}；'
                'next={next_action}；owner_gate={owner_gate}'.format(**row))
    else:
        lines.append('- 无')
    lines.append('<!-- PLAN_SUPERVISION_AUTOGEN_END -->')
    section = '\n'.join(lines)

    def merge(existing):
        existing = existing or ''
        pattern = re.compile(
            r'<!-- PLAN_SUPERVISION_AUTOGEN_START -->.*?'
            r'<!-- PLAN_SUPERVISION_AUTOGEN_END -->', re.S)
        return (pattern.sub(section, existing) if pattern.search(existing)
                else (existing.rstrip() + '\n\n' + section).strip())

    legacy = TestPlanReport.query.filter_by(
        plan_id=plan.id,
        title='计划 #%s 自动监督看板' % plan.id).first()
    if not legacy:
        legacy = TestPlanReport(
            plan_id=plan.id,
            title='计划 #%s 自动监督看板' % plan.id,
            content=section, format='markdown', created_by='Hub Supervisor')
        db.session.add(legacy)
    else:
        legacy.content = merge(legacy.content)
        legacy.updated_at = now
    bound_id = observations.get('supervision_report_id')
    bound = db.session.get(TestReport, bound_id) if bound_id else None
    if bound and not bound.is_deleted and bound.project_id == plan.project_id:
        bound.content = merge(bound.content)
        bound.updated_at = now
        legacy.linked_test_report_id = bound.id
    db.session.flush()
    observations['supervision_report_digest'] = report_digest
    observations['supervision_report_updated_at'] = now.isoformat() + '+08:00'
    sup.observations_json = observations
    add_event(sup.plan_id, 'supervision_report_updated', [
        'supervision-report-updated', report_digest,
    ], {
        'plan_report_id': legacy.id,
        'test_report_id': bound.id if bound else None,
        'status_counts': status_counts,
        'gap_count': len(gaps),
    }, now)
    return True


def stage_truth_snapshot(sup):
    if not sup or not sup.mission_id:
        return {'independent_stage_status': [], 'actionable_gaps': [],
                'pending_control_actions': [], 'allowed_actions': [],
                'next_check_at': None}
    stages = MissionStage.query.filter_by(mission_id=sup.mission_id).order_by(
        MissionStage.id).all()
    ordinary_tasks = _plan_agent_tasks(sup)
    control_tasks = _plan_control_tasks(sup)
    latest_versions = {}
    for row in stages:
        latest_versions[row.stage_key] = max(
            latest_versions.get(row.stage_key, 0),
            int(row.stage_version or 1))
    rows, gaps, pending_controls = [], [], []
    can_create_attempt = False
    can_reassign = False
    can_request_help = False
    for stage in stages:
        task_id = _stage_task_id(stage)
        task = db.session.get(TestTask, task_id) if task_id else None
        occurrence_id = _stage_occurrence_id(stage)
        occurrence = (db.session.get(TestTaskOccurrence, occurrence_id)
                      if occurrence_id else None)
        ordinary = ordinary_tasks.get(stage.id)
        control = control_tasks.get(stage.id)
        active_task = ordinary or control
        snapshot = stage.input_snapshot_json or {}
        item = {
            'stage_id': stage.id,
            'stage_key': stage.stage_key,
            'stage_version': int(stage.stage_version or 1),
            'state': stage.state,
            'test_task_id': task_id,
            'test_task_status': task.status if task else None,
            'occurrence_id': occurrence_id,
            'occurrence_status': occurrence.status if occurrence else None,
            'occurrence_date': (str(occurrence.occurrence_date)
                                if occurrence else None),
            'workflow_run_id': stage.workflow_run_id,
            'control_action': snapshot.get('kind'),
            'agent_task_id': active_task.task_id if active_task else None,
            'agent_task_status': active_task.status if active_task else None,
            'attempt_no': int(active_task.attempt_no or 0) if active_task else None,
            'last_heartbeat_at': (
                str(active_task.last_heartbeat_at)
                if active_task and active_task.last_heartbeat_at else None),
            'next_action': occurrence.next_action if occurrence else None,
            'recommended_action': (
                occurrence.recommended_action if occurrence else None),
            'owner_gate': bool(occurrence.owner_gate) if occurrence else False,
            'condition_state': (
                occurrence.condition_state if occurrence else ''),
            'next_probe_at': (
                str(occurrence.next_probe_at)
                if occurrence and occurrence.next_probe_at else None),
            'next_check_at': (
                str(occurrence.next_check_at)
                if occurrence and occurrence.next_check_at else None),
            'executor_claw_id': stage.assigned_claw_id,
            'reason_code': stage.last_reason_code or '',
            'depends_on_task_ids': snapshot.get('depends_on_task_ids') or [],
            'dependencies': snapshot.get('dependencies') or {},
            'available_actions': [],
        }
        is_latest = int(stage.stage_version or 1) == latest_versions.get(
            stage.stage_key)
        if (is_latest and task and not task.schedule_enabled
                and stage.state == 'ready' and not (
                    active_task and active_task.status in (
                        'pending', 'running', 'waiting_condition'))):
            alternate, match = _alternate_executor(
                sup, stage, stage.assigned_claw_id)
            if alternate:
                item['available_actions'].append({
                    'action': 'reassign_ready_stage',
                    'method': 'POST',
                    'endpoint': (
                        '/api/v1/test-plans/%s/supervision/tasks/%s/reassign'
                        % (sup.plan_id, task.id)),
                    'test_task_id': task.id,
                    'expected_stage_version': int(stage.stage_version or 1),
                    'suggested_assignee_claw_id': alternate,
                    'capability_match': match,
                })
                can_reassign = True
        rows.append(item)
        if (snapshot.get('kind') in (
                'manager_review', 'workflow_execution_reconciliation',
                'environment_repair', 'evidence_review')
                and stage.state not in ('completed', 'cancelled')):
            pending_controls.append({
                'stage_key': stage.stage_key,
                'action_kind': snapshot.get('kind'),
                'source_occurrence_id': snapshot.get(
                    'source_occurrence_id'),
                'agent_task_id': active_task.task_id if active_task else None,
                'agent_task_status': (
                    active_task.status if active_task else None),
                'manager_claw_id': (
                    stage.assigned_claw_id
                    if snapshot.get('kind') == 'manager_review' else None),
                'allowed_decisions': snapshot.get(
                    'allowed_decisions') or [],
            })
        if (stage.state in ('blocked', 'failed', 'cancelled')
                or (occurrence and occurrence.owner_gate)
                or (occurrence and occurrence.status == 'waiting_condition'
                    and occurrence.next_probe_at
                    and occurrence.next_probe_at <= _now())):
            gap = dict(item)
            if (is_latest and task and not task.schedule_enabled
                    and stage.state in (
                        'ready', 'dispatched', 'blocked', 'failed',
                        'cancelled')
                    and not (active_task and active_task.status in (
                        'pending', 'running', 'waiting_condition'))):
                gap['available_actions'] = list(
                    gap.get('available_actions') or []) + [{
                        'action': 'create_terminal_task_attempt',
                        'method': 'POST',
                        'endpoint': (
                            '/api/v1/test-plans/%s/supervision/tasks/%s/'
                            'new-attempt' % (sup.plan_id, task.id)),
                        'test_task_id': task.id,
                        'expected_stage_version': int(
                            stage.stage_version or 1),
                    }]
                can_create_attempt = True
                alternate, match = _alternate_executor(
                    sup, stage, stage.assigned_claw_id)
                if alternate:
                    gap['available_actions'].append({
                        'action': 'create_reassigned_task_attempt',
                        'method': 'POST',
                        'endpoint': (
                            '/api/v1/test-plans/%s/supervision/tasks/%s/'
                            'new-attempt' % (sup.plan_id, task.id)),
                        'test_task_id': task.id,
                        'expected_stage_version': int(
                            stage.stage_version or 1),
                        'suggested_assignee_claw_id': alternate,
                        'capability_match': match,
                    })
                    can_reassign = True
                gap['available_actions'].append({
                    'action': 'request_owner_assistance',
                    'contract': 'decision.owner_notification.required=true',
                    'test_task_id': task.id,
                })
                can_request_help = True
            gaps.append(gap)
    actions = ['wait', 'dispatch_ready_stage', 'recover_run']
    if can_create_attempt:
        actions.append('create_terminal_task_attempt')
    if can_reassign:
        actions.append('reassign_ready_stage')
        actions.append('create_reassigned_task_attempt')
    if can_request_help:
        actions.append('request_owner_assistance')
    if any(row['action_kind'] == 'manager_review'
           for row in pending_controls):
        actions.append('review_manager_action')
    if sup.status in OWNER_GATE_STATUSES:
        actions = ['wait_for_owner_resume']
    return {
        'independent_stage_status': rows,
        'actionable_gaps': gaps,
        'pending_control_actions': pending_controls,
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
    # Mission expiry is authoritative even when the legacy Supervisor row is
    # already terminal or was never created. Close these rows before any Plan
    # bootstrap/dispatch logic can observe them as runnable.
    close_all_expired_missions(now)
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
        PlanSupervisor.status.notin_(['expired', 'stopped'])).order_by(
            PlanSupervisor.plan_id).all()]
    # One small transaction per Plan. The caller's scheduler already serializes scans.
    for plan_id in ids:
        sup = locked(plan_id)
        plan = db.session.get(TestPlan, plan_id)
        member_wake_ids = []
        if close_expired_mission_supervision(sup, now):
            db.session.commit()
            continue
        repair_recoverable_state(sup, now)
        for notice in PlanSupervisorEvent.query.filter_by(
                plan_id=plan_id, kind='block_notification_queued').order_by(
                PlanSupervisorEvent.id).all():
            if (notice.payload_json or {}).get('chat_status') in ('pending', 'retryable'):
                _deliver_block_chat(sup, notice, now)
        # Ordinary AgentTasks can become terminal in the timeout watcher rather
        # than through the result endpoint.  Reconcile them even while the
        # supervisor is waiting/blocked so stale "running" UI never persists.
        reconcile_ordinary_task_truth(sup, now)
        guard_execution_goals(sup, now)
        if available(sup, now):
            ensure_manager_tenure(sup, now)
            if sup.team_id:
                ensure_team_mission(sup, now=now)
            # Scheduling is Hub-owned: materialize exactly one dated instance,
            # enforce not_before, then create a durable AgentTask for templates
            # that explicitly opted into automatic dispatch.
            sync_plan_stages(sup, now=now)
            member_wake_ids = promote_and_dispatch_due_occurrences(
                sup, now=now)
            member_wake_ids.extend(advance_occurrence_actions(sup, now=now))
            member_wake_ids.extend(dispatch_ready_control_stages(sup, now=now))
            member_wake_ids = sorted(set(member_wake_ids))
            enqueue_schedule_ticks(sup, now)
            reconcile_plan_truth(sup, now)
            refresh_supervision_report(sup, now)
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
        for member_claw_id in member_wake_ids:
            count += 1
            wake(member_claw_id)
    return count


def wake(claw_id):
    try:
        from app.api.agent_client import notify_claw
        notify_claw(claw_id)
    except Exception:
        current_app.logger.warning('Plan supervisor outbox pending for claw_id=%s', claw_id)
