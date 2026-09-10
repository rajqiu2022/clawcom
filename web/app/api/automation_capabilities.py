"""Project-scoped automation capability catalog API."""

from datetime import datetime
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
from app.models import AutomationCapability
from app.models import (
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
    CapabilityGapEvent,
)
from app.services.entity_relations import best_effort_upsert_relations


STATUSES = {'planned', 'available', 'degraded', 'unavailable', 'retired'}


def _error(code, message, status=400, details=None):
    return jsonify({
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': str(
            request.headers.get('X-Request-ID') or uuid4().hex)[:128],
    }), status


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}', 'claw': claw,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': user.username or f'user:{user.id}', 'user': user,
        }
    return None


def _can_access_project(actor, project_id):
    if is_admin_user():
        return True
    if not actor:
        return False
    if actor['type'] == 'claw':
        project = actor['claw'].project_id
        return project is None or int(project) == int(project_id)
    return int(project_id) in user_project_ids(actor['user'])


def _string_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{field} must be an array')
    result = []
    for item in value:
        normalized = str(item or '').strip()
        if not normalized:
            raise ValueError(f'{field} must not contain empty values')
        if normalized not in result:
            result.append(normalized)
    return result


def _health_checked_at(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(
            tzinfo=None)
    except ValueError as exc:
        raise ValueError('health_checked_at must be an ISO-8601 datetime') from exc


def _available_capability_snapshot(project_id):
    """Return healthy versioned capability tokens and their source rows."""
    rows = AutomationCapability.query.filter_by(
        project_id=project_id, status='available').all()
    tokens = set()
    sources = []
    for row in rows:
        # A catalog declaration alone must not close a Gap.  Automatic
        # requalification requires a concrete implementation version and an
        # observed health check from the capability producer.
        if not row.implementation_version or not row.health_checked_at:
            continue
        row_tokens = {
            row.capability_key,
            *(row.operations_json or []),
            *(row.observables_json or []),
            *(row.reset_hooks_json or []),
        }
        tokens.update(str(item).strip() for item in row_tokens if str(item).strip())
        sources.append({
            'capability_id': row.id,
            'key': row.capability_key,
            'catalog_version': row.version,
            'implementation_version': row.implementation_version,
            'health_checked_at': str(row.health_checked_at),
        })
    return tokens, sources


def _auto_resolve_gaps_and_requeue(project_id, actor):
    """Atomically close satisfied Gaps and return candidates to canary."""
    tokens, sources = _available_capability_snapshot(project_id)
    if not tokens:
        return {'resolved_gap_ids': [], 'requeued_candidate_ids': []}
    resolved = []
    requeued = []
    relations = []
    now = datetime.now()
    gaps = (CapabilityGap.query.filter(
        CapabilityGap.project_id == project_id,
        CapabilityGap.status.in_(('open', 'in_progress')),
    ).with_for_update().all())
    for gap in gaps:
        required = set()
        for values in (
                gap.missing_capabilities_json,
                gap.required_operations_json,
                gap.required_observables_json,
                gap.required_reset_hooks_json):
            required.update(
                str(item).strip() for item in (values or [])
                if str(item).strip())
        if not required or not required.issubset(tokens):
            continue
        gap_before = int(gap.version or 1)
        previous_status = gap.status
        candidate_ids = []
        candidates = (AutomationCaseCandidate.query.filter_by(
            capability_gap_id=gap.id,
            state='WAITING_CAPABILITY',
        ).with_for_update().all())
        for candidate in candidates:
            candidate_before = int(candidate.version or 1)
            candidate.state = 'READY_FOR_CANARY'
            candidate.qualification_outcome = None
            candidate.qualification_run_id = None
            candidate.qualification_evidence_json = {}
            candidate.version = candidate_before + 1
            candidate.updated_by = actor['name']
            candidate.updated_at = now
            db.session.add(AutomationCaseCandidateEvent(
                candidate_id=candidate.id,
                event_type='capability_catalog_auto_requeued',
                from_state='WAITING_CAPABILITY',
                to_state='READY_FOR_CANARY',
                version_before=candidate_before,
                version_after=candidate.version,
                payload_json={
                    'capability_gap_id': gap.id,
                    'capability_sources': sources,
                },
                actor_type=actor['type'], actor_id=actor['id'],
                actor_name=actor['name'], request_id=str(
                    request.headers.get('X-Request-ID') or uuid4().hex)[:128],
            ))
            candidate_ids.append(candidate.id)
            requeued.append(candidate.id)
            relations.append({
                'from_type': 'automation_case_candidate',
                'from_id': str(candidate.id),
                'relation_type': 'unblocked_by',
                'to_type': 'capability_gap',
                'to_id': str(gap.id),
                'metadata': {
                    'gap_version': gap_before + 1,
                    'resolution': 'capability_catalog_auto_resolved',
                },
            })
        gap.status = 'resolved'
        gap.resolution_json = {
            'summary': 'Hub capability catalog verified every required token',
            'required_tokens': sorted(required),
            'capability_sources': sources,
            'auto_requeued_candidate_ids': candidate_ids,
        }
        gap.resolved_by = actor['name']
        gap.resolved_at = now
        gap.candidate_requeue_pending = False
        gap.version = gap_before + 1
        gap.updated_by = actor['name']
        gap.updated_at = now
        db.session.add(CapabilityGapEvent(
            capability_gap_id=gap.id,
            event_type='capability_catalog_auto_resolved',
            from_status=previous_status,
            to_status='resolved',
            version_before=gap_before,
            version_after=gap.version,
            payload_json={
                'required_tokens': sorted(required),
                'capability_sources': sources,
                'requeued_candidate_ids': candidate_ids,
            },
            actor_type=actor['type'], actor_id=actor['id'],
            actor_name=actor['name'], request_id=str(
                request.headers.get('X-Request-ID') or uuid4().hex)[:128],
        ))
        resolved.append(gap.id)
    best_effort_upsert_relations(project_id, relations, actor['name'])
    return {
        'resolved_gap_ids': resolved,
        'requeued_candidate_ids': requeued,
    }


@api_bp.route('/automation-capabilities', methods=['GET'])
def list_automation_capabilities():
    actor = _actor()
    project_id = request.args.get('project_id', type=int)
    if not project_id:
        return _error('PROJECT_ID_REQUIRED', 'project_id is required')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', 'Project access denied', 403)
    query = AutomationCapability.query.filter_by(project_id=project_id)
    if request.args.get('status'):
        query = query.filter_by(status=request.args['status'])
    if request.args.get('platform'):
        # JSON containment differs between SQLite/MariaDB; keep catalog sizes
        # bounded and apply the portable filter after the project query.
        platform = request.args['platform']
    else:
        platform = None
    page = max(request.args.get('page', 1, type=int), 1)
    page_size = min(max(request.args.get('page_size', 50, type=int), 1), 200)
    rows = query.order_by(
        AutomationCapability.capability_key.asc()).all()
    if platform:
        rows = [row for row in rows if platform in (row.platforms_json or [])]
    total = len(rows)
    start = (page - 1) * page_size
    return jsonify({
        'items': [row.to_dict() for row in rows[start:start + page_size]],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/automation-capabilities', methods=['POST'])
def upsert_automation_capability():
    actor = _actor()
    data = request.get_json(silent=True) or {}
    try:
        project_id = int(data.get('project_id'))
    except (TypeError, ValueError):
        return _error('PROJECT_ID_REQUIRED', 'project_id must be an integer')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', 'Project access denied', 403)
    key = str(data.get('key') or '').strip()
    name = str(data.get('name') or '').strip()
    if not key or not name:
        return _error(
            'AUTOMATION_CAPABILITY_INVALID', 'key and name are required')
    status = str(data.get('status') or 'available').strip().lower()
    if status not in STATUSES:
        return _error(
            'AUTOMATION_CAPABILITY_INVALID',
            f'status must be one of {sorted(STATUSES)}')
    try:
        values = {
            'operations_json': _string_list(data.get('operations'), 'operations'),
            'observables_json': _string_list(data.get('observables'), 'observables'),
            'reset_hooks_json': _string_list(data.get('reset_hooks'), 'reset_hooks'),
            'platforms_json': _string_list(data.get('platforms'), 'platforms'),
            'health_checked_at': _health_checked_at(data.get('health_checked_at')),
        }
    except ValueError as exc:
        return _error('AUTOMATION_CAPABILITY_INVALID', str(exc))
    row = (AutomationCapability.query.filter_by(
        project_id=project_id, capability_key=key)
        .with_for_update().first())
    created = row is None
    if row:
        try:
            expected_version = int(data.get('expected_version'))
        except (TypeError, ValueError):
            return _error(
                'EXPECTED_VERSION_REQUIRED',
                'expected_version is required when updating')
        if expected_version != row.version:
            return _error(
                'AUTOMATION_CAPABILITY_VERSION_CONFLICT',
                'Automation capability has been updated by another writer',
                409, {'expected_version': expected_version,
                      'current_version': row.version})
        row.version += 1
    else:
        row = AutomationCapability(
            project_id=project_id,
            capability_key=key,
            version=1,
            created_by=actor['name'],
        )
        db.session.add(row)
    row.name = name[:300]
    row.status = status
    row.implementation_version = str(
        data.get('implementation_version') or '').strip()[:80]
    row.updated_by = actor['name']
    for field, value in values.items():
        setattr(row, field, value)
    db.session.flush()
    auto_requalification = _auto_resolve_gaps_and_requeue(project_id, actor)
    db.session.commit()
    payload = row.to_dict()
    payload['auto_requalification'] = auto_requalification
    return jsonify(payload), 201 if created else 200
