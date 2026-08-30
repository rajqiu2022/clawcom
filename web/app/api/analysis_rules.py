"""Finding feedback learning and replay-gated immutable analysis rules."""

import json
from datetime import timedelta
from uuid import uuid4

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    is_admin_user,
    user_project_ids,
)
from app.models import (
    AnalysisRule,
    AnalysisRuleReplay,
    AnalysisRuleVersion,
    AuditLog,
    Project,
    ShiftLeftFinding,
    WorkflowOperationIdempotency,
    WorkflowRun,
)
from app.services.analysis_rules import (
    content_hash,
    evaluate_replay_gate,
    normalize_artifact_refs,
    normalize_definition,
    normalize_finding_ids,
    normalize_impact,
    normalize_metrics,
    normalize_thresholds,
)
from app.services.entity_relations import best_effort_upsert_relations
from app.services.shift_left import now_cst_naive, payload_hash


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


def _can_access_project(actor, project_id):
    if not actor:
        return False
    if is_admin_user():
        return True
    if actor['type'] == 'claw':
        claw = actor['claw']
        return claw.project_id is None or int(claw.project_id) == int(project_id)
    return int(project_id) in user_project_ids(actor['user'])


def _project_or_error(actor, raw):
    try:
        project_id = int(raw)
    except (TypeError, ValueError):
        return None, _error('INVALID_PROJECT_ID', 'project_id must be an integer')
    if (not db.session.get(Project, project_id)
            or not _can_access_project(actor, project_id)):
        return None, _error('PROJECT_NOT_FOUND', 'Project was not found', 404)
    return project_id, None


def _idempotency_begin(actor):
    key = str(request.headers.get('Idempotency-Key') or '').strip()
    if not key:
        return None, _error(
            'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key is required')
    if len(key) > 128:
        return None, _error(
            'IDEMPOTENCY_KEY_TOO_LONG',
            'Idempotency-Key must not exceed 128 characters')
    body = request.get_json(silent=True) or {}
    request_fingerprint = payload_hash({
        'method': request.method,
        'path': request.path,
        'query': sorted(request.args.items(multi=True)),
        'body': body,
    })
    row = WorkflowOperationIdempotency.query.filter_by(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=key).first()
    if row:
        if (row.method != request.method or row.path != request.path
                or row.request_hash != request_fingerprint):
            return None, _error(
                'IDEMPOTENCY_KEY_REUSED',
                'The same idempotency key was used for a different request', 409)
        if row.response_status is None:
            return None, _error(
                'IDEMPOTENCY_IN_PROGRESS',
                'The same request is still in progress', 409)
        return None, (jsonify(row.response_body_json or {}), row.response_status)
    row = WorkflowOperationIdempotency(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=key, method=request.method, path=request.path,
        request_hash=request_fingerprint,
        expires_at=now_cst_naive() + timedelta(days=7),
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return None, _error(
            'IDEMPOTENCY_CONFLICT', 'Concurrent idempotent request conflict', 409)
    return row, None


def _commit_payload(idem, payload, status=200):
    try:
        idem.response_status = status
        idem.response_body_json = payload
        db.session.commit()
        return jsonify(payload), status
    except SQLAlchemyError:
        db.session.rollback()
        return _error('ANALYSIS_RULE_WRITE_FAILED', 'Failed to save analysis rule', 500)


def _add_audit(action, resource_type, resource_id, resource_name,
               actor, detail=None):
    db.session.add(AuditLog(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_name=str(resource_name or '')[:200],
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps(
            detail or {}, ensure_ascii=False, sort_keys=True, default=str),
    ))


def _rule_or_error(rule_id, actor, lock=False):
    query = AnalysisRule.query.filter_by(id=rule_id)
    row = query.with_for_update().first() if lock else query.first()
    if not row or not _can_access_project(actor, row.project_id):
        return None, _error('ANALYSIS_RULE_NOT_FOUND', 'Analysis rule was not found', 404)
    return row, None


def _version_or_error(version_id, actor, lock=False):
    # Resolve ownership first, then lock the stable rule identity before the
    # child version. This keeps concurrent activation of two versions on one
    # rule in a consistent lock order and avoids a cross-version deadlock.
    version = AnalysisRuleVersion.query.filter_by(id=version_id).first()
    if not version:
        return None, None, _error(
            'ANALYSIS_RULE_VERSION_NOT_FOUND', 'Analysis rule version was not found', 404)
    rule, error = _rule_or_error(version.rule_id, actor, lock=lock)
    if error:
        return None, None, error
    if lock:
        version = (AnalysisRuleVersion.query.filter_by(id=version_id)
                   .with_for_update().first())
    return version, rule, None


def _validate_source_findings(project_id, finding_ids):
    if not finding_ids:
        return [], None
    rows = ShiftLeftFinding.query.filter(
        ShiftLeftFinding.id.in_(finding_ids),
        ShiftLeftFinding.project_id == project_id,
        ShiftLeftFinding.is_archived.is_(False),
    ).all()
    found = {row.id for row in rows}
    missing = [value for value in finding_ids if value not in found]
    if missing:
        return None, _error(
            'FINDING_PROJECT_MISMATCH',
            'Source findings do not exist or belong to another project', 400,
            {'finding_ids': missing})
    return rows, None


def _version_payload(version, with_replays=False):
    payload = version.to_dict()
    if with_replays:
        payload['replays'] = [row.to_dict() for row in version.replays]
    return payload


@api_bp.route('/analysis-rules', methods=['POST'])
def create_analysis_rule():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    project_id, error = _project_or_error(actor, data.get('project_id'))
    if error:
        return error
    rule_key = str(data.get('rule_key') or '').strip()
    name = str(data.get('name') or '').strip()
    if not rule_key or not name or len(rule_key) > 255 or len(name) > 300:
        return _error(
            'ANALYSIS_RULE_IDENTITY_INVALID',
            'rule_key and name are required and must fit their limits')
    try:
        definition = normalize_definition(data.get('definition'))
        thresholds = normalize_thresholds(data.get('thresholds'))
        finding_ids = normalize_finding_ids(data.get('source_finding_ids'))
    except ValueError as exc:
        return _error('ANALYSIS_RULE_VALIDATION_FAILED', str(exc))
    _, finding_error = _validate_source_findings(project_id, finding_ids)
    if finding_error:
        return finding_error
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    existing = AnalysisRule.query.filter_by(
        project_id=project_id, rule_key=rule_key).first()
    if existing:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_KEY_EXISTS', 'rule_key already exists in the project', 409,
            {'rule_id': existing.id})
    rule = AnalysisRule(
        project_id=project_id, rule_key=rule_key, name=name,
        description=str(data.get('description') or ''),
        status='draft', latest_version=1, version=1,
        created_by=actor['name'], updated_by=actor['name'])
    db.session.add(rule)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        winner = AnalysisRule.query.filter_by(
            project_id=project_id, rule_key=rule_key).first()
        return _error(
            'ANALYSIS_RULE_KEY_EXISTS',
            'rule_key already exists in the project', 409,
            {'rule_id': winner.id if winner else None})
    version = AnalysisRuleVersion(
        rule_id=rule.id, version_no=1, status='draft',
        definition_json=definition, thresholds_json=thresholds,
        change_summary=str(data.get('change_summary') or 'initial version'),
        source_finding_ids_json=finding_ids,
        definition_hash=content_hash({
            'definition': definition, 'thresholds': thresholds}),
        created_by=actor['name'])
    db.session.add(version)
    db.session.flush()
    relations = [{
        'from_type': 'finding', 'from_id': str(finding_id),
        'relation_type': 'learned_into', 'to_type': 'learned_rule',
        'to_id': f'{rule.id}:1',
        'metadata': {'rule_key': rule.rule_key, 'version_status': 'draft'},
    } for finding_id in finding_ids]
    best_effort_upsert_relations(project_id, relations, actor['name'])
    _add_audit(
        'create', 'analysis_rule', rule.id, rule.name, actor,
        {'rule_key': rule.rule_key, 'rule_version_id': version.id,
         'version_no': 1})
    payload = rule.to_dict()
    payload['latest_version_detail'] = version.to_dict()
    return _commit_payload(idem, payload, 201)


@api_bp.route('/analysis-rules', methods=['GET'])
def list_analysis_rules():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    project_id, error = _project_or_error(actor, request.args.get('project_id'))
    if error:
        return error
    query = AnalysisRule.query.filter_by(project_id=project_id)
    status = str(request.args.get('status') or '').strip().lower()
    if status:
        if status not in {'draft', 'active', 'retired'}:
            return _error('INVALID_ANALYSIS_RULE_STATUS', 'Invalid analysis rule status')
        query = query.filter_by(status=status)
    try:
        page = max(1, int(request.args.get('page') or 1))
        page_size = max(1, min(200, int(request.args.get('page_size') or 50)))
    except (TypeError, ValueError):
        return _error('INVALID_PAGINATION', 'page and page_size must be integers')
    total = query.count()
    rows = (query.order_by(AnalysisRule.updated_at.desc(), AnalysisRule.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return jsonify({
        'items': [row.to_dict() for row in rows], 'total': total,
        'page': page, 'page_size': page_size,
        'pages': (total + page_size - 1) // page_size,
    })


@api_bp.route('/analysis-rules/<int:rule_id>', methods=['GET'])
def get_analysis_rule(rule_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    rule, error = _rule_or_error(rule_id, actor)
    if error:
        return error
    payload = rule.to_dict()
    payload['versions'] = [_version_payload(row, with_replays=True)
                           for row in rule.versions]
    return jsonify(payload)


@api_bp.route('/analysis-rules/<int:rule_id>/versions', methods=['POST'])
def create_analysis_rule_version(rule_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    rule, error = _rule_or_error(rule_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        definition = normalize_definition(data.get('definition'))
        thresholds = normalize_thresholds(data.get('thresholds'))
        finding_ids = normalize_finding_ids(data.get('source_finding_ids'))
    except ValueError as exc:
        return _error('ANALYSIS_RULE_VALIDATION_FAILED', str(exc))
    _, finding_error = _validate_source_findings(rule.project_id, finding_ids)
    if finding_error:
        return finding_error
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    try:
        expected_version = int(data.get('expected_version'))
    except (TypeError, ValueError):
        db.session.rollback()
        return _error('EXPECTED_VERSION_REQUIRED', 'expected_version must be an integer')
    if expected_version != rule.version:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_VERSION_CONFLICT',
            'Analysis rule was updated by another writer', 409,
            {'expected_version': expected_version, 'current_version': rule.version})
    definition_digest = content_hash({
        'definition': definition, 'thresholds': thresholds})
    duplicate = AnalysisRuleVersion.query.filter_by(
        rule_id=rule.id, definition_hash=definition_digest).first()
    if duplicate:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_DEFINITION_EXISTS',
            'An identical immutable definition already exists', 409,
            {'rule_version_id': duplicate.id, 'version_no': duplicate.version_no})
    based_on_id = data.get('based_on_version_id') or rule.active_version_id
    if based_on_id is not None:
        based_on = db.session.get(AnalysisRuleVersion, based_on_id)
        if not based_on or based_on.rule_id != rule.id:
            db.session.rollback()
            return _error(
                'ANALYSIS_RULE_BASE_VERSION_INVALID',
                'based_on_version_id does not belong to this rule')
    version_no = int(rule.latest_version or 0) + 1
    version = AnalysisRuleVersion(
        rule_id=rule.id, version_no=version_no, status='draft',
        definition_json=definition, thresholds_json=thresholds,
        change_summary=str(data.get('change_summary') or ''),
        based_on_version_id=based_on_id,
        source_finding_ids_json=finding_ids,
        definition_hash=definition_digest, created_by=actor['name'])
    db.session.add(version)
    rule.latest_version = version_no
    rule.version += 1
    if not rule.active_version_id:
        rule.status = 'draft'
    rule.updated_by = actor['name']
    rule.updated_at = now_cst_naive()
    db.session.flush()
    relations = [{
        'from_type': 'finding', 'from_id': str(finding_id),
        'relation_type': 'learned_into', 'to_type': 'learned_rule',
        'to_id': f'{rule.id}:{version_no}',
        'metadata': {'rule_key': rule.rule_key, 'version_status': 'draft'},
    } for finding_id in finding_ids]
    best_effort_upsert_relations(rule.project_id, relations, actor['name'])
    _add_audit(
        'create_version', 'analysis_rule', rule.id, rule.name, actor,
        {'rule_version_id': version.id, 'version_no': version_no,
         'based_on_version_id': based_on_id, 'rule_version': rule.version})
    payload = version.to_dict()
    payload['rule_version'] = rule.version
    return _commit_payload(idem, payload, 201)


@api_bp.route('/analysis-rule-versions/<int:version_id>/replays', methods=['POST'])
def record_analysis_rule_replay(version_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    version, rule, error = _version_or_error(version_id, actor, lock=True)
    if error:
        return error
    if version.status not in {'draft', 'replay_passed'}:
        return _error(
            'ANALYSIS_RULE_REPLAY_NOT_ALLOWED',
            'Only draft or replay_passed versions accept qualification replay', 409)
    data = request.get_json(silent=True) or {}
    dataset_ref = str(data.get('dataset_snapshot_ref') or '').strip()
    dataset_hash = str(data.get('dataset_hash') or '').strip().lower()
    if not dataset_ref or len(dataset_ref) > 1000:
        return _error(
            'ANALYSIS_RULE_REPLAY_INVALID',
            'dataset_snapshot_ref is required and limited to 1000 characters')
    if len(dataset_hash) != 64 or any(c not in '0123456789abcdef' for c in dataset_hash):
        return _error(
            'ANALYSIS_RULE_REPLAY_INVALID', 'dataset_hash must be 64 hex characters')
    try:
        metrics = normalize_metrics(data.get('metrics'))
        impact = normalize_impact(data.get('impact'))
        artifact_refs = normalize_artifact_refs(data.get('artifact_refs'))
    except ValueError as exc:
        return _error('ANALYSIS_RULE_REPLAY_INVALID', str(exc))
    workflow_run_id = data.get('workflow_run_id')
    if workflow_run_id is not None:
        try:
            workflow_run_id = int(workflow_run_id)
        except (TypeError, ValueError):
            return _error(
                'ANALYSIS_RULE_REPLAY_INVALID', 'workflow_run_id must be an integer')
        workflow_run = db.session.get(WorkflowRun, workflow_run_id)
        if not workflow_run or workflow_run.project_id != rule.project_id:
            return _error(
                'ANALYSIS_RULE_REPLAY_RUN_MISMATCH',
                'workflow_run_id does not belong to the rule project')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    gate_passed, reasons = evaluate_replay_gate(
        metrics, version.thresholds_json or {})
    replay_row = AnalysisRuleReplay(
        rule_version_id=version.id, workflow_run_id=workflow_run_id,
        dataset_snapshot_ref=dataset_ref, dataset_hash=dataset_hash,
        metrics_json=metrics, impact_json=impact,
        artifact_refs_json=artifact_refs, gate_passed=gate_passed,
        gate_reasons_json=reasons, status='completed',
        idempotency_key=str(request.headers.get('Idempotency-Key')),
        request_hash=idem.request_hash, actor_type=actor['type'],
        actor_id=actor['id'], actor_name=actor['name'])
    db.session.add(replay_row)
    if gate_passed and version.status == 'draft':
        version.status = 'replay_passed'
    db.session.flush()
    _add_audit(
        'replay', 'analysis_rule_version', version.id,
        f'Rule #{rule.id} v{version.version_no}', actor,
        {'replay_id': replay_row.id, 'gate_passed': gate_passed,
         'dataset_hash': dataset_hash, 'metrics': metrics})
    payload = replay_row.to_dict()
    payload['rule_version_status'] = version.status
    return _commit_payload(idem, payload, 201)


@api_bp.route('/analysis-rule-versions/<int:version_id>/activate', methods=['POST'])
def activate_analysis_rule_version(version_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    version, rule, error = _version_or_error(version_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    try:
        expected_version = int(data.get('expected_version'))
    except (TypeError, ValueError):
        db.session.rollback()
        return _error('EXPECTED_VERSION_REQUIRED', 'expected_version must be an integer')
    if expected_version != rule.version:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_VERSION_CONFLICT',
            'Analysis rule was updated by another writer', 409,
            {'expected_version': expected_version, 'current_version': rule.version})
    if version.status != 'replay_passed' or not any(
            row.gate_passed for row in version.replays):
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_REPLAY_GATE_REQUIRED',
            'A replay_passed version with a passing replay is required', 409)
    now = now_cst_naive()
    previous_active = None
    if rule.active_version_id:
        previous_active = (AnalysisRuleVersion.query.filter_by(
            id=rule.active_version_id).with_for_update().first())
        if previous_active and previous_active.id != version.id:
            previous_active.status = 'retired'
            previous_active.retired_by = actor['name']
            previous_active.retired_at = now
    version.status = 'active'
    version.activated_by = actor['name']
    version.activated_at = now
    rule.active_version_id = version.id
    rule.status = 'active'
    rule.version += 1
    rule.updated_by = actor['name']
    rule.updated_at = now
    relations = [{
        'from_type': 'finding', 'from_id': str(finding_id),
        'relation_type': 'learned_into', 'to_type': 'learned_rule',
        'to_id': f'{rule.id}:{version.version_no}',
        'metadata': {'rule_key': rule.rule_key, 'version_status': 'active'},
    } for finding_id in (version.source_finding_ids_json or [])]
    best_effort_upsert_relations(rule.project_id, relations, actor['name'])
    _add_audit(
        'activate', 'analysis_rule_version', version.id,
        f'Rule #{rule.id} v{version.version_no}', actor,
        {'rule_id': rule.id,
         'retired_previous_version_id': (
             previous_active.id
             if previous_active and previous_active.id != version.id else None),
         'rule_version': rule.version})
    payload = version.to_dict()
    payload['rule'] = rule.to_dict()
    payload['retired_previous_version_id'] = (
        previous_active.id if previous_active and previous_active.id != version.id
        else None)
    return _commit_payload(idem, payload, 200)


@api_bp.route('/analysis-rule-versions/<int:version_id>/retire', methods=['POST'])
def retire_analysis_rule_version(version_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    version, rule, error = _version_or_error(version_id, actor, lock=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    try:
        expected_version = int(data.get('expected_version'))
    except (TypeError, ValueError):
        db.session.rollback()
        return _error('EXPECTED_VERSION_REQUIRED', 'expected_version must be an integer')
    if expected_version != rule.version:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_VERSION_CONFLICT',
            'Analysis rule was updated by another writer', 409,
            {'expected_version': expected_version, 'current_version': rule.version})
    if version.status != 'active' or rule.active_version_id != version.id:
        db.session.rollback()
        return _error(
            'ANALYSIS_RULE_NOT_ACTIVE', 'Only the active version can be retired', 409)
    reason = str(data.get('reason') or '').strip()
    if not reason:
        db.session.rollback()
        return _error('ANALYSIS_RULE_RETIRE_INVALID', 'reason is required')
    version.status = 'retired'
    version.retired_by = actor['name']
    version.retired_at = now_cst_naive()
    rule.active_version_id = None
    has_pending_version = any(
        row.id != version.id and row.status in {'draft', 'replay_passed'}
        for row in rule.versions)
    rule.status = 'draft' if has_pending_version else 'retired'
    rule.version += 1
    rule.updated_by = actor['name']
    rule.updated_at = now_cst_naive()
    payload = version.to_dict()
    payload['rule'] = rule.to_dict()
    payload['reason'] = reason
    _add_audit(
        'retire', 'analysis_rule_version', version.id,
        f'Rule #{rule.id} v{version.version_no}', actor,
        {'rule_id': rule.id, 'reason': reason, 'rule_version': rule.version})
    return _commit_payload(idem, payload, 200)
