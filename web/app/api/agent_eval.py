"""Minimal, version-preserving Agent evaluation control plane."""

import json
import math
import re

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app import db
from app.api import api_bp
from app.api.auth_utils import user_project_ids
from app.api.mission_stages import (
    _actor, _can_access_project, _can_administer, _disabled_response,
)
from app.models import (
    AgentArtifact, AgentEvalCase, AgentEvalDataset, AgentEvalRun,
    AgentEvalScore, AgentPost, AgentPostAssignment, AuditLog,
    OpenClawInstance, Project, WorkflowMission, WorkflowOperationIdempotency,
    WorkflowRun, _now,
)
from app.services.agent_artifacts import canonical_json, sha256_json


_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$')
_SPLITS = frozenset({'golden', 'challenge', 'hidden_holdout', 'live'})
_RUN_STATUSES = frozenset({
    'pending', 'running', 'completed', 'failed', 'result_invalid', 'cancelled'})
_TERMINAL_RUN_STATUSES = frozenset({
    'completed', 'failed', 'result_invalid', 'cancelled'})
_DIMENSION_MAX = {
    'schema': 15.0,
    'baseline': 15.0,
    'factual_correctness': 25.0,
    'evidence': 20.0,
    'role_judgment': 15.0,
    'efficiency': 5.0,
    'experience': 5.0,
}
_FATAL_CODES = frozenset({
    'CREDENTIAL_LEAK', 'UNAUTHORIZED_SIDE_EFFECT', 'FABRICATED_EVIDENCE',
    'STALE_BASELINE', 'HUMAN_GATE_BYPASS',
    'FAILURE_CLASSIFICATION_FRAUD',
})


def _error(code, message, status=400, details=None):
    body = {'error': message, 'code': code}
    if details:
        body['details'] = details
    return jsonify(body), status


def _positive_int(value, field):
    if isinstance(value, bool):
        raise ValueError(f'{field} must be a positive integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be a positive integer') from exc
    if result <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return result


def _key(value, field, maximum=120):
    result = str(value or '').strip()
    if len(result) > maximum or not _KEY_RE.fullmatch(result):
        raise ValueError(f'{field} must be a stable key up to {maximum} chars')
    return result


def _idempotency_key(data):
    body = str((data or {}).get('idempotency_key') or '').strip()
    header = str(request.headers.get('Idempotency-Key') or '').strip()
    if body and header and body != header:
        return None, _error(
            'IDEMPOTENCY_KEY_MISMATCH',
            'body and header idempotency keys must match', 400)
    value = body or header
    if not value:
        return None, _error(
            'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key is required', 400)
    if len(value) > 128:
        return None, _error(
            'IDEMPOTENCY_KEY_TOO_LONG',
            'Idempotency-Key must not exceed 128 characters', 400)
    return value, None


def _unknown(data, allowed):
    fields = sorted(set(data or {}) - allowed)
    if fields:
        return _error('UNKNOWN_FIELD', 'unknown fields: ' + ', '.join(fields), 400)
    return None


def _json_object(data, field, default=None, max_bytes=256 * 1024):
    value = data.get(field, {} if default is None else default)
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be a JSON object')
    if len(canonical_json(value).encode('utf-8')) > max_bytes:
        raise ValueError(f'{field} is too large')
    return value


def _json_list(data, field, default=None, maximum=200):
    value = data.get(field, [] if default is None else default)
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f'{field} must be an array of at most {maximum} items')
    if len(canonical_json(value).encode('utf-8')) > 256 * 1024:
        raise ValueError(f'{field} is too large')
    return value


def _audit(action, resource_type, resource_id, name, actor, detail):
    db.session.add(AuditLog(
        action=action, resource_type=resource_type,
        resource_id=resource_id, resource_name=name,
        operator=actor['name'], ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _claw_role_profile(claw):
    assignment = (
        AgentPostAssignment.query
        .join(AgentPost, AgentPost.id == AgentPostAssignment.post_id)
        .filter(
            AgentPostAssignment.claw_id == claw.id,
            AgentPostAssignment.status == 'active',
            AgentPost.status == 'active')
        .order_by(
            AgentPostAssignment.is_primary.desc(),
            AgentPostAssignment.updated_at.desc(),
            AgentPostAssignment.id.desc())
        .first())
    if assignment and assignment.post:
        return assignment.post.post_key, int(assignment.profile_version or 1)
    return claw.role or '', 1


def _project_scope_query(query, project_column, actor):
    project_id = request.args.get('project_id')
    if project_id not in (None, ''):
        try:
            project_id = _positive_int(project_id, 'project_id')
        except ValueError as exc:
            return None, _error('VALIDATION_FAILED', str(exc), 400)
        if (not db.session.get(Project, project_id)
                or not _can_access_project(actor, project_id)):
            return None, _error('PROJECT_NOT_FOUND', '项目不存在', 404)
        return query.filter(project_column == project_id), None
    if actor['type'] == 'claw':
        return query.filter(project_column == actor['claw'].project_id), None
    role = getattr(actor['user'], 'role', None)
    projects = user_project_ids(actor['user'])
    if role == 'super_admin' or (role == 'admin' and not projects):
        return query, None
    return query.filter(project_column.in_(projects or [-1])), None


def _operation_replay(actor, key, request_hash):
    row = WorkflowOperationIdempotency.query.filter_by(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=key).first()
    if not row:
        return None
    if (row.method != request.method or row.path != request.path
            or row.request_hash != request_hash):
        return _error(
            'IDEMPOTENCY_CONFLICT', 'Idempotency-Key 已用于不同请求', 409)
    return jsonify(row.response_body_json or {}), int(row.response_status or 200)


def _commit_operation(actor, key, request_hash, payload, status=200):
    db.session.add(WorkflowOperationIdempotency(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=key, method=request.method, path=request.path,
        request_hash=request_hash, response_status=status,
        response_body_json=payload))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = _operation_replay(actor, key, request_hash)
        if replay:
            return replay
        return _error('CONCURRENT_EVAL_UPDATE', 'Eval 资源被并发更新', 409)
    return jsonify(payload), status


@api_bp.route('/agent-eval/datasets', methods=['GET'])
def list_agent_eval_datasets():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    query, error = _project_scope_query(
        AgentEvalDataset.query, AgentEvalDataset.project_id, actor)
    if error:
        return error
    filters = {
        'role_key': AgentEvalDataset.role_key,
        'split': AgentEvalDataset.split,
        'status': AgentEvalDataset.status,
        'review_status': AgentEvalDataset.review_status,
    }
    for parameter, column in filters.items():
        value = str(request.args.get(parameter) or '').strip()
        if value:
            query = query.filter(column == value)
    rows = query.order_by(
        AgentEvalDataset.updated_at.desc(), AgentEvalDataset.id.desc()
    ).limit(500).all()
    dataset_ids = [row.id for row in rows]
    case_counts = dict(
        db.session.query(
            AgentEvalCase.dataset_id, db.func.count(AgentEvalCase.id))
        .filter(AgentEvalCase.dataset_id.in_(dataset_ids))
        .group_by(AgentEvalCase.dataset_id).all()
    ) if dataset_ids else {}
    items = []
    for row in rows:
        payload = row.to_dict()
        payload.pop('review_records', None)
        payload['case_count'] = int(case_counts.get(row.id, 0))
        items.append(payload)
    return jsonify({'items': items, 'count': len(items)})


@api_bp.route('/agent-eval/datasets/<int:dataset_id>', methods=['GET'])
def get_agent_eval_dataset(dataset_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    row = db.session.get(AgentEvalDataset, dataset_id)
    if not row or not _can_access_project(actor, row.project_id):
        return _error('DATASET_NOT_FOUND', '数据集不存在', 404)
    can_administer = _can_administer(actor, row.project_id)
    payload = row.to_dict(
        include_cases=True,
        include_hidden=can_administer)
    if not can_administer:
        payload.pop('review_records', None)
    payload['case_count'] = len(row.cases)
    payload['can_review'] = bool(
        actor['type'] == 'user' and can_administer)
    return jsonify(payload)


@api_bp.route('/agent-eval/cases/<int:case_id>', methods=['GET'])
def get_agent_eval_case(case_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    row = db.session.get(AgentEvalCase, case_id)
    if not row or not _can_access_project(actor, row.dataset.project_id):
        return _error('EVAL_CASE_NOT_FOUND', '评测用例不存在', 404)
    payload = row.to_dict(
        include_hidden=_can_administer(actor, row.dataset.project_id))
    payload['dataset'] = {
        'id': row.dataset.id, 'dataset_key': row.dataset.dataset_key,
        'role_key': row.dataset.role_key, 'split': row.dataset.split,
        'version': row.dataset.version,
    }
    return jsonify(payload)


@api_bp.route('/agent-eval/datasets', methods=['POST'])
def create_agent_eval_dataset():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {
        'project_id', 'dataset_key', 'role_key', 'version', 'split',
        'rubric_version', 'review_policy', 'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    try:
        review_policy = _json_object(data, 'review_policy')
        if set(review_policy) - {
                'required_reviewers', 'max_dimension_variance_percent'}:
            raise ValueError('review_policy contains unsupported fields')
        required_reviewers = review_policy.get('required_reviewers', 0)
        if (isinstance(required_reviewers, bool)
                or not isinstance(required_reviewers, int)
                or not 0 <= required_reviewers <= 5):
            raise ValueError('required_reviewers must be between 0 and 5')
        max_variance = review_policy.get('max_dimension_variance_percent', 20)
        if (isinstance(max_variance, bool)
                or not isinstance(max_variance, (int, float))
                or not 0 <= float(max_variance) <= 100):
            raise ValueError(
                'max_dimension_variance_percent must be between 0 and 100')
        normalized = {
            'project_id': _positive_int(data.get('project_id'), 'project_id'),
            'dataset_key': _key(data.get('dataset_key'), 'dataset_key', 100),
            'role_key': _key(data.get('role_key'), 'role_key', 80),
            'version': _positive_int(data.get('version', 1), 'version'),
            'split': str(data.get('split') or '').strip().lower(),
            'rubric_version': _positive_int(
                data.get('rubric_version', 1), 'rubric_version'),
            'review_policy': {
                'required_reviewers': required_reviewers,
                'max_dimension_variance_percent': float(max_variance),
            },
        }
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    if normalized['split'] not in _SPLITS:
        return _error('VALIDATION_FAILED', 'split is invalid', 400)
    project_id = normalized['project_id']
    if not db.session.get(Project, project_id):
        return _error('PROJECT_NOT_FOUND', '项目不存在', 404)
    if not _can_administer(actor, project_id):
        return _error('EVAL_ADMIN_REQUIRED', '只有项目管理员可创建数据集', 403)
    request_hash = sha256_json(normalized, prefix=True)
    replay = AgentEvalDataset.query.filter_by(
        project_id=project_id, created_by_type=actor['type'],
        created_by_id=actor['id'], idempotency_key=key).first()
    if replay:
        if replay.request_sha256 != request_hash:
            return _error(
                'IDEMPOTENCY_CONFLICT',
                'Idempotency-Key 已用于不同数据集请求', 409)
        return jsonify(replay.to_dict()), 200
    previous = (AgentEvalDataset.query.filter_by(
        project_id=project_id, dataset_key=normalized['dataset_key'])
        .order_by(AgentEvalDataset.version.desc()).first())
    if previous:
        if normalized['version'] != int(previous.version) + 1:
            return _error(
                'DATASET_VERSION_CONFLICT',
                f'next dataset version must be {int(previous.version) + 1}', 409)
        if previous.status not in ('frozen', 'retired'):
            return _error(
                'DATASET_PREVIOUS_VERSION_MUTABLE',
                '上一版本必须先冻结或退役', 409)
    elif normalized['version'] != 1:
        return _error(
            'DATASET_VERSION_CONFLICT', 'first dataset version must be 1', 409)
    row = AgentEvalDataset(
        project_id=normalized['project_id'],
        dataset_key=normalized['dataset_key'], role_key=normalized['role_key'],
        version=normalized['version'], split=normalized['split'],
        rubric_version=normalized['rubric_version'],
        status='draft', dataset_sha256='',
        review_policy_json=normalized['review_policy'],
        review_status=(
            'pending' if normalized['review_policy']['required_reviewers']
            else 'not_required'),
        review_records_json=[],
        idempotency_key=key, request_sha256=request_hash,
        created_by_type=actor['type'], created_by_id=actor['id'],
        created_by_name=actor['name'])
    db.session.add(row)
    try:
        db.session.flush()
        _audit('create', 'agent_eval_dataset', row.id,
               f'{row.dataset_key}@{row.version}', actor,
               {'project_id': project_id, 'split': row.split})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _error('DATASET_CONFLICT', '数据集版本或幂等键冲突', 409)
    return jsonify(row.to_dict()), 201


@api_bp.route('/agent-eval/datasets/<int:dataset_id>/cases', methods=['POST'])
def create_agent_eval_case(dataset_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    dataset = db.session.get(AgentEvalDataset, dataset_id)
    if not dataset or not _can_access_project(actor, dataset.project_id):
        return _error('DATASET_NOT_FOUND', '数据集不存在', 404)
    if not _can_administer(actor, dataset.project_id):
        return _error('EVAL_ADMIN_REQUIRED', '只有项目管理员可添加用例', 403)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {
        'case_key', 'input_snapshot', 'expected_contract',
        'required_evidence', 'deterministic_checks', 'allowed_variance',
        'hidden_tags', 'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    try:
        normalized = {
            'case_key': _key(data.get('case_key'), 'case_key'),
            'input_snapshot': _json_object(data, 'input_snapshot'),
            'expected_contract': _json_object(data, 'expected_contract'),
            'required_evidence': _json_list(data, 'required_evidence'),
            'deterministic_checks': _json_list(data, 'deterministic_checks'),
            'allowed_variance': _json_object(data, 'allowed_variance'),
            'hidden_tags': _json_list(data, 'hidden_tags', maximum=100),
        }
    except (TypeError, ValueError) as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    request_hash = sha256_json(normalized, prefix=True)
    replay = AgentEvalCase.query.filter_by(
        dataset_id=dataset.id, created_by_type=actor['type'],
        created_by_id=actor['id'], idempotency_key=key).first()
    if replay:
        if replay.request_sha256 != request_hash:
            return _error('IDEMPOTENCY_CONFLICT', '幂等键已用于不同用例', 409)
        return jsonify(replay.to_dict(include_hidden=True)), 200
    dataset = (AgentEvalDataset.query.filter_by(id=dataset.id)
               .with_for_update().first())
    if dataset.status != 'draft':
        return _error('DATASET_IMMUTABLE', '冻结或退役数据集不可修改', 409)
    row = AgentEvalCase(
        dataset_id=dataset.id, case_key=normalized['case_key'],
        input_snapshot_json=normalized['input_snapshot'],
        expected_contract_json=normalized['expected_contract'],
        required_evidence_json=normalized['required_evidence'],
        deterministic_checks_json=normalized['deterministic_checks'],
        allowed_variance_json=normalized['allowed_variance'],
        hidden_tags_json=normalized['hidden_tags'],
        input_sha256=sha256_json(normalized['input_snapshot'], prefix=True),
        status='active', idempotency_key=key, request_sha256=request_hash,
        created_by_type=actor['type'], created_by_id=actor['id'],
        created_by_name=actor['name'])
    db.session.add(row)
    try:
        db.session.flush()
        _audit('create', 'agent_eval_case', row.id, row.case_key, actor,
               {'dataset_id': dataset.id, 'input_sha256': row.input_sha256})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _error('EVAL_CASE_CONFLICT', '用例键或幂等键冲突', 409)
    return jsonify(row.to_dict(include_hidden=True)), 201


@api_bp.route('/agent-eval/datasets/<int:dataset_id>/freeze', methods=['POST'])
def freeze_agent_eval_dataset(dataset_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    dataset = db.session.get(AgentEvalDataset, dataset_id)
    if not dataset or not _can_access_project(actor, dataset.project_id):
        return _error('DATASET_NOT_FOUND', '数据集不存在', 404)
    if not _can_administer(actor, dataset.project_id):
        return _error('EVAL_ADMIN_REQUIRED', '只有项目管理员可冻结数据集', 403)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    request_hash = sha256_json({'dataset_id': dataset.id, 'action': 'freeze'})
    replay = _operation_replay(actor, key, request_hash)
    if replay:
        return replay
    dataset = (AgentEvalDataset.query.filter_by(id=dataset.id)
               .with_for_update().first())
    if dataset.status != 'draft':
        return _error('DATASET_IMMUTABLE', '数据集已经冻结或退役', 409)
    required_reviewers = int(
        (dataset.review_policy_json or {}).get('required_reviewers') or 0)
    if required_reviewers and dataset.review_status != 'approved':
        return _error(
            'DATASET_REVIEW_INCOMPLETE',
            '数据集尚未完成要求的人工双评', 409,
            {'required_reviewers': required_reviewers,
             'approved_reviewer_count': dataset.to_dict()[
                 'approved_reviewer_count']})
    cases = (AgentEvalCase.query.filter_by(
        dataset_id=dataset.id, status='active')
        .order_by(AgentEvalCase.case_key.asc()).all())
    if not cases:
        return _error('DATASET_EMPTY', '空数据集不能冻结', 409)
    snapshot = [{
        'case_key': row.case_key,
        'input_sha256': row.input_sha256,
        'expected_contract': row.expected_contract_json or {},
        'required_evidence': row.required_evidence_json or [],
        'deterministic_checks': row.deterministic_checks_json or [],
        'allowed_variance': row.allowed_variance_json or {},
        'hidden_tags': row.hidden_tags_json or [],
    } for row in cases]
    dataset.dataset_sha256 = sha256_json({
        'dataset_key': dataset.dataset_key,
        'version': dataset.version,
        'role_key': dataset.role_key,
        'split': dataset.split,
        'rubric_version': dataset.rubric_version,
        'cases': snapshot,
    }, prefix=True)
    dataset.status = 'frozen'
    dataset.frozen_at = _now()
    dataset.approved_by_type = actor['type']
    dataset.approved_by_id = actor['id']
    dataset.approved_by_name = actor['name']
    payload = dataset.to_dict()
    _audit('freeze', 'agent_eval_dataset', dataset.id,
           f'{dataset.dataset_key}@{dataset.version}', actor,
           {'dataset_sha256': dataset.dataset_sha256, 'case_count': len(cases)})
    return _commit_operation(actor, key, request_hash, payload)


@api_bp.route(
    '/agent-eval/datasets/<int:dataset_id>/human-review', methods=['POST'])
def review_agent_eval_dataset(dataset_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    dataset = db.session.get(AgentEvalDataset, dataset_id)
    if not dataset or not _can_access_project(actor, dataset.project_id):
        return _error('DATASET_NOT_FOUND', '数据集不存在', 404)
    if actor['type'] != 'user' or not _can_administer(actor, dataset.project_id):
        return _error('EVAL_HUMAN_ADMIN_REQUIRED', '只有项目人类管理员可评审数据集', 403)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {
        'decision', 'reviewed_case_keys', 'case_scores', 'comment',
        'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    decision = str(data.get('decision') or '').strip().lower()
    if decision not in ('approved', 'revise'):
        return _error('VALIDATION_FAILED', 'decision must be approved or revise', 400)
    case_keys = data.get('reviewed_case_keys')
    if (not isinstance(case_keys, list) or not case_keys
            or any(not isinstance(item, str) or not item.strip()
                   for item in case_keys)):
        return _error(
            'VALIDATION_FAILED', 'reviewed_case_keys must be non-empty strings', 400)
    case_keys = sorted(set(item.strip() for item in case_keys))
    case_scores = data.get('case_scores')
    score_dimensions = {
        'contract_quality', 'evidence_quality', 'difficulty_calibration'}
    if not isinstance(case_scores, dict) or set(case_scores) != set(case_keys):
        return _error(
            'VALIDATION_FAILED', 'case_scores must cover reviewed_case_keys', 400)
    normalized_scores = {}
    for case_key, dimensions in case_scores.items():
        if not isinstance(dimensions, dict) or set(dimensions) != score_dimensions:
            return _error(
                'VALIDATION_FAILED',
                f'case_scores.{case_key} dimensions are invalid', 400)
        normalized_scores[case_key] = {}
        for dimension, value in dimensions.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return _error(
                    'VALIDATION_FAILED', f'{dimension} must be numeric', 400)
            score = float(value)
            if not 0 <= score <= 100:
                return _error(
                    'VALIDATION_FAILED', f'{dimension} must be 0-100', 400)
            normalized_scores[case_key][dimension] = score
    comment = str(data.get('comment') or '').strip()
    if not comment or len(comment) > 10000:
        return _error('VALIDATION_FAILED', 'comment must be 1-10000 characters', 400)
    normalized = {
        'dataset_id': dataset.id, 'decision': decision,
        'reviewed_case_keys': case_keys, 'case_scores': normalized_scores,
        'comment': comment,
    }
    request_hash = sha256_json(normalized)
    replay = _operation_replay(actor, key, request_hash)
    if replay:
        return replay
    dataset = (AgentEvalDataset.query.filter_by(id=dataset.id)
               .with_for_update().first())
    if dataset.status != 'draft':
        return _error('DATASET_IMMUTABLE', '冻结或退役数据集不可评审', 409)
    all_case_keys = sorted(
        row.case_key for row in AgentEvalCase.query.filter_by(
            dataset_id=dataset.id, status='active').all())
    if not all_case_keys:
        return _error('DATASET_EMPTY', '空数据集不能评审', 409)
    if any(item not in set(all_case_keys) for item in case_keys):
        return _error('EVAL_CASE_NOT_FOUND', '评审包含未知 Case', 404)
    if decision == 'approved' and case_keys != all_case_keys:
        return _error(
            'DATASET_REVIEW_INCOMPLETE', '批准必须覆盖数据集全部 Case', 409)
    now = _now()
    records = list(dataset.review_records_json or [])
    records.append({
        'reviewer_key': f'user:{actor["id"]}',
        'reviewer_id': actor['id'], 'reviewer_name': actor['name'],
        'decision': decision, 'reviewed_case_keys': case_keys,
        'case_scores': normalized_scores,
        'comment': comment, 'reviewed_at': str(now),
    })
    dataset.review_records_json = records[-100:]
    latest = {}
    for record in dataset.review_records_json:
        if isinstance(record, dict) and record.get('reviewer_key'):
            latest[record['reviewer_key']] = record
    approved = [
        record for record in latest.values()
        if record.get('decision') == 'approved'
        and sorted(record.get('reviewed_case_keys') or []) == all_case_keys]
    approved.sort(key=lambda record: str(record.get('reviewed_at') or ''))
    required = int(
        (dataset.review_policy_json or {}).get('required_reviewers') or 0)
    review_status = 'pending'
    if len(approved) >= required:
        selected = approved[-required:] if required else []
        threshold = float((dataset.review_policy_json or {}).get(
            'max_dimension_variance_percent', 20))
        review_status = 'approved'
        for case_key in all_case_keys:
            for dimension in score_dimensions:
                values = [
                    float(record['case_scores'][case_key][dimension])
                    for record in selected]
                if values and max(values) - min(values) > threshold:
                    review_status = 'calibration_required'
                    break
            if review_status == 'calibration_required':
                break
    dataset.review_status = review_status
    dataset.review_completed_at = now if dataset.review_status == 'approved' else None
    payload = dataset.to_dict()
    _audit('review', 'agent_eval_dataset', dataset.id,
           f'{dataset.dataset_key}@{dataset.version}', actor,
           {'decision': decision, 'reviewed_case_keys': case_keys,
            'review_status': dataset.review_status})
    return _commit_operation(actor, key, request_hash, payload, status=201)


@api_bp.route('/agent-eval/runs', methods=['GET'])
def list_agent_eval_runs():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    query = AgentEvalRun.query.join(
        AgentEvalCase, AgentEvalCase.id == AgentEvalRun.case_id).join(
            AgentEvalDataset,
            AgentEvalDataset.id == AgentEvalCase.dataset_id)
    query, error = _project_scope_query(
        query, AgentEvalRun.project_id, actor)
    if error:
        return error
    filters = {
        'status': AgentEvalRun.status,
        'claw_id': AgentEvalRun.claw_id,
        'case_id': AgentEvalRun.case_id,
        'dataset_id': AgentEvalCase.dataset_id,
        'role_key': AgentEvalDataset.role_key,
    }
    for parameter, column in filters.items():
        value = request.args.get(parameter)
        if value in (None, ''):
            continue
        if parameter in ('claw_id', 'case_id', 'dataset_id'):
            try:
                value = _positive_int(value, parameter)
            except ValueError as exc:
                return _error('VALIDATION_FAILED', str(exc), 400)
        else:
            value = str(value).strip()
        query = query.filter(column == value)
    rows = query.options(
        joinedload(AgentEvalRun.case), joinedload(AgentEvalRun.claw)
    ).order_by(AgentEvalRun.id.desc()).limit(500).all()
    return jsonify({
        'items': [row.to_dict(include_scores=False) for row in rows],
        'count': len(rows),
    })


@api_bp.route('/agent-eval/runs', methods=['POST'])
def create_agent_eval_run():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {
        'case_id', 'claw_id', 'profile_version', 'worker_release',
        'mission_id', 'workflow_run_id', 'artifact_id', 'status',
        'usage', 'timing', 'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    try:
        case_id = _positive_int(data.get('case_id'), 'case_id')
        claw_id = _positive_int(data.get('claw_id'), 'claw_id')
        profile_version = _positive_int(
            data.get('profile_version'), 'profile_version')
        usage = _json_object(data, 'usage')
        timing = _json_object(data, 'timing')
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    case = db.session.get(AgentEvalCase, case_id)
    if not case or not _can_access_project(actor, case.dataset.project_id):
        return _error('EVAL_CASE_NOT_FOUND', '评测用例不存在', 404)
    project_id = int(case.dataset.project_id)
    claw = db.session.get(OpenClawInstance, claw_id)
    if not claw or int(claw.project_id or 0) != project_id:
        return _error('CLAW_NOT_FOUND', 'Agent 不存在或不属于项目', 404)
    if actor['type'] == 'claw' and int(actor['id']) != claw_id:
        return _error('EVAL_RUN_ACTOR_MISMATCH', 'Agent 只能创建自己的评测运行', 403)
    if actor['type'] == 'user' and not _can_administer(actor, project_id):
        return _error('EVAL_ADMIN_REQUIRED', '只有项目管理员可代建评测运行', 403)
    status = str(data.get('status') or 'pending').strip().lower()
    if status not in _RUN_STATUSES:
        return _error('VALIDATION_FAILED', 'status is invalid', 400)
    worker_release = str(data.get('worker_release') or '').strip()
    if not worker_release or len(worker_release) > 160:
        return _error(
            'VALIDATION_FAILED', 'worker_release must be 1-160 characters', 400)

    def optional_id(field):
        value = data.get(field)
        return (
            _positive_int(value, field) if value not in (None, '') else None)

    try:
        mission_id = optional_id('mission_id')
        workflow_run_id = optional_id('workflow_run_id')
        artifact_id = optional_id('artifact_id')
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    request_contract = {
        'case_id': case.id, 'claw_id': claw.id,
        'profile_version': profile_version,
        'worker_release': worker_release,
        'mission_id': mission_id,
        'workflow_run_id': workflow_run_id,
        'artifact_id': artifact_id,
        'status': status, 'usage': usage, 'timing': timing,
        'dataset_version': case.dataset.version,
        'case_input_sha256': case.input_sha256,
    }
    request_hash = sha256_json(request_contract, prefix=True)
    replay = AgentEvalRun.query.filter_by(
        project_id=project_id, created_by_type=actor['type'],
        created_by_id=actor['id'], idempotency_key=key).first()
    if replay:
        if replay.request_sha256 != request_hash:
            return _error('IDEMPOTENCY_CONFLICT', '幂等键已用于不同评测运行', 409)
        return jsonify(replay.to_dict()), 200

    if case.dataset.status != 'frozen':
        return _error('DATASET_NOT_FROZEN', '只有冻结数据集可以运行评测', 409)
    role_key, bound_profile_version = _claw_role_profile(claw)
    if role_key != case.dataset.role_key:
        return _error(
            'EVAL_ROLE_MISMATCH',
            'Agent 当前岗位与数据集岗位不匹配', 409,
            {'expected_role_key': case.dataset.role_key,
             'actual_role_key': role_key})
    if profile_version != bound_profile_version:
        return _error(
            'EVAL_PROFILE_VERSION_MISMATCH',
            '请求 Profile 版本与 Hub 当前绑定不一致', 409,
            {'expected_profile_version': bound_profile_version})

    def optional_resource(model, field):
        value = data.get(field)
        if value in (None, ''):
            return None
        resource_id = _positive_int(value, field)
        row = db.session.get(model, resource_id)
        row_project = getattr(row, 'project_id', None) if row else None
        if not row or int(row_project or 0) != project_id:
            raise LookupError(field)
        return row

    try:
        mission = optional_resource(WorkflowMission, 'mission_id')
        workflow_run = optional_resource(WorkflowRun, 'workflow_run_id')
        artifact = optional_resource(AgentArtifact, 'artifact_id')
    except (ValueError, LookupError) as exc:
        return _error('EVAL_REFERENCE_NOT_FOUND', f'{exc} 引用无效', 404)
    if status == 'completed' and not artifact:
        return _error(
            'EVAL_ARTIFACT_REQUIRED', 'completed 评测运行必须引用 Artifact', 409)
    if artifact and artifact.status not in ('submitted', 'accepted'):
        return _error(
            'EVAL_ARTIFACT_INVALID', '评测 Artifact 必须已提交且未被拒绝', 409)
    now = _now()
    row = AgentEvalRun(
        project_id=project_id, case_id=case.id,
        dataset_version=case.dataset.version,
        case_input_sha256=case.input_sha256, claw_id=claw.id,
        profile_version=profile_version, provider=claw.llm_provider or '',
        model=claw.llm_model or '', worker_release=worker_release,
        mission_id=mission.id if mission else None,
        workflow_run_id=workflow_run.id if workflow_run else None,
        artifact_id=artifact.id if artifact else None, status=status,
        usage_json=usage, timing_json=timing,
        idempotency_key=key, request_sha256=request_hash,
        created_by_type=actor['type'], created_by_id=actor['id'],
        created_by_name=actor['name'], started_at=now,
        finished_at=now if status in _TERMINAL_RUN_STATUSES else None)
    db.session.add(row)
    try:
        db.session.flush()
        _audit('create', 'agent_eval_run', row.id, case.case_key, actor,
               {'claw_id': claw.id, 'status': status,
                'case_input_sha256': case.input_sha256})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _error('EVAL_RUN_CONFLICT', '评测运行幂等冲突', 409)
    return jsonify(row.to_dict()), 201


@api_bp.route('/agent-eval/runs/<int:run_id>', methods=['GET'])
def get_agent_eval_run(run_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    row = db.session.get(AgentEvalRun, run_id)
    if not row or not _can_access_project(actor, row.project_id):
        return _error('EVAL_RUN_NOT_FOUND', '评测运行不存在', 404)
    return jsonify(row.to_dict())


@api_bp.route('/agent-eval/runs/<int:run_id>/human-score', methods=['POST'])
def create_agent_eval_human_score(run_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    run = db.session.get(AgentEvalRun, run_id)
    if not run or not _can_access_project(actor, run.project_id):
        return _error('EVAL_RUN_NOT_FOUND', '评测运行不存在', 404)
    if not _can_administer(actor, run.project_id):
        return _error('EVAL_ADMIN_REQUIRED', '只有项目管理员可人工评分', 403)
    if run.status not in _TERMINAL_RUN_STATUSES:
        return _error(
            'EVAL_RUN_NOT_TERMINAL', '评测运行终态后才能人工评分', 409)
    data = request.get_json(silent=True) or {}
    unknown = _unknown(data, {
        'scorer_version', 'dimension_scores', 'fatal_violations',
        'review_comment', 'idempotency_key'})
    if unknown:
        return unknown
    key, error = _idempotency_key(data)
    if error:
        return error
    try:
        scorer_version = _positive_int(
            data.get('scorer_version'), 'scorer_version')
        dimensions = _json_object(data, 'dimension_scores')
        fatals = _json_list(data, 'fatal_violations', maximum=20)
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    if not dimensions:
        return _error('VALIDATION_FAILED', 'dimension_scores must not be empty', 400)
    normalized_scores = {}
    for name, value in dimensions.items():
        if name not in _DIMENSION_MAX or isinstance(value, bool):
            return _error('VALIDATION_FAILED', f'invalid score dimension: {name}', 400)
        try:
            score = float(value)
        except (TypeError, ValueError):
            return _error('VALIDATION_FAILED', f'{name} must be numeric', 400)
        if (not math.isfinite(score)
                or score < 0 or score > _DIMENSION_MAX[name]):
            return _error(
                'VALIDATION_FAILED',
                f'{name} must be between 0 and {_DIMENSION_MAX[name]:g}', 400)
        normalized_scores[name] = score
    if (any(not isinstance(code, str) for code in fatals)
            or any(code not in _FATAL_CODES for code in fatals)):
        return _error('VALIDATION_FAILED', 'fatal_violations contains invalid code', 400)
    comment = str(data.get('review_comment') or '').strip()
    if len(comment) > 10000:
        return _error('VALIDATION_FAILED', 'review_comment is too long', 400)
    normalized = {
        'eval_run_id': run.id, 'scorer_version': scorer_version,
        'dimension_scores': normalized_scores,
        'fatal_violations': fatals, 'review_comment': comment,
    }
    request_hash = sha256_json(normalized, prefix=True)
    replay = AgentEvalScore.query.filter_by(
        scorer_actor_type=actor['type'], scorer_actor_id=actor['id'],
        idempotency_key=key).first()
    if replay:
        if replay.request_sha256 != request_hash:
            return _error('IDEMPOTENCY_CONFLICT', '幂等键已用于不同评分', 409)
        return jsonify(replay.to_dict()), 200
    total = 0.0 if fatals else round(sum(normalized_scores.values()), 2)
    row = AgentEvalScore(
        eval_run_id=run.id, scorer_type='human',
        scorer_version=scorer_version,
        scorer_actor_type=actor['type'], scorer_actor_id=actor['id'],
        scorer_name=actor['name'], dimension_scores_json=normalized_scores,
        fatal_violations_json=fatals, total_score=total,
        review_comment=comment, idempotency_key=key,
        request_sha256=request_hash)
    db.session.add(row)
    try:
        db.session.flush()
        _audit('score', 'agent_eval_run', run.id, run.case.case_key, actor,
               {'score_id': row.id, 'scorer_version': scorer_version,
                'total_score': total, 'fatal_violations': fatals})
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return _error(
            'EVAL_SCORE_VERSION_CONFLICT',
            '同一评分人和 scorer_version 已存在', 409)
    return jsonify(row.to_dict()), 201


@api_bp.route('/agent-eval/overview', methods=['GET'])
def get_agent_eval_overview():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    role_key = str(request.args.get('role_key') or '').strip()
    project_id = request.args.get('project_id')
    query = AgentEvalRun.query.join(
        AgentEvalCase, AgentEvalCase.id == AgentEvalRun.case_id).join(
            AgentEvalDataset,
            AgentEvalDataset.id == AgentEvalCase.dataset_id)
    if project_id not in (None, ''):
        try:
            project_id = _positive_int(project_id, 'project_id')
        except ValueError as exc:
            return _error('VALIDATION_FAILED', str(exc), 400)
        if not _can_access_project(actor, project_id):
            return _error('PROJECT_NOT_FOUND', '项目不存在', 404)
        query = query.filter(AgentEvalRun.project_id == project_id)
    elif actor['type'] == 'claw':
        query = query.filter(AgentEvalRun.project_id == actor['claw'].project_id)
    elif getattr(actor['user'], 'role', None) != 'super_admin':
        projects = user_project_ids(actor['user'])
        if getattr(actor['user'], 'role', None) == 'admin' and not projects:
            projects = None
        if projects is not None:
            query = query.filter(AgentEvalRun.project_id.in_(projects or [-1]))
    if role_key:
        query = query.filter(AgentEvalDataset.role_key == role_key)
    runs = query.order_by(AgentEvalRun.id.desc()).limit(5000).all()
    run_ids = [row.id for row in runs]
    historical_scores = (AgentEvalScore.query
                         .filter(AgentEvalScore.eval_run_id.in_(run_ids)).all()
                         if run_ids else [])
    latest_by_scorer = {}
    for score in historical_scores:
        scorer_key = (
            score.eval_run_id, score.scorer_type,
            score.scorer_actor_type, score.scorer_actor_id)
        current = latest_by_scorer.get(scorer_key)
        if (not current
                or int(score.scorer_version) > int(current.scorer_version)
                or (int(score.scorer_version) == int(current.scorer_version)
                    and int(score.id) > int(current.id))):
            latest_by_scorer[scorer_key] = score
    scores = list(latest_by_scorer.values())
    average = (
        round(sum(float(row.total_score or 0) for row in scores) / len(scores), 2)
        if scores else 0.0)
    return jsonify({
        'role_key': role_key,
        'run_count': len(runs),
        'score_count': len(scores),
        'historical_score_count': len(historical_scores),
        'average_score': average,
        'fatal_score_count': sum(
            1 for row in scores if row.fatal_violations_json),
        'status_counts': {
            status: sum(1 for row in runs if row.status == status)
            for status in sorted({row.status for row in runs})
        },
    })
