"""Pure helpers for Hub Workflow Runbook orchestration.

The service intentionally avoids Flask/SQLAlchemy dependencies so the workflow
state rules can be tested without booting the Hub app.
"""

import copy
import json
import re

from app.services.workflow_direct_execution import (
    normalize_direct_execution_lease,
)

VALID_STEP_TYPES = {
    'worker_task',
    'agent_task',
    'llm_call',
    'approval',
    'gate',
    'notification',
}

RUN_STATUSES = {
    'pending',
    'running',
    'retrying',
    'waiting_approval',
    'blocked',
    'failed',
    'succeeded',
    'cancelled',
}

STEP_STATUSES = {
    'pending',
    'running',
    'retrying',
    'passed',
    'blocked',
    'failed',
    'skipped',
    'waiting_approval',
}

# ``warn`` is a legacy gate policy used by the long-running Flow #12.  It is
# deliberately not a WorkflowStep status: a failed warning gate records the
# diagnostic but lets the step keep the status reported by its executor.
GATE_ON_FAIL_POLICIES = STEP_STATUSES | {'warn'}

TERMINAL_STEP_STATUSES = {'passed', 'skipped'}
TERMINAL_RESULT_STEP_STATUSES = TERMINAL_STEP_STATUSES | {'blocked', 'failed'}
PROPAGATED_UPSTREAM_BLOCKED = 'upstream_blocked'
WORKER_RESULT_STATUSES = {'passed', 'failed', 'blocked', 'skipped'}
WORKFLOW_OUTCOME_VALUES = {
    'business': {'PASSED', 'FAILED', 'INCONCLUSIVE', 'NOT_EXECUTED'},
    'automation': {'SUCCEEDED', 'PARTIAL', 'BLOCKED', 'FAILED'},
    'evidence': {'COMPLETE', 'ANALYSIS_INCOMPLETE', 'NOT_REQUIRED'},
    'report': {'PUBLISHED', 'DRAFT', 'FAILED', 'NOT_APPLICABLE'},
    'notification': {'SENT', 'PENDING', 'FAILED', 'NOT_REQUIRED'},
    'review': {'COMPLETED', 'FAILED', 'INCOMPLETE', 'NOT_ASSIGNED'},
}
WORKFLOW_CATALOG_KINDS = {'business', 'probe', 'legacy'}
WORKFLOW_READINESS_VALUES = {
    'unverified', 'canary', 'stable', 'probe_only', 'legacy', 'blocked',
}
DEFAULT_STEP_LEASE_SECONDS = 180
MIN_STEP_LEASE_SECONDS = 30
MAX_STEP_LEASE_SECONDS = 900
DISPLAY_STEP_STATES = {
    'todo': {'pending'},
    'running': {'running', 'retrying', 'waiting_approval'},
    'blocked': {'blocked', 'failed'},
    'done': {'passed', 'skipped', 'succeeded'},
}
VALID_REFERENCE_TYPES = {
    'knowledge',
    'test_report',
    'topic',
    'testcase',
    'skill',
    'work_rule',
    'url',
}
WORKFLOW_PAGE_SIZES = {10, 20, 50}

# Flow #25 opts into this contract explicitly.  Existing Workflow Definitions
# do not inherit it, so legacy Sidecars and Job Services keep their current
# behavior until a Definition is deliberately migrated.
RACINGGO_FLOW25_WORKER_CONTRACT_VERSION = 'deepflow.racinggo.flow25_worker@1'
RACINGGO_FLOW25_CONTROLLED_RUNNER = 'deepflow.racinggo.flow25_worker_v1'
RACINGGO_FLOW25_WORKER_OPERATIONS = {
    'merge_latest_dev2': 'merge_latest_dev2',
    'runtime_bootstrap': 'runtime_bootstrap',
    'editor_health': 'editor_health',
    'playmode_bootstrap': 'playmode_bootstrap',
    'bridge_ping': 'bridge_ping',
    'bridge_snapshot': 'bridge_snapshot',
    'bridge_contract': 'bridge_contract',
    'login_lobby': 'login_lobby',
    'library20_execute': 'library20_execute',
}
RACINGGO_MERGE_OUTPUTS = (
    'source_head',
    'target_head',
    'backup_branch',
    'merge_commit',
    'workspace_clean',
    'operation_receipt',
)
RACINGGO_RUNTIME_OUTPUTS = (
    'runtime_generation',
    'unity_pid',
    'unity_process_started_at',
    'mcp_pid',
    'mcp_url',
    'mcp_port',
    'mobile_bridge_port',
    'ui_bridge_port',
    'project_root',
    'unity_project_root',
    'source_head',
    'target_head',
    'readiness_consecutive_successes',
)
_ARBITRARY_COMMAND_INPUT_KEYS = {
    'args',
    'cmd',
    'command',
    'exec_cmd',
    'executable',
    'executable_path',
    'powershell',
    'script',
    'script_path',
    'shell',
}


def workflow_step_display_state(status):
    """Map internal workflow step status to the four user-facing states."""
    status = str(status or 'pending').strip() or 'pending'
    for display_state, statuses in DISPLAY_STEP_STATES.items():
        if status in statuses:
            return display_state
    return 'todo'


def _slug(value):
    text = ''.join(ch.lower() if ch.isalnum() else '_' for ch in str(value or '').strip())
    text = '_'.join(part for part in text.split('_') if part)
    return text[:80] or 'workflow'


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v or '').strip()]
    return [str(value)]


def _as_int_list(value):
    result = []
    for item in _as_list(value):
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return sorted(set(result))


def _as_positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def normalize_pagination(page=None, per_page=None):
    """Normalize workflow list pagination parameters."""
    page = _as_positive_int(page, 1)
    per_page = _as_positive_int(per_page, 10)
    if per_page not in WORKFLOW_PAGE_SIZES:
        per_page = 10
    return page, per_page


def paginate_items(items, page=None, per_page=None):
    """Paginate an in-memory list after permission filtering."""
    page, per_page = normalize_pagination(page, per_page)
    items = list(items or [])
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    start = (page - 1) * per_page
    end = start + per_page
    return {
        'items': items[start:end],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': pages,
            'has_prev': page > 1,
            'has_next': page < pages,
        },
    }


def normalize_executor_acl(value):
    """Normalize workflow execution ACL.

    ``all`` is reserved for built-in/system templates. User-created templates
    should normally rely on owner + explicit ``claw_ids`` / ``user_ids``.
    """
    value = value if isinstance(value, dict) else {}
    return {
        'all': bool(value.get('all')),
        'claw_ids': _as_int_list(value.get('claw_ids')),
        'user_ids': _as_int_list(value.get('user_ids')),
    }


def normalize_editor_acl(value):
    """Normalize the multi-actor Workflow Definition editor ACL."""
    value = value if isinstance(value, dict) else {}
    return {
        'claw_ids': _as_int_list(value.get('claw_ids')),
        'user_ids': _as_int_list(value.get('user_ids')),
    }


def _same_actor(owner_type, owner_id, actor_type, actor_id):
    try:
        return str(owner_type or '') == str(actor_type or '') and int(owner_id) == int(actor_id)
    except (TypeError, ValueError):
        return False


def can_execute_workflow(owner_type, owner_id, executor_acl, actor_type,
                         actor_id, is_admin=False, editor_acl=None):
    """Return whether actor can create a run from a definition.

    Product permission semantics are intentionally asymmetric: an editor can
    both modify and execute a Definition, while an executor can only execute.
    """
    if is_admin:
        return True
    acl = normalize_executor_acl(executor_acl)
    if acl.get('all'):
        return True
    if _same_actor(owner_type, owner_id, actor_type, actor_id):
        return True
    try:
        actor_id = int(actor_id)
    except (TypeError, ValueError):
        return False
    if actor_type == 'claw':
        if actor_id in acl.get('claw_ids', []):
            return True
        return actor_id in normalize_editor_acl(editor_acl).get('claw_ids', [])
    if actor_type == 'user':
        if actor_id in acl.get('user_ids', []):
            return True
        return actor_id in normalize_editor_acl(editor_acl).get('user_ids', [])
    return False


def can_manage_workflow(owner_type, owner_id, executor_acl, actor_type, actor_id, is_admin=False):
    """Return whether actor can change workflow executor permissions."""
    if is_admin:
        return True
    return _same_actor(owner_type, owner_id, actor_type, actor_id)


def can_edit_workflow(owner_type, owner_id, editor_acl,
                      actor_type, actor_id, is_admin=False):
    """Return whether actor can edit Definition content, not its permissions."""
    if is_admin or _same_actor(owner_type, owner_id, actor_type, actor_id):
        return True
    acl = normalize_editor_acl(editor_acl)
    try:
        actor_id = int(actor_id)
    except (TypeError, ValueError):
        return False
    if actor_type == 'claw':
        return actor_id in acl['claw_ids']
    if actor_type == 'user':
        return actor_id in acl['user_ids']
    return False


def can_view_workflow(visibility_scope, owner_type, owner_id, project_id,
                      actor_type, actor_id, actor_project_ids, is_admin=False):
    """Return whether actor can see a workflow definition."""
    if is_admin:
        return True
    if str(owner_type or '') == 'system':
        return True
    if _same_actor(owner_type, owner_id, actor_type, actor_id):
        return True
    scope = str(visibility_scope or 'project').strip() or 'project'
    if scope == 'private':
        return False
    if scope == 'project':
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return False
        return project_id in _as_int_list(actor_project_ids)
    return False


def can_claim_step(claimed_by, claimed_at, worker_id, now, lease_seconds):
    """Return whether a worker can claim or renew a workflow step lease."""
    worker_id = str(worker_id or '').strip()
    if not worker_id:
        return False
    claimed_by = str(claimed_by or '').strip()
    if not claimed_by:
        return True
    if claimed_by == worker_id:
        return True
    if not claimed_at:
        return True
    try:
        return (now - claimed_at).total_seconds() > int(lease_seconds or 0)
    except Exception:
        return True


def normalize_step_lease_seconds(value):
    """Normalize server-side workflow worker lease seconds."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_STEP_LEASE_SECONDS
    if seconds < MIN_STEP_LEASE_SECONDS:
        return MIN_STEP_LEASE_SECONDS
    if seconds > MAX_STEP_LEASE_SECONDS:
        return MAX_STEP_LEASE_SECONDS
    return seconds


def _same_claim_owner(claimed_claw_id, claimed_by, claw_id, worker_id):
    try:
        same_claw = int(claimed_claw_id) == int(claw_id)
    except (TypeError, ValueError):
        return False
    return same_claw and str(claimed_by or '').strip() == str(worker_id or '').strip()


def can_claim_step_lease(claimed_claw_id, claimed_by, claim_expires_at,
                         claw_id, worker_id, now):
    """Return whether a worker can create or renew a strong step claim."""
    worker_id = str(worker_id or '').strip()
    if not worker_id:
        return {'allowed': False, 'reason': 'missing_worker_id'}
    has_claim = bool(claimed_claw_id or str(claimed_by or '').strip() or claim_expires_at)
    if not has_claim:
        return {'allowed': True, 'reason': 'unclaimed'}
    if _same_claim_owner(claimed_claw_id, claimed_by, claw_id, worker_id):
        return {'allowed': True, 'reason': 'same_owner'}
    if not claim_expires_at or claim_expires_at <= now:
        return {'allowed': True, 'reason': 'expired'}
    return {'allowed': False, 'reason': 'claim_conflict'}


def active_step_claim_state(step_type, claimed_claw_id, claimed_by,
                            claim_expires_at, claw_id, worker_id, now):
    """Validate the active claim required for worker_task write operations."""
    if str(step_type or 'worker_task') != 'worker_task':
        return {'active': True, 'reason': 'not_worker_task'}
    if not (claimed_claw_id or str(claimed_by or '').strip() or claim_expires_at):
        return {'active': False, 'reason': 'claim_required'}
    if not _same_claim_owner(claimed_claw_id, claimed_by, claw_id, worker_id):
        return {'active': False, 'reason': 'claim_owner_mismatch'}
    if not claim_expires_at or claim_expires_at <= now:
        return {'active': False, 'reason': 'claim_expired'}
    return {'active': True, 'reason': 'active'}


def validate_worker_result_status(status):
    """Return a terminal worker result status or fail closed."""
    status = str(status or '').strip()
    if status not in WORKER_RESULT_STATUSES:
        raise ValueError('status 必须是 passed/failed/blocked/skipped')
    return status


def validate_workflow_finalizer_result(step_config, result):
    """Require an explicit completion receipt only for opted-in finalizers."""
    config = step_config if isinstance(step_config, dict) else {}
    if config.get('finalizer') is not True and config.get('is_finalizer') is not True:
        return {'valid': True, 'missing': []}
    result = result if isinstance(result, dict) else {}
    outputs = result.get('outputs') if isinstance(result.get('outputs'), dict) else {}
    missing = []
    if outputs.get('finalizer_complete') is not True:
        missing.append('outputs.finalizer_complete')
    receipt = outputs.get('finalizer_receipt')
    if not isinstance(receipt, dict) or not receipt:
        missing.append('outputs.finalizer_receipt')
    return {
        'valid': not missing,
        'missing': missing,
        'code': '' if not missing else 'FINALIZER_RECEIPT_REQUIRED',
    }


def _canonical_outcome(domain, value):
    rendered = str(value or '').strip().upper()
    aliases = {
        'business': {
            'COMPLETED': 'PASSED',
            'CONFIRMED_BUSINESS_FAILURE': 'FAILED',
            'COMPLETED_WITH_BUGS': 'FAILED',
        },
        'automation': {
            'COMPLETED': 'SUCCEEDED',
            'COMPLETED_WITH_AUTOMATION_ERROR': 'PARTIAL',
            'AUTOMATION_ENV_BLOCKED': 'BLOCKED',
        },
        'evidence': {
            'EVIDENCE_INGEST_INCOMPLETE': 'ANALYSIS_INCOMPLETE',
        },
    }
    rendered = aliases.get(domain, {}).get(rendered, rendered)
    return rendered if rendered in WORKFLOW_OUTCOME_VALUES[domain] else ''


def workflow_catalog_metadata(definition):
    """Describe what a Definition proves without inferring production readiness.

    The catalog classification is intentionally conservative.  A successful
    command/lease probe is not a business test, and a Definition containing a
    legacy ``worker_task`` is never advertised as Direct-ready merely because
    it is active. Operators can refine the generated values through the
    top-level ``workflow_catalog`` object.
    """
    definition = definition if isinstance(definition, dict) else {}
    context = definition.get('context') if isinstance(
        definition.get('context'), dict) else {}
    declared = definition.get('workflow_catalog') if isinstance(
        definition.get('workflow_catalog'), dict) else {}
    steps = [row for row in (definition.get('steps') or [])
             if isinstance(row, dict)]
    has_legacy_worker_task = any(
        str(row.get('type') or '') == 'worker_task' for row in steps)
    searchable = ' '.join(str(value or '') for value in (
        definition.get('key'), definition.get('name'),
        definition.get('description'), declared.get('validation_scope'))
    ).casefold()
    declared_kind = str(declared.get('kind') or '').strip().lower()
    if has_legacy_worker_task:
        kind = 'legacy'
    elif declared_kind in WORKFLOW_CATALOG_KINDS:
        kind = declared_kind
    elif any(token in searchable for token in ('probe', '探针', 'contract check')):
        kind = 'probe'
    else:
        kind = 'business'

    default_readiness = {
        'legacy': 'legacy',
        'probe': 'probe_only',
        'business': 'unverified',
    }[kind]
    declared_readiness = str(
        declared.get('readiness') or '').strip().lower()
    readiness = (
        default_readiness
        if has_legacy_worker_task
        else declared_readiness
        if declared_readiness in WORKFLOW_READINESS_VALUES
        else default_readiness)
    validation_scope = str(
        declared.get('validation_scope') or {
            'legacy': 'legacy_execution_path',
            'probe': 'control_plane_probe',
            'business': 'business_workflow',
        }[kind]).strip()
    return {
        'kind': kind,
        'readiness': readiness,
        'validation_scope': validation_scope[:160],
        'has_legacy_worker_task': has_legacy_worker_task,
        'declared': bool(declared),
    }


def workflow_outcome_requirements(definition):
    """Return which result dimensions a Definition explicitly requires."""
    definition = definition if isinstance(definition, dict) else {}
    context = definition.get('context') if isinstance(
        definition.get('context'), dict) else {}
    steps = [row for row in (definition.get('steps') or [])
             if isinstance(row, dict)]
    catalog = workflow_catalog_metadata(definition)
    declared = context.get('outcome_requirements') if isinstance(
        context.get('outcome_requirements'), dict) else {}

    def declared_or(name, inferred):
        value = declared.get(name)
        return value if isinstance(value, bool) else bool(inferred)

    evidence = any(
        row.get('evidence_requirements') or row.get('required_evidence')
        for row in steps)
    report = any(
        any(name in {'hub_report_id', 'test_report_id', 'workflow_report_id',
                     'report_outcome', 'report_status'}
            for name in (row.get('outputs') or []))
        or bool(
            row.get('analysis').get('enabled')
            if isinstance(row.get('analysis'), dict) else False)
        for row in steps)
    notification = any(
        row.get('type') == 'notification'
        or row.get('notification_delivery_mode') == 'outbox'
        for row in steps)
    review = any(
        row.get('assignment_role') == 'reviewer'
        or row.get('type') == 'approval'
        or row.get('approval_required') is True
        for row in steps)
    return {
        'business': declared_or('business', catalog['kind'] == 'business'),
        'automation': declared_or('automation', bool(steps)),
        'evidence': declared_or('evidence', evidence),
        'report': declared_or('report', report),
        'notification': declared_or('notification', notification),
        'review': declared_or('review', review),
    }


def merge_workflow_run_outcomes(current, result, step_config=None):
    """Merge one result into the six independent Run outcome dimensions."""
    merged = {
        key: _canonical_outcome(key, (current or {}).get(key))
        for key in WORKFLOW_OUTCOME_VALUES
    }
    result = result if isinstance(result, dict) else {}
    outputs = result.get('outputs') if isinstance(result.get('outputs'), dict) else {}
    metrics = result.get('metrics') if isinstance(result.get('metrics'), dict) else {}

    def first(*names):
        for name in names:
            for source in (result, outputs, metrics):
                if source.get(name) not in (None, ''):
                    return source.get(name)
        return None

    candidates = {
        'business': first('business_outcome', 'business_conclusion'),
        'automation': first('automation_outcome', 'automation_conclusion'),
        'evidence': first('evidence_outcome', 'evidence_ingest_status'),
        'report': first('report_outcome', 'report_status'),
        'notification': first('notification_outcome'),
        'review': first('review_outcome'),
    }
    from app.services.workflow_evidence_scope import evidence_outcome_values
    evidence_values = evidence_outcome_values(result)
    if any(item['status'] == 'ANALYSIS_INCOMPLETE' for item in evidence_values):
        candidates['evidence'] = 'ANALYSIS_INCOMPLETE'
    if candidates['business'] in (None, ''):
        if first('business_failure_confirmed') is True:
            candidates['business'] = 'FAILED'
        elif first('business_passed') is True:
            candidates['business'] = 'PASSED'
        elif first('business_inconclusive') is True:
            candidates['business'] = 'INCONCLUSIVE'
    if candidates['report'] in (None, ''):
        if first('hub_report_id') not in (None, ''):
            candidates['report'] = 'PUBLISHED'
        elif first('report_published') is False:
            candidates['report'] = 'FAILED'
    if candidates['notification'] in (None, ''):
        if first('wecom_sent') is True:
            candidates['notification'] = 'SENT'
        elif first('notification_skipped') is True:
            candidates['notification'] = 'NOT_REQUIRED'
        elif first('notification_required') is True and first('wecom_sent') is False:
            candidates['notification'] = 'FAILED'
    config = step_config if isinstance(step_config, dict) else {}
    if config.get('assignment_role') == 'reviewer' and candidates['review'] in (
            None, ''):
        status = str(result.get('status') or '').lower()
        candidates['review'] = (
            'COMPLETED' if status in ('passed', 'skipped') else 'FAILED')
    severity = {
        'business': {
            '': -1, 'NOT_EXECUTED': 0, 'INCONCLUSIVE': 1,
            'PASSED': 2, 'FAILED': 3,
        },
        'automation': {
            '': -1, 'SUCCEEDED': 0, 'PARTIAL': 1,
            'BLOCKED': 2, 'FAILED': 3,
        },
    }
    sticky_success = {
        'evidence': 'ANALYSIS_INCOMPLETE',
        'report': 'PUBLISHED',
        'notification': 'SENT',
        'review': 'COMPLETED',
    }
    for domain, value in candidates.items():
        canonical = _canonical_outcome(domain, value)
        if not canonical:
            continue
        current_value = merged.get(domain) or ''
        if domain in severity:
            if severity[domain].get(canonical, -1) >= severity[domain].get(
                    current_value, -1):
                merged[domain] = canonical
            continue
        if current_value == sticky_success.get(domain):
            continue
        merged[domain] = canonical
    return merged


def composite_workflow_terminal_status(outcomes, has_blocked=False,
                                       has_failed=False):
    """Map composite outcomes to a technical Run terminal status."""
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    business = _canonical_outcome('business', outcomes.get('business'))
    automation = _canonical_outcome('automation', outcomes.get('automation'))
    if business in {'PASSED', 'FAILED', 'INCONCLUSIVE'}:
        return 'succeeded'
    if business == 'NOT_EXECUTED':
        return 'failed' if automation == 'FAILED' or has_failed else 'blocked'
    if has_failed:
        return 'failed'
    if has_blocked:
        return 'blocked'
    return 'succeeded'


def resolve_workflow_step_claw_ids(
        step_type, target_claw_id=None, step_executor_claw_ids=None,
        run_executor_claw_ids=None, target_post=None, target_agent=None):
    """Resolve explicit or inherited executors allowed to handle one step."""
    explicit_target = _as_int_list([target_claw_id])
    if explicit_target:
        return explicit_target
    explicit_executors = _as_int_list(step_executor_claw_ids)
    if explicit_executors:
        return explicit_executors
    if step_type == 'worker_task' and not target_post and not target_agent:
        return _as_int_list(run_executor_claw_ids)
    return []


def can_dispatch_workflow_agent_task(run_status, step_status, claw_id,
                                     target_claw_id=None, executor_claw_ids=None):
    """Return whether a workflow AgentTask may be delivered to this claw."""
    if run_status not in ('running', 'retrying'):
        return False
    if step_status not in ('running', 'retrying'):
        return False
    try:
        current = int(claw_id)
    except (TypeError, ValueError):
        return False
    allowed = []
    if target_claw_id:
        try:
            allowed.append(int(target_claw_id))
        except (TypeError, ValueError):
            pass
    if not allowed and isinstance(executor_claw_ids, (list, tuple, set)):
        for raw in executor_claw_ids:
            try:
                allowed.append(int(raw))
            except (TypeError, ValueError):
                continue
    return not allowed or current in set(allowed)


def compute_step_health(status, heartbeat_at, now, interval_sec=30, max_missed=3,
                        progress_at=None, progress_timeout_sec=300,
                        auto_fail_on_missed=False):
    """Return response/heartbeat state for a running workflow step.

    Hub is the workflow coordinator, not the remote execution watchdog. Missing
    heartbeat/progress is treated as "stale/no response" by default; only steps
    that explicitly opt in should be auto-failed by Hub.
    """
    if status not in ('running', 'retrying'):
        return {
            'status': 'idle',
            'age_seconds': 0,
            'missed_count': 0,
            'progress_status': 'idle',
            'progress_age_seconds': 0,
            'failed': False,
        }
    if not heartbeat_at or not now:
        return {
            'status': 'stale',
            'age_seconds': None,
            'missed_count': 1,
            'progress_status': 'unknown',
            'progress_age_seconds': None,
            'failed': False,
        }
    try:
        age = max(0, int((now - heartbeat_at).total_seconds()))
    except Exception:
        age = None
    if age is None:
        return {
            'status': 'stale',
            'age_seconds': None,
            'missed_count': 1,
            'progress_status': 'unknown',
            'progress_age_seconds': None,
            'failed': False,
        }
    interval_sec = max(1, int(interval_sec or 30))
    max_missed = max(1, int(max_missed or 3))
    if age <= interval_sec:
        missed = 0
    else:
        missed = min(max_missed, (age - 1) // interval_sec)
    if age > interval_sec * max_missed and auto_fail_on_missed:
        missed = max_missed
        health = 'failed'
    elif age > interval_sec * max_missed:
        missed = max_missed
        health = 'stale'
    elif missed >= 1:
        health = 'stale'
    else:
        health = 'healthy'
    progress_status = 'unknown'
    progress_age = None
    if progress_at and now:
        try:
            progress_age = max(0, int((now - progress_at).total_seconds()))
            progress_timeout_sec = max(1, int(progress_timeout_sec or 300))
            progress_status = 'stale' if progress_age > progress_timeout_sec else 'fresh'
        except Exception:
            progress_status = 'unknown'
            progress_age = None
    return {
        'status': health,
        'age_seconds': age,
        'missed_count': missed,
        'progress_status': progress_status,
        'progress_age_seconds': progress_age,
        'failed': health == 'failed',
    }


def build_heartbeat_fallback_notice(run_id, step_id, step_name='', blocker=None):
    """Build consistent chat/todo copy for no-response reminder."""
    blocker = blocker if isinstance(blocker, dict) else {}
    title = '【提醒】处理 Workflow Run #%s %s 节点未响应' % (run_id, step_id)
    content = (
        '【提醒】Workflow Run #%s 节点已派发但长时间未响应，需要推进处理。\n\n'
        '节点：%s（%s）\n'
        '提醒原因：%s\n'
        '最近响应/心跳：%s\n\n'
        '请检查：\n'
        '1. 目标 Agent/worker 是否在线并已领取任务。\n'
        '2. 如果正在执行，请回写 progress，说明当前阶段。\n'
        '3. 如果已完成，请回写 step result。\n'
        '4. 如果无法执行，请明确回写 blocked/failed 与 blocker，或通知管理员介入。\n'
    ) % (
        run_id,
        step_id,
        step_name or step_id,
        blocker.get('type') or 'workflow_step_no_response',
        blocker.get('last_heartbeat_at') or '-',
    )
    return {'title': title, 'content': content}


def build_step_blocked_notice(run_id, step_id, step_name='', status='blocked',
                              summary='', blocker=None):
    """Build consistent chat/todo copy for blocked or failed workflow steps."""
    blocker = blocker if isinstance(blocker, dict) else {}
    status_label = '失败' if status == 'failed' else '阻断'
    reason = blocker.get('message') or summary or '未提供具体原因'
    title = '【处理】Workflow Run #%s %s 节点%s' % (run_id, step_id, status_label)
    content = (
        '【处理】Workflow Run #%s 节点已%s，需要目标 Agent/owner 介入。\n\n'
        '节点：%s（%s）\n'
        '状态：%s\n'
        '原因类型：%s\n'
        '原因：%s\n\n'
        '请处理该节点阻断：\n'
        '1. 查看 AgentTask/sidecar 日志和节点参考内容。\n'
        '2. 如果环境或配置问题已修复，请在页面将节点设为执行中或重试。\n'
        '3. 如果业务确实无法继续，请补充 blocker 信息并通知 owner。\n'
    ) % (
        run_id,
        status_label,
        step_id,
        step_name or step_id,
        status,
        blocker.get('type') or 'workflow_step_%s' % status,
        reason,
    )
    return {'title': title, 'content': content}


def can_accept_step_result_after_blocker(step_status, blocker, force_recover=False, can_manage=False):
    """Protect heartbeat-failed steps from late worker result overwrites."""
    blocker = blocker if isinstance(blocker, dict) else {}
    if step_status == 'blocked' and blocker.get('type') == 'workflow_step_heartbeat_lost':
        if force_recover and can_manage:
            return {'accepted': True, 'reason': 'force_recover'}
        return {
            'accepted': False,
            'reason': 'heartbeat_lost_requires_force_recover',
        }
    return {'accepted': True, 'reason': ''}


def normalize_branch(branch):
    """Normalize an if/else branch rule for a step."""
    if not isinstance(branch, dict):
        return None
    expression = str(branch.get('if') or branch.get('expression') or '').strip()
    if not expression:
        return None
    return {
        'if': expression,
        'then': _as_list(branch.get('then')),
        'else': _as_list(branch.get('else')),
    }


def normalize_step_references(value):
    """Normalize optional context references attached to a workflow node."""
    refs = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        ref_type = str(item.get('type') or '').strip()
        if ref_type not in VALID_REFERENCE_TYPES:
            continue
        if ref_type == 'url':
            url = str(item.get('url') or '').strip()
            if not (
                url.startswith(('http://', 'https://'))
                or (url.startswith('/') and not url.startswith('//'))
            ):
                continue
        ref = {'type': ref_type}
        for key in ('id', 'title', 'url', 'path', 'summary'):
            if item.get(key) not in (None, ''):
                ref[key] = item.get(key)
        refs.append(ref)
    return refs


def normalize_analysis_config(value):
    """Normalize the optional code-analysis contract attached to a node.

    Keeping this as node metadata lets old workflow runners ignore it while
    capable Agent runners receive a stable API contract in their task payload.
    """
    if not isinstance(value, dict):
        return {}
    allowed_baseline_vars = (
        'client_repo', 'client_base_sha', 'client_target_sha',
        'server_repo', 'server_base_sha', 'server_target_sha',
        'config_digest', 'proto_digest', 'requirement_revision',
        'testcase_revisions', 'knowledge_snapshots',
        'analysis_profile_version', 'rule_snapshot_id',
    )
    baseline_vars = {}
    raw_baseline_vars = value.get('baseline_vars')
    if isinstance(raw_baseline_vars, dict):
        for key in allowed_baseline_vars:
            raw = raw_baseline_vars.get(key)
            if raw not in (None, ''):
                baseline_vars[key] = str(raw).strip()[:240]
    report_format = str(value.get('report_format') or 'html').strip().lower()
    if report_format not in ('html', 'markdown'):
        report_format = 'html'
    return {
        'enabled': bool(value.get('enabled', True)),
        'profile': str(value.get('profile') or 'requirement_code_joint').strip()[:100],
        'create_report': bool(value.get('create_report', True)),
        'report_title_template': str(
            value.get('report_title_template') or '代码分析报告 - {run_name}'
        ).strip()[:300],
        'report_format': report_format,
        'iteration_id_var': str(
            value.get('iteration_id_var') or 'iteration_id').strip()[:160],
        'report_id_var': str(
            value.get('report_id_var') or 'analysis_report_id').strip()[:160],
        'baseline_vars': baseline_vars,
        'require_independent_review': bool(
            value.get('require_independent_review', True)),
    }


def _gate_operands(expression, path):
    """Return the two operands of the small, supported gate expression DSL."""
    text = str(expression or '').strip()
    for operator in ('>=', '<=', '==', '!=', '>', '<'):
        if operator not in text:
            continue
        left, right = (part.strip() for part in text.split(operator, 1))
        if not left or not right:
            break
        return left, right
    raise ValueError(
        f'{path}: expected a comparison using ==, !=, >, >=, < or <=')


def _metric_schema_paths(schema, prefix='metrics'):
    """Collect metric paths from JSON Schema, a simple mapping, or a list."""
    paths = set()
    if isinstance(schema, list):
        for item in schema:
            value = str(item or '').strip()
            if value:
                paths.add(value if value.startswith('metrics.')
                          else f'{prefix}.{value}')
        return paths
    if not isinstance(schema, dict):
        return paths

    properties = schema.get('properties')
    if not isinstance(properties, dict):
        ignored = {
            '$schema', 'type', 'title', 'description', 'required',
            'additionalProperties', 'examples', 'default',
        }
        properties = {
            key: value for key, value in schema.items() if key not in ignored
        }
    for key, value in properties.items():
        key = str(key or '').strip()
        if not key:
            continue
        path = f'{prefix}.{key}'
        nested = _metric_schema_paths(value, path)
        if nested:
            paths.update(nested)
        else:
            paths.add(path)
    return paths


def normalize_step_gates(value, step_index, metrics_schema=None):
    """Validate gate syntax and declared metric references with exact paths."""
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise ValueError(f'steps[{step_index}].gates: expected an array')
    declared_metrics = (
        _metric_schema_paths(metrics_schema)
        if metrics_schema is not None else None
    )
    normalized = []
    for gate_index, gate in enumerate(value):
        gate_path = f'steps[{step_index}].gates[{gate_index}]'
        if not isinstance(gate, dict):
            raise ValueError(f'{gate_path}: expected an object')
        expression = str(gate.get('expression') or '').strip()
        left, right = _gate_operands(expression, f'{gate_path}.expression')
        on_fail = str(gate.get('on_fail') or 'blocked').strip()
        if on_fail not in GATE_ON_FAIL_POLICIES:
            raise ValueError(
                f'{gate_path}.on_fail: unsupported gate policy {on_fail!r}')
        if declared_metrics is not None:
            operands = (left, right)
            for operand in operands:
                if not operand.startswith('metrics.'):
                    continue
                if operand not in declared_metrics:
                    raise ValueError(
                        f'{gate_path}.expression: metric path {operand!r} '
                        'is not declared in metrics_schema')
        item = dict(gate)
        item['expression'] = expression
        item['on_fail'] = on_fail
        normalized.append(item)
    return normalized


def normalize_step(step, idx):
    """Normalize a single workflow step definition."""
    if not isinstance(step, dict):
        raise ValueError('step 必须是对象')
    step_id = _slug(step.get('id') or step.get('key') or step.get('name') or f'step_{idx + 1}')
    step_type = step.get('type') or ('approval' if step.get('approval_required') else 'worker_task')
    if step_type not in VALID_STEP_TYPES:
        step_type = 'worker_task'
    approval_required = bool(step.get('approval_required') or step_type == 'approval')
    metrics_schema = step.get('metrics_schema')
    if metrics_schema is not None and not isinstance(metrics_schema, (dict, list)):
        raise ValueError(f'steps[{idx}].metrics_schema: expected an object or array')
    normalized = {
        'id': step_id,
        'name': str(step.get('name') or step.get('title') or step_id),
        'type': step_type,
        'runner': str(step.get('runner') or ''),
        'depends_on': _as_list(step.get('depends_on')),
        'approval_required': approval_required,
        'gates': normalize_step_gates(step.get('gates'), idx, metrics_schema),
        'target_agent': str(step.get('target_agent') or ''),
        'target_claw_id': step.get('target_claw_id'),
        'prompt': str(step.get('prompt') or ''),
        'references': normalize_step_references(step.get('references')),
        'inputs': step.get('inputs') if isinstance(step.get('inputs'), dict) else {},
        'input_vars': step.get('input_vars') if isinstance(step.get('input_vars'), dict) else {},
        'outputs': _as_list(step.get('outputs')),
        'branches': [
            b for b in (normalize_branch(x) for x in (step.get('branches') or []))
            if b
        ],
        'retry_max': int(step.get('retry_max') or 0),
    }
    if metrics_schema is not None:
        normalized['metrics_schema'] = copy.deepcopy(metrics_schema)
    if 'evidence_requirements' in step:
        from app.services.workflow_evidence_scope import normalize_evidence_requirements
        normalized['evidence_requirements'] = normalize_evidence_requirements(
            step['evidence_requirements'])
    if 'notification_delivery_mode' in step:
        if step['notification_delivery_mode'] not in ('inline', 'outbox'):
            raise ValueError('notification_delivery_mode must be inline/outbox')
        normalized['notification_delivery_mode'] = step['notification_delivery_mode']
        if step['notification_delivery_mode'] == 'outbox':
            # Move only delivery-success gates; never relax business/artifact gates.
            import re
            delivery_gates = []
            execution_gates = []
            for gate in normalized['gates']:
                expression = str(gate.get('expression') or '')
                if re.fullmatch(r'\s*(metrics|outputs)\.(wecom_sent\s*==\s*true|wecom_errcode\s*==\s*0)\s*', expression):
                    delivery_gates.append(gate)
                else:
                    execution_gates.append(gate)
            normalized['gates'] = execution_gates
            normalized['notification_delivery_gates'] = delivery_gates or step.get('notification_delivery_gates', [])
    if 'notification_owner_role' in step:
        owner_role = str(step.get('notification_owner_role') or '').strip()
        if owner_role not in ('executor', 'reviewer'):
            raise ValueError(
                'notification_owner_role must be executor/reviewer')
        normalized['notification_owner_role'] = owner_role
    for key in ('required_metrics', 'required_evidence', 'required_outputs'):
        if key in step:
            normalized[key] = _as_list(step.get(key))
    if normalized.get('notification_delivery_mode') == 'outbox':
        for key in ('required_metrics', 'required_outputs'):
            if key in normalized:
                normalized[key] = [field for field in normalized[key]
                                   if field not in ('wecom_sent', 'wecom_errcode')]
    if 'allow_empty_contract_fields' in step:
        allow_empty = step.get('allow_empty_contract_fields')
        if not isinstance(allow_empty, dict):
            raise ValueError(
                f'steps[{idx}].allow_empty_contract_fields: expected an object')
        normalized['allow_empty_contract_fields'] = {
            section: _as_list(allow_empty.get(section))
            for section in ('metrics', 'evidence', 'outputs')
            if section in allow_empty
        }
    if 'contract_on_fail' in step:
        contract_on_fail = str(step.get('contract_on_fail') or 'blocked').strip()
        if contract_on_fail not in ('blocked', 'failed', 'warn'):
            raise ValueError(
                f'steps[{idx}].contract_on_fail: expected blocked/failed/warn')
        normalized['contract_on_fail'] = contract_on_fail
    # Preserve legacy JSON shape on targeted Definition updates. Flow #12
    # predates target_post; adding an empty field to every step made a harmless
    # PATCH rewrite the full stored definition and changed its content hash.
    if 'target_post' in step or step.get('target_post'):
        normalized['target_post'] = str(step.get('target_post') or '').strip()
    if isinstance(step.get('analysis'), dict):
        normalized['analysis'] = normalize_analysis_config(step.get('analysis'))
    for key in ('auto_block_on_heartbeat_loss', 'heartbeat_auto_block', 'auto_block_on_no_response'):
        if key in step:
            normalized[key] = bool(step.get(key))
    for key in ('target_claw_id_var', 'target_claw_ids_var', 'executor_claw_ids_var'):
        if step.get(key):
            normalized[key] = str(step.get(key)).strip()
    if 'notify_agent_on_start' in step:
        normalized['notify_agent_on_start'] = bool(step.get('notify_agent_on_start'))
    if 'require_fencing_token' in step:
        normalized['require_fencing_token'] = bool(step.get('require_fencing_token'))
    if 'direct_execution_lease' in step:
        normalized['direct_execution_lease'] = normalize_direct_execution_lease(
            step.get('direct_execution_lease'),
            path=f'steps[{idx}].direct_execution_lease',
        )
    # These are orchestration policies, not executor-only hints. Preserve the
    # canonical top-level shape while continuing to read the legacy values
    # already shipped under ``inputs`` by Flow #12/#25.
    if 'advance_on_any_result' in step:
        normalized['advance_on_any_result'] = (
            step.get('advance_on_any_result') is True)
    if 'run_even_if_upstream_blocked' in step:
        normalized['run_even_if_upstream_blocked'] = (
            step.get('run_even_if_upstream_blocked') is True)
    if 'dependency_failure_policy' in step:
        dependency_policy = str(
            step.get('dependency_failure_policy') or '').strip().lower()
        if dependency_policy not in ('fatal', 'continue'):
            raise ValueError(
                f'steps[{idx}].dependency_failure_policy: '
                'expected fatal/continue')
        normalized['dependency_failure_policy'] = dependency_policy
    if 'assignment_role' in step:
        assignment_role = str(step.get('assignment_role') or '').strip()
        if assignment_role not in ('executor', 'reviewer'):
            raise ValueError(
                f'steps[{idx}].assignment_role: expected executor/reviewer')
        normalized['assignment_role'] = assignment_role
    if 'finalizer' in step or 'is_finalizer' in step:
        normalized['finalizer'] = bool(
            step.get('finalizer') is True or step.get('is_finalizer') is True)
    if 'advance_policy' in step:
        advance_policy = str(step.get('advance_policy') or '').strip()
        if advance_policy:
            normalized['advance_policy'] = advance_policy
    return normalized


def _validate_controlled_worker_contract(context, steps):
    policy = (
        context.get('executor_operation_policy')
        if isinstance(context, dict)
        and isinstance(context.get('executor_operation_policy'), dict)
        else {})
    version = str(policy.get('worker_contract_version') or '').strip()
    if not version:
        return
    if version != RACINGGO_FLOW25_WORKER_CONTRACT_VERSION:
        raise ValueError(
            'context.executor_operation_policy.worker_contract_version: '
            f'unsupported controlled worker contract {version!r}')
    if policy.get('mode') != 'workflow_run_executor':
        raise ValueError(
            'controlled worker contract requires mode=workflow_run_executor')
    if policy.get('require_worker_binding') is not True:
        raise ValueError(
            'controlled worker contract requires require_worker_binding=true')

    allowed_operations = set(RACINGGO_FLOW25_WORKER_OPERATIONS.values())
    for idx, step in enumerate(steps):
        if step.get('type') != 'worker_task':
            continue
        if step.get('runner') != RACINGGO_FLOW25_CONTROLLED_RUNNER:
            raise ValueError(
                f'steps[{idx}].runner: controlled runner must be '
                f'{RACINGGO_FLOW25_CONTROLLED_RUNNER!r}')
        inputs = step.get('inputs') if isinstance(step.get('inputs'), dict) else {}
        operation = str(inputs.get('operation') or '').strip()
        expected_operation = RACINGGO_FLOW25_WORKER_OPERATIONS.get(
            str(step.get('id') or '').strip())
        if operation not in allowed_operations or expected_operation is None:
            raise ValueError(
                f'steps[{idx}].inputs.operation: unsupported operation '
                f'{operation!r}')
        if operation != expected_operation:
            raise ValueError(
                f'steps[{idx}].inputs.operation: operation {operation!r} does '
                f'not match step {step.get("id")!r}')
        command_keys = sorted(_ARBITRARY_COMMAND_INPUT_KEYS.intersection(inputs))
        if command_keys:
            raise ValueError(
                f'steps[{idx}].inputs: arbitrary command fields are forbidden: '
                + ', '.join(command_keys))
        if operation == 'merge_latest_dev2':
            unexpected = sorted(
                set(inputs) - {'operation', 'protected_target_id'})
            if unexpected:
                raise ValueError(
                    f'steps[{idx}].inputs: merge_latest_dev2 does not '
                    'accept Hub-controlled operation inputs: '
                    + ', '.join(unexpected))
        step['require_fencing_token'] = True


def _replace_flow25_runtime_references(value):
    replacements = {
        '{context.mcp_url}': '{steps.runtime_bootstrap.outputs.mcp_url}',
        '{start_vars.mcp_url}': '{steps.runtime_bootstrap.outputs.mcp_url}',
        '{context.mobile_bridge_port}': (
            '{steps.runtime_bootstrap.outputs.mobile_bridge_port}'),
        '{start_vars.mobile_bridge_port}': (
            '{steps.runtime_bootstrap.outputs.mobile_bridge_port}'),
        '{context.ui_bridge_port}': (
            '{steps.runtime_bootstrap.outputs.ui_bridge_port}'),
        '{start_vars.ui_bridge_port}': (
            '{steps.runtime_bootstrap.outputs.ui_bridge_port}'),
        '{context.unity_project}': (
            '{steps.runtime_bootstrap.outputs.unity_project_root}'),
        '{context.racinggo_root}': (
            '{steps.runtime_bootstrap.outputs.project_root}'),
    }
    if isinstance(value, dict):
        return {
            key: _replace_flow25_runtime_references(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_flow25_runtime_references(item) for item in value]
    if isinstance(value, str):
        result = value
        for old, new in replacements.items():
            result = result.replace(old, new)
        return result
    return value


def build_racinggo_flow25_controlled_worker_candidate(definition):
    """Return a non-deployed Flow #25 candidate using one controlled runner.

    The helper intentionally operates on an exported Definition.  It does not
    persist or activate anything, which lets Hub and Worker teams review the
    exact migration before a canary.
    """
    candidate = copy.deepcopy(definition if isinstance(definition, dict) else {})
    steps = candidate.get('steps') if isinstance(candidate.get('steps'), list) else []
    by_id = {
        str(step.get('id') or ''): step
        for step in steps if isinstance(step, dict)
    }
    required_existing = set(RACINGGO_FLOW25_WORKER_OPERATIONS) - {'runtime_bootstrap'}
    missing = sorted(required_existing - set(by_id))
    for step_id in ('merge_latest_dev2', 'precheck'):
        if step_id not in by_id:
            missing.append(step_id)
    if missing:
        raise ValueError(
            'Flow #25 candidate is missing required steps: '
            + ', '.join(sorted(set(missing))))

    if 'runtime_bootstrap' not in by_id:
        merge_index = next(
            idx for idx, step in enumerate(steps)
            if isinstance(step, dict) and step.get('id') == 'merge_latest_dev2')
        bootstrap = {
            'id': 'runtime_bootstrap',
            'name': 'Dev2 Runtime Bootstrap',
            'type': 'worker_task',
            'runner': RACINGGO_FLOW25_CONTROLLED_RUNNER,
            'depends_on': ['merge_latest_dev2'],
            'inputs': {
                'operation': 'runtime_bootstrap',
                'protected_target_id': 'racinggo-dev2',
                'required_readiness_successes': 5,
                'runtime_output_missing_code': 'RUNTIME_OUTPUT_MISSING',
            },
            'outputs': list(RACINGGO_RUNTIME_OUTPUTS),
            'required_metrics': [
                'unity_started',
                'mcp_started',
                'project_root_matches',
                'readiness_consecutive_successes',
            ],
            'required_evidence': [
                'process_ownership',
                'project_root_check',
                'readiness_samples',
            ],
            'contract_on_fail': 'blocked',
            'require_fencing_token': True,
        }
        steps.insert(merge_index + 1, bootstrap)
        by_id['runtime_bootstrap'] = bootstrap

    by_id['precheck']['depends_on'] = ['runtime_bootstrap']
    command_keys = _ARBITRARY_COMMAND_INPUT_KEYS
    for step_id, operation in RACINGGO_FLOW25_WORKER_OPERATIONS.items():
        step = by_id[step_id]
        step['type'] = 'worker_task'
        step['runner'] = RACINGGO_FLOW25_CONTROLLED_RUNNER
        inputs = (
            {}
            if step_id == 'merge_latest_dev2'
            else dict(step.get('inputs'))
            if isinstance(step.get('inputs'), dict)
            else {})
        for key in command_keys:
            inputs.pop(key, None)
        inputs.update({
            'operation': operation,
            'protected_target_id': 'racinggo-dev2',
        })
        if step_id == 'merge_latest_dev2':
            step['prompt'] = (
                '由受控 DeepFlow Runner 执行 merge_latest_dev2；Hub 不下发 '
                'Git 命令、路径或文件白名单。')
            step['gates'] = []
            step['outputs'] = list(RACINGGO_MERGE_OUTPUTS)
            step['required_metrics'] = [
                'restored_file_count',
                'changed_file_count',
                'workspace_clean',
            ]
            step['required_evidence'] = ['operation_receipt']
            step['allow_empty_contract_fields'] = {
                'outputs': ['backup_branch'],
            }
            step['contract_on_fail'] = 'blocked'
        elif step_id != 'runtime_bootstrap':
            inputs.update({
                'runtime_output_missing_code': 'RUNTIME_OUTPUT_MISSING',
                'required_runtime_outputs': list(RACINGGO_RUNTIME_OUTPUTS),
                'runtime_generation': (
                    '{steps.runtime_bootstrap.outputs.runtime_generation}'),
                'unity_pid': '{steps.runtime_bootstrap.outputs.unity_pid}',
                'mcp_url': '{steps.runtime_bootstrap.outputs.mcp_url}',
                'mobile_bridge_port': (
                    '{steps.runtime_bootstrap.outputs.mobile_bridge_port}'),
                'ui_bridge_port': (
                    '{steps.runtime_bootstrap.outputs.ui_bridge_port}'),
                'project_root': (
                    '{steps.runtime_bootstrap.outputs.project_root}'),
                'unity_project_root': (
                    '{steps.runtime_bootstrap.outputs.unity_project_root}'),
            })
        step['inputs'] = inputs
        step['require_fencing_token'] = True

    # Keep the DeepFlow operation input contract narrow (mode only), while
    # making the merge receipt lineage explicit in the Hub Definition and in
    # the task payload available to the Worker.
    by_id['runtime_bootstrap']['input_vars'] = {
        key: f'{{steps.merge_latest_dev2.outputs.{key}}}'
        for key in RACINGGO_MERGE_OUTPUTS
    }

    candidate['steps'] = _replace_flow25_runtime_references(steps)
    bootstrap_position = next(
        idx for idx, step in enumerate(candidate['steps'])
        if isinstance(step, dict) and step.get('id') == 'runtime_bootstrap')
    dynamic_runtime_inputs = {
        'runtime_generation': (
            '{steps.runtime_bootstrap.outputs.runtime_generation}'),
        'unity_pid': '{steps.runtime_bootstrap.outputs.unity_pid}',
        'mcp_url': '{steps.runtime_bootstrap.outputs.mcp_url}',
        'mobile_bridge_port': (
            '{steps.runtime_bootstrap.outputs.mobile_bridge_port}'),
        'ui_bridge_port': (
            '{steps.runtime_bootstrap.outputs.ui_bridge_port}'),
        'project_root': '{steps.runtime_bootstrap.outputs.project_root}',
        'racinggo_root': '{steps.runtime_bootstrap.outputs.project_root}',
        'unity_project': (
            '{steps.runtime_bootstrap.outputs.unity_project_root}'),
        'unity_project_root': (
            '{steps.runtime_bootstrap.outputs.unity_project_root}'),
    }
    for step in candidate['steps'][bootstrap_position + 1:]:
        if not isinstance(step, dict):
            continue
        inputs = dict(step.get('inputs')) if isinstance(step.get('inputs'), dict) else {}
        # Preserve the historical field names where consumers still expect
        # them, but source every value from the bootstrap receipt.
        for key, value in dynamic_runtime_inputs.items():
            if key in inputs or step.get('type') in ('worker_task', 'agent_task'):
                inputs[key] = value
        inputs['runtime_output_missing_code'] = 'RUNTIME_OUTPUT_MISSING'
        step['inputs'] = inputs
    context = copy.deepcopy(candidate.get('context')) if isinstance(
        candidate.get('context'), dict) else {}
    for key in ('mcp_url', 'mobile_bridge_port', 'ui_bridge_port'):
        context.pop(key, None)
    policy = dict(context.get('executor_operation_policy')) if isinstance(
        context.get('executor_operation_policy'), dict) else {}
    policy.update({
        'mode': 'workflow_run_executor',
        'require_worker_binding': True,
        'worker_contract_version': RACINGGO_FLOW25_WORKER_CONTRACT_VERSION,
        'controlled_runner': RACINGGO_FLOW25_CONTROLLED_RUNNER,
        'protected_target_id': 'racinggo-dev2',
    })
    context['executor_operation_policy'] = policy
    candidate['context'] = context

    if '8091' in str(candidate):
        raise ValueError('Flow #25 candidate still contains forbidden port fallback 8091')
    # Validate without replacing the richer exported Definition shape.
    normalize_workflow_definition(candidate)
    return candidate


def normalize_workflow_definition(data):
    """Normalize a workflow definition JSON payload."""
    data = data or {}
    raw_steps = data.get('steps') or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError('steps 必须是非空数组')
    steps = [normalize_step(s, i) for i, s in enumerate(raw_steps)]
    seen = set()
    for step in steps:
        if step['id'] in seen:
            raise ValueError('step id 重复：%s' % step['id'])
        seen.add(step['id'])
    unknown_deps = []
    for step in steps:
        for dep in step['depends_on']:
            if dep not in seen:
                unknown_deps.append('%s -> %s' % (step['id'], dep))
    if unknown_deps:
        raise ValueError('depends_on 引用了不存在的步骤：%s' % ', '.join(unknown_deps))
    key = _slug(data.get('key') or data.get('id') or data.get('name'))
    context = copy.deepcopy(data.get('context')) if isinstance(
        data.get('context'), dict) else {}
    _validate_controlled_worker_contract(context, steps)
    normalized = {
        'key': key,
        'name': str(data.get('name') or key),
        'description': str(data.get('description') or ''),
        'version': int(data.get('version') or 1),
        'steps': steps,
        'context': context,
    }
    outcome_status_mode = str(
        data.get('outcome_status_mode') or 'legacy').strip().lower()
    if outcome_status_mode not in ('legacy', 'composite'):
        raise ValueError(
            'outcome_status_mode: expected legacy or composite')
    if 'outcome_status_mode' in data or outcome_status_mode == 'composite':
        normalized['outcome_status_mode'] = outcome_status_mode
    if 'composite_outcomes' in data:
        if not isinstance(data.get('composite_outcomes'), bool):
            raise ValueError('composite_outcomes: expected a boolean')
        normalized['composite_outcomes'] = data.get('composite_outcomes')
    if 'start_vars_schema' in data:
        if not isinstance(data.get('start_vars_schema'), dict):
            raise ValueError('start_vars_schema: expected an object')
        normalized['start_vars_schema'] = copy.deepcopy(
            data.get('start_vars_schema'))
    if 'workflow_catalog' in data:
        catalog = data.get('workflow_catalog')
        if not isinstance(catalog, dict):
            raise ValueError('workflow_catalog: expected an object')
        kind = str(catalog.get('kind') or '').strip().lower()
        readiness = str(catalog.get('readiness') or '').strip().lower()
        if kind and kind not in WORKFLOW_CATALOG_KINDS:
            raise ValueError(
                'workflow_catalog.kind: expected business/probe/legacy')
        if readiness and readiness not in WORKFLOW_READINESS_VALUES:
            raise ValueError(
                'workflow_catalog.readiness: unsupported readiness value')
        normalized['workflow_catalog'] = {
            key: copy.deepcopy(value)
            for key, value in catalog.items()
            if key in {'kind', 'readiness', 'validation_scope'}
        }
    return normalized


def merge_workflow_definition_update(existing_definition, patch):
    """Merge an update payload into an existing workflow definition.

    The workflow key is immutable for in-place updates so callers cannot turn a
    PATCH into a hidden create/rename operation.
    """
    existing = existing_definition if isinstance(existing_definition, dict) else {}
    patch = patch if isinstance(patch, dict) else {}
    nested = patch.get('definition') if isinstance(patch.get('definition'), dict) else {}
    merged = dict(existing)
    original_key = existing.get('key') or existing.get('workflow_key') or patch.get('key')
    if original_key:
        merged['key'] = original_key
    for source in (nested, patch):
        for field in (
                'name', 'description', 'version', 'steps', 'context',
                'start_vars_schema', 'outcome_status_mode',
                'composite_outcomes', 'workflow_catalog'):
            if field in source:
                merged[field] = source[field]
    if original_key:
        merged['key'] = original_key
    normalized = normalize_workflow_definition(merged)
    # In-place updates promise an immutable key. Preserve the exact persisted
    # spelling as well as its identity; legacy keys may contain hyphens that
    # the create-time slugger would otherwise rewrite to underscores.
    if original_key:
        normalized['key'] = str(original_key)
    return normalized


def normalize_start_vars(value):
    """Normalize user supplied run start variables.

    Workflow start variables are intentionally limited to a JSON object so they
    can be passed to workers, agents, gates and branches without schema drift.
    """
    return dict(value) if isinstance(value, dict) else {}


def _dig_path(data, path):
    current = data if isinstance(data, dict) else {}
    for part in str(path or '').split('.'):
        part = part.strip()
        if not part:
            return None
        if not isinstance(current, dict) or part not in current:
            return None
        current = current.get(part)
    return current


def resolve_start_var_claw_ids(start_vars, var_path, strict=False):
    """Resolve a start_vars path to one or more OpenClaw ids."""
    raw = _dig_path(start_vars, var_path)
    values = raw if isinstance(raw, (list, tuple, set)) else [raw]
    result = []
    for value in values:
        if value in (None, ''):
            continue
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            if strict:
                raise ValueError('启动参数 %s 不是有效 Agent ID：%s' % (var_path, value))
            continue
    return sorted(set(result))


def _positive_assignment_claw_id(value, field, required=False):
    if value in (None, ''):
        if required:
            raise ValueError('%s is required' % field)
        return None
    if isinstance(value, bool):
        raise ValueError('%s must be a positive Claw ID' % field)
    try:
        claw_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('%s must be a positive Claw ID' % field) from exc
    if claw_id <= 0:
        raise ValueError('%s must be a positive Claw ID' % field)
    return claw_id


def resolve_workflow_run_assignment(
        definition, start_vars=None, worker_claw_id=None,
        selected_executor_claw_ids=None):
    """Resolve one immutable executor/reviewer assignment at Run creation.

    Definitions opt in by declaring ``worker_claw_id`` or
    ``reviewer_claw_id`` in ``start_vars_schema``, or by marking a step with
    ``assignment_role``. Legacy definitions retain their existing routing.
    """
    definition = definition if isinstance(definition, dict) else {}
    variables = normalize_start_vars(start_vars)
    schema = (
        definition.get('start_vars_schema')
        if isinstance(definition.get('start_vars_schema'), dict) else {})
    steps = [
        step for step in (definition.get('steps') or [])
        if isinstance(step, dict)]
    explicit_roles = {
        str(step.get('assignment_role') or '').strip()
        for step in steps
        if step.get('assignment_role')
    }
    role_bound = bool(
        {'worker_claw_id', 'executor_claw_id', 'reviewer_claw_id'}
        & set(schema)
    ) or bool(explicit_roles & {'executor', 'reviewer'})
    if not role_bound:
        return None

    worker = _positive_assignment_claw_id(
        worker_claw_id, 'worker_claw_id',
        required=bool(
            (schema.get('worker_claw_id') or {}).get('required')))
    declared_worker = _positive_assignment_claw_id(
        variables.get('worker_claw_id'), 'start_vars.worker_claw_id')
    if worker and declared_worker and worker != declared_worker:
        raise ValueError('worker_claw_id does not match start_vars.worker_claw_id')
    worker = worker or declared_worker

    declared_executor = _positive_assignment_claw_id(
        variables.get('executor_claw_id'), 'start_vars.executor_claw_id')
    executor = declared_executor or worker
    if worker and executor and executor != worker:
        raise ValueError('executor_claw_id must equal worker_claw_id')
    if executor is None:
        raise ValueError('executor_claw_id is required')

    selected = []
    for raw in selected_executor_claw_ids or []:
        selected.append(_positive_assignment_claw_id(
            raw, 'executor_claw_ids', required=True))
    selected = sorted(set(selected))
    if selected and selected != [executor]:
        raise ValueError('executor_claw_ids must contain only executor_claw_id')

    reviewer_spec = schema.get('reviewer_claw_id') or {}
    reviewer_required = bool(reviewer_spec.get('required')) or (
        'reviewer' in explicit_roles)
    reviewer = _positive_assignment_claw_id(
        variables.get('reviewer_claw_id'), 'start_vars.reviewer_claw_id',
        required=reviewer_required)
    if reviewer is not None and reviewer == executor:
        raise ValueError('reviewer_claw_id must differ from executor_claw_id')

    step_roles = {}
    for step in steps:
        if step.get('type') not in ('agent_task', 'worker_task'):
            continue
        step_id = str(step.get('id') or '')
        role = str(step.get('assignment_role') or '').strip()
        # Compatibility for Definition #12 v21. New definitions must declare
        # assignment_role explicitly; this exact legacy id is not generalized.
        if not role and step_id == 'peer_review_finalize':
            role = 'reviewer'
        if role not in ('executor', 'reviewer'):
            role = 'executor'
        if role == 'reviewer' and reviewer is None:
            raise ValueError('reviewer_claw_id is required for reviewer step')
        step_roles[step_id] = role

    variables['worker_claw_id'] = worker or executor
    variables['executor_claw_id'] = executor
    variables['executor_claw_ids'] = [executor]
    if reviewer is not None:
        variables['reviewer_claw_id'] = reviewer
    return {
        'schema': 'hub.workflow.assignment@1',
        'executor_claw_id': executor,
        'reviewer_claw_id': reviewer,
        'worker_claw_id': worker or executor,
        'start_vars': variables,
        'step_roles': step_roles,
        'source': {
            'executor': 'worker_binding',
            'reviewer': 'start_vars.reviewer_claw_id' if reviewer else '',
        },
    }


def materialize_workflow_run_assignment(
        definition, assignment, claw_names=None):
    """Copy a definition and freeze every execution step to its role owner."""
    value = copy.deepcopy(definition if isinstance(definition, dict) else {})
    if not isinstance(assignment, dict):
        return value
    names = claw_names if isinstance(claw_names, dict) else {}
    executor = int(assignment['executor_claw_id'])
    reviewer = assignment.get('reviewer_claw_id')
    roles = assignment.get('step_roles') or {}
    for step in value.get('steps') or []:
        if not isinstance(step, dict) or step.get('type') not in (
                'agent_task', 'worker_task'):
            continue
        role = roles.get(str(step.get('id') or ''), 'executor')
        target = int(reviewer) if role == 'reviewer' else executor
        step['assignment_role'] = role
        step['target_claw_id'] = target
        step['executor_claw_ids'] = [target]
        step['target_agent'] = names.get(target) or str(target)
        step['target_post'] = ''
    value['assignment_contract'] = {
        'schema': assignment.get('schema'),
        'executor_claw_id': executor,
        'reviewer_claw_id': reviewer,
    }
    return value


def build_workflow_start_context(start_vars=None, context=None, start_mode='immediate',
                                 schedule_cron='', executor_claw_ids=None,
                                 executor_user_ids=None, worker_claw_id=None):
    """Build the run-level context shared by every workflow step."""
    base = dict(context) if isinstance(context, dict) else {}
    merged_start_vars = normalize_start_vars(base.get('start_vars'))
    merged_start_vars.update(normalize_start_vars(start_vars))
    base['start_vars'] = merged_start_vars

    workflow_start = dict(base.get('workflow_start')) if isinstance(base.get('workflow_start'), dict) else {}
    workflow_start.update({
        'mode': start_mode or 'immediate',
        'schedule_cron': schedule_cron or '',
        'executor_claw_ids': _as_int_list(executor_claw_ids),
        'executor_user_ids': _as_int_list(executor_user_ids),
        'variables': merged_start_vars,
    })
    bound_workers = _as_int_list([worker_claw_id])
    if bound_workers:
        workflow_start['worker_claw_id'] = bound_workers[0]
        workflow_start['worker_binding_mode'] = 'single_flow_worker'
    else:
        # Binding is accepted only through the dedicated start parameter. Do
        # not trust a caller-supplied nested context to select another Worker.
        workflow_start.pop('worker_claw_id', None)
        workflow_start.pop('worker_binding_mode', None)
    base['workflow_start'] = workflow_start
    return base


def _workflow_referenced_step_ids(step, config):
    referenced = set()
    for source in (step, config):
        dependencies = source.get('depends_on') if isinstance(source, dict) else None
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if isinstance(dependencies, list):
            referenced.update(
                str(value) for value in dependencies
                if isinstance(value, (str, int)) and str(value)
            )
    try:
        serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        serialized = ''
    referenced.update(
        match.group(1)
        for match in re.finditer(
            r'(?:steps|outputs)\.([A-Za-z0-9_-]+)\.', serialized)
    )
    return referenced


def _project_workflow_agent_outputs(step, config, outputs):
    if not isinstance(outputs, dict):
        return {}
    required = _workflow_referenced_step_ids(step, config)
    return {
        step_id: value
        for step_id, value in outputs.items()
        if str(step_id) in required
    }


def build_workflow_agent_task_payload(run, step, outputs=None):
    """Build structured payload consumed by sidecar workflow_agent_task."""
    run = run if isinstance(run, dict) else {}
    step = step if isinstance(step, dict) else {}
    config = step.get('config') if isinstance(step.get('config'), dict) else {}
    run_id = run.get('id') or step.get('run_id')
    step_id = step.get('step_id') or step.get('id') or config.get('id')
    context = run.get('context') if isinstance(run.get('context'), dict) else {}
    start_vars = context.get('start_vars') if isinstance(context.get('start_vars'), dict) else {}
    references = config.get('references') if isinstance(config.get('references'), list) else []
    payload = {
        'kind': 'workflow_agent_task',
        'run_id': run_id,
        'attempt_no': step.get('attempt_no') or 1,
        'definition_version': (
            run.get('definition_version')
            or run.get('workflow_definition_version')
            or (
                context.get('execution_input_snapshot', {}) or {}
            ).get('workflow_definition_version')
        ),
        'run_name': run.get('run_name') or run.get('workflow_name') or '',
        'step_id': step_id,
        'step_name': step.get('name') or config.get('name') or step_id,
        'display_state': workflow_step_display_state(step.get('status')),
        'runner': step.get('runner') or config.get('runner') or '',
        'prompt': config.get('prompt') or step.get('prompt') or '',
        'references': references,
        'inputs': config.get('inputs') if isinstance(config.get('inputs'), dict) else {},
        'input_vars': config.get('input_vars') if isinstance(config.get('input_vars'), dict) else {},
        'outputs': _project_workflow_agent_outputs(
            step, config, outputs),
        'context': context,
        'start_vars': start_vars,
        'progress_api': '/api/v1/workflow-runs/%s/steps/%s/progress' % (run_id, step_id),
        'result_api': '/api/v1/workflow-runs/%s/steps/%s/result' % (run_id, step_id),
    }
    config_inputs = (
        config.get('inputs') if isinstance(config.get('inputs'), dict) else {})
    delivery_mode = (
        config.get('notification_delivery_mode')
        or config_inputs.get('notification_delivery_mode'))
    notification_policy = (
        config.get('notification_policy')
        if isinstance(config.get('notification_policy'), dict)
        else config_inputs.get('notification_policy'))
    if delivery_mode == 'outbox':
        payload['notification_delivery_mode'] = 'outbox'
    if isinstance(notification_policy, dict):
        payload['notification_policy'] = dict(notification_policy)
    workflow_start = (
        context.get('workflow_start')
        if isinstance(context.get('workflow_start'), dict) else {})
    worker_ids = _as_int_list([workflow_start.get('worker_claw_id')])
    if worker_ids:
        acting_ids = _as_int_list([
            step.get('target_claw_id'), config.get('target_claw_id')])
        payload['execution_route'] = {
            'mode': 'single_flow_worker',
            'worker_claw_id': worker_ids[0],
            'acting_claw_id': acting_ids[0] if acting_ids else None,
            'acting_agent': str(
                step.get('target_agent') or config.get('target_agent') or ''),
            'acting_post': str(
                step.get('target_post') or config.get('target_post') or ''),
        }
    snapshot = run.get('testcase_library_snapshot')
    if not isinstance(snapshot, dict):
        snapshot = context.get('testcase_library_snapshot')
    if isinstance(snapshot, dict):
        payload['testcase_library_snapshot'] = snapshot
    # 透传统一任务上下文包（由 step.to_dict(with_context=True) 注入）
    if isinstance(step.get('task_context'), dict):
        payload['task_context'] = step['task_context']
    if (step.get('step_type') or step.get('type') or config.get('type')) == 'notification':
        authorization = config.get('notification_authorization')
        payload['notification'] = {
            'authorized': bool(
                isinstance(authorization, dict) and authorization.get('allowed')),
            'target': '大群2',
            'credential_mode': 'hub_brokered',
            'send_api': '/api/v1/wecom/send',
            'template_version': str(
                config.get('template_version') or 'workflow-wecom-v1'),
        }
    analysis = config.get('analysis') if isinstance(config.get('analysis'), dict) else {}
    if analysis.get('enabled'):
        payload['analysis'] = dict(analysis)
        payload['analysis']['api_contract'] = {
            'create_run': {
                'method': 'POST',
                'path': '/api/v1/shift-left/analysis-runs',
            },
            'upsert_finding': {
                'method': 'PUT',
                'path': '/api/v1/shift-left/findings:upsert',
            },
            'complete_run': {
                'method': 'POST',
                'path_template': '/api/v1/shift-left/analysis-runs/{analysis_run_id}/result',
            },
            'create_report': {
                'method': 'POST',
                'path': '/api/v1/test-reports',
            },
        }
        payload['analysis']['report_binding'] = {
            'workflow_artifact_type': 'test_report',
            'result_field': analysis.get('report_id_var') or 'analysis_report_id',
        }
    return payload


def initial_step_status(step):
    """Initial status for a step when a run is created."""
    return 'waiting_approval' if step.get('approval_required') else 'pending'


def ready_step_ids(definition, step_states, strict_result_step_ids=None):
    """Return pending steps whose dependency-result policies are satisfied.

    Normal edges remain fail-closed and only accept ``passed``/``skipped``.
    A ``blocked``/``failed`` result may satisfy an edge only when either the
    upstream explicitly advances on any result or the downstream explicitly
    opts into running after an upstream blocker.
    """
    steps = definition.get('steps') or []
    step_by_id = {
        step.get('id'): step for step in steps if isinstance(step, dict)
    }
    strict_result_step_ids = set(strict_result_step_ids or ())
    ready = []
    for step in steps:
        if step_states.get(step['id'], 'pending') != 'pending':
            continue
        deps = step.get('depends_on') or []
        if all(_workflow_dependency_satisfied(
                step_by_id.get(dep) or {}, step, step_states.get(dep),
                require_downstream_opt_in=(dep in strict_result_step_ids))
               for dep in deps):
            ready.append(step['id'])
    return ready


def blocked_dependency_ids(definition, step_states, strict_result_step_ids=None):
    """Return pending steps made unreachable by terminal dependency failures.

    A fatal or propagated upstream result requires the downstream step to opt in
    explicitly. This lets Hub skip an unsafe business chain while still running
    cleanup and failure-report finalizers that declare
    ``run_even_if_upstream_blocked``.
    """
    steps = definition.get('steps') or []
    step_by_id = {
        step.get('id'): step for step in steps if isinstance(step, dict)
    }
    strict_result_step_ids = set(strict_result_step_ids or ())
    blocked = {}
    for step in steps:
        step_id = step.get('id')
        if not step_id or step_states.get(step_id, 'pending') != 'pending':
            continue
        failed_dependencies = []
        for dependency_id in step.get('depends_on') or []:
            status = step_states.get(dependency_id)
            if status not in (
                    'blocked', 'failed', PROPAGATED_UPSTREAM_BLOCKED):
                continue
            if not _workflow_dependency_satisfied(
                    step_by_id.get(dependency_id) or {}, step, status,
                    require_downstream_opt_in=(
                        dependency_id in strict_result_step_ids
                        or status == PROPAGATED_UPSTREAM_BLOCKED)):
                failed_dependencies.append(dependency_id)
        if failed_dependencies:
            blocked[step_id] = failed_dependencies
    return blocked


def _workflow_policy_inputs(step):
    inputs = step.get('inputs') if isinstance(step, dict) else None
    return inputs if isinstance(inputs, dict) else {}


def step_advances_on_any_result(step):
    """Return whether a step's terminal failure may release its dependants."""
    if not isinstance(step, dict):
        return False
    if step.get('advance_on_any_result') is True:
        return True
    if str(step.get('advance_policy') or '').strip() == 'advance_on_any_result':
        return True
    inputs = _workflow_policy_inputs(step)
    return (
        inputs.get('advance_on_any_result') is True
        or str(inputs.get('advance_policy') or '').strip()
        == 'advance_on_any_result'
    )


def step_runs_if_upstream_blocked(step):
    """Return whether a step explicitly accepts blocked/failed dependencies."""
    if not isinstance(step, dict):
        return False
    if step.get('run_even_if_upstream_blocked') is True:
        return True
    return _workflow_policy_inputs(step).get(
        'run_even_if_upstream_blocked') is True


def _workflow_dependency_satisfied(
        upstream, downstream, upstream_status,
        require_downstream_opt_in=False):
    if upstream_status == PROPAGATED_UPSTREAM_BLOCKED:
        return step_runs_if_upstream_blocked(downstream)
    if upstream_status in TERMINAL_STEP_STATUSES:
        return True
    if upstream_status not in TERMINAL_RESULT_STEP_STATUSES:
        return False
    if require_downstream_opt_in:
        return step_runs_if_upstream_blocked(downstream)
    return (
        step_advances_on_any_result(upstream)
        or step_runs_if_upstream_blocked(downstream)
    )


def _dig(payload, path):
    cur = payload
    for part in path.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _parse_expected(value):
    raw = str(value).strip()
    quoted = (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'"))
    if quoted:
        return raw[1:-1]
    if raw.lower() == 'true':
        return True
    if raw.lower() == 'false':
        return False
    try:
        if '.' in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _resolve_expected(raw, result):
    text = str(raw).strip()
    quoted = (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'"))
    if not quoted and '.' in text:
        return _dig(result or {}, text)
    return _parse_expected(text)


def _compare(actual, op, expected):
    if op == '==':
        return actual == expected
    if op == '!=':
        return actual != expected
    try:
        actual_num = float(actual)
        expected_num = float(expected)
    except (TypeError, ValueError):
        return False
    if op == '>':
        return actual_num > expected_num
    if op == '>=':
        return actual_num >= expected_num
    if op == '<':
        return actual_num < expected_num
    if op == '<=':
        return actual_num <= expected_num
    return False


def evaluate_gate_expression(expression, result):
    """Evaluate a small whitelist expression such as ``metrics.failed == 0``."""
    text = str(expression or '').strip()
    for op in ('>=', '<=', '==', '!=', '>', '<'):
        if op in text:
            left, right = text.split(op, 1)
            actual = _dig(result or {}, left.strip())
            expected = _resolve_expected(right, result)
            return _compare(actual, op, expected)
    raise ValueError('不支持的 gate 表达式：%s' % expression)


def evaluate_step_gates(step, result):
    """Evaluate all gates for a step result."""
    failed = []
    for gate in step.get('gates') or []:
        expression = gate.get('expression')
        try:
            ok = evaluate_gate_expression(expression, result)
        except ValueError as exc:
            ok = False
            gate = dict(gate)
            gate['error'] = str(exc)
        if not ok:
            failed.append({
                'expression': expression,
                'on_fail': gate.get('on_fail') or 'blocked',
                'message': gate.get('message') or '',
                'error': gate.get('error') or '',
            })
    result_status = result.get('status') or 'passed'
    if not failed:
        return {
            'passed': True,
            'status': result_status,
            'failed_gates': [],
            'warning_only': False,
        }
    blocking = [
        gate for gate in failed if gate.get('on_fail') != 'warn'
    ]
    if not blocking:
        return {
            'passed': True,
            'status': result_status,
            'failed_gates': failed,
            'warning_only': True,
        }
    status = (
        blocking[0].get('on_fail')
        if blocking[0].get('on_fail') in STEP_STATUSES
        else 'blocked'
    )
    return {
        'passed': False,
        'status': status,
        'failed_gates': failed,
        'warning_only': False,
    }


def _contract_path_exists(payload, path):
    """Return true when a declared path exists, preserving valid false/zero values."""
    current = payload if isinstance(payload, dict) else {}
    parts = [part for part in str(path or '').strip().split('.') if part]
    if not parts:
        return False
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _contract_field_present(payload, path, allow_empty_array=False):
    if not _contract_path_exists(payload, path):
        return False
    value = _dig(payload, path)
    # Empty result sets are legitimate outputs for queue/list operations.  A
    # present [] means "evaluated and found none", not "worker forgot field".
    # Evidence and metrics remain strict unless explicitly configured.
    if allow_empty_array and isinstance(value, list):
        return True
    return value not in (None, '', [], {})


def _contract_fields(value):
    fields = []
    for raw in value or []:
        if isinstance(raw, dict):
            raw = raw.get('name') or raw.get('path') or raw.get('key')
        name = str(raw or '').strip()
        if name and name not in fields:
            fields.append(name)
    return fields


def step_contract_failure_policy(step):
    """Resolve contract failure behavior without weakening explicit blockers."""
    step = step if isinstance(step, dict) else {}
    explicit = str(step.get('contract_on_fail') or '').strip()
    if explicit in ('blocked', 'failed', 'warn'):
        return explicit
    gates = step.get('gates') if isinstance(step.get('gates'), list) else []
    if gates and all(str(gate.get('on_fail') or 'blocked') == 'warn'
                     for gate in gates if isinstance(gate, dict)):
        return 'warn'
    return 'blocked'


def collect_declared_step_outputs(step, result):
    """Persist every declared output, accepting legacy top-level writebacks."""
    step = step if isinstance(step, dict) else {}
    result = result if isinstance(result, dict) else {}
    outputs = dict(result.get('outputs') or {}) if isinstance(
        result.get('outputs'), dict) else {}
    declared = _contract_fields(step.get('outputs'))
    control_fields = (
        'business_failure_confirmed', 'notification_required',
        'report_required', 'business_conclusion', 'automation_conclusion',
        'automation_failure_confirmed', 'automation_error_count',
    )
    for name in list(dict.fromkeys(declared + list(control_fields))):
        if _contract_path_exists(outputs, name):
            continue
        if _contract_path_exists(result, name):
            value = _dig(result, name)
            target = outputs
            parts = name.split('.')
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
    return outputs


def validate_step_result_contract(step, result):
    """Validate declared metrics, evidence and outputs on every result writeback."""
    step = step if isinstance(step, dict) else {}
    result = result if isinstance(result, dict) else {}
    sections = {
        'metrics': _contract_fields(step.get('required_metrics')),
        'evidence': _contract_fields(step.get('required_evidence')),
        # Definitions may declare every possible output for downstream
        # mapping while requiring only the branch-common subset.  Definitions
        # without required_outputs keep the legacy strict behavior.
        'outputs': _contract_fields(
            step.get('required_outputs')
            if 'required_outputs' in step else step.get('outputs')),
    }
    allow_empty = (
        step.get('allow_empty_contract_fields')
        if isinstance(step.get('allow_empty_contract_fields'), dict)
        else {})
    missing = {}
    for section, fields in sections.items():
        payload = result.get(section) if isinstance(result.get(section), dict) else {}
        empty_is_valid = set(_contract_fields(allow_empty.get(section)))
        absent = [
            field for field in fields
            if not (
                _contract_path_exists(payload, field)
                if field in empty_is_valid
                else _contract_field_present(
                    payload, field, allow_empty_array=(section == 'outputs'))
            )
        ]
        if absent:
            missing[section] = absent
    valid = not missing
    return {
        'valid': valid,
        'code': 'CONTRACT_VALID' if valid else 'CONTRACT_INVALID',
        'policy': step_contract_failure_policy(step),
        'declared': sections,
        'missing': missing,
    }


def _find_output_value(outputs, key):
    """Find the last non-empty key in deterministic step/output insertion order."""
    found = None
    def visit(value):
        nonlocal found
        if not isinstance(value, dict):
            return
        if key in value and value.get(key) not in (None, ''):
            found = value.get(key)
        for nested in value.values():
            if isinstance(nested, dict):
                visit(nested)
    visit(outputs if isinstance(outputs, dict) else {})
    return found


def _notification_policy_snapshot(notification_policy):
    policy = notification_policy if isinstance(notification_policy, dict) else {}
    return {
        'strict_silent': policy.get('strict_silent') is True,
        'notification_required': policy.get('notification_required') is True,
        'no_external_notification': policy.get('no_external_notification') is True,
        'version': str(policy.get('version') or '').strip(),
    }


def evaluate_notification_authorization(
        outputs, report_readback=False, notification_policy=None):
    """Platform hard gate for brokered Workflow group notifications."""
    outputs = outputs if isinstance(outputs, dict) else {}
    checks = {
        'business_failure_confirmed': _find_output_value(
            outputs, 'business_failure_confirmed') is True,
        'notification_required': _find_output_value(
            outputs, 'notification_required') is True,
        'report_required': _find_output_value(outputs, 'report_required') is True,
        'hub_report_id': _find_output_value(outputs, 'hub_report_id') not in (None, ''),
        'share_url': bool(str(_find_output_value(outputs, 'share_url') or '').strip()),
        'report_readback': bool(report_readback),
    }
    policy = _notification_policy_snapshot(notification_policy)
    strict_silent = (
        policy['strict_silent']
        and not policy['notification_required']
    )
    allowed = all(checks.values()) and not strict_silent
    skip_reason_code = (
        (policy['version'] or 'notification_policy_strict_silent')
        if strict_silent else 'business_pass_or_automation_only'
    )
    return {
        'allowed': allowed,
        'checks': checks,
        'notification_policy': policy,
        'strict_silent': strict_silent,
        'notification_skipped': not allowed,
        'wecom_sent': False,
        'skip_reason': '' if allowed else skip_reason_code,
        'skip_reason_code': '' if allowed else skip_reason_code,
    }


def validate_notification_result_contract(
        result, authorization, sent_audit=False, notification_policy=None):
    """Reject notification claims that were not authorized and audited by Hub."""
    result = result if isinstance(result, dict) else {}
    authorization = authorization if isinstance(authorization, dict) else {}
    metrics = result.get('metrics') if isinstance(result.get('metrics'), dict) else {}
    outputs = result.get('outputs') if isinstance(result.get('outputs'), dict) else {}
    claim_keys = {
        'notification_skipped', 'wecom_sent', 'skip_reason',
        'skip_reason_code', 'skip_reason_detail', 'wecom_log_id',
    }
    has_claim = any(
        _contract_path_exists(section, key)
        for section in (metrics, outputs)
        for key in claim_keys
    )
    if not has_claim:
        return {
            'valid': True,
            'code': 'NOTIFICATION_NOT_CLAIMED',
            'checked': False,
            'violations': [],
        }

    claimed_sent = any(
        _find_output_value(section, 'wecom_sent') is True
        for section in (metrics, outputs)
    )
    claimed_skipped = next((
        _find_output_value(section, 'notification_skipped')
        for section in (outputs, metrics)
        if _find_output_value(section, 'notification_skipped') is not None
    ), None)
    skip_reason = next((
        str(_find_output_value(section, 'skip_reason') or '').strip()
        for section in (outputs, metrics)
        if _find_output_value(section, 'skip_reason') is not None
    ), '')
    explicit_reason_code = next((
        str(_find_output_value(section, 'skip_reason_code') or '').strip()
        for section in (outputs, metrics)
        if _find_output_value(section, 'skip_reason_code') is not None
    ), '')
    explicit_reason_detail = next((
        str(_find_output_value(section, 'skip_reason_detail') or '').strip()
        for section in (outputs, metrics)
        if _find_output_value(section, 'skip_reason_detail') is not None
    ), '')
    allowed = authorization.get('allowed') is True
    policy = _notification_policy_snapshot(
        notification_policy or authorization.get('notification_policy'))
    strict_silent = (
        authorization.get('strict_silent') is True
        or (policy['strict_silent'] and not policy['notification_required'])
    )
    expected_reason_code = (
        (policy['version'] or 'notification_policy_strict_silent')
        if strict_silent else 'business_pass_or_automation_only'
    )
    violations = []
    if claimed_sent and not allowed:
        violations.append('notification_not_authorized')
    if claimed_sent and not sent_audit:
        violations.append('wecom_send_audit_missing')
    if allowed and not claimed_sent:
        violations.append('authorized_notification_not_sent')
    if not allowed and claimed_skipped is not True:
        violations.append('notification_skipped_must_be_true')
    if not allowed:
        if strict_silent:
            # The frozen policy is the authorization fact. Human-readable text
            # may evolve without turning a legitimate no-send into a blocker.
            # A structured code, when supplied, must still match that policy.
            if explicit_reason_code and explicit_reason_code != expected_reason_code:
                violations.append('skip_reason_invalid')
        elif skip_reason != expected_reason_code:
            violations.append('skip_reason_invalid')

    reason_detail = explicit_reason_detail or skip_reason

    return {
        'valid': not violations,
        'code': ('NOTIFICATION_CONTRACT_VALID' if not violations
                 else 'NOTIFICATION_CONTRACT_INVALID'),
        'checked': True,
        'authorized': allowed,
        'sent_audit': bool(sent_audit),
        'claimed_sent': claimed_sent,
        'claimed_skipped': claimed_skipped,
        'skip_reason': skip_reason,
        'skip_reason_code': expected_reason_code if not allowed else '',
        'skip_reason_detail': reason_detail if not allowed else '',
        'strict_silent': strict_silent,
        'violations': violations,
    }


def evaluate_step_branches(step, result):
    """Evaluate a step's if/else branch rules.

    Returns selected and skipped step ids. Non-selected branch targets should be
    marked skipped by the orchestration layer while selected targets stay pending.
    """
    selected = []
    skipped = []
    decisions = []
    for branch in step.get('branches') or []:
        expression = branch.get('if') or branch.get('expression')
        try:
            matched = evaluate_gate_expression(expression, result or {})
            error = ''
        except ValueError as exc:
            matched = False
            error = str(exc)
        then_ids = _as_list(branch.get('then'))
        else_ids = _as_list(branch.get('else'))
        chosen = then_ids if matched else else_ids
        rejected = else_ids if matched else then_ids
        selected.extend(chosen)
        skipped.extend(rejected)
        decisions.append({
            'expression': expression,
            'matched': bool(matched),
            'selected': chosen,
            'skipped': rejected,
            'error': error,
        })
    return {
        'selected': sorted(set(selected), key=selected.index),
        'skipped': sorted(set(skipped), key=skipped.index),
        'decisions': decisions,
    }

