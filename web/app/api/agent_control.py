"""Hub APIs for the opt-in long-running Agent control-plane pilot."""

from datetime import timedelta
from uuid import uuid4

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    is_admin_user,
    user_project_ids,
)
from app.models import (
    AgentControlGate,
    AgentGoal,
    AgentTransitionReceipt,
    AgentTurn,
    GoalTodo,
    Project,
)
from app.services.agent_authorization import (
    AuthorizationSigningError,
    public_key_document,
)
from app.services.long_agent_control import (
    ACTIVE_GOAL_STATUSES,
    ControlPlaneConflict,
    ControlPlaneRejected,
    accept_writeback,
    authorize_turn_effects,
    dispatch_turn,
    normalize_capabilities,
    now_cst_naive,
    should_run,
    stable_hash,
    todo_requires_claim,
)


GOAL_STATUSES = {
    'DRAFT', 'ACTIVE', 'WAITING_USER', 'WAITING_EVIDENCE',
    'REPLAN_REQUIRED', 'REPAIR_REQUIRED', 'PAUSED', 'COMPLETED', 'ARCHIVED',
}
CONTROL_MODES = {
    'legacy_passthrough', 'shadow', 'warn', 'enforce_p0', 'enforce_p1',
}
TODO_TASK_CLASSES = {
    'advancement_task', 'continuous_monitor', 'user_action', 'user_gate',
}
TODO_STATUSES = {'open', 'claimed', 'waiting', 'completed', 'cancelled', 'superseded'}
GATE_LEVELS = {'hard', 'automatic', 'warning'}
GATE_SCOPES = {'work_item', 'agent_lane', 'goal'}


def _request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex)[:128]


def _error(code, message, status=400, details=None):
    return jsonify({
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': _request_id(),
    }), status


@api_bp.errorhandler(ControlPlaneRejected)
def _handle_control_plane_rejected(exc):
    db.session.rollback()
    return _error(exc.code, str(exc), exc.status_code)


@api_bp.errorhandler(ControlPlaneConflict)
def _handle_control_plane_conflict(exc):
    db.session.rollback()
    return _error(exc.code, str(exc), 409)


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}',
            'admin': claw.role == 'admin', 'claw': claw,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': getattr(user, 'username', '') or f'user:{user.id}',
            'admin': is_admin_user(), 'user': user,
        }
    return None


def _can_access_project(actor, project_id):
    if not actor:
        return False
    if actor['admin']:
        return True
    if actor['type'] == 'claw':
        claw = actor['claw']
        return bool(claw.project_id and int(claw.project_id) == int(project_id))
    return int(project_id) in user_project_ids(actor['user'])


def _require_goal(actor, goal_id, lock=False):
    query = AgentGoal.query.filter_by(id=goal_id)
    if lock:
        query = query.with_for_update()
    goal = query.first()
    if not goal or not _can_access_project(actor, goal.project_id):
        return None
    return goal


def _idempotency_key(data, required=True):
    body = str((data or {}).get('idempotency_key') or '').strip()
    header = str(request.headers.get('Idempotency-Key') or '').strip()
    if body and header and body != header:
        raise ControlPlaneRejected(
            'IDEMPOTENCY_KEY_MISMATCH',
            'body and header idempotency keys must match')
    value = body or header
    if required and not value:
        raise ControlPlaneRejected(
            'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key is required')
    if len(value) > 128:
        raise ControlPlaneRejected(
            'IDEMPOTENCY_KEY_TOO_LONG',
            'Idempotency-Key must not exceed 128 characters')
    return value


def _positive_int(value, field, default=0, minimum=0, maximum=1000000):
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError):
        raise ControlPlaneRejected('VALIDATION_FAILED', f'{field} must be an integer')
    if result < minimum or result > maximum:
        raise ControlPlaneRejected(
            'VALIDATION_FAILED', f'{field} must be between {minimum} and {maximum}')
    return result


@api_bp.route('/agent-goals', methods=['POST'])
def create_agent_goal():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    if actor['type'] != 'user':
        return _error(
            'HUMAN_AUTH_REQUIRED',
            'A signed-in Hub user must create a formal Goal', 403)
    data = request.get_json(silent=True) or {}
    project_id = _positive_int(data.get('project_id'), 'project_id', minimum=1)
    if not db.session.get(Project, project_id):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_FORBIDDEN', 'Project access is required', 403)
    title = str(data.get('title') or '').strip()
    objective = str(data.get('objective') or '').strip()
    status = str(data.get('status') or 'ACTIVE').strip().upper()
    control_mode = str(data.get('control_mode') or 'shadow').strip().lower()
    if not title or not objective:
        return _error('VALIDATION_FAILED', 'title and objective are required')
    if status not in GOAL_STATUSES or control_mode not in CONTROL_MODES:
        return _error('VALIDATION_FAILED', 'unsupported status or control_mode')
    goal = AgentGoal(
        project_id=project_id,
        title=title[:255],
        objective=objective,
        scope_json=data.get('scope') if isinstance(data.get('scope'), dict) else {},
        non_goals_json=data.get('non_goals') if isinstance(data.get('non_goals'), list) else [],
        authority_sources_json=(
            data.get('authority_sources')
            if isinstance(data.get('authority_sources'), list) else []),
        current_belief=str(data.get('current_belief') or ''),
        next_action=str(data.get('next_action') or ''),
        status=status,
        priority=str(data.get('priority') or 'P1')[:16],
        compute_quota=_positive_int(data.get('compute_quota'), 'compute_quota'),
        control_mode=control_mode,
        created_by_type=actor['type'],
        created_by_id=actor['id'],
        created_by_name=actor['name'],
    )
    db.session.add(goal)
    db.session.commit()
    return jsonify({'goal': goal.to_dict(), 'control_mode': control_mode}), 201


@api_bp.route('/agent-goals', methods=['GET'])
def list_agent_goals():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    query = AgentGoal.query
    project_id = request.args.get('project_id', type=int)
    if project_id:
        if not _can_access_project(actor, project_id):
            return _error('PROJECT_FORBIDDEN', 'Project access is required', 403)
        query = query.filter_by(project_id=project_id)
    elif not actor['admin']:
        if actor['type'] == 'claw':
            query = query.filter_by(project_id=actor['claw'].project_id)
        else:
            query = query.filter(AgentGoal.project_id.in_(user_project_ids(actor['user'])))
    status = str(request.args.get('status') or '').strip().upper()
    if status:
        query = query.filter_by(status=status)
    rows = query.order_by(AgentGoal.updated_at.desc(), AgentGoal.id.desc()).all()
    return jsonify({'items': [row.to_dict() for row in rows], 'total': len(rows)})


@api_bp.route('/agent-goals/<int:goal_id>', methods=['GET'])
def get_agent_goal(goal_id):
    actor = _actor()
    goal = _require_goal(actor, goal_id)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    payload = goal.to_dict()
    payload['todos'] = [row.to_dict() for row in GoalTodo.query.filter_by(
        goal_id=goal.id).order_by(GoalTodo.id.asc()).all()]
    payload['open_gates'] = [row.to_dict() for row in AgentControlGate.query.filter_by(
        goal_id=goal.id, status='open').order_by(AgentControlGate.id.asc()).all()]
    return jsonify(payload)


@api_bp.route('/agent-goals/<int:goal_id>', methods=['PATCH'])
def patch_agent_goal(goal_id):
    actor = _actor()
    if not actor or actor['type'] != 'user':
        return _error(
            'HUMAN_AUTH_REQUIRED',
            'A signed-in Hub user must update a formal Goal', 403)
    goal = _require_goal(actor, goal_id, lock=True)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    data = request.get_json(silent=True) or {}
    expected = _positive_int(data.get('expected_version'), 'expected_version', minimum=1)
    if goal.version != expected:
        return _error(
            'GOAL_VERSION_CONFLICT', 'Goal version is stale', 409,
            {'current_version': goal.version})
    if 'status' in data:
        status = str(data.get('status') or '').strip().upper()
        if status not in GOAL_STATUSES:
            return _error('VALIDATION_FAILED', 'unsupported Goal status')
        goal.status = status
    if 'control_mode' in data:
        mode = str(data.get('control_mode') or '').strip().lower()
        if mode not in CONTROL_MODES:
            return _error('VALIDATION_FAILED', 'unsupported control_mode')
        goal.control_mode = mode
    for field in ('title', 'objective', 'current_belief', 'next_action'):
        if field in data:
            setattr(goal, field, str(data.get(field) or '').strip())
    goal.version += 1
    db.session.commit()
    return jsonify(goal.to_dict())


def _transition_goal(goal_id, target_status):
    actor = _actor()
    goal = _require_goal(actor, goal_id, lock=True)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    data = request.get_json(silent=True) or {}
    expected = _positive_int(
        data.get('expected_version'), 'expected_version', minimum=1)
    if goal.version != expected:
        return _error(
            'GOAL_VERSION_CONFLICT', 'Goal version is stale', 409,
            {'current_version': goal.version})
    goal.status = target_status
    goal.version = int(goal.version or 0) + 1
    db.session.commit()
    return jsonify(goal.to_dict())


@api_bp.route('/agent-goals/<int:goal_id>/pause', methods=['POST'])
def pause_agent_goal(goal_id):
    return _transition_goal(goal_id, 'PAUSED')


@api_bp.route('/agent-goals/<int:goal_id>/resume', methods=['POST'])
def resume_agent_goal(goal_id):
    return _transition_goal(goal_id, 'ACTIVE')


@api_bp.route('/agent-goals/<int:goal_id>/archive', methods=['POST'])
def archive_agent_goal(goal_id):
    return _transition_goal(goal_id, 'ARCHIVED')


@api_bp.route('/agent-goals/<int:goal_id>/todos', methods=['POST'])
def create_goal_todo(goal_id):
    actor = _actor()
    if not actor or actor['type'] != 'user':
        return _error(
            'HUMAN_AUTH_REQUIRED',
            'A signed-in Hub user must create a formal Todo', 403)
    goal = _require_goal(actor, goal_id)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()
    task_class = str(data.get('task_class') or 'advancement_task').strip()
    if not title or task_class not in TODO_TASK_CLASSES:
        return _error('VALIDATION_FAILED', 'title or task_class is invalid')
    todo = GoalTodo(
        goal_id=goal.id,
        title=title[:255],
        description=str(data.get('description') or ''),
        task_class=task_class,
        action_kind=str(data.get('action_kind') or 'analyze').strip()[:64],
        priority=str(data.get('priority') or 'P1').strip()[:16],
        status='open',
        assigned_agent_id=data.get('assigned_agent_id'),
        authorization_envelope_json=(
            data.get('authorization_envelope')
            if isinstance(data.get('authorization_envelope'), dict) else {}),
        required_capabilities_json=(
            data.get('required_capabilities')
            if isinstance(data.get('required_capabilities'), list) else []),
        required_write_scopes_json=(
            data.get('required_write_scopes')
            if isinstance(data.get('required_write_scopes'), list) else []),
        resume_when_json=(
            data.get('resume_when') if isinstance(data.get('resume_when'), dict) else {}),
        completion_criteria_json=(
            data.get('completion_criteria')
            if isinstance(data.get('completion_criteria'), dict) else {}),
        verification_policy_json=(
            data.get('verification_policy')
            if isinstance(data.get('verification_policy'), dict) else {}),
    )
    db.session.add(todo)
    goal.version = int(goal.version or 0) + 1
    db.session.commit()
    return jsonify({'todo': todo.to_dict(), 'goal_version': goal.version}), 201


@api_bp.route('/agent-goals/<int:goal_id>/todos', methods=['GET'])
def list_goal_todos(goal_id):
    actor = _actor()
    goal = _require_goal(actor, goal_id)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    rows = GoalTodo.query.filter_by(goal_id=goal.id).order_by(
        GoalTodo.id.asc()).all()
    return jsonify({'items': [row.to_dict() for row in rows], 'total': len(rows)})


def _require_todo(actor, todo_id, lock=False):
    query = GoalTodo.query.filter_by(id=todo_id)
    if lock:
        query = query.with_for_update()
    todo = query.first()
    if not todo:
        return None, None
    goal = _require_goal(actor, todo.goal_id)
    return (goal, todo) if goal else (None, None)


@api_bp.route('/goal-todos/<int:todo_id>', methods=['GET'])
def get_goal_todo(todo_id):
    actor = _actor()
    goal, todo = _require_todo(actor, todo_id)
    if not todo:
        return _error('TODO_NOT_FOUND', 'Todo was not found', 404)
    return jsonify({'todo': todo.to_dict(), 'goal_version': goal.version})


@api_bp.route('/goal-todos/<int:todo_id>', methods=['PATCH'])
def patch_goal_todo(todo_id):
    actor = _actor()
    goal, todo = _require_todo(actor, todo_id, lock=True)
    if not todo:
        return _error('TODO_NOT_FOUND', 'Todo was not found', 404)
    data = request.get_json(silent=True) or {}
    expected = _positive_int(
        data.get('expected_version'), 'expected_version', minimum=1)
    if todo.version != expected:
        return _error(
            'TODO_VERSION_CONFLICT', 'Todo version is stale', 409,
            {'current_version': todo.version})
    if 'task_class' in data:
        task_class = str(data.get('task_class') or '').strip()
        if task_class not in TODO_TASK_CLASSES:
            return _error('VALIDATION_FAILED', 'unsupported task_class')
        todo.task_class = task_class
    if 'status' in data:
        status = str(data.get('status') or '').strip().lower()
        if status not in TODO_STATUSES or status == 'claimed':
            return _error(
                'VALIDATION_FAILED',
                'status must be open, waiting, completed, cancelled or superseded')
        todo.status = status
        if status in ('completed', 'cancelled', 'superseded'):
            todo.claimed_claw_id = None
            todo.claimed_by_worker_id = ''
            todo.claimed_at = None
            todo.claim_expires_at = None
    for field in ('title', 'description', 'action_kind', 'priority'):
        if field in data:
            value = str(data.get(field) or '').strip()
            if field == 'title' and not value:
                return _error('VALIDATION_FAILED', 'title cannot be empty')
            setattr(todo, field, value[:255] if field == 'title' else value)
    if 'assigned_agent_id' in data:
        todo.assigned_agent_id = (
            _positive_int(data.get('assigned_agent_id'), 'assigned_agent_id', minimum=1)
            if data.get('assigned_agent_id') is not None else None)
    json_fields = {
        'authorization_envelope': ('authorization_envelope_json', dict),
        'required_capabilities': ('required_capabilities_json', list),
        'required_write_scopes': ('required_write_scopes_json', list),
        'resume_when': ('resume_when_json', dict),
        'completion_criteria': ('completion_criteria_json', dict),
        'verification_policy': ('verification_policy_json', dict),
    }
    for public_name, (model_name, expected_type) in json_fields.items():
        if public_name in data:
            value = data.get(public_name)
            if not isinstance(value, expected_type):
                return _error(
                    'VALIDATION_FAILED',
                    f'{public_name} must be a {expected_type.__name__}')
            setattr(todo, model_name, value)
    todo.version = int(todo.version or 0) + 1
    goal.version = int(goal.version or 0) + 1
    db.session.commit()
    return jsonify({'todo': todo.to_dict(), 'goal_version': goal.version})


@api_bp.route('/goal-todos/<int:todo_id>/claim', methods=['POST'])
def claim_goal_todo(todo_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    goal, todo = _require_todo(actor, todo_id, lock=True)
    if not todo:
        return _error('TODO_NOT_FOUND', 'Todo was not found', 404)
    if goal.control_mode != 'legacy_passthrough':
        return _error(
            'ATOMIC_DISPATCH_REQUIRED',
            'This Todo must be claimed by atomic Turn Dispatch', 409)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    if not worker_id:
        return _error('WORKER_ID_REQUIRED', 'worker_id is required')
    if not todo_requires_claim(todo):
        return jsonify({'ok': True, 'claim_required': False, 'todo': todo.to_dict()})
    lease_seconds = _positive_int(
        data.get('lease_seconds'), 'lease_seconds', 180, 30, 900)
    now = now_cst_naive()
    active = bool(todo.claim_expires_at and todo.claim_expires_at > now)
    same_owner = bool(
        active and todo.claimed_claw_id == actor['id']
        and todo.claimed_by_worker_id == worker_id)
    if active and not same_owner:
        return _error('CLAIM_CONFLICT', 'Todo is already claimed', 409, todo.to_dict())
    if not same_owner:
        todo.fencing_token = int(todo.fencing_token or 0) + 1
        todo.claimed_at = now
        todo.version += 1
    todo.claimed_claw_id = actor['id']
    todo.claimed_by_worker_id = worker_id
    todo.claim_lease_seconds = lease_seconds
    todo.claim_expires_at = now + timedelta(seconds=lease_seconds)
    todo.status = 'claimed'
    db.session.commit()
    return jsonify({'ok': True, 'claim_required': True, 'todo': todo.to_dict()})


@api_bp.route('/goal-todos/<int:todo_id>/heartbeat', methods=['POST'])
def heartbeat_goal_todo(todo_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    goal, todo = _require_todo(actor, todo_id, lock=True)
    if not todo:
        return _error('TODO_NOT_FOUND', 'Todo was not found', 404)
    if goal.control_mode != 'legacy_passthrough':
        return _error(
            'TURN_HEARTBEAT_REQUIRED',
            'This Todo lease must be renewed through its active Turn', 409)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    fencing = _positive_int(data.get('fencing_token'), 'fencing_token')
    now = now_cst_naive()
    if (todo.claimed_claw_id != actor['id']
            or todo.claimed_by_worker_id != worker_id):
        return _error('CLAIM_OWNER_MISMATCH', 'Claim owner does not match', 409)
    if todo.claim_expires_at and todo.claim_expires_at <= now:
        return _error('CLAIM_EXPIRED', 'Claim has expired', 409)
    if int(todo.fencing_token or 0) != fencing:
        return _error('FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
    todo.claim_expires_at = now + timedelta(
        seconds=todo.claim_lease_seconds or 180)
    db.session.commit()
    return jsonify({'ok': True, 'todo': todo.to_dict()})


@api_bp.route('/goal-todos/<int:todo_id>/release', methods=['POST'])
def release_goal_todo(todo_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    goal, todo = _require_todo(actor, todo_id, lock=True)
    if not todo:
        return _error('TODO_NOT_FOUND', 'Todo was not found', 404)
    if goal.control_mode != 'legacy_passthrough':
        return _error(
            'TURN_RELEASE_REQUIRED',
            'This Todo lease must be released through its active Turn', 409)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    fencing = _positive_int(data.get('fencing_token'), 'fencing_token')
    if (todo.claimed_claw_id != actor['id']
            or todo.claimed_by_worker_id != worker_id):
        return _error('CLAIM_OWNER_MISMATCH', 'Claim owner does not match', 409)
    if int(todo.fencing_token or 0) != fencing:
        return _error('FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
    todo.claimed_claw_id = None
    todo.claimed_by_worker_id = ''
    todo.claimed_at = None
    todo.claim_expires_at = None
    if todo.status == 'claimed':
        todo.status = 'open'
    todo.version += 1
    db.session.commit()
    return jsonify({'ok': True, 'todo': todo.to_dict()})


@api_bp.route('/control-gates', methods=['POST'])
@api_bp.route('/agent-goals/<int:goal_id>/gates', methods=['POST'])
def create_control_gate(goal_id=None):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    goal_id = _positive_int(
        goal_id if goal_id is not None else data.get('goal_id'),
        'goal_id', minimum=1)
    goal = _require_goal(actor, goal_id)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    try:
        idem = _idempotency_key(data)
    except ControlPlaneRejected as exc:
        return _error(exc.code, str(exc), exc.status_code)
    policy = str(data.get('policy_level') or 'hard').strip().lower()
    scope = str(data.get('blocking_scope') or 'work_item').strip().lower()
    gate_type = str(data.get('gate_type') or '').strip()
    question = str(data.get('question') or '').strip()
    reason_code = str(data.get('reason_code') or '').strip()
    if (policy not in GATE_LEVELS or scope not in GATE_SCOPES
            or not gate_type or not question or not reason_code):
        return _error('VALIDATION_FAILED', 'Gate fields are invalid')
    work_item_id = data.get('work_item_id')
    if work_item_id is not None:
        work_item_id = _positive_int(work_item_id, 'work_item_id', minimum=1)
    if scope in ('work_item', 'agent_lane'):
        todo = db.session.get(GoalTodo, work_item_id) if work_item_id else None
        if not todo or todo.goal_id != goal.id:
            return _error(
                'VALIDATION_FAILED',
                'selected Gate scope requires a Todo in this Goal')
    expires_in_seconds = data.get('expires_in_seconds')
    if expires_in_seconds is not None:
        expires_in_seconds = _positive_int(
            expires_in_seconds, 'expires_in_seconds', minimum=30,
            maximum=604800)
    request_doc = {
        'goal_id': goal.id,
        'work_item_type': str(data.get('work_item_type') or 'goal_todo'),
        'work_item_id': work_item_id,
        'turn_id': str(data.get('turn_id') or '')[:80],
        'gate_type': gate_type,
        'policy_level': policy,
        'blocking_scope': scope,
        'question': question,
        'reason_code': reason_code,
        'requested_scope': data.get('requested_scope') or {},
        'safe_fallback': data.get('safe_fallback') or {},
        'decision_options': data.get('decision_options') or [],
        'expires_in_seconds': expires_in_seconds,
    }
    req_hash = stable_hash(request_doc)
    existing = AgentControlGate.query.filter_by(
        goal_id=goal.id, idempotency_key=idem).first()
    if existing:
        if existing.request_hash != req_hash:
            return _error(
                'IDEMPOTENCY_KEY_REUSED',
                'Gate idempotency key was reused with another request', 409)
        return jsonify({'gate': existing.to_dict(), 'replayed': True})
    expires_at = None
    if expires_in_seconds is not None:
        expires_at = now_cst_naive() + timedelta(seconds=expires_in_seconds)
    gate = AgentControlGate(
        goal_id=goal.id,
        work_item_type=request_doc['work_item_type'],
        work_item_id=request_doc['work_item_id'],
        turn_id=request_doc['turn_id'] or None,
        gate_type=gate_type,
        policy_level=policy,
        status='open',
        blocking_scope=scope,
        question=question,
        reason_code=reason_code,
        requested_scope_json=request_doc['requested_scope'],
        safe_fallback_json=request_doc['safe_fallback'],
        decision_options_json=request_doc['decision_options'],
        requested_by=actor['name'],
        expires_at=expires_at,
        idempotency_key=idem,
        request_hash=req_hash,
    )
    db.session.add(gate)
    db.session.commit()
    return jsonify({'gate': gate.to_dict(), 'replayed': False}), 201


@api_bp.route('/control-gates/<int:gate_id>/resolve', methods=['POST'])
@api_bp.route('/agent-control-gates/<int:gate_id>/resolve', methods=['POST'])
def resolve_control_gate(gate_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    if actor['type'] != 'user':
        return _error(
            'HUMAN_GATE_REQUIRES_USER',
            'A signed-in Hub user must resolve a human Gate', 403)
    gate = AgentControlGate.query.filter_by(id=gate_id).with_for_update().first()
    if not gate or not _require_goal(actor, gate.goal_id):
        return _error('GATE_NOT_FOUND', 'Gate was not found', 404)
    data = request.get_json(silent=True) or {}
    try:
        idem = _idempotency_key(data)
    except ControlPlaneRejected as exc:
        return _error(exc.code, str(exc), exc.status_code)
    resolution_doc = {
        'resolution': str(data.get('resolution') or '').strip().lower(),
        'expected_version': data.get('expected_version'),
        'note': str(data.get('note') or ''),
        'approved_scope': (
            data.get('approved_scope')
            if isinstance(data.get('approved_scope'), dict) else {}),
    }
    resolution_hash = stable_hash(resolution_doc)
    if gate.resolution_idempotency_key:
        if (gate.resolution_idempotency_key == idem
                and gate.resolution_request_hash == resolution_hash):
            return jsonify({
                'gate': gate.to_dict(),
                'replayed': True,
                'wake': {
                    'goal_id': gate.goal_id,
                    'work_item_id': gate.work_item_id,
                },
            })
        return _error(
            'GATE_ALREADY_RESOLVED', 'Gate already has another resolution', 409)
    expected = _positive_int(data.get('expected_version'), 'expected_version', minimum=1)
    resolution = resolution_doc['resolution']
    if gate.version != expected:
        return _error(
            'GATE_VERSION_CONFLICT', 'Gate version is stale', 409,
            {'current_version': gate.version})
    if gate.status != 'open':
        return _error('GATE_NOT_OPEN', 'Gate is no longer open', 409)
    if gate.expires_at and gate.expires_at <= now_cst_naive():
        gate.status = 'expired'
        gate.version += 1
        db.session.commit()
        return _error('GATE_EXPIRED', 'Gate has expired', 409)
    if resolution not in ('approved', 'rejected', 'cancelled'):
        return _error('VALIDATION_FAILED', 'unsupported Gate resolution')
    gate.status = resolution
    gate.resolution = resolution
    gate.resolution_note = str(data.get('note') or '')
    gate.resolved_by = actor['name']
    gate.resolved_at = now_cst_naive()
    gate.resolution_idempotency_key = idem
    gate.resolution_request_hash = resolution_hash
    gate.version += 1
    if resolution == 'approved' and gate.work_item_type == 'goal_todo' and gate.work_item_id:
        todo = GoalTodo.query.filter_by(id=gate.work_item_id).first()
        approved_scope = data.get('approved_scope')
        if todo and isinstance(approved_scope, dict):
            todo.authorization_envelope_json = approved_scope
            todo.version += 1
    db.session.commit()
    return jsonify({
        'gate': gate.to_dict(),
        'replayed': False,
        'wake': {'goal_id': gate.goal_id, 'work_item_id': gate.work_item_id},
    })


@api_bp.route('/agent-goals/<int:goal_id>/should-run', methods=['POST'])
def agent_goal_should_run(goal_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    goal = _require_goal(actor, goal_id)
    if not goal:
        return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    if not worker_id:
        return _error('WORKER_ID_REQUIRED', 'worker_id is required')
    decision = should_run(
        goal,
        agent_id=data.get('agent_id'),
        worker_id=worker_id,
        capabilities=normalize_capabilities(
            data.get('available_capabilities')
            if isinstance(data.get('available_capabilities'), list)
            else data.get('capabilities')),
    )
    return jsonify(dict(
        decision,
        schedule_version=goal.version,
        read_only_preflight=True,
        authority_granted=False,
        control_mode=goal.control_mode or 'shadow'))


@api_bp.route('/agent-goals/<int:goal_id>/turn-dispatches', methods=['POST'])
def dispatch_agent_turn(goal_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    if not worker_id:
        return _error('WORKER_ID_REQUIRED', 'worker_id is required')
    try:
        idem = _idempotency_key(data)
        goal = _require_goal(actor, goal_id, lock=True)
        if not goal:
            return _error('GOAL_NOT_FOUND', 'Goal was not found', 404)
        turn, meta = dispatch_turn(
            goal,
            claw_id=actor['id'],
            agent_id=data.get('agent_id'),
            worker_id=worker_id,
            capabilities=normalize_capabilities(
                data.get('available_capabilities')
                if isinstance(data.get('available_capabilities'), list)
                else data.get('capabilities')),
            idempotency_key=idem,
            request_document=data,
            lease_seconds=data.get('lease_seconds') or 180,
            execution_mode=data.get('execution_mode') or 'legacy_unmanaged',
        )
        if turn is None:
            db.session.rollback()
            return jsonify(dict(
                meta,
                goal_id=goal.id,
                dispatched=False,
                schedule_version=goal.version,
                control_mode=goal.control_mode or 'shadow'))
        db.session.commit()
        return jsonify({
            'turn': turn.to_dict(),
            'dispatched': True,
            'replayed': bool(meta.get('replayed')),
            'claim_required': bool(meta.get('claim_required')),
            'control_mode': goal.control_mode or 'shadow',
        }), (200 if meta.get('replayed') else 201)
    except (ControlPlaneConflict, ControlPlaneRejected) as exc:
        db.session.rollback()
        return _error(
            exc.code, str(exc), getattr(exc, 'status_code', 409))
    except AuthorizationSigningError as exc:
        db.session.rollback()
        return _error(exc.code, str(exc), 503)
    except IntegrityError:
        db.session.rollback()
        replay = AgentTurn.query.filter_by(
            goal_id=goal_id, worker_id=worker_id, idempotency_key=idem).first()
        if replay and replay.request_hash == stable_hash(data):
            return jsonify({
                'turn': replay.to_dict(),
                'dispatched': True,
                'replayed': True,
                'claim_required': bool(replay.fencing_token),
                'control_mode': 'shadow',
            }), 200
        return _error(
            'DISPATCH_CONFLICT',
            'Another Worker dispatched the work item concurrently', 409)


@api_bp.route('/agent-control/signing-keys', methods=['GET'])
def get_agent_control_signing_keys():
    if not _actor():
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    try:
        return jsonify({'keys': [public_key_document()]})
    except AuthorizationSigningError as exc:
        return _error(exc.code, str(exc), 503)


@api_bp.route('/agent-turns/<turn_id>/heartbeat', methods=['POST'])
def heartbeat_agent_turn(turn_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error(
            'WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    fencing = _positive_int(
        data.get('fencing_token'), 'fencing_token', minimum=0)
    turn = AgentTurn.query.filter_by(
        turn_id=turn_id).with_for_update().first()
    if (not turn or not _require_goal(actor, turn.goal_id)
            or turn.claw_id != actor['id']):
        return _error('TURN_NOT_FOUND', 'Turn was not found', 404)
    if turn.worker_id != worker_id:
        return _error('TURN_OWNER_MISMATCH', 'Turn owner does not match', 409)
    if turn.status != 'dispatched':
        return _error('TURN_NOT_ACTIVE', 'Turn is no longer active', 409)
    now = now_cst_naive()
    if turn.deadline_at and turn.deadline_at <= now:
        return _error('TURN_EXPIRED', 'Turn deadline has expired', 409)
    if int(turn.fencing_token or 0) != fencing:
        return _error('FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
    todo = GoalTodo.query.filter_by(id=turn.todo_id).with_for_update().first()
    if not todo:
        return _error('TODO_NOT_FOUND', 'Turn Todo no longer exists', 409)
    lease_seconds = _positive_int(
        data.get('lease_seconds'), 'lease_seconds',
        default=todo.claim_lease_seconds or 180, minimum=30, maximum=900)
    if todo_requires_claim(todo):
        if (todo.claimed_claw_id != actor['id']
                or todo.claimed_by_worker_id != worker_id):
            return _error(
                'CLAIM_OWNER_MISMATCH', 'Todo claim owner does not match', 409)
        if todo.claim_expires_at and todo.claim_expires_at <= now:
            return _error('CLAIM_EXPIRED', 'Todo claim has expired', 409)
        if int(todo.fencing_token or 0) != fencing:
            return _error('FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
        todo.claim_lease_seconds = lease_seconds
        todo.claim_expires_at = now + timedelta(seconds=lease_seconds)
        turn.claim_expires_at = todo.claim_expires_at
    turn.deadline_at = now + timedelta(seconds=lease_seconds)
    db.session.commit()
    return jsonify({'turn': turn.to_dict(), 'lease_extended': True})


@api_bp.route('/agent-turns/<turn_id>/release', methods=['POST'])
def release_agent_turn(turn_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error(
            'WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    fencing = _positive_int(
        data.get('fencing_token'), 'fencing_token', minimum=0)
    turn = AgentTurn.query.filter_by(
        turn_id=turn_id).with_for_update().first()
    if (not turn or not _require_goal(actor, turn.goal_id)
            or turn.claw_id != actor['id']):
        return _error('TURN_NOT_FOUND', 'Turn was not found', 404)
    if turn.worker_id != worker_id:
        return _error('TURN_OWNER_MISMATCH', 'Turn owner does not match', 409)
    if turn.status == 'released':
        return jsonify({'turn': turn.to_dict(), 'replayed': True})
    if turn.status != 'dispatched':
        return _error('TURN_NOT_ACTIVE', 'Turn is no longer active', 409)
    if int(turn.fencing_token or 0) != fencing:
        return _error('FENCING_TOKEN_STALE', 'Fencing token is stale', 409)
    todo = GoalTodo.query.filter_by(id=turn.todo_id).with_for_update().first()
    if todo and todo_requires_claim(todo):
        if (todo.claimed_claw_id != actor['id']
                or todo.claimed_by_worker_id != worker_id
                or int(todo.fencing_token or 0) != fencing):
            return _error(
                'CLAIM_OWNER_MISMATCH', 'Todo claim owner does not match', 409)
        todo.claimed_claw_id = None
        todo.claimed_by_worker_id = ''
        todo.claimed_at = None
        todo.claim_expires_at = None
        if todo.status == 'claimed':
            todo.status = 'open'
        todo.version = int(todo.version or 0) + 1
    turn.status = 'released'
    turn.active_todo_slot = None
    turn.quota_reserved = False
    db.session.commit()
    return jsonify({'turn': turn.to_dict(), 'replayed': False})


@api_bp.route('/agent-turns/<turn_id>/effect-authorizations', methods=['POST'])
def authorize_agent_turn_effects(turn_id):
    """Phase two: bind Hub authority to a materialized typed manifest."""
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error(
            'WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    if not worker_id:
        return _error('WORKER_ID_REQUIRED', 'worker_id is required')
    try:
        idem = _idempotency_key(data)
        turn = AgentTurn.query.filter_by(
            turn_id=turn_id).with_for_update().first()
        if not turn or not _require_goal(actor, turn.goal_id):
            return _error('TURN_NOT_FOUND', 'Turn was not found', 404)
        request_doc = {
            'worker_id': worker_id,
            'manifest': (
                data.get('manifest')
                if isinstance(data.get('manifest'), dict) else None),
            'manifest_hash': str(data.get('manifest_hash') or '').strip(),
            'approval_scope_hash': str(
                data.get('approval_scope_hash') or '').strip(),
            'audience': str(data.get('audience') or '').strip(),
        }
        envelope, replayed = authorize_turn_effects(
            turn,
            claw_id=actor['id'],
            worker_id=worker_id,
            idempotency_key=idem,
            manifest=request_doc['manifest'],
            manifest_hash=request_doc['manifest_hash'],
            approval_scope_hash=request_doc['approval_scope_hash'],
            audience=request_doc['audience'],
            request_document=request_doc,
        )
        db.session.commit()
        return jsonify({
            'turn_id': turn.turn_id,
            'authorization_envelope': envelope,
            'idempotent_replay': replayed,
        }), 200 if replayed else 201
    except ControlPlaneRejected as exc:
        db.session.rollback()
        return _error(exc.code, str(exc), exc.status_code)
    except AuthorizationSigningError as exc:
        db.session.rollback()
        return _error(exc.code, str(exc), 503)


@api_bp.route('/agent-turns/<turn_id>/writeback', methods=['POST'])
def writeback_agent_turn(turn_id):
    actor = _actor()
    if not actor or actor['type'] != 'claw':
        return _error('WORKER_AUTH_REQUIRED', 'OpenClaw Worker token is required', 401)
    data = request.get_json(silent=True) or {}
    worker_id = str(data.get('worker_id') or '').strip()
    transition_id = str(
        data.get('transition_id') or request.headers.get('Idempotency-Key') or '').strip()
    if not worker_id or not transition_id:
        return _error(
            'WRITEBACK_IDENTITY_REQUIRED',
            'worker_id and transition_id are required')
    turn = AgentTurn.query.filter_by(turn_id=turn_id).with_for_update().first()
    if not turn or not _require_goal(actor, turn.goal_id):
        return _error('TURN_NOT_FOUND', 'Turn was not found', 404)
    if turn.claw_id != actor['id']:
        return _error('TURN_OWNER_MISMATCH', 'Turn belongs to another Claw', 403)
    try:
        receipt, replayed = accept_writeback(
            turn,
            worker_id=worker_id,
            transition_id=transition_id,
            result_kind=str(data.get('result_kind') or '').strip().upper(),
            summary=str(data.get('summary') or ''),
            verification=(
                data.get('verification')
                if isinstance(data.get('verification'), dict) else {}),
            evidence=(
                data.get('evidence') if isinstance(data.get('evidence'), dict) else {}),
            effect_receipts=(
                data.get('effect_receipts')
                if isinstance(data.get('effect_receipts'), list) else []),
            expected_todo_version=data.get('expected_todo_version'),
            fencing_token=data.get('fencing_token') or 0,
            request_document=data,
        )
        db.session.commit()
        return jsonify(dict(
            receipt.to_dict(), idempotent_replay=replayed)), (200 if replayed else 201)
    except (ControlPlaneConflict, ControlPlaneRejected) as exc:
        db.session.rollback()
        return _error(
            exc.code, str(exc), getattr(exc, 'status_code', 409))
