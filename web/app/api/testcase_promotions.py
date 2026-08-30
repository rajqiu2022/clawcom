"""Atomic automation-candidate promotion and formal library revisions (P0-C)."""

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
    Project,
    TestCase,
    TestCaseLibrary,
    TestCaseLibraryPromotion,
    TestCaseLibraryRevision,
)
from app.services.test_case_delete import cleanup_test_case_dependencies
from app.services.entity_relations import best_effort_upsert_relations
from app.services.testcase_library_versioning import (
    candidate_case_values,
    canonical_hash,
    canonical_library_snapshot,
    ensure_library_revision,
    request_hash,
    snapshot_case_values,
)


_CST = timezone(timedelta(hours=8))
_ELIGIBLE_STATES = {'QUALIFIED', 'PENDING_PUBLISH'}


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


def _library_accessible(actor, library):
    if is_admin_user():
        return True
    project = Project.query.filter_by(name=library.project_name).first()
    if actor['type'] == 'claw':
        claw = actor['claw']
        return claw.project_id is None or (
            project is not None and int(claw.project_id) == int(project.id))
    if project is not None and int(project.id) in user_project_ids(actor['user']):
        return True
    return library.owner == actor['name'] or library.created_by == actor['name']


def _library_or_error(library_id, actor, lock=False):
    query = TestCaseLibrary.query.filter_by(id=library_id)
    if lock:
        query = query.with_for_update()
    library = query.first()
    if not library or not _library_accessible(actor, library):
        return None, _error(
            'TESTCASE_LIBRARY_NOT_FOUND', 'Testcase library was not found', 404)
    return library, None


def _idempotency_key(data):
    body_key = str(data.get('idempotency_key') or '').strip()
    header_key = str(request.headers.get('Idempotency-Key') or '').strip()
    if body_key and header_key and body_key != header_key:
        raise ValueError('Body and header idempotency keys must match')
    key = body_key or header_key
    if not key:
        raise ValueError('idempotency_key is required')
    if len(key) > 128:
        raise ValueError('idempotency_key must not exceed 128 characters')
    return key


def _integer_list(value, field, required=False):
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError(f'{field} must be an array')
    result = []
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            raise ValueError(f'{field} must contain only integers') from None
        if parsed not in result:
            result.append(parsed)
    if required and not result:
        raise ValueError(f'{field} must contain at least one item')
    return sorted(result)


def _expected_revision(data):
    try:
        value = int(data.get('expected_library_revision'))
    except (TypeError, ValueError):
        raise ValueError('expected_library_revision must be an integer') from None
    if value < 0:
        raise ValueError('expected_library_revision must not be negative')
    return value


def _event(candidate, actor, from_state, version_before, payload):
    db.session.add(AutomationCaseCandidateEvent(
        candidate_id=candidate.id,
        event_type='promoted',
        from_state=from_state,
        to_state='ACTIVE',
        version_before=version_before,
        version_after=candidate.version,
        payload_json=payload,
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        request_id=_request_id(),
    ))


def _idempotent_result(existing, expected_hash):
    if not existing:
        return None
    if existing.request_hash != expected_hash:
        return _error(
            'IDEMPOTENCY_KEY_CONFLICT',
            'idempotency_key was already used with a different request', 409,
            {'operation_id': existing.id})
    payload = dict(existing.response_json or {})
    payload['idempotent_replay'] = True
    return jsonify(payload), 200


def _revision_conflict_after_rollback(library_id, expected, message):
    """Return a stable 409 after a concurrent unique/lock conflict."""
    db.session.rollback()
    current = db.session.get(TestCaseLibrary, library_id)
    actual = int(current.revision or 0) if current else None
    return _error(
        'LIBRARY_REVISION_CONFLICT', message, 409, {
            'expected_library_revision': expected,
            'actual_library_revision': actual,
            'content_hash': current.content_hash if current else '',
            'retryable': True,
        })


@api_bp.route('/testcase-libraries/<int:library_id>/promotions', methods=['POST'])
def promote_automation_candidates(library_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    try:
        candidate_ids = _integer_list(
            data.get('candidate_ids'), 'candidate_ids', required=True)
        source_run_ids = _integer_list(data.get('source_run_ids'), 'source_run_ids')
        expected = _expected_revision(data)
        idem_key = _idempotency_key(data)
    except ValueError as exc:
        return _error('PROMOTION_VALIDATION_FAILED', str(exc))
    dry_run = bool(data.get('dry_run', False))
    message = str(data.get('message') or '').strip()[:500]
    hash_payload = {
        'operation': 'promotion',
        'library_id': library_id,
        'candidate_ids': candidate_ids,
        'source_run_ids': source_run_ids,
        'expected_library_revision': expected,
        'message': message,
        'dry_run': dry_run,
    }
    req_hash = request_hash(hash_payload)

    library, access_error = _library_or_error(library_id, actor, lock=True)
    if access_error:
        return access_error
    # Check idempotency only after the library writer lock. Concurrent replays
    # then observe the committed result from the first writer.
    existing = TestCaseLibraryPromotion.query.filter_by(
        library_id=library_id, idempotency_key=idem_key).first()
    replay = _idempotent_result(existing, req_hash)
    if replay:
        return replay
    candidates = (AutomationCaseCandidate.query
                  .filter(AutomationCaseCandidate.id.in_(candidate_ids))
                  .order_by(AutomationCaseCandidate.id.asc())
                  .with_for_update().all())
    found_ids = {candidate.id for candidate in candidates}
    missing = [candidate_id for candidate_id in candidate_ids
               if candidate_id not in found_ids]
    if missing:
        db.session.rollback()
        return _error(
            'AUTOMATION_CASE_CANDIDATE_NOT_FOUND',
            'One or more automation candidates were not found', 404,
            {'candidate_ids': missing})

    project = Project.query.filter_by(name=library.project_name).first()
    conflicts = []
    plans = []
    for candidate in candidates:
        if project and candidate.project_id != project.id:
            conflicts.append({
                'candidate_id': candidate.id, 'code': 'PROJECT_MISMATCH'})
        if (candidate.production_library_id is not None and
                candidate.production_library_id != library.id):
            conflicts.append({
                'candidate_id': candidate.id, 'code': 'LIBRARY_MISMATCH'})
        if (candidate.state not in _ELIGIBLE_STATES or
                candidate.qualification_outcome != 'QUALIFIED'):
            conflicts.append({
                'candidate_id': candidate.id,
                'code': 'CANDIDATE_NOT_QUALIFIED',
                'state': candidate.state,
                'qualification_outcome': candidate.qualification_outcome,
            })
        try:
            values = candidate_case_values(candidate, actor['name'])
        except ValueError as exc:
            conflicts.append({
                'candidate_id': candidate.id,
                'code': 'INVALID_CASE_DRAFT', 'message': str(exc)})
            continue
        production_case = None
        if candidate.production_case_id is not None:
            production_case = db.session.get(TestCase, candidate.production_case_id)
            if not production_case or production_case.library_id != library.id:
                conflicts.append({
                    'candidate_id': candidate.id,
                    'code': 'PRODUCTION_CASE_MISMATCH'})
                continue
        plans.append((candidate, production_case, values))

    if conflicts:
        db.session.rollback()
        return _error(
            'PROMOTION_CONFLICT', 'Candidates are not eligible for promotion',
            409, {'conflicts': conflicts})

    try:
        current_row, drifted = ensure_library_revision(library, actor['name'])
    except IntegrityError:
        return _revision_conflict_after_rollback(
            library_id, expected,
            'Concurrent testcase library revision update; reload and retry')
    current_revision = int(library.revision or 0)
    if current_revision != expected:
        # ``ensure_library_revision`` may have staged a baseline/legacy-sync
        # row while checking live content.  A rejected writer must not publish
        # that row: promotion, snapshot and candidate state are one atomic
        # transaction, including the failure path.
        db.session.rollback()
        return _error(
            'LIBRARY_REVISION_CONFLICT',
            'Testcase library revision changed; run dry-run again', 409, {
                'expected_library_revision': expected,
                'actual_library_revision': current_revision,
                'content_hash': current_row.content_hash,
                'legacy_content_captured': drifted,
            })

    created_candidate_ids = [candidate.id for candidate, case, _ in plans if not case]
    updated_candidate_ids = [candidate.id for candidate, case, _ in plans if case]
    if dry_run:
        # A dry-run is a read-only preview.  In particular, do not leave the
        # implicit revision-0 baseline created by ensure_library_revision.
        preview_content_hash = current_row.content_hash
        db.session.rollback()
        return jsonify({
            'dry_run': True,
            'library_id': library.id,
            'before_revision': current_revision,
            'expected_after_revision': current_revision + 1,
            'content_hash': preview_content_hash,
            'candidate_states': {
                str(candidate.id): candidate.state for candidate in candidates},
            'diff': {
                'create_count': len(created_candidate_ids),
                'update_count': len(updated_candidate_ids),
                'create_candidate_ids': created_candidate_ids,
                'update_candidate_ids': updated_candidate_ids,
            },
            'conflicts': [],
            'idempotent_replay': False,
        }), 200

    created_case_ids = []
    updated_case_ids = []
    for candidate, production_case, values in plans:
        if production_case is None:
            production_case = TestCase(
                library_id=library.id,
                created_by=actor['name'],
                **values,
            )
            db.session.add(production_case)
            db.session.flush()
            created_case_ids.append(production_case.id)
        else:
            for key, value in values.items():
                setattr(production_case, key, value)
            db.session.flush()
            updated_case_ids.append(production_case.id)
        from_state = candidate.state
        version_before = candidate.version
        candidate.state = 'ACTIVE'
        candidate.production_library_id = library.id
        candidate.production_case_id = production_case.id
        candidate.updated_by = actor['name']
        candidate.updated_at = _now()
        candidate.version += 1
        _event(candidate, actor, from_state, version_before, {
            'library_id': library.id,
            'production_case_id': production_case.id,
            'source_run_ids': source_run_ids,
        })

    db.session.flush()
    after_revision = current_revision + 1
    library.revision = after_revision
    library.updated_by = actor['name']
    library.updated_at = _now()
    snapshot = canonical_library_snapshot(library)
    content_hash = canonical_hash(snapshot)
    library.content_hash = content_hash
    operation = TestCaseLibraryPromotion(
        library_id=library.id,
        operation_type='promotion',
        idempotency_key=idem_key,
        request_hash=req_hash,
        before_revision=current_revision,
        after_revision=after_revision,
        content_hash=content_hash,
        candidate_ids_json=candidate_ids,
        created_case_ids_json=created_case_ids,
        updated_case_ids_json=updated_case_ids,
        source_run_ids_json=source_run_ids,
        message=message,
        response_json={},
        created_by=actor['name'],
    )
    db.session.add(operation)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = TestCaseLibraryPromotion.query.filter_by(
            library_id=library_id, idempotency_key=idem_key).first()
        replay = _idempotent_result(existing, req_hash)
        if replay:
            return replay
        raise
    revision = TestCaseLibraryRevision(
        library_id=library.id,
        revision=after_revision,
        content_hash=content_hash,
        case_count=len(snapshot['cases']),
        snapshot_json=snapshot,
        source_type='promotion',
        source_reference=f'promotion:{operation.id}',
        message=message,
        created_by=actor['name'],
    )
    db.session.add(revision)
    try:
        db.session.flush()
    except IntegrityError:
        return _revision_conflict_after_rollback(
            library_id, expected,
            'Concurrent testcase library revision update; reload and retry')
    lineage = []
    for candidate, production_case, _ in plans:
        lineage.append({
            'from_type': 'automation_case_candidate',
            'from_id': str(candidate.id),
            'relation_type': 'promoted_to',
            'to_type': 'testcase',
            'to_id': str(candidate.production_case_id),
            'metadata': {
                'library_id': library.id,
                'library_revision': after_revision,
                'promotion_id': operation.id,
            },
        })
        lineage.append({
            'from_type': 'testcase',
            'from_id': str(candidate.production_case_id),
            'relation_type': 'included_in',
            'to_type': 'testcase_library_revision',
            'to_id': f'{library.id}:{after_revision}',
            'metadata': {
                'library_id': library.id,
                'content_hash': content_hash,
            },
        })
    best_effort_upsert_relations(
        candidates[0].project_id if candidates else project.id,
        lineage,
        actor['name'],
    )
    response = {
        'promotion_id': operation.id,
        'library_id': library.id,
        'before_revision': current_revision,
        'after_revision': after_revision,
        'content_hash': content_hash,
        'created_case_ids': created_case_ids,
        'updated_case_ids': updated_case_ids,
        'snapshot_version': after_revision,
        'candidate_states': {
            str(candidate.id): candidate.state for candidate in candidates},
        'idempotent_replay': False,
    }
    operation.response_json = response
    db.session.commit()
    return jsonify(response), 201


@api_bp.route('/testcase-libraries/<int:library_id>/revisions', methods=['GET'])
def list_testcase_library_revisions(library_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    library, access_error = _library_or_error(library_id, actor, lock=False)
    if access_error:
        return access_error
    # GET is strictly read-only. Returning history must never create a
    # legacy_sync revision or make the library detail and history endpoints
    # briefly disagree. Expose drift explicitly for clients instead.
    live_snapshot = canonical_library_snapshot(library)
    live_content_hash = canonical_hash(live_snapshot)
    formal_row = TestCaseLibraryRevision.query.filter_by(
        library_id=library_id,
        revision=int(library.revision or 0),
    ).first()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(100, max(1, int(request.args.get('per_page', 20))))
    except ValueError:
        return _error('INVALID_PAGINATION', 'page and per_page must be integers')
    query = TestCaseLibraryRevision.query.filter_by(library_id=library_id)
    content_hash_filter = str(request.args.get('content_hash') or '').strip()
    if content_hash_filter:
        query = query.filter_by(content_hash=content_hash_filter)
    pagination = (query
                  .order_by(TestCaseLibraryRevision.revision.desc())
                  .paginate(page=page, per_page=per_page, error_out=False))
    return jsonify({
        'items': [row.to_dict() for row in pagination.items],
        'page': page,
        'per_page': per_page,
        'total': pagination.total,
        'library_revision': int(library.revision or 0),
        'content_hash': library.content_hash,
        'live_content_hash': live_content_hash,
        'content_drift': (
            formal_row is None or formal_row.content_hash != live_content_hash),
    })


@api_bp.route(
    '/testcase-libraries/<int:library_id>/revisions/<int:revision_number>',
    methods=['GET'])
def get_testcase_library_revision(library_id, revision_number):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    library, access_error = _library_or_error(library_id, actor, lock=False)
    if access_error:
        return access_error
    row = TestCaseLibraryRevision.query.filter_by(
        library_id=library_id, revision=revision_number).first()
    if not row:
        return _error(
            'TESTCASE_LIBRARY_REVISION_NOT_FOUND',
            'Testcase library revision was not found', 404)
    include_snapshot = str(request.args.get('include_snapshot', 'true')).lower() in {
        '1', 'true', 'yes'}
    return jsonify(row.to_dict(with_snapshot=include_snapshot))


@api_bp.route(
    '/testcase-libraries/<int:library_id>/revisions/<int:revision_number>/'
    'cases/<int:case_id>', methods=['GET'])
def get_testcase_at_library_revision(library_id, revision_number, case_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    library, access_error = _library_or_error(library_id, actor, lock=False)
    if access_error:
        return access_error
    row = TestCaseLibraryRevision.query.filter_by(
        library_id=library_id, revision=revision_number).first()
    if not row:
        return _error(
            'TESTCASE_LIBRARY_REVISION_NOT_FOUND',
            'Testcase library revision was not found', 404)
    for case in (row.snapshot_json or {}).get('cases', []):
        if int(case.get('id') or 0) == case_id:
            return jsonify({
                'library_id': library_id,
                'revision': revision_number,
                'content_hash': row.content_hash,
                'case': case,
            })
    return _error(
        'TESTCASE_NOT_FOUND_AT_REVISION',
        'Testcase did not exist at the requested revision', 404)


@api_bp.route('/testcase-libraries/<int:library_id>/promotions', methods=['GET'])
def list_testcase_library_promotions(library_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    library, access_error = _library_or_error(library_id, actor, lock=False)
    if access_error:
        return access_error
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = min(100, max(1, int(request.args.get('per_page', 20))))
    except ValueError:
        return _error('INVALID_PAGINATION', 'page and per_page must be integers')
    query = TestCaseLibraryPromotion.query.filter_by(library_id=library_id)
    operation_type = str(request.args.get('operation_type') or '').strip()
    if operation_type:
        if operation_type not in {'promotion', 'rollback'}:
            return _error(
                'INVALID_OPERATION_TYPE',
                'operation_type must be promotion or rollback')
        query = query.filter_by(operation_type=operation_type)
    pagination = (query.order_by(TestCaseLibraryPromotion.id.desc())
                  .paginate(page=page, per_page=per_page, error_out=False))
    return jsonify({
        'items': [row.to_dict() for row in pagination.items],
        'page': page,
        'per_page': per_page,
        'total': pagination.total,
    })


@api_bp.route(
    '/testcase-libraries/<int:library_id>/revisions/<int:target_revision>/rollback',
    methods=['POST'])
def rollback_testcase_library_revision(library_id, target_revision):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    try:
        expected = _expected_revision(data)
        idem_key = _idempotency_key(data)
    except ValueError as exc:
        return _error('ROLLBACK_VALIDATION_FAILED', str(exc))
    message = str(data.get('message') or '').strip()[:500]
    req_hash = request_hash({
        'operation': 'rollback', 'library_id': library_id,
        'target_revision': target_revision,
        'expected_library_revision': expected, 'message': message,
    })
    library, access_error = _library_or_error(library_id, actor, lock=True)
    if access_error:
        return access_error
    existing = TestCaseLibraryPromotion.query.filter_by(
        library_id=library_id, idempotency_key=idem_key).first()
    replay = _idempotent_result(existing, req_hash)
    if replay:
        return replay
    try:
        current_row, drifted = ensure_library_revision(library, actor['name'])
    except IntegrityError:
        return _revision_conflict_after_rollback(
            library_id, expected,
            'Concurrent testcase library revision update; reload and retry')
    current_revision = int(library.revision or 0)
    if current_revision != expected:
        # Conflict responses are side-effect free, just like promotions.
        db.session.rollback()
        return _error(
            'LIBRARY_REVISION_CONFLICT',
            'Testcase library revision changed; reload history', 409, {
                'expected_library_revision': expected,
                'actual_library_revision': current_revision,
                'content_hash': current_row.content_hash,
                'legacy_content_captured': drifted,
            })
    target = TestCaseLibraryRevision.query.filter_by(
        library_id=library_id, revision=target_revision).first()
    if not target:
        db.session.rollback()
        return _error(
            'TESTCASE_LIBRARY_REVISION_NOT_FOUND',
            'Target testcase library revision was not found', 404)

    snapshot = target.snapshot_json or {}
    snapshot_cases = snapshot.get('cases')
    if snapshot.get('schema') != 'testcase-library-revision-v1' or not isinstance(
            snapshot_cases, list):
        db.session.rollback()
        return _error(
            'INVALID_LIBRARY_REVISION',
            'Target revision does not contain a restorable full snapshot', 409)
    target_by_id = {}
    try:
        for item in snapshot_cases:
            case_pk = int(item.get('id'))
            target_by_id[case_pk] = snapshot_case_values(item)
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return _error('INVALID_LIBRARY_REVISION', str(exc), 409)

    current_cases = TestCase.query.filter_by(library_id=library.id).all()
    current_by_id = {case.id: case for case in current_cases}
    extra_ids = sorted(set(current_by_id) - set(target_by_id))
    withdrawn_candidates = []
    if extra_ids:
        withdrawn_candidates = (AutomationCaseCandidate.query
                                .filter(AutomationCaseCandidate.production_case_id.in_(
                                    extra_ids))
                                .with_for_update().all())
        for candidate in withdrawn_candidates:
            from_state = candidate.state
            version_before = candidate.version
            removed_case_id = candidate.production_case_id
            candidate.state = 'PENDING_PUBLISH'
            candidate.production_case_id = None
            candidate.updated_by = actor['name']
            candidate.updated_at = _now()
            candidate.version += 1
            db.session.add(AutomationCaseCandidateEvent(
                candidate_id=candidate.id,
                event_type='promotion_rolled_back',
                from_state=from_state,
                to_state='PENDING_PUBLISH',
                version_before=version_before,
                version_after=candidate.version,
                payload_json={
                    'library_id': library.id,
                    'removed_case_id': removed_case_id,
                    'rollback_from_revision': target_revision,
                },
                actor_type=actor['type'],
                actor_id=actor['id'],
                actor_name=actor['name'],
                request_id=_request_id(),
            ))
    cleanup_test_case_dependencies(extra_ids)
    for case_id in extra_ids:
        db.session.delete(current_by_id[case_id])
    for case_id, values in target_by_id.items():
        case = current_by_id.get(case_id)
        if case is None:
            occupied = db.session.get(TestCase, case_id)
            if occupied is not None:
                db.session.rollback()
                return _error(
                    'CASE_ID_RESTORE_CONFLICT',
                    'A historical testcase ID is occupied by another library', 409,
                    {'case_id': case_id})
            case = TestCase(id=case_id, library_id=library.id)
            db.session.add(case)
        for key, value in values.items():
            setattr(case, key, value)
    library.mindmap = snapshot.get('mindmap')
    library.updated_by = actor['name']
    library.updated_at = _now()
    db.session.flush()

    after_revision = current_revision + 1
    library.revision = after_revision
    restored_snapshot = canonical_library_snapshot(library)
    content_hash = canonical_hash(restored_snapshot)
    library.content_hash = content_hash
    operation = TestCaseLibraryPromotion(
        library_id=library.id,
        operation_type='rollback',
        idempotency_key=idem_key,
        request_hash=req_hash,
        before_revision=current_revision,
        after_revision=after_revision,
        content_hash=content_hash,
        rollback_from_revision=target_revision,
        candidate_ids_json=[],
        created_case_ids_json=sorted(set(target_by_id) - set(current_by_id)),
        updated_case_ids_json=sorted(set(target_by_id) & set(current_by_id)),
        source_run_ids_json=[],
        message=message,
        response_json={},
        created_by=actor['name'],
    )
    db.session.add(operation)
    db.session.flush()
    revision = TestCaseLibraryRevision(
        library_id=library.id,
        revision=after_revision,
        content_hash=content_hash,
        case_count=len(restored_snapshot['cases']),
        snapshot_json=restored_snapshot,
        source_type='rollback',
        source_reference=f'rollback:{operation.id}',
        rollback_from_revision=target_revision,
        message=message,
        created_by=actor['name'],
    )
    db.session.add(revision)
    try:
        db.session.flush()
    except IntegrityError:
        return _revision_conflict_after_rollback(
            library_id, expected,
            'Concurrent testcase library revision update; reload and retry')
    response = {
        'rollback_id': operation.id,
        'library_id': library.id,
        'before_revision': current_revision,
        'after_revision': after_revision,
        'rollback_from_revision': target_revision,
        'content_hash': content_hash,
        'snapshot_version': after_revision,
        'restored_case_count': len(restored_snapshot['cases']),
        'idempotent_replay': False,
    }
    operation.response_json = response
    db.session.commit()
    return jsonify(response), 201


@api_bp.route(
    '/testcase-libraries/<int:library_id>/rollback', methods=['POST'])
def rollback_testcase_library_compat(library_id):
    """Compatibility form used by early clients and the acceptance script.

    The canonical resource-oriented route remains
    ``/revisions/{target_revision}/rollback``; both execute the exact same
    transaction and append-only audit revision.
    """
    data = request.get_json(silent=True) or {}
    try:
        target_revision = int(data.get('target_revision'))
    except (TypeError, ValueError):
        return _error(
            'ROLLBACK_VALIDATION_FAILED',
            'target_revision must be an integer')
    if target_revision < 0:
        return _error(
            'ROLLBACK_VALIDATION_FAILED',
            'target_revision must be zero or greater')
    return rollback_testcase_library_revision(library_id, target_revision)
