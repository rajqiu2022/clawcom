"""Mission Stage state machine with optimistic locking and fencing."""

import json
import re

from flask import current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    user_project_ids,
)
from app.models import (
    AgentArtifact,
    AuditLog,
    MissionHandoff,
    MissionStage,
    WorkflowMission,
    WorkflowOperationIdempotency,
    _now,
)
from app.services.agent_artifacts import canonical_json, sha256_json


STAGE_STATES = frozenset({
    'ready', 'running', 'waiting_input', 'waiting_permission',
    'waiting_human_review', 'submitted', 'accepted', 'rejected',
    'completed', 'blocked', 'failed', 'cancelled', 'superseded',
})
_TRANSITIONS = {
    'ready': frozenset({'running', 'cancelled', 'superseded'}),
    'running': frozenset({
        'waiting_input', 'waiting_permission', 'waiting_human_review',
        'submitted', 'blocked', 'failed', 'cancelled', 'superseded',
    }),
    'waiting_input': frozenset({
        'running', 'blocked', 'failed', 'cancelled', 'superseded'}),
    'waiting_permission': frozenset({
        'running', 'blocked', 'failed', 'cancelled', 'superseded'}),
    'waiting_human_review': frozenset({
        'running', 'submitted', 'blocked', 'failed', 'cancelled',
        'superseded'}),
    'submitted': frozenset({'accepted', 'rejected', 'cancelled', 'superseded'}),
    'rejected': frozenset({'running', 'cancelled', 'superseded'}),
    'accepted': frozenset({'completed', 'superseded'}),
    'blocked': frozenset({'running', 'cancelled', 'superseded'}),
    'failed': frozenset({'running', 'cancelled', 'superseded'}),
    'completed': frozenset(),
    'cancelled': frozenset(),
    'superseded': frozenset(),
}
_REASON_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,79}$')
_CLAIM_FIELDS = frozenset({'expected_version', 'idempotency_key'})
_TRANSITION_FIELDS = frozenset({
    'expected_version', 'fencing_token', 'to_state', 'reason_code',
    'artifact_id', 'evidence_refs', 'idempotency_key',
})


def _error(code, message, status=400, details=None):
    body = {'error': message, 'code': code}
    if details:
        body['details'] = details
    return jsonify(body), status


def _disabled_response():
    value = current_app.config.get('AGENT_TEAM_CONTRACTS_ENABLED', False)
    if isinstance(value, str):
        value = value.lower() in ('1', 'true', 'yes', 'on')
    if value:
        return None
    return _error(
        'AGENT_TEAM_CONTRACTS_DISABLED', 'Agent 团队合同能力尚未开启', 404)


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}',
            'claw': claw, 'user': None,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': (getattr(user, 'display_name', None)
                     or getattr(user, 'username', None)
                     or f'user:{user.id}'),
            'claw': None, 'user': user,
        }
    return None


def _can_access_project(actor, project_id):
    if not actor:
        return False
    project_id = int(project_id)
    if actor['type'] == 'claw':
        claw = actor['claw']
        if claw.role == 'admin' and claw.project_id is None:
            return True
        return bool(
            claw.project_id and int(claw.project_id) == project_id)
    user = actor['user']
    role = getattr(user, 'role', None)
    if role == 'super_admin':
        return True
    projects = user_project_ids(user)
    if role == 'admin' and not projects:
        return True
    return project_id in projects


def _can_administer(actor, project_id):
    if not _can_access_project(actor, project_id):
        return False
    if actor['type'] == 'claw':
        return actor['claw'].role == 'admin'
    return getattr(actor['user'], 'role', None) in ('super_admin', 'admin')


def _stage_query(mission_id, stage_key, lock=False):
    query = MissionStage.query.filter_by(
        mission_id=mission_id, stage_key=stage_key)
    requested_version = request.args.get('stage_version')
    if requested_version not in (None, ''):
        try:
            requested_version = int(requested_version)
        except (TypeError, ValueError):
            return None
        query = query.filter(MissionStage.stage_version == requested_version)
    query = query.order_by(
        MissionStage.stage_version.desc(), MissionStage.id.desc())
    if lock:
        query = query.with_for_update()
    return query.first()


def _mission_and_stage(actor, mission_id, stage_key, lock=False):
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission or not _can_access_project(actor, mission.project_id):
        return None, None
    stage = _stage_query(mission_id, stage_key, lock=lock)
    if not stage:
        return mission, None
    return mission, stage


def _positive_int(data, field):
    value = (data or {}).get(field)
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


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


def _request_hash(data):
    return sha256_json({
        key: value for key, value in (data or {}).items()
        if key != 'idempotency_key'
    })


def _reject_unknown_fields(data, allowed):
    unknown = sorted(set(data or {}) - allowed)
    if not unknown:
        return None
    return _error(
        'UNKNOWN_FIELD', f"unknown fields: {', '.join(unknown)}", 400)


def _replay(actor, idempotency_key, request_hash):
    row = WorkflowOperationIdempotency.query.filter_by(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=idempotency_key).first()
    if not row:
        return None
    if (row.method != request.method
            or row.path != request.path
            or row.request_hash != request_hash):
        return _error(
            'IDEMPOTENCY_CONFLICT',
            'Idempotency-Key 已用于不同请求', 409)
    return jsonify(row.response_body_json or {}), int(row.response_status or 200)


def _save_receipt(actor, idempotency_key, request_hash, payload, status=200):
    db.session.add(WorkflowOperationIdempotency(
        actor_type=actor['type'], actor_id=actor['id'],
        idempotency_key=idempotency_key,
        method=request.method, path=request.path,
        request_hash=request_hash,
        response_status=status,
        response_body_json=payload,
    ))


def _audit(action, mission, stage, actor, detail):
    db.session.add(AuditLog(
        action=action,
        resource_type='mission_stage',
        resource_id=stage.id,
        resource_name=f'{mission.mission_key}:{stage.stage_key}',
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    ))


def _commit_mutation(actor, key, request_hash, payload):
    _save_receipt(actor, key, request_hash, payload)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = _replay(actor, key, request_hash)
        if replay:
            return replay
        return _error(
            'CONCURRENT_STAGE_UPDATE', 'Stage 被并发更新，请刷新后重试', 409)
    return jsonify(payload)


@api_bp.route('/missions/<int:mission_id>/stages', methods=['GET'])
def list_mission_stages(mission_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission or not _can_access_project(actor, mission.project_id):
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)
    rows = (MissionStage.query
            .filter_by(mission_id=mission.id)
            .order_by(
                MissionStage.stage_version.asc(), MissionStage.id.asc())
            .all())
    return jsonify({
        'mission_id': mission.id,
        'project_id': mission.project_id,
        'items': [row.to_dict() for row in rows],
        'count': len(rows),
    })


@api_bp.route(
    '/missions/<int:mission_id>/stages/<string:stage_key>/context',
    methods=['GET'])
def get_mission_stage_context(mission_id, stage_key):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission, stage = _mission_and_stage(actor, mission_id, stage_key)
    if not mission or not stage:
        return _error('STAGE_NOT_FOUND', 'Mission Stage 不存在', 404)
    return jsonify({
        'mission': {
            'id': mission.id,
            'mission_key': mission.mission_key,
            'project_id': mission.project_id,
            'objective': mission.objective,
            'status': mission.effective_status(),
        },
        'stage': stage.to_dict(),
        'input_snapshot': stage.input_snapshot_json or {},
    })


@api_bp.route(
    '/missions/<int:mission_id>/stages/<string:stage_key>/claim',
    methods=['POST'])
def claim_mission_stage(mission_id, stage_key):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    if actor['type'] != 'claw':
        return _error('CLAW_REQUIRED', 'Stage 只能由 Agent 领取', 403)
    data = request.get_json(silent=True) or {}
    unknown_error = _reject_unknown_fields(data, _CLAIM_FIELDS)
    if unknown_error:
        return unknown_error
    key, error = _idempotency_key(data)
    if error:
        return error
    request_hash = _request_hash(data)
    mission, stage = _mission_and_stage(actor, mission_id, stage_key)
    if not mission or not stage:
        return _error('STAGE_NOT_FOUND', 'Mission Stage 不存在', 404)
    replay = _replay(actor, key, request_hash)
    if replay:
        return replay

    expected_version = _positive_int(data, 'expected_version')
    if expected_version is None:
        return _error(
            'VALIDATION_FAILED', 'expected_version must be a non-negative integer',
            400)
    mission, stage = _mission_and_stage(
        actor, mission_id, stage_key, lock=True)
    if not mission or not stage:
        return _error('STAGE_NOT_FOUND', 'Mission Stage 不存在', 404)
    if mission.effective_status() != 'active':
        return _error('MISSION_NOT_ACTIVE', 'Mission 已结束或过期', 409)
    input_snapshot = (
        stage.input_snapshot_json
        if isinstance(stage.input_snapshot_json, dict) else {})
    if input_snapshot.get('requires_handoff'):
        handoff = (
            db.session.get(MissionHandoff, stage.active_handoff_id)
            if stage.active_handoff_id else None)
        if not handoff or handoff.status != 'accepted':
            return _error(
                'HANDOFF_NOT_ACCEPTED',
                '目标 Stage 必须先接受有效 Handoff', 409)
    if int(stage.version or 1) != expected_version:
        return _error(
            'STAGE_VERSION_CONFLICT', 'Stage 版本已变化', 409,
            {'expected_version': expected_version,
             'actual_version': int(stage.version or 1)})
    if stage.state not in (
            'ready', 'running', 'waiting_input', 'waiting_permission',
            'waiting_human_review', 'rejected', 'blocked', 'failed'):
        return _error(
            'STAGE_NOT_CLAIMABLE', '当前状态不可领取', 409,
            {'state': stage.state})
    if (stage.assigned_claw_id
            and int(stage.assigned_claw_id) != int(actor['id'])):
        return _error(
            'STAGE_ALREADY_ASSIGNED', 'Stage 已由其他 Agent 领取', 409,
            {'assigned_claw_id': stage.assigned_claw_id})

    previous_state = stage.state
    stage.assigned_claw_id = actor['id']
    stage.state = 'running'
    stage.fencing_token = int(stage.fencing_token or 0) + 1
    stage.version = int(stage.version or 1) + 1
    stage.last_reason_code = 'STAGE_CLAIMED'
    if previous_state in ('blocked', 'failed'):
        stage.retry_count = int(stage.retry_count or 0) + 1
    if previous_state == 'rejected':
        stage.repair_count = int(stage.repair_count or 0) + 1
    stage.updated_at = _now()
    payload = stage.to_dict()
    _audit('claim', mission, stage, actor, {
        'from_state': previous_state,
        'to_state': stage.state,
        'fencing_token': stage.fencing_token,
        'version': stage.version,
    })
    return _commit_mutation(actor, key, request_hash, payload)


def _validate_transition_payload(data):
    expected_version = _positive_int(data, 'expected_version')
    fencing_token = _positive_int(data, 'fencing_token')
    to_state = str(data.get('to_state') or '').strip().lower()
    reason_code = str(data.get('reason_code') or '').strip().upper()
    evidence_refs = data.get('evidence_refs', [])
    if expected_version is None or fencing_token is None:
        return None, _error(
            'VALIDATION_FAILED',
            'expected_version and fencing_token must be non-negative integers',
            400)
    if to_state not in STAGE_STATES:
        return None, _error(
            'VALIDATION_FAILED', 'to_state is invalid', 400)
    if not _REASON_RE.fullmatch(reason_code):
        return None, _error(
            'VALIDATION_FAILED',
            'reason_code must be an uppercase stable code', 400)
    if (not isinstance(evidence_refs, list)
            or len(evidence_refs) > 100
            or any(not isinstance(item, dict) for item in evidence_refs)):
        return None, _error(
            'VALIDATION_FAILED',
            'evidence_refs must be an array of at most 100 objects', 400)
    try:
        if len(canonical_json(evidence_refs).encode('utf-8')) > 128 * 1024:
            return None, _error(
                'VALIDATION_FAILED',
                'evidence_refs must not exceed 131072 bytes', 400)
    except (TypeError, ValueError):
        return None, _error(
            'VALIDATION_FAILED', 'evidence_refs must be finite JSON', 400)
    artifact_id = data.get('artifact_id')
    if artifact_id not in (None, ''):
        if isinstance(artifact_id, bool):
            return None, _error(
                'VALIDATION_FAILED', 'artifact_id must be a positive integer', 400)
        try:
            artifact_id = int(artifact_id)
        except (TypeError, ValueError):
            return None, _error(
                'VALIDATION_FAILED', 'artifact_id must be a positive integer', 400)
        if artifact_id <= 0:
            return None, _error(
                'VALIDATION_FAILED', 'artifact_id must be a positive integer', 400)
    else:
        artifact_id = None
    return {
        'expected_version': expected_version,
        'fencing_token': fencing_token,
        'to_state': to_state,
        'reason_code': reason_code,
        'evidence_refs': evidence_refs,
        'artifact_id': artifact_id,
    }, None


@api_bp.route(
    '/missions/<int:mission_id>/stages/<string:stage_key>/transition',
    methods=['POST'])
def transition_mission_stage(mission_id, stage_key):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True) or {}
    unknown_error = _reject_unknown_fields(data, _TRANSITION_FIELDS)
    if unknown_error:
        return unknown_error
    key, error = _idempotency_key(data)
    if error:
        return error
    request_hash = _request_hash(data)
    mission, stage = _mission_and_stage(actor, mission_id, stage_key)
    if not mission or not stage:
        return _error('STAGE_NOT_FOUND', 'Mission Stage 不存在', 404)
    replay = _replay(actor, key, request_hash)
    if replay:
        return replay
    values, error = _validate_transition_payload(data)
    if error:
        return error

    mission, stage = _mission_and_stage(
        actor, mission_id, stage_key, lock=True)
    if not mission or not stage:
        return _error('STAGE_NOT_FOUND', 'Mission Stage 不存在', 404)
    if values['to_state'] not in _TRANSITIONS.get(stage.state, frozenset()):
        return _error(
            'INVALID_STAGE_TRANSITION',
            f"不允许从 {stage.state} 迁移到 {values['to_state']}", 409,
            {'from_state': stage.state, 'to_state': values['to_state']})
    if int(stage.version or 1) != values['expected_version']:
        return _error(
            'STAGE_VERSION_CONFLICT', 'Stage 版本已变化', 409,
            {'expected_version': values['expected_version'],
             'actual_version': int(stage.version or 1)})
    if int(stage.fencing_token or 0) != values['fencing_token']:
        return _error(
            'FENCING_TOKEN_STALE', '执行围栏已失效', 409,
            {'expected_fencing_token': int(stage.fencing_token or 0)})
    if actor['type'] == 'claw':
        if (not stage.assigned_claw_id
                or int(stage.assigned_claw_id) != int(actor['id'])):
            return _error(
                'STAGE_ACTOR_MISMATCH', '只有当前 Stage 执行者可以迁移状态',
                403)
    elif not _can_administer(actor, mission.project_id):
        return _error('STAGE_TRANSITION_DENIED', '无权迁移 Stage 状态', 403)

    artifact = None
    if values['artifact_id']:
        artifact = db.session.get(AgentArtifact, values['artifact_id'])
        if (not artifact
                or int(artifact.project_id) != int(mission.project_id)
                or int(artifact.mission_id) != int(mission.id)
                or artifact.stage_id != stage.stage_key):
            return _error(
                'ARTIFACT_NOT_FOUND', '产物不存在或不属于当前 Stage', 404)
    if values['to_state'] == 'submitted':
        if not artifact or artifact.status not in ('submitted', 'accepted'):
            return _error(
                'ARTIFACT_REQUIRED',
                '提交 Stage 前必须提供 submitted/accepted 主产物', 409)
    if values['to_state'] == 'accepted':
        artifact = artifact or stage.output_artifact
        if not artifact or artifact.status != 'accepted':
            return _error(
                'ARTIFACT_NOT_ACCEPTED',
                '接受 Stage 前主产物必须已通过 Review', 409)

    previous_state = stage.state
    stage.state = values['to_state']
    stage.last_reason_code = values['reason_code']
    stage.evidence_refs_json = values['evidence_refs']
    if artifact:
        stage.output_artifact_id = artifact.id
    stage.version = int(stage.version or 1) + 1
    stage.updated_at = _now()
    payload = stage.to_dict()
    _audit('transition', mission, stage, actor, {
        'from_state': previous_state,
        'to_state': stage.state,
        'reason_code': stage.last_reason_code,
        'fencing_token': stage.fencing_token,
        'version': stage.version,
        'artifact_id': stage.output_artifact_id,
        'evidence_refs': stage.evidence_refs_json or [],
    })
    return _commit_mutation(actor, key, request_hash, payload)
