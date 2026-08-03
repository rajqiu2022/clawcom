"""Pure helpers for Hub Workflow Runbook orchestration.

The service intentionally avoids Flask/SQLAlchemy dependencies so the workflow
state rules can be tested without booting the Hub app.
"""

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

TERMINAL_STEP_STATUSES = {'passed', 'skipped'}
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


def _same_actor(owner_type, owner_id, actor_type, actor_id):
    try:
        return str(owner_type or '') == str(actor_type or '') and int(owner_id) == int(actor_id)
    except (TypeError, ValueError):
        return False


def can_execute_workflow(owner_type, owner_id, executor_acl, actor_type, actor_id, is_admin=False):
    """Return whether actor can create a run from a definition."""
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
        return actor_id in acl.get('claw_ids', [])
    if actor_type == 'user':
        return actor_id in acl.get('user_ids', [])
    return False


def can_manage_workflow(owner_type, owner_id, executor_acl, actor_type, actor_id, is_admin=False):
    """Return whether actor can change workflow executor permissions."""
    if is_admin:
        return True
    return _same_actor(owner_type, owner_id, actor_type, actor_id)


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


def normalize_step(step, idx):
    """Normalize a single workflow step definition."""
    if not isinstance(step, dict):
        raise ValueError('step 必须是对象')
    step_id = _slug(step.get('id') or step.get('key') or step.get('name') or f'step_{idx + 1}')
    step_type = step.get('type') or ('approval' if step.get('approval_required') else 'worker_task')
    if step_type not in VALID_STEP_TYPES:
        step_type = 'worker_task'
    approval_required = bool(step.get('approval_required') or step_type == 'approval')
    normalized = {
        'id': step_id,
        'name': str(step.get('name') or step.get('title') or step_id),
        'type': step_type,
        'runner': str(step.get('runner') or ''),
        'depends_on': _as_list(step.get('depends_on')),
        'approval_required': approval_required,
        'gates': [g for g in (step.get('gates') or []) if isinstance(g, dict)],
        'target_agent': str(step.get('target_agent') or ''),
        'target_post': str(step.get('target_post') or '').strip(),
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
    for key in ('auto_block_on_heartbeat_loss', 'heartbeat_auto_block', 'auto_block_on_no_response'):
        if key in step:
            normalized[key] = bool(step.get(key))
    for key in ('target_claw_id_var', 'target_claw_ids_var', 'executor_claw_ids_var'):
        if step.get(key):
            normalized[key] = str(step.get(key)).strip()
    if 'notify_agent_on_start' in step:
        normalized['notify_agent_on_start'] = bool(step.get('notify_agent_on_start'))
    return normalized


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
    return {
        'key': key,
        'name': str(data.get('name') or key),
        'description': str(data.get('description') or ''),
        'version': int(data.get('version') or 1),
        'steps': steps,
        'context': data.get('context') if isinstance(data.get('context'), dict) else {},
    }


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
        for field in ('name', 'description', 'version', 'steps', 'context'):
            if field in source:
                merged[field] = source[field]
    if original_key:
        merged['key'] = original_key
    return normalize_workflow_definition(merged)


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


def build_workflow_start_context(start_vars=None, context=None, start_mode='immediate',
                                 schedule_cron='', executor_claw_ids=None,
                                 executor_user_ids=None):
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
    base['workflow_start'] = workflow_start
    return base


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
        'run_name': run.get('run_name') or run.get('workflow_name') or '',
        'step_id': step_id,
        'step_name': step.get('name') or config.get('name') or step_id,
        'display_state': workflow_step_display_state(step.get('status')),
        'runner': step.get('runner') or config.get('runner') or '',
        'prompt': config.get('prompt') or step.get('prompt') or '',
        'references': references,
        'inputs': config.get('inputs') if isinstance(config.get('inputs'), dict) else {},
        'input_vars': config.get('input_vars') if isinstance(config.get('input_vars'), dict) else {},
        'outputs': outputs if isinstance(outputs, dict) else {},
        'context': context,
        'start_vars': start_vars,
        'progress_api': '/api/v1/workflow-runs/%s/steps/%s/progress' % (run_id, step_id),
        'result_api': '/api/v1/workflow-runs/%s/steps/%s/result' % (run_id, step_id),
    }
    # 透传统一任务上下文包（由 step.to_dict(with_context=True) 注入）
    if isinstance(step.get('task_context'), dict):
        payload['task_context'] = step['task_context']
    return payload


def initial_step_status(step):
    """Initial status for a step when a run is created."""
    return 'waiting_approval' if step.get('approval_required') else 'pending'


def ready_step_ids(definition, step_states):
    """Return pending step ids whose dependencies are all terminal success states."""
    ready = []
    for step in definition.get('steps') or []:
        if step_states.get(step['id'], 'pending') != 'pending':
            continue
        deps = step.get('depends_on') or []
        if all(step_states.get(dep) in TERMINAL_STEP_STATUSES for dep in deps):
            ready.append(step['id'])
    return ready


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
    if not failed:
        return {'passed': True, 'status': result.get('status') or 'passed', 'failed_gates': []}
    status = failed[0].get('on_fail') if failed[0].get('on_fail') in STEP_STATUSES else 'blocked'
    return {'passed': False, 'status': status, 'failed_gates': failed}


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

