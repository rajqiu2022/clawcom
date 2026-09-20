"""Versioned Mission Handoff contracts and downstream acceptance gates."""

import json
import re

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.agent_artifacts import _producer_metadata
from app.api.mission_stages import (
    _actor,
    _can_access_project,
    _can_administer,
    _disabled_response,
)
from app.models import (
    AgentArtifact,
    AgentPost,
    AgentPostAssignment,
    AuditLog,
    MissionHandoff,
    MissionStage,
    WorkflowMission,
    WorkflowOperationIdempotency,
    _now,
)
from app.services.agent_artifacts import canonical_json, sha256_json


_CREATE_FIELDS = frozenset({
    'handoff_version', 'source_stage_id', 'target_stage_id', 'objective',
    'input_artifact_refs', 'accepted_findings', 'open_questions',
    'constraints', 'acceptance_criteria', 'known_pitfalls',
    'recommended_next_actions', 'evidence_refs', 'source_run_id',
    'source_step_id', 'idempotency_key',
})
_SERVER_FIELDS = frozenset({
    'id', 'handoff_id', 'mission_id', 'mission_version', 'from_role',
    'to_role', 'producer', 'status', 'content_sha256', 'request_sha256',
    'version', 'decision', 'created_at', 'updated_at', 'accepted_at',
    'rejected_at', 'submitted_at', 'superseded_at',
})
_SUBMIT_FIELDS = frozenset({'expected_version', 'idempotency_key'})
_ACCEPT_FIELDS = frozenset({
    'expected_version', 'source_stage_expected_version',
    'source_stage_fencing_token', 'target_stage_expected_version',
    'idempotency_key',
})
_REJECT_FIELDS = frozenset({
    'expected_version', 'reason_code', 'comment', 'idempotency_key',
})
_REASON_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,79}$')
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$')


def _error(code, message, status=400, details=None):
    body = {'error': message, 'code': code}
    if details:
        body['details'] = details
    return jsonify(body), status


def _positive_int(value, field, minimum=1):
    if isinstance(value, bool):
        raise ValueError(f'{field} must be an integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{field} must be an integer') from exc
    if result < minimum:
        raise ValueError(f'{field} must be at least {minimum}')
    return result


def _key(value, field):
    result = str(value or '').strip()
    if not _KEY_RE.fullmatch(result):
        raise ValueError(f'{field} must be a 1-80 character stable key')
    return result


def _string_list(data, field, maximum=100):
    value = data.get(field, [])
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, str) for item in value)):
        raise ValueError(f'{field} must be an array of at most {maximum} strings')
    result = [item.strip() for item in value]
    if any(len(item) > 4000 for item in result):
        raise ValueError(f'{field} items must not exceed 4000 characters')
    return result


def _object_list(data, field, maximum=100):
    value = data.get(field, [])
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(item, dict) for item in value)):
        raise ValueError(f'{field} must be an array of at most {maximum} objects')
    if len(canonical_json(value).encode('utf-8')) > 128 * 1024:
        raise ValueError(f'{field} is too large')
    return value


def _normalize_create(data):
    if not isinstance(data, dict):
        raise ValueError('JSON object body is required')
    server_fields = sorted(set(data) & _SERVER_FIELDS)
    if server_fields:
        raise ValueError(
            'server-owned fields are read-only: ' + ', '.join(server_fields))
    unknown = sorted(set(data) - _CREATE_FIELDS)
    if unknown:
        raise ValueError('unknown fields: ' + ', '.join(unknown))
    objective = str(data.get('objective') or '').strip()
    if not objective or len(objective) > 5000:
        raise ValueError('objective must be 1-5000 characters')
    refs = _object_list(data, 'input_artifact_refs', maximum=50)
    if not refs:
        raise ValueError('input_artifact_refs must not be empty')
    normalized_refs = []
    for ref in refs:
        unknown_ref_fields = sorted(
            set(ref) - {'artifact_id', 'content_sha256'})
        if unknown_ref_fields:
            raise ValueError(
                'unknown input_artifact_refs fields: '
                + ', '.join(unknown_ref_fields))
        normalized_ref = {
            'artifact_id': _positive_int(
                ref.get('artifact_id'), 'artifact_id'),
        }
        expected_hash = str(ref.get('content_sha256') or '').strip()
        if expected_hash:
            normalized_ref['content_sha256'] = expected_hash
        normalized_refs.append(normalized_ref)
    return {
        'handoff_version': _positive_int(
            data.get('handoff_version', 1), 'handoff_version'),
        'source_stage_id': _key(data.get('source_stage_id'), 'source_stage_id'),
        'target_stage_id': _key(data.get('target_stage_id'), 'target_stage_id'),
        'objective': objective,
        'input_artifact_refs': normalized_refs,
        'accepted_findings': _string_list(data, 'accepted_findings'),
        'open_questions': _string_list(data, 'open_questions'),
        'constraints': _string_list(data, 'constraints'),
        'acceptance_criteria': _string_list(data, 'acceptance_criteria'),
        'known_pitfalls': _string_list(data, 'known_pitfalls'),
        'recommended_next_actions': _string_list(
            data, 'recommended_next_actions'),
        'evidence_refs': _object_list(data, 'evidence_refs'),
        'source_run_id': (
            _positive_int(data.get('source_run_id'), 'source_run_id')
            if data.get('source_run_id') not in (None, '') else None),
        'source_step_id': str(data.get('source_step_id') or '').strip()[:100],
    }


def _unknown_field_error(data, allowed):
    unknown = sorted(set(data or {}) - allowed)
    if not unknown:
        return None
    return _error(
        'UNKNOWN_FIELD', f"unknown fields: {', '.join(unknown)}", 400)


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


def _resolve_artifact_refs(mission, source_stage, refs):
    resolved = []
    seen = set()
    for ref in refs:
        try:
            artifact_id = _positive_int(ref.get('artifact_id'), 'artifact_id')
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        artifact = db.session.get(AgentArtifact, artifact_id)
        if (not artifact
                or int(artifact.project_id) != int(mission.project_id)
                or int(artifact.mission_id) != int(mission.id)
                or artifact.stage_id != source_stage.stage_key):
            raise LookupError(f'artifact {artifact_id} not found in source Stage')
        expected_hash = str(ref.get('content_sha256') or '').strip()
        if expected_hash and expected_hash != artifact.content_sha256:
            raise RuntimeError(f'artifact {artifact_id} hash mismatch')
        resolved.append({
            'schema': 1,
            'artifact_id': artifact.id,
            'artifact_type': artifact.artifact_type,
            'artifact_version': artifact.artifact_version,
            'content_sha256': artifact.content_sha256,
            'mission_id': artifact.mission_id,
            'stage_id': artifact.stage_id,
            'status': artifact.status,
        })
    return resolved


def _handoff_content(mission, source_stage, target_stage, normalized, refs):
    return {
        'schema': 1,
        'handoff_version': normalized['handoff_version'],
        'mission_id': mission.id,
        'mission_version': int(mission.version or 1),
        'source_stage_id': source_stage.stage_key,
        'target_stage_id': target_stage.stage_key,
        'from_role': source_stage.role_key,
        'to_role': target_stage.role_key,
        'objective': normalized['objective'],
        'input_artifact_refs': refs,
        'accepted_findings': normalized['accepted_findings'],
        'open_questions': normalized['open_questions'],
        'constraints': normalized['constraints'],
        'acceptance_criteria': normalized['acceptance_criteria'],
        'known_pitfalls': normalized['known_pitfalls'],
        'recommended_next_actions': normalized['recommended_next_actions'],
        'evidence_refs': normalized['evidence_refs'],
        'source_run_id': normalized['source_run_id'],
        'source_step_id': normalized['source_step_id'],
    }


def _create_request_hash(mission, source_stage, target_stage, normalized):
    """Hash caller intent without mutable server state such as Artifact status."""
    return sha256_json({
        'mission_id': mission.id,
        'mission_version': int(mission.version or 1),
        'source_stage_id': source_stage.stage_key,
        'target_stage_id': target_stage.stage_key,
        'from_role': source_stage.role_key,
        'to_role': target_stage.role_key,
        **normalized,
    }, prefix=True)


def _can_source(actor, stage, project_id):
    if _can_administer(actor, project_id):
        return True
    return bool(
        actor['type'] == 'claw' and stage.assigned_claw_id
        and int(stage.assigned_claw_id) == int(actor['id']))


def _actor_role_key(actor):
    if actor['type'] != 'claw':
        return ''
    claw = actor['claw']
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
    return (
        assignment.post.post_key if assignment and assignment.post
        else claw.role or '')


def _can_target(actor, handoff, project_id):
    if handoff.mission.control_mode == 'team_managed':
        # Team roles never fall back to global Claw.role / AgentPost.
        stage = db.session.get(MissionStage, handoff.target_stage_record_id)
        return bool(actor['type'] == 'claw' and stage
                    and stage.assigned_claw_id == actor['id']
                    and stage.role_key == handoff.to_role)
    if _can_administer(actor, project_id):
        return True
    return bool(
        actor['type'] == 'claw'
        and _actor_role_key(actor) == handoff.to_role)


def _handoff_for_actor(actor, mission_id, handoff_id, lock=False):
    query = MissionHandoff.query.filter_by(
        id=handoff_id, mission_id=mission_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if not row or not _can_access_project(actor, row.mission.project_id):
        return None
    return row


def _operation_hash(data):
    return sha256_json({
        key: value for key, value in (data or {}).items()
        if key != 'idempotency_key'
    })


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


def _save_operation(actor, key, request_hash, payload):
    db.session.add(WorkflowOperationIdempotency(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=key, method=request.method, path=request.path,
        request_hash=request_hash, response_status=200,
        response_body_json=payload))


def _audit(action, handoff, actor, detail):
    db.session.add(AuditLog(
        action=action, resource_type='mission_handoff',
        resource_id=handoff.id,
        resource_name=(
            f'{handoff.source_stage_key}->{handoff.target_stage_key}'
            f'@{handoff.handoff_version}'),
        operator=actor['name'], ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _commit_operation(actor, key, request_hash, payload):
    _save_operation(actor, key, request_hash, payload)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = _operation_replay(actor, key, request_hash)
        if replay:
            return replay
        return _error('CONCURRENT_HANDOFF_UPDATE', 'Handoff 被并发更新', 409)
    return jsonify(payload)


@api_bp.route('/missions/<int:mission_id>/handoffs', methods=['POST'])
def create_mission_handoff(mission_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True)
    key, error = _idempotency_key(data)
    if error:
        return error
    try:
        normalized = _normalize_create(data)
    except (TypeError, ValueError) as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission or not _can_access_project(actor, mission.project_id):
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    source = MissionStage.query.filter_by(
        mission_id=mission.id,
        stage_key=normalized['source_stage_id']).order_by(
            MissionStage.stage_version.desc()).first()
    target = MissionStage.query.filter_by(
        mission_id=mission.id,
        stage_key=normalized['target_stage_id']).order_by(
            MissionStage.stage_version.desc()).first()
    if not source or not target or source.id == target.id:
        return _error('STAGE_NOT_FOUND', '源或目标 Stage 不存在', 404)
    if not _can_source(actor, source, mission.project_id):
        return _error('HANDOFF_CREATE_DENIED', '只有源 Stage 执行者可以交接', 403)
    request_hash = _create_request_hash(
        mission, source, target, normalized)
    replay = MissionHandoff.query.filter_by(
        mission_id=mission.id, producer_type=actor['type'],
        producer_id=actor['id'], idempotency_key=key).first()
    if replay:
        if replay.request_sha256 != request_hash:
            return _error(
                'IDEMPOTENCY_CONFLICT',
                'Idempotency-Key 已用于不同 Handoff', 409)
        return jsonify(replay.to_dict()), 200

    try:
        refs = _resolve_artifact_refs(
            mission, source, normalized['input_artifact_refs'])
    except LookupError as exc:
        return _error('ARTIFACT_NOT_FOUND', str(exc), 404)
    except RuntimeError as exc:
        return _error('ARTIFACT_HASH_MISMATCH', str(exc), 409)
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    content = _handoff_content(mission, source, target, normalized, refs)

    previous = (MissionHandoff.query.filter_by(
        mission_id=mission.id, source_stage_key=source.stage_key,
        target_stage_key=target.stage_key)
        .order_by(MissionHandoff.handoff_version.desc()).first())
    if previous:
        expected = int(previous.handoff_version) + 1
        if normalized['handoff_version'] != expected:
            return _error(
                'HANDOFF_VERSION_CONFLICT',
                f'next handoff_version must be {expected}', 409)
        if previous.status not in ('draft', 'rejected', 'superseded'):
            return _error(
                'HANDOFF_ACTIVE_VERSION_EXISTS',
                '当前 Handoff 尚未退回或结束，不能创建新版本', 409)
    elif normalized['handoff_version'] != 1:
        return _error(
            'HANDOFF_VERSION_CONFLICT', 'first handoff_version must be 1', 409)

    producer = _producer_metadata(actor)
    handoff = MissionHandoff(
        mission_id=mission.id, mission_version=int(mission.version or 1),
        handoff_version=normalized['handoff_version'],
        source_stage_record_id=source.id, target_stage_record_id=target.id,
        source_stage_key=source.stage_key, target_stage_key=target.stage_key,
        from_role=source.role_key, to_role=target.role_key,
        objective=normalized['objective'],
        input_artifact_refs_json=refs,
        accepted_findings_json=normalized['accepted_findings'],
        open_questions_json=normalized['open_questions'],
        constraints_json=normalized['constraints'],
        acceptance_criteria_json=normalized['acceptance_criteria'],
        known_pitfalls_json=normalized['known_pitfalls'],
        recommended_next_actions_json=normalized['recommended_next_actions'],
        evidence_refs_json=normalized['evidence_refs'],
        source_run_id=normalized['source_run_id'],
        source_step_id=normalized['source_step_id'],
        producer_type=actor['type'], producer_id=actor['id'],
        producer_name=actor['name'], status='draft',
        content_sha256=sha256_json(content, prefix=True),
        idempotency_key=key, request_sha256=request_hash,
        version=1, **producer)
    if previous and previous.status != 'superseded':
        previous.status = 'superseded'
        previous.superseded_at = _now()
        previous.version = int(previous.version or 1) + 1
    db.session.add(handoff)
    try:
        db.session.flush()
        _audit('create', handoff, actor, {
            'content_sha256': handoff.content_sha256,
            'artifact_ids': [item['artifact_id'] for item in refs],
        })
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = MissionHandoff.query.filter_by(
            mission_id=mission.id, producer_type=actor['type'],
            producer_id=actor['id'], idempotency_key=key).first()
        if replay and replay.request_sha256 == request_hash:
            return jsonify(replay.to_dict()), 200
        return _error('HANDOFF_CONFLICT', 'Handoff 版本或幂等键冲突', 409)
    return jsonify(handoff.to_dict()), 201


@api_bp.route('/missions/<int:mission_id>/handoffs', methods=['GET'])
def list_mission_handoffs(mission_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission or not _can_access_project(actor, mission.project_id):
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    rows = (MissionHandoff.query.filter_by(mission_id=mission.id)
            .order_by(MissionHandoff.id.desc()).limit(500).all())
    return jsonify({'mission_id': mission.id,
                    'items': [row.to_dict() for row in rows],
                    'count': len(rows)})


@api_bp.route(
    '/missions/<int:mission_id>/handoffs/<int:handoff_id>', methods=['GET'])
def get_mission_handoff(mission_id, handoff_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    handoff = _handoff_for_actor(actor, mission_id, handoff_id)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    return jsonify(handoff.to_dict())


def _begin_operation(actor, data):
    key, error = _idempotency_key(data)
    if error:
        return None, None, error
    request_hash = _operation_hash(data)
    replay = _operation_replay(actor, key, request_hash)
    return key, request_hash, replay


@api_bp.route(
    '/missions/<int:mission_id>/handoffs/<int:handoff_id>/submit',
    methods=['POST'])
def submit_mission_handoff(mission_id, handoff_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown_error = _unknown_field_error(data, _SUBMIT_FIELDS)
    if unknown_error:
        return unknown_error
    handoff = _handoff_for_actor(actor, mission_id, handoff_id)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    key, request_hash, replay = _begin_operation(actor, data)
    if replay:
        return replay
    try:
        expected_version = _positive_int(
            data.get('expected_version'), 'expected_version')
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    handoff = _handoff_for_actor(actor, mission_id, handoff_id, lock=True)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    if not _can_source(actor, handoff.source_stage, handoff.mission.project_id):
        return _error('HANDOFF_SUBMIT_DENIED', '只有源 Stage 执行者可以提交', 403)
    if int(handoff.version or 1) != expected_version:
        return _error('HANDOFF_VERSION_CONFLICT', 'Handoff 版本已变化', 409)
    if handoff.status != 'draft':
        return _error('HANDOFF_IMMUTABLE', '只有 draft Handoff 可以提交', 409)
    artifact_ids = [
        item['artifact_id'] for item in handoff.input_artifact_refs_json or []]
    artifacts = AgentArtifact.query.filter(AgentArtifact.id.in_(artifact_ids)).all()
    if (len(artifacts) != len(artifact_ids)
            or any(row.status not in ('submitted', 'accepted') for row in artifacts)):
        return _error(
            'HANDOFF_ARTIFACT_NOT_SUBMITTED',
            'Handoff 引用的 Artifact 必须已提交且未被拒绝', 409)
    handoff.status = 'submitted'
    handoff.submitted_at = _now()
    handoff.version = int(handoff.version or 1) + 1
    payload = handoff.to_dict()
    _audit('submit', handoff, actor, {'version': handoff.version})
    return _commit_operation(actor, key, request_hash, payload)


@api_bp.route(
    '/missions/<int:mission_id>/handoffs/<int:handoff_id>/accept',
    methods=['POST'])
def accept_mission_handoff(mission_id, handoff_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown_error = _unknown_field_error(data, _ACCEPT_FIELDS)
    if unknown_error:
        return unknown_error
    handoff = _handoff_for_actor(actor, mission_id, handoff_id)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    key, request_hash, replay = _begin_operation(actor, data)
    if replay:
        return replay
    try:
        expected_version = _positive_int(
            data.get('expected_version'), 'expected_version')
        source_version = _positive_int(
            data.get('source_stage_expected_version'),
            'source_stage_expected_version')
        source_fence = _positive_int(
            data.get('source_stage_fencing_token'),
            'source_stage_fencing_token', minimum=0)
        target_version = _positive_int(
            data.get('target_stage_expected_version'),
            'target_stage_expected_version')
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    handoff = _handoff_for_actor(actor, mission_id, handoff_id, lock=True)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    if not _can_target(actor, handoff, handoff.mission.project_id):
        return _error('HANDOFF_ACCEPT_DENIED', '只有目标岗位可以接受交接', 403)
    if int(handoff.version or 1) != expected_version:
        return _error('HANDOFF_VERSION_CONFLICT', 'Handoff 版本已变化', 409)
    if handoff.status != 'submitted':
        return _error('HANDOFF_NOT_SUBMITTED', '只有 submitted Handoff 可接受', 409)
    source = (MissionStage.query.filter_by(id=handoff.source_stage_record_id)
              .with_for_update().first())
    target = (MissionStage.query.filter_by(id=handoff.target_stage_record_id)
              .with_for_update().first())
    if int(source.version or 1) != source_version:
        return _error('SOURCE_STAGE_VERSION_CONFLICT', '源 Stage 版本已变化', 409)
    if int(source.fencing_token or 0) != source_fence:
        return _error('FENCING_TOKEN_STALE', '源 Stage fencing token 已失效', 409)
    if int(target.version or 1) != target_version:
        return _error('TARGET_STAGE_VERSION_CONFLICT', '目标 Stage 版本已变化', 409)
    if source.state != 'accepted':
        return _error('SOURCE_STAGE_NOT_ACCEPTED', '源 Stage 尚未 accepted', 409)
    if target.state not in ('ready', 'waiting_input') or target.assigned_claw_id:
        return _error('TARGET_STAGE_ALREADY_STARTED', '目标 Stage 已开始执行', 409)
    artifact_ids = [
        item['artifact_id'] for item in handoff.input_artifact_refs_json or []]
    artifacts = AgentArtifact.query.filter(AgentArtifact.id.in_(artifact_ids)).all()
    if (len(artifacts) != len(artifact_ids)
            or any(row.status != 'accepted' for row in artifacts)):
        return _error(
            'HANDOFF_ARTIFACT_NOT_ACCEPTED',
            '接受 Handoff 前所有 Artifact 必须已通过 Review', 409)

    now = _now()
    handoff.status = 'accepted'
    handoff.accepted_at = now
    handoff.decision_reason_code = 'HANDOFF_ACCEPTED'
    handoff.decided_by_type = actor['type']
    handoff.decided_by_id = actor['id']
    handoff.decided_by_name = actor['name']
    handoff.version = int(handoff.version or 1) + 1
    source.state = 'completed'
    source.last_reason_code = 'HANDOFF_ACCEPTED'
    source.version = int(source.version or 1) + 1
    source.updated_at = now
    target.active_handoff_id = handoff.id
    target.last_reason_code = 'HANDOFF_ACCEPTED'
    target.version = int(target.version or 1) + 1
    target.updated_at = now
    payload = handoff.to_dict()
    _audit('accept', handoff, actor, {
        'source_stage_version': source.version,
        'target_stage_version': target.version,
        'artifact_ids': artifact_ids,
    })
    return _commit_operation(actor, key, request_hash, payload)


@api_bp.route(
    '/missions/<int:mission_id>/handoffs/<int:handoff_id>/reject',
    methods=['POST'])
def reject_mission_handoff(mission_id, handoff_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown_error = _unknown_field_error(data, _REJECT_FIELDS)
    if unknown_error:
        return unknown_error
    handoff = _handoff_for_actor(actor, mission_id, handoff_id)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    key, request_hash, replay = _begin_operation(actor, data)
    if replay:
        return replay
    try:
        expected_version = _positive_int(
            data.get('expected_version'), 'expected_version')
    except ValueError as exc:
        return _error('VALIDATION_FAILED', str(exc), 400)
    reason = str(data.get('reason_code') or '').strip().upper()
    comment = str(data.get('comment') or '').strip()
    if not _REASON_RE.fullmatch(reason):
        return _error(
            'VALIDATION_FAILED',
            'reason_code must be an uppercase stable code', 400)
    if not comment or len(comment) > 10000:
        return _error(
            'VALIDATION_FAILED', 'comment must be 1-10000 characters', 400)
    handoff = _handoff_for_actor(actor, mission_id, handoff_id, lock=True)
    if not handoff:
        return _error('HANDOFF_NOT_FOUND', 'Handoff 不存在', 404)
    if not _can_target(actor, handoff, handoff.mission.project_id):
        return _error('HANDOFF_REJECT_DENIED', '只有目标岗位可以退回交接', 403)
    if int(handoff.version or 1) != expected_version:
        return _error('HANDOFF_VERSION_CONFLICT', 'Handoff 版本已变化', 409)
    if handoff.status != 'submitted':
        return _error('HANDOFF_NOT_SUBMITTED', '只有 submitted Handoff 可退回', 409)
    handoff.status = 'rejected'
    handoff.rejected_at = _now()
    handoff.decision_reason_code = reason
    handoff.decision_comment = comment
    handoff.decided_by_type = actor['type']
    handoff.decided_by_id = actor['id']
    handoff.decided_by_name = actor['name']
    handoff.version = int(handoff.version or 1) + 1
    payload = handoff.to_dict()
    _audit('reject', handoff, actor, {
        'reason_code': reason, 'version': handoff.version})
    return _commit_operation(actor, key, request_hash, payload)
