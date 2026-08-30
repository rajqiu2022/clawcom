"""Project-scoped read-only aggregate API for the P0-J dashboard."""

from uuid import uuid4

from flask import jsonify, request

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    is_admin_user,
    user_project_ids,
)
from app.models import Project
from app.services.automation_closed_loop import (
    build_automation_closed_loop_overview,
)


def _request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex).strip()[:128]


def _error(code, message, status=400, details=None):
    return jsonify({
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': _request_id(),
    }), status


def _actor():
    claw = get_current_claw()
    if claw:
        return {'type': 'claw', 'claw': claw}
    user = get_current_user()
    if user:
        return {'type': 'user', 'user': user}
    return None


def _can_access_project(actor, project_id):
    if is_admin_user():
        return True
    if actor['type'] == 'claw':
        claw = actor['claw']
        return claw.project_id is None or int(claw.project_id) == int(project_id)
    return int(project_id) in user_project_ids(actor['user'])


def _integer_arg(name, default=None, minimum=None, maximum=None):
    raw = request.args.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be an integer') from None
    if minimum is not None and value < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    if maximum is not None and value > maximum:
        raise ValueError(f'{name} must not exceed {maximum}')
    return value


@api_bp.route('/automation-closed-loop/overview', methods=['GET'])
def automation_closed_loop_overview():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    try:
        project_id = _integer_arg('project_id', minimum=1)
        library_id = _integer_arg('library_id', default=33, minimum=1)
        definition_id = _integer_arg(
            'workflow_definition_id', default=12, minimum=1)
        stale_hours = _integer_arg(
            'stale_hours', default=24, minimum=1, maximum=24 * 90)
        limit = _integer_arg('limit', default=12, minimum=1, maximum=50)
    except ValueError as exc:
        return _error('AUTOMATION_CLOSED_LOOP_QUERY_INVALID', str(exc))
    if project_id is None:
        return _error(
            'AUTOMATION_CLOSED_LOOP_QUERY_INVALID',
            'project_id is required')
    project = db.session.get(Project, project_id)
    if not project or not _can_access_project(actor, project_id):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)

    payload = build_automation_closed_loop_overview(
        project,
        library_id=library_id,
        workflow_definition_id=definition_id,
        stale_hours=stale_hours,
        limit=limit,
    )
    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store'
    return response
