"""Automation case candidate lifecycle API (P0-B)."""

from datetime import datetime, timezone, timedelta
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
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
    CapabilityGapEvent,
    Project,
    TestCaseLibrary,
    WorkflowRun,
)
from app.services.automation_candidates import (
    CANDIDATE_STATES,
    capability_gap_key,
    default_dedupe_key,
    normalize_case_draft,
    normalize_source_refs,
    normalize_string_list,
    qualification_target_state,
    validate_candidate_transition,
)
from app.services.entity_relations import (
    best_effort_upsert_relations,
    candidate_source_relations,
)
from app.services.capability_gaps import (
    CAPABILITY_GAP_STATUSES,
    gap_request_hash,
    normalize_evidence,
    normalize_gap_payload,
    normalize_requirement,
)


_CST = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_CST).replace(tzinfo=None)


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
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}', 'claw': claw,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': getattr(user, 'username', '') or f'user:{user.id}',
            'user': user,
        }
    return None


def _require_actor():
    actor = _actor()
    if actor:
        return actor, None
    return None, _error('UNAUTHENTICATED', 'Authentication is required', 401)


def _can_access_project(actor, project_id):
    if is_admin_user():
        return True
    if actor.get('type') == 'claw':
        claw = actor.get('claw')
        return claw and (
            claw.project_id is None or int(claw.project_id) == int(project_id))
    user = actor.get('user')
    return int(project_id) in user_project_ids(user)


def _candidate_or_error(candidate_id, actor):
    candidate = db.session.get(AutomationCaseCandidate, candidate_id)
    if not candidate or not _can_access_project(actor, candidate.project_id):
        return None, _error(
            'AUTOMATION_CASE_CANDIDATE_NOT_FOUND',
            'Automation case candidate was not found', 404)
    return candidate, None


def _idempotency_key(data):
    body_key = str(data.get('idempotency_key') or '').strip()
    header_key = str(request.headers.get('Idempotency-Key') or '').strip()
    if body_key and header_key and body_key != header_key:
        raise ValueError('Body and header idempotency keys must match')
    key = body_key or header_key
    if len(key) > 128:
        raise ValueError('idempotency_key must not exceed 128 characters')
    return key or None


def _event(candidate, event_type, actor, from_state, to_state,
           version_before, version_after, payload=None, idempotency_key=None):
    row = AutomationCaseCandidateEvent(
        candidate_id=candidate.id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        version_before=version_before,
        version_after=version_after,
        payload_json=payload or {},
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        request_id=_request_id(),
        idempotency_key=idempotency_key,
    )
    db.session.add(row)
    return row


def _gap_event(gap, event_type, actor, from_status, to_status,
               version_before, version_after, payload=None,
               idempotency_key=None, request_hash=None):
    row = CapabilityGapEvent(
        capability_gap_id=gap.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        version_before=version_before,
        version_after=version_after,
        payload_json=payload or {},
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        request_id=_request_id(),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(row)
    return row


def _refresh_gap_requeue_pending(gap_id, actor, candidate_id=None):
    """Keep the derived requeue flag consistent with candidate state.

    The flag is meaningful only after a Gap is resolved.  Candidate PATCH and
    qualification APIs can move a candidate without going through the bulk
    requeue endpoint, so they must refresh it in the same transaction.
    """
    if not gap_id:
        return None
    gap = (CapabilityGap.query.filter_by(id=int(gap_id))
           .with_for_update().first())
    if not gap:
        return None
    pending = False
    if gap.status == 'resolved':
        pending = (AutomationCaseCandidate.query.filter_by(
            capability_gap_id=gap.id,
            state='WAITING_CAPABILITY',
        ).first() is not None)
    if bool(gap.candidate_requeue_pending) == pending:
        return gap
    version_before = int(gap.version or 1)
    gap.candidate_requeue_pending = pending
    gap.version = version_before + 1
    gap.updated_by = actor['name']
    gap.updated_at = _now()
    _gap_event(
        gap, 'candidate_requeue_flag_refreshed', actor,
        gap.status, gap.status, version_before, gap.version,
        payload={
            'candidate_id': candidate_id,
            'candidate_requeue_pending': pending,
        },
    )
    return gap


def _required_idempotency_key(data):
    key = _idempotency_key(data)
    if not key:
        raise ValueError('idempotency_key or Idempotency-Key header is required')
    return key


def _gap_replay(gap, event_type, actor, idempotency_key, request_hash):
    if not gap or not idempotency_key:
        return None
    event = CapabilityGapEvent.query.filter_by(
        capability_gap_id=gap.id,
        event_type=event_type,
        actor_type=actor['type'],
        actor_id=actor['id'],
        idempotency_key=idempotency_key,
    ).first()
    if not event:
        return None
    if event.request_hash != request_hash:
        return _error(
            'IDEMPOTENCY_KEY_REUSED',
            'The same idempotency key was used for a different request', 409)
    payload = dict((event.payload_json or {}).get('response') or gap.to_dict())
    payload['idempotent_replay'] = True
    return jsonify(payload), int((event.payload_json or {}).get('status_code') or 200)


def _gap_payload(gap, with_events=False):
    payload = gap.to_dict()
    candidates = sorted(gap.case_candidates, key=lambda row: row.id)
    payload['candidate_ids'] = [row.id for row in candidates]
    payload['candidate_state_counts'] = {}
    for candidate in candidates:
        payload['candidate_state_counts'][candidate.state] = (
            payload['candidate_state_counts'].get(candidate.state, 0) + 1)
    if with_events:
        payload['events'] = [row.to_dict() for row in gap.events]
    return payload


def _gap_relations(gap):
    relations = []
    if gap.development_requirement_ref:
        relations.append({
            'from_type': 'capability_gap',
            'from_id': str(gap.id),
            'relation_type': 'tracked_by',
            'to_type': 'requirement',
            'to_id': gap.development_requirement_ref,
            'metadata': {
                'status': gap.development_requirement_status or '',
                'url': gap.development_requirement_url or '',
            },
        })
    return relations


def _candidate_payload(candidate, with_events=False):
    payload = candidate.to_dict()
    payload['source_lineage'] = payload['source_refs']
    payload['downstream'] = {
        'qualification_run_id': candidate.qualification_run_id,
        'capability_gap_id': candidate.capability_gap_id,
        'production_library_id': candidate.production_library_id,
        'production_case_id': candidate.production_case_id,
    }
    if candidate.capability_gap:
        payload['capability_gap'] = candidate.capability_gap.to_dict()
    if with_events:
        payload['events'] = [event.to_dict() for event in candidate.events]
    return payload


def _parse_datetime_filter(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(f'{field} must be an ISO-8601 datetime') from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_CST).replace(tzinfo=None)
    return parsed


@api_bp.route('/automation-case-candidates:upsert', methods=['POST'])
def upsert_automation_case_candidate():
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    try:
        project_id = int(data.get('project_id'))
    except (TypeError, ValueError):
        return _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if not db.session.get(Project, project_id) or not _can_access_project(actor, project_id):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    title = str(data.get('title') or '').strip()
    if not title:
        return _error('CANDIDATE_TITLE_REQUIRED', 'title is required')
    try:
        idem_key = _idempotency_key(data)
        source_refs = normalize_source_refs(data.get('source_refs'))
        required_capabilities = normalize_string_list(
            data.get('required_capabilities'), 'required_capabilities')
        state = str(data.get('state') or 'DISCOVERED').strip().upper()
        if state not in ('DISCOVERED', 'DESIGNED'):
            raise ValueError(
                'new candidates may only start in DISCOVERED or DESIGNED')
        case_draft = normalize_case_draft(
            data.get('case_draft'), require_runnable=state == 'DESIGNED')
    except ValueError as exc:
        return _error('CANDIDATE_VALIDATION_FAILED', str(exc))
    module_key = str(data.get('module_key') or '').strip()[:200]
    source_type = str(data.get('source_type') or 'manual').strip()[:64]
    dedupe_key = str(data.get('dedupe_key') or '').strip()
    if not dedupe_key:
        if idem_key:
            dedupe_key = f'idempotency:{actor["type"]}:{actor["id"]}:{idem_key}'
        else:
            dedupe_key = default_dedupe_key(
                project_id, title, module_key, source_type, source_refs)
    if len(dedupe_key) > 255:
        return _error(
            'DEDUPE_KEY_TOO_LONG', 'dedupe_key must not exceed 255 characters')
    production_library_id = data.get('production_library_id')
    if production_library_id not in (None, ''):
        try:
            production_library_id = int(production_library_id)
        except (TypeError, ValueError):
            return _error(
                'INVALID_PRODUCTION_LIBRARY_ID',
                'production_library_id must be an integer')
        if not db.session.get(TestCaseLibrary, production_library_id):
            return _error(
                'PRODUCTION_LIBRARY_NOT_FOUND',
                'Production testcase library was not found', 404)

    existing = AutomationCaseCandidate.query.filter_by(
        project_id=project_id, dedupe_key=dedupe_key).first()
    if existing:
        payload = _candidate_payload(existing)
        payload['idempotent_replay'] = True
        return jsonify(payload), 200

    candidate = AutomationCaseCandidate(
        project_id=project_id,
        title=title[:300],
        module_key=module_key,
        source_type=source_type,
        source_refs_json=source_refs,
        case_draft_json=case_draft,
        required_capabilities_json=required_capabilities,
        state=state,
        production_library_id=production_library_id,
        dedupe_key=dedupe_key,
        version=1,
        created_by=actor['name'],
        updated_by=actor['name'],
    )
    db.session.add(candidate)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = AutomationCaseCandidate.query.filter_by(
            project_id=project_id, dedupe_key=dedupe_key).first()
        if existing:
            payload = _candidate_payload(existing)
            payload['idempotent_replay'] = True
            return jsonify(payload), 200
        raise
    _event(
        candidate, 'created', actor, None, state, None, 1,
        payload={'dedupe_key': dedupe_key}, idempotency_key=idem_key)
    best_effort_upsert_relations(
        candidate.project_id,
        candidate_source_relations(candidate),
        actor['name'],
    )
    db.session.commit()
    payload = _candidate_payload(candidate)
    payload['idempotent_replay'] = False
    return jsonify(payload), 201


@api_bp.route('/automation-case-candidates', methods=['GET'])
def list_automation_case_candidates():
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    try:
        project_id = int(request.args.get('project_id'))
    except (TypeError, ValueError):
        return _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    q = AutomationCaseCandidate.query.filter_by(project_id=project_id)
    states = [
        state.strip().upper()
        for state in str(request.args.get('state') or '').split(',')
        if state.strip()
    ]
    if states:
        unknown = sorted(set(states) - CANDIDATE_STATES)
        if unknown:
            return _error(
                'INVALID_CANDIDATE_STATE', 'Unknown candidate state',
                details={'states': unknown})
        q = q.filter(AutomationCaseCandidate.state.in_(states))
    for field, column in (
        ('module_key', AutomationCaseCandidate.module_key),
        ('source_type', AutomationCaseCandidate.source_type),
    ):
        value = str(request.args.get(field) or '').strip()
        if value:
            q = q.filter(column == value)
    if request.args.get('updated_after'):
        try:
            updated_after = _parse_datetime_filter(
                request.args.get('updated_after'), 'updated_after')
        except ValueError as exc:
            return _error('INVALID_DATETIME_FILTER', str(exc))
        q = q.filter(AutomationCaseCandidate.updated_at > updated_after)
    try:
        page = max(1, int(request.args.get('page') or 1))
        page_size = max(1, min(200, int(request.args.get('page_size') or 50)))
    except (TypeError, ValueError):
        return _error('INVALID_PAGINATION', 'page and page_size must be integers')
    q = q.order_by(
        AutomationCaseCandidate.updated_at.desc(),
        AutomationCaseCandidate.id.desc())
    total = q.order_by(None).count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    return jsonify({
        'items': [_candidate_payload(row) for row in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': (total + page_size - 1) // page_size,
    })


@api_bp.route('/automation-case-candidates/<int:candidate_id>', methods=['GET'])
def get_automation_case_candidate(candidate_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    candidate, candidate_error = _candidate_or_error(candidate_id, actor)
    if candidate_error:
        return candidate_error
    return jsonify(_candidate_payload(candidate, with_events=True))


@api_bp.route('/automation-case-candidates/<int:candidate_id>', methods=['PATCH'])
def update_automation_case_candidate(candidate_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    candidate, candidate_error = _candidate_or_error(candidate_id, actor)
    if candidate_error:
        return candidate_error
    data = request.get_json(silent=True) or {}
    try:
        expected_version = int(data.get('expected_version'))
    except (TypeError, ValueError):
        return _error(
            'EXPECTED_VERSION_REQUIRED', 'expected_version must be an integer')
    if candidate.version != expected_version:
        return _error(
            'CANDIDATE_VERSION_CONFLICT',
            'Candidate has been updated by another writer', 409,
            details={'expected_version': expected_version,
                     'current_version': candidate.version})
    updates = {}
    try:
        if 'title' in data:
            title = str(data.get('title') or '').strip()
            if not title:
                raise ValueError('title is required')
            updates['title'] = title[:300]
        if 'module_key' in data:
            updates['module_key'] = str(data.get('module_key') or '').strip()[:200]
        if 'source_type' in data:
            updates['source_type'] = str(data.get('source_type') or 'manual').strip()[:64]
        if 'source_refs' in data:
            updates['source_refs_json'] = normalize_source_refs(data.get('source_refs'))
        if 'required_capabilities' in data:
            updates['required_capabilities_json'] = normalize_string_list(
                data.get('required_capabilities'), 'required_capabilities')
        target_state = str(data.get('state') or candidate.state).strip().upper()
        validate_candidate_transition(candidate.state, target_state)
        draft_value = (
            data.get('case_draft')
            if 'case_draft' in data else candidate.case_draft_json)
        updates['case_draft_json'] = normalize_case_draft(
            draft_value,
            require_runnable=target_state not in ('DISCOVERED', 'CASE_DESIGN_ERROR'))
        updates['state'] = target_state
    except ValueError as exc:
        return _error('CANDIDATE_VALIDATION_FAILED', str(exc))
    previous_state = candidate.state
    previous_gap_id = candidate.capability_gap_id
    updates.update({
        'version': expected_version + 1,
        'updated_by': actor['name'],
        'updated_at': _now(),
    })
    changed = AutomationCaseCandidate.query.filter_by(
        id=candidate.id, version=expected_version).update(
            updates, synchronize_session=False)
    if not changed:
        db.session.rollback()
        current = db.session.get(AutomationCaseCandidate, candidate.id)
        return _error(
            'CANDIDATE_VERSION_CONFLICT',
            'Candidate has been updated by another writer', 409,
            details={'expected_version': expected_version,
                     'current_version': current.version if current else None})
    db.session.expire_all()
    updated = db.session.get(AutomationCaseCandidate, candidate.id)
    _event(
        updated, 'updated', actor, previous_state, updated.state,
        expected_version, updated.version,
        payload={'changed_fields': sorted(updates.keys())})
    _refresh_gap_requeue_pending(
        previous_gap_id, actor, candidate_id=updated.id)
    if {'source_refs_json', 'module_key'} & set(updates):
        best_effort_upsert_relations(
            updated.project_id,
            candidate_source_relations(updated),
            actor['name'],
        )
    db.session.commit()
    return jsonify(_candidate_payload(updated))


@api_bp.route(
    '/automation-case-candidates/<int:candidate_id>/qualification-result',
    methods=['POST'])
def record_automation_case_qualification(candidate_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    candidate, candidate_error = _candidate_or_error(candidate_id, actor)
    if candidate_error:
        return candidate_error
    data = request.get_json(silent=True) or {}
    previous_gap_id = candidate.capability_gap_id
    try:
        idem_key = _idempotency_key(data)
    except ValueError as exc:
        return _error('QUALIFICATION_RESULT_INVALID', str(exc))
    if idem_key:
        replay_event = AutomationCaseCandidateEvent.query.filter_by(
            candidate_id=candidate.id,
            event_type='qualification_result',
            actor_type=actor['type'],
            actor_id=actor['id'],
            idempotency_key=idem_key,
        ).first()
        if replay_event:
            payload = _candidate_payload(candidate, with_events=True)
            payload['idempotent_replay'] = True
            return jsonify(payload), 200
    try:
        expected_version = int(data.get('expected_version'))
    except (TypeError, ValueError):
        return _error(
            'EXPECTED_VERSION_REQUIRED', 'expected_version must be an integer')
    if candidate.version != expected_version:
        return _error(
            'CANDIDATE_VERSION_CONFLICT',
            'Candidate has been updated by another writer', 409,
            details={'expected_version': expected_version,
                     'current_version': candidate.version})
    try:
        outcome, target_state = qualification_target_state(
            candidate.state, data.get('qualification_outcome') or data.get('outcome'))
    except ValueError as exc:
        return _error('QUALIFICATION_RESULT_INVALID', str(exc))
    run_id = data.get('qualification_run_id')
    if run_id not in (None, ''):
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            return _error(
                'INVALID_QUALIFICATION_RUN_ID',
                'qualification_run_id must be an integer')
        run = db.session.get(WorkflowRun, run_id)
        if not run or (run.project_id and run.project_id != candidate.project_id):
            return _error(
                'QUALIFICATION_RUN_NOT_FOUND',
                'Qualification workflow run was not found', 404)
    evidence = data.get('evidence') or {}
    if not isinstance(evidence, dict):
        return _error('INVALID_EVIDENCE', 'evidence must be an object')
    gap = None
    if outcome == 'AUTOMATION_CAPABILITY_GAP':
        try:
            missing = normalize_string_list(
                data.get('missing_capabilities')
                or candidate.required_capabilities_json,
                'missing_capabilities')
        except ValueError as exc:
            return _error('CAPABILITY_GAP_INVALID', str(exc))
        if not missing:
            return _error(
                'CAPABILITY_GAP_INVALID',
                'missing_capabilities is required for AUTOMATION_CAPABILITY_GAP')
        gap_key = str(data.get('capability_gap_key') or '').strip()
        if not gap_key:
            gap_key = capability_gap_key(candidate.project_id, missing)
        gap = (CapabilityGap.query.filter_by(
            project_id=candidate.project_id, gap_key=gap_key)
            .with_for_update().first())
        gap_values_data = {
            'gap_key': gap_key,
            'missing_capabilities': list(dict.fromkeys(
                list(gap.missing_capabilities_json or []) + missing
                if gap else missing)),
            'evidence': evidence,
        }
        if not gap or data.get('capability_gap_title') is not None:
            gap_values_data['title'] = str(
                data.get('capability_gap_title') or
                f'Automation capability gap for {candidate.title}')
        try:
            for field in (
                    'required_operations', 'required_observables',
                    'required_reset_hooks'):
                if field in data:
                    existing_field = {
                        'required_operations': 'required_operations_json',
                        'required_observables': 'required_observables_json',
                        'required_reset_hooks': 'required_reset_hooks_json',
                    }[field]
                    incoming = normalize_string_list(data.get(field), field)
                    gap_values_data[field] = list(dict.fromkeys(
                        list(getattr(gap, existing_field, None) or []) + incoming
                        if gap else incoming))
        except ValueError as exc:
            return _error('CAPABILITY_GAP_INVALID', str(exc))
        if data.get('development_requirement') is not None:
            gap_values_data['development_requirement'] = data.get(
                'development_requirement')
        try:
            gap_values = normalize_gap_payload(
                gap_values_data, candidate.project_id, existing=gap)
        except ValueError as exc:
            return _error('CAPABILITY_GAP_INVALID', str(exc))
        if not gap:
            gap = CapabilityGap(
                project_id=candidate.project_id,
                status='open',
                version=1,
                created_by=actor['name'],
                updated_by=actor['name'],
                **gap_values,
            )
            db.session.add(gap)
            db.session.flush()
            _gap_event(
                gap, 'created_by_qualification', actor, None, 'open', None, 1,
                payload={'candidate_id': candidate.id, 'outcome': outcome},
                idempotency_key=idem_key)
        else:
            gap_before = gap.version
            previous_gap_status = gap.status
            changed_gap = any(
                getattr(gap, field) != value
                for field, value in gap_values.items())
            if gap.status == 'resolved':
                gap.status = 'open'
                gap.candidate_requeue_pending = False
                gap.resolution_json = {}
                gap.resolved_by = ''
                gap.resolved_at = None
                changed_gap = True
            if changed_gap:
                for field, value in gap_values.items():
                    setattr(gap, field, value)
                gap.version = int(gap.version or 1) + 1
                gap.updated_by = actor['name']
                gap.updated_at = _now()
                _gap_event(
                    gap,
                    ('reopened_by_qualification'
                     if previous_gap_status == 'resolved'
                     else 'observed_by_qualification'),
                    actor, previous_gap_status, gap.status,
                    gap_before, gap.version,
                    payload={'candidate_id': candidate.id, 'outcome': outcome},
                    idempotency_key=idem_key)
    previous_state = candidate.state
    updates = {
        'state': target_state,
        'qualification_outcome': outcome,
        'qualification_run_id': run_id,
        'qualification_evidence_json': evidence,
        'capability_gap_id': gap.id if gap else None,
        'version': expected_version + 1,
        'updated_by': actor['name'],
        'updated_at': _now(),
    }
    changed = AutomationCaseCandidate.query.filter_by(
        id=candidate.id, version=expected_version).update(
            updates, synchronize_session=False)
    if not changed:
        db.session.rollback()
        current = db.session.get(AutomationCaseCandidate, candidate.id)
        return _error(
            'CANDIDATE_VERSION_CONFLICT',
            'Candidate has been updated by another writer', 409,
            details={'expected_version': expected_version,
                     'current_version': current.version if current else None})
    db.session.expire_all()
    updated = db.session.get(AutomationCaseCandidate, candidate.id)
    _event(
        updated, 'qualification_result', actor, previous_state, target_state,
        expected_version, updated.version,
        payload={
            'outcome': outcome,
            'qualification_run_id': run_id,
            'capability_gap_id': gap.id if gap else None,
            'evidence': evidence,
        },
        idempotency_key=idem_key,
    )
    _refresh_gap_requeue_pending(
        previous_gap_id, actor, candidate_id=updated.id)
    lineage = []
    if run_id is not None:
        lineage.append({
            'from_type': 'automation_case_candidate',
            'from_id': str(updated.id),
            'relation_type': 'qualified_by',
            'to_type': 'workflow_run',
            'to_id': str(run_id),
            'metadata': {
                'qualification_outcome': outcome,
                'candidate_version': updated.version,
            },
        })
    if gap is not None:
        lineage.append({
            'from_type': 'automation_case_candidate',
            'from_id': str(updated.id),
            'relation_type': 'blocked_by',
            'to_type': 'capability_gap',
            'to_id': str(gap.id),
            'metadata': {'qualification_outcome': outcome},
        })
        lineage.extend(_gap_relations(gap))
    best_effort_upsert_relations(
        updated.project_id, lineage, actor['name'])
    db.session.commit()
    payload = _candidate_payload(updated, with_events=True)
    payload['idempotent_replay'] = False
    return jsonify(payload)


def _gap_or_error(gap_id, actor, lock=False):
    query = CapabilityGap.query.filter_by(id=gap_id)
    gap = query.with_for_update().first() if lock else query.first()
    if not gap or not _can_access_project(actor, gap.project_id):
        return None, _error(
            'CAPABILITY_GAP_NOT_FOUND', 'Capability gap was not found', 404)
    return gap, None


def _gap_expected_version(data, gap):
    try:
        expected = int(data.get('expected_version'))
    except (TypeError, ValueError):
        raise ValueError('expected_version must be an integer') from None
    if expected != int(gap.version or 1):
        raise RuntimeError(expected)
    return expected


@api_bp.route('/capability-gaps:upsert', methods=['POST'])
def upsert_capability_gap():
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    data = request.get_json(silent=True) or {}
    try:
        project_id = int(data.get('project_id'))
    except (TypeError, ValueError):
        return _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if (not db.session.get(Project, project_id)
            or not _can_access_project(actor, project_id)):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    try:
        idem_key = _required_idempotency_key(data)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_VALIDATION_FAILED', str(exc))
    requested_gap_key = str(data.get('gap_key') or '').strip()
    gap = (CapabilityGap.query.filter_by(
        project_id=project_id, gap_key=requested_gap_key)
           .with_for_update().first()
           if requested_gap_key else None)
    try:
        initial_values = normalize_gap_payload(
            data, project_id, existing=gap)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_VALIDATION_FAILED', str(exc))
    if gap is None:
        gap = (CapabilityGap.query.filter_by(
            project_id=project_id, gap_key=initial_values['gap_key'])
            .with_for_update().first())
    request_fingerprint = gap_request_hash('upsert', data)
    replay = _gap_replay(
        gap, 'upserted', actor, idem_key, request_fingerprint)
    if replay:
        return replay
    try:
        values = normalize_gap_payload(data, project_id, existing=gap)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_VALIDATION_FAILED', str(exc))

    created = gap is None
    if created:
        gap = CapabilityGap(
            project_id=project_id,
            status='open',
            candidate_requeue_pending=False,
            version=1,
            created_by=actor['name'],
            updated_by=actor['name'],
            **values,
        )
        db.session.add(gap)
        db.session.flush()
        previous_status = None
        previous_version = None
    else:
        try:
            expected_version = _gap_expected_version(data, gap)
        except ValueError as exc:
            return _error('EXPECTED_VERSION_REQUIRED', str(exc))
        except RuntimeError:
            return _error(
                'CAPABILITY_GAP_VERSION_CONFLICT',
                'Capability Gap has been updated by another writer', 409,
                details={'expected_version': data.get('expected_version'),
                         'current_version': gap.version})
        previous_status = gap.status
        previous_version = expected_version
        for field, value in values.items():
            setattr(gap, field, value)
        gap.version = expected_version + 1
        gap.updated_by = actor['name']
        gap.updated_at = _now()
        db.session.flush()
    best_effort_upsert_relations(
        gap.project_id, _gap_relations(gap), actor['name'])
    response = _gap_payload(gap)
    response['idempotent_replay'] = False
    event = _gap_event(
        gap, 'upserted', actor, previous_status, gap.status,
        previous_version, gap.version,
        payload={'response': response, 'status_code': 201 if created else 200},
        idempotency_key=idem_key, request_hash=request_fingerprint)
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = CapabilityGap.query.filter_by(
            project_id=project_id, gap_key=values['gap_key']).first()
        replay = _gap_replay(
            winner, 'upserted', actor, idem_key, request_fingerprint)
        if replay:
            return replay
        return _error(
            'CAPABILITY_GAP_CONCURRENT_WRITE',
            'Capability Gap changed concurrently; retry the same request', 409)
    return jsonify(response), 201 if created else 200


@api_bp.route('/capability-gaps', methods=['GET'])
def list_capability_gaps():
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    try:
        project_id = int(request.args.get('project_id'))
    except (TypeError, ValueError):
        return _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    query = CapabilityGap.query.filter_by(project_id=project_id)
    statuses = {
        value.strip().lower()
        for value in str(request.args.get('status') or '').split(',')
        if value.strip()
    }
    unknown = statuses - CAPABILITY_GAP_STATUSES
    if unknown:
        return _error(
            'INVALID_CAPABILITY_GAP_STATUS', 'Unknown Capability Gap status',
            details={'statuses': sorted(unknown)})
    if statuses:
        query = query.filter(CapabilityGap.status.in_(statuses))
    pending = str(request.args.get('candidate_requeue_pending') or '').strip().lower()
    if pending:
        if pending not in {'true', 'false', '1', '0'}:
            return _error(
                'INVALID_REQUEUE_FILTER',
                'candidate_requeue_pending must be true or false')
        query = query.filter(
            CapabilityGap.candidate_requeue_pending == (pending in {'true', '1'}))
    requirement_ref = str(
        request.args.get('development_requirement_ref') or '').strip()
    if requirement_ref:
        query = query.filter_by(development_requirement_ref=requirement_ref)
    owner = str(request.args.get('owner') or '').strip()
    if owner:
        query = query.filter_by(owner=owner)
    if request.args.get('updated_after'):
        try:
            updated_after = _parse_datetime_filter(
                request.args.get('updated_after'), 'updated_after')
        except ValueError as exc:
            return _error('INVALID_DATETIME_FILTER', str(exc))
        query = query.filter(CapabilityGap.updated_at > updated_after)
    try:
        page = max(1, int(request.args.get('page') or 1))
        page_size = max(1, min(200, int(request.args.get('page_size') or 50)))
    except (TypeError, ValueError):
        return _error('INVALID_PAGINATION', 'page and page_size must be integers')
    total = query.count()
    rows = (query.order_by(
        CapabilityGap.updated_at.desc(), CapabilityGap.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all())
    return jsonify({
        'items': [_gap_payload(row) for row in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': (total + page_size - 1) // page_size,
    })


@api_bp.route('/capability-gaps/<int:gap_id>', methods=['GET'])
def get_capability_gap(gap_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    gap, error = _gap_or_error(gap_id, actor)
    if error:
        return error
    return jsonify(_gap_payload(gap, with_events=True))


def _save_gap_transition(gap, actor, event_type, idem_key,
                         request_fingerprint, previous_status,
                         previous_version, status_code=200, extra=None):
    response = _gap_payload(gap)
    response.update(extra or {})
    response['idempotent_replay'] = False
    _gap_event(
        gap, event_type, actor, previous_status, gap.status,
        previous_version, gap.version,
        payload={'response': response, 'status_code': status_code},
        idempotency_key=idem_key, request_hash=request_fingerprint)
    db.session.commit()
    return jsonify(response), status_code


@api_bp.route('/capability-gaps/<int:gap_id>/start', methods=['POST'])
def start_capability_gap_delivery(gap_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    gap, error = _gap_or_error(gap_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        idem_key = _required_idempotency_key(data)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_START_INVALID', str(exc))
    request_fingerprint = gap_request_hash('start', data)
    replay = _gap_replay(
        gap, 'delivery_started', actor, idem_key, request_fingerprint)
    if replay:
        return replay
    try:
        expected_version = _gap_expected_version(data, gap)
    except ValueError as exc:
        return _error('EXPECTED_VERSION_REQUIRED', str(exc))
    except RuntimeError:
        return _error(
            'CAPABILITY_GAP_VERSION_CONFLICT',
            'Capability Gap has been updated by another writer', 409,
            details={'expected_version': data.get('expected_version'),
                     'current_version': gap.version})
    if gap.status != 'open':
        return _error(
            'CAPABILITY_GAP_NOT_OPEN',
            'Only an open Capability Gap can start delivery', 409)
    requirement = None
    try:
        if 'development_requirement' in data:
            requirement = normalize_requirement(data.get('development_requirement'))
    except ValueError as exc:
        return _error('CAPABILITY_GAP_START_INVALID', str(exc))
    previous_status = gap.status
    gap.status = 'in_progress'
    gap.owner = str(data.get('owner') or gap.owner or actor['name']).strip()[:160]
    if requirement is not None:
        gap.development_requirement_ref = requirement['ref']
        gap.development_requirement_url = requirement['url']
        gap.development_requirement_status = requirement['status']
    gap.version = expected_version + 1
    gap.updated_by = actor['name']
    gap.updated_at = _now()
    db.session.flush()
    best_effort_upsert_relations(
        gap.project_id, _gap_relations(gap), actor['name'])
    return _save_gap_transition(
        gap, actor, 'delivery_started', idem_key, request_fingerprint,
        previous_status, expected_version)


@api_bp.route('/capability-gaps/<int:gap_id>/resolve', methods=['POST'])
def resolve_capability_gap(gap_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    gap, error = _gap_or_error(gap_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        idem_key = _required_idempotency_key(data)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_RESOLVE_INVALID', str(exc))
    request_fingerprint = gap_request_hash('resolve', data)
    replay = _gap_replay(
        gap, 'resolved', actor, idem_key, request_fingerprint)
    if replay:
        return replay
    try:
        expected_version = _gap_expected_version(data, gap)
    except ValueError as exc:
        return _error('EXPECTED_VERSION_REQUIRED', str(exc))
    except RuntimeError:
        return _error(
            'CAPABILITY_GAP_VERSION_CONFLICT',
            'Capability Gap has been updated by another writer', 409,
            details={'expected_version': data.get('expected_version'),
                     'current_version': gap.version})
    if gap.status == 'resolved':
        return _error(
            'CAPABILITY_GAP_ALREADY_RESOLVED',
            'Capability Gap is already resolved', 409)
    try:
        resolution = normalize_evidence(data.get('resolution') or {
            'summary': data.get('reason')})
        if not any(str(value or '').strip() for value in resolution.values()):
            raise ValueError('resolution summary or evidence is required')
        requirement = (normalize_requirement(data.get('development_requirement'))
                       if 'development_requirement' in data else None)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_RESOLVE_INVALID', str(exc))
    previous_status = gap.status
    gap.status = 'resolved'
    gap.resolution_json = resolution
    gap.resolved_by = actor['name']
    gap.resolved_at = _now()
    gap.candidate_requeue_pending = any(
        row.state == 'WAITING_CAPABILITY' for row in gap.case_candidates)
    if requirement is not None:
        gap.development_requirement_ref = requirement['ref']
        gap.development_requirement_url = requirement['url']
        gap.development_requirement_status = requirement['status']
    gap.version = expected_version + 1
    gap.updated_by = actor['name']
    gap.updated_at = _now()
    db.session.flush()
    best_effort_upsert_relations(
        gap.project_id, _gap_relations(gap), actor['name'])
    return _save_gap_transition(
        gap, actor, 'resolved', idem_key, request_fingerprint,
        previous_status, expected_version)


@api_bp.route('/capability-gaps/<int:gap_id>/reopen', methods=['POST'])
def reopen_capability_gap(gap_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    gap, error = _gap_or_error(gap_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    reason = str(data.get('reason') or '').strip()
    if not reason:
        return _error(
            'CAPABILITY_GAP_REOPEN_INVALID', 'reason is required')
    try:
        idem_key = _required_idempotency_key(data)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_REOPEN_INVALID', str(exc))
    request_fingerprint = gap_request_hash('reopen', data)
    replay = _gap_replay(
        gap, 'reopened', actor, idem_key, request_fingerprint)
    if replay:
        return replay
    try:
        expected_version = _gap_expected_version(data, gap)
    except ValueError as exc:
        return _error('EXPECTED_VERSION_REQUIRED', str(exc))
    except RuntimeError:
        return _error(
            'CAPABILITY_GAP_VERSION_CONFLICT',
            'Capability Gap has been updated by another writer', 409,
            details={'expected_version': data.get('expected_version'),
                     'current_version': gap.version})
    if gap.status != 'resolved':
        return _error(
            'CAPABILITY_GAP_NOT_RESOLVED',
            'Only a resolved Capability Gap can be reopened', 409)
    previous_status = gap.status
    gap.status = 'open'
    gap.candidate_requeue_pending = False
    gap.resolution_json = {}
    gap.resolved_by = ''
    gap.resolved_at = None
    gap.version = expected_version + 1
    gap.updated_by = actor['name']
    gap.updated_at = _now()
    return _save_gap_transition(
        gap, actor, 'reopened', idem_key, request_fingerprint,
        previous_status, expected_version, extra={'reason': reason})


@api_bp.route(
    '/capability-gaps/<int:gap_id>/requeue-candidates', methods=['POST'])
def requeue_capability_gap_candidates(gap_id):
    actor, auth_error = _require_actor()
    if auth_error:
        return auth_error
    gap, error = _gap_or_error(gap_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        idem_key = _required_idempotency_key(data)
    except ValueError as exc:
        return _error('CAPABILITY_GAP_REQUEUE_INVALID', str(exc))
    request_fingerprint = gap_request_hash('requeue_candidates', data)
    replay = _gap_replay(
        gap, 'candidates_requeued', actor, idem_key, request_fingerprint)
    if replay:
        return replay
    try:
        expected_version = _gap_expected_version(data, gap)
    except ValueError as exc:
        return _error('EXPECTED_VERSION_REQUIRED', str(exc))
    except RuntimeError:
        return _error(
            'CAPABILITY_GAP_VERSION_CONFLICT',
            'Capability Gap has been updated by another writer', 409,
            details={'expected_version': data.get('expected_version'),
                     'current_version': gap.version})
    if gap.status != 'resolved':
        return _error(
            'CAPABILITY_GAP_NOT_RESOLVED',
            'Candidates can only be requeued after the Gap is resolved', 409)
    raw_candidate_ids = data.get('candidate_ids')
    selected_ids = None
    if raw_candidate_ids is not None:
        if not isinstance(raw_candidate_ids, list) or not raw_candidate_ids:
            return _error(
                'CAPABILITY_GAP_REQUEUE_INVALID',
                'candidate_ids must be a non-empty array when provided')
        try:
            selected_ids = {int(value) for value in raw_candidate_ids}
        except (TypeError, ValueError):
            return _error(
                'CAPABILITY_GAP_REQUEUE_INVALID',
                'candidate_ids must contain integers')
    candidates = (AutomationCaseCandidate.query.filter_by(
        capability_gap_id=gap.id).order_by(AutomationCaseCandidate.id.asc())
        .with_for_update().all())
    associated_ids = {row.id for row in candidates}
    if selected_ids is not None and not selected_ids.issubset(associated_ids):
        return _error(
            'CAPABILITY_GAP_CANDIDATE_MISMATCH',
            'One or more candidates are not associated with this Gap', 400,
            details={'candidate_ids': sorted(selected_ids - associated_ids)})
    requeued = []
    skipped = []
    relations = []
    for candidate in candidates:
        if selected_ids is not None and candidate.id not in selected_ids:
            continue
        if candidate.state != 'WAITING_CAPABILITY':
            skipped.append({'candidate_id': candidate.id, 'state': candidate.state})
            continue
        version_before = candidate.version
        candidate.state = 'READY_FOR_CANARY'
        candidate.qualification_outcome = None
        candidate.qualification_run_id = None
        candidate.qualification_evidence_json = {}
        candidate.version = int(candidate.version or 1) + 1
        candidate.updated_by = actor['name']
        candidate.updated_at = _now()
        _event(
            candidate, 'capability_gap_requeued', actor,
            'WAITING_CAPABILITY', 'READY_FOR_CANARY',
            version_before, candidate.version,
            payload={'capability_gap_id': gap.id},
            idempotency_key=idem_key)
        requeued.append(candidate.id)
        relations.append({
            'from_type': 'automation_case_candidate',
            'from_id': str(candidate.id),
            'relation_type': 'unblocked_by',
            'to_type': 'capability_gap',
            'to_id': str(gap.id),
            'metadata': {'gap_version': expected_version},
        })
    db.session.flush()
    gap.candidate_requeue_pending = (
        AutomationCaseCandidate.query.filter_by(
            capability_gap_id=gap.id,
            state='WAITING_CAPABILITY',
        ).first() is not None)
    gap.version = expected_version + 1
    gap.updated_by = actor['name']
    gap.updated_at = _now()
    best_effort_upsert_relations(
        gap.project_id, relations, actor['name'])
    return _save_gap_transition(
        gap, actor, 'candidates_requeued', idem_key, request_fingerprint,
        gap.status, expected_version, extra={
            'requeued_candidate_ids': requeued,
            'skipped_candidates': skipped,
        })
