"""Project-scoped, versioned Agent Artifact hand-off APIs."""

import json

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
    AgentPost,
    AgentPostAssignment,
    AuditLog,
    WorkflowMission,
    WorkflowRun,
    _now,
)
from app.services.agent_artifacts import (
    ArtifactValidationError,
    REVIEW_DECISIONS,
    normalize_create_payload,
)
from app.services.artifact_contracts import (
    ArtifactContractError,
    validate_artifact_contract,
)


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
            'type': 'claw',
            'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}',
            'claw': claw,
            'user': None,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user',
            'id': int(user.id),
            'name': (getattr(user, 'display_name', None)
                     or getattr(user, 'username', None)
                     or f'user:{user.id}'),
            'claw': None,
            'user': user,
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


def _can_create(actor, project_id):
    if not _can_access_project(actor, project_id):
        return False
    if actor['type'] == 'claw':
        return True
    return getattr(actor['user'], 'role', None) != 'guest'


def _can_review(actor, project_id):
    if not _can_access_project(actor, project_id):
        return False
    if actor['type'] == 'claw':
        return actor['claw'].role == 'admin'
    return getattr(actor['user'], 'role', None) in ('super_admin', 'admin')


def _producer_matches(actor, artifact):
    return bool(
        actor
        and artifact.producer_type == actor['type']
        and int(artifact.producer_id) == int(actor['id']))


def _artifact_for_actor(actor, artifact_id, lock=False):
    query = AgentArtifact.query.filter_by(id=artifact_id)
    if lock:
        query = query.with_for_update()
    artifact = query.first()
    if not artifact or not _can_access_project(actor, artifact.project_id):
        return None
    return artifact


def _idempotency_key(data):
    body = str((data or {}).get('idempotency_key') or '').strip()
    header = str(request.headers.get('Idempotency-Key') or '').strip()
    if body and header and body != header:
        raise ArtifactValidationError(
            'IDEMPOTENCY_KEY_MISMATCH',
            'body and header idempotency keys must match')
    value = body or header
    if not value:
        raise ArtifactValidationError(
            'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key is required')
    if len(value) > 128:
        raise ArtifactValidationError(
            'IDEMPOTENCY_KEY_TOO_LONG',
            'Idempotency-Key must not exceed 128 characters')
    return value


def _producer_metadata(actor):
    if actor['type'] == 'user':
        return {
            'producer_claw_id': None,
            'producer_role_key': getattr(actor['user'], 'role', None) or 'user',
            'producer_provider': 'web',
            'producer_profile_version': 1,
        }

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
    return {
        'producer_claw_id': claw.id,
        'producer_role_key': (
            assignment.post.post_key if assignment and assignment.post
            else claw.role or ''),
        'producer_provider': claw.llm_provider or '',
        'producer_profile_version': (
            int(assignment.profile_version or 1) if assignment else 1),
    }


def _audit(action, artifact, actor, detail=None):
    db.session.add(AuditLog(
        action=action,
        resource_type='agent_artifact',
        resource_id=artifact.id,
        resource_name=(
            f'{artifact.artifact_type}@{artifact.artifact_version}'),
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
    ))


@api_bp.route('/agent-artifacts', methods=['POST'])
def create_agent_artifact():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    data = request.get_json(silent=True)
    try:
        idempotency_key = _idempotency_key(data)
        normalized = normalize_create_payload(data)
    except ArtifactValidationError as exc:
        return _error(exc.code, str(exc), 400)

    project_id = normalized['project_id']
    if not _can_create(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', '无权在该项目创建产物', 403)

    mission = db.session.get(WorkflowMission, normalized['mission_id'])
    if (not mission
            or int(mission.project_id) != project_id):
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)

    run_id = normalized['workflow_run_id']
    if run_id:
        run = db.session.get(WorkflowRun, run_id)
        if not run or int(run.project_id or 0) != project_id:
            return _error('WORKFLOW_RUN_NOT_FOUND', 'Workflow Run 不存在', 404)

    replay = AgentArtifact.query.filter_by(
        project_id=project_id,
        producer_type=actor['type'],
        producer_id=actor['id'],
        idempotency_key=idempotency_key,
    ).first()
    if replay:
        if replay.request_sha256 != normalized['request_sha256']:
            return _error(
                'IDEMPOTENCY_CONFLICT',
                'Idempotency-Key 已用于不同的产物请求', 409)
        return jsonify(replay.to_dict()), 200

    try:
        validation = validate_artifact_contract(
            normalized['artifact_type'], normalized['schema_version'],
            normalized['content'], normalized['input_baseline_sha256'],
            mission)
    except ArtifactContractError as exc:
        return _error(
            exc.code, str(exc), 400, {'errors': exc.errors})

    version_exists = AgentArtifact.query.filter_by(
        mission_id=normalized['mission_id'],
        stage_id=normalized['stage_id'],
        artifact_type=normalized['artifact_type'],
        artifact_version=normalized['artifact_version'],
    ).first()
    if version_exists:
        return _error(
            'ARTIFACT_VERSION_CONFLICT',
            '该 Mission 阶段和产物类型的版本已存在', 409,
            {'artifact_id': version_exists.id})

    producer = _producer_metadata(actor)
    artifact = AgentArtifact(
        project_id=project_id,
        mission_id=normalized['mission_id'],
        stage_id=normalized['stage_id'],
        workflow_run_id=run_id,
        artifact_type=normalized['artifact_type'],
        schema_version=normalized['schema_version'],
        artifact_version=normalized['artifact_version'],
        producer_type=actor['type'],
        producer_id=actor['id'],
        producer_name=actor['name'],
        input_baseline_sha256=normalized['input_baseline_sha256'],
        content_json=normalized['content'],
        content_sha256=normalized['content_sha256'],
        contract_validation_json=validation,
        validated_at=_now(),
        status='draft',
        idempotency_key=idempotency_key,
        request_sha256=normalized['request_sha256'],
        **producer,
    )
    db.session.add(artifact)
    try:
        db.session.flush()
        _audit('create', artifact, actor, {
            'project_id': project_id,
            'mission_id': artifact.mission_id,
            'stage_id': artifact.stage_id,
            'content_sha256': artifact.content_sha256,
        })
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        replay = AgentArtifact.query.filter_by(
            project_id=project_id,
            producer_type=actor['type'],
            producer_id=actor['id'],
            idempotency_key=idempotency_key,
        ).first()
        if replay and replay.request_sha256 == normalized['request_sha256']:
            return jsonify(replay.to_dict()), 200
        if replay:
            return _error(
                'IDEMPOTENCY_CONFLICT',
                'Idempotency-Key 已用于不同的产物请求', 409)
        return _error(
            'ARTIFACT_VERSION_CONFLICT',
            '该 Mission 阶段和产物类型的版本已存在', 409)
    return jsonify(artifact.to_dict()), 201


@api_bp.route('/agent-artifacts/<int:artifact_id>', methods=['GET'])
def get_agent_artifact(artifact_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    artifact = _artifact_for_actor(actor, artifact_id)
    if not artifact:
        return _error('ARTIFACT_NOT_FOUND', '产物不存在', 404)
    return jsonify(artifact.to_dict())


@api_bp.route('/agent-artifacts/<int:artifact_id>/submit', methods=['POST'])
def submit_agent_artifact(artifact_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    artifact = _artifact_for_actor(actor, artifact_id, lock=True)
    if not artifact:
        return _error('ARTIFACT_NOT_FOUND', '产物不存在', 404)
    if not _producer_matches(actor, artifact) and not _can_review(
            actor, artifact.project_id):
        return _error('ARTIFACT_SUBMIT_DENIED', '只有产出者可以提交产物', 403)
    if artifact.status == 'submitted':
        return jsonify(artifact.to_dict())
    if artifact.status != 'draft':
        return _error(
            'ARTIFACT_IMMUTABLE', '终态产物不可再次提交', 409,
            {'status': artifact.status})
    try:
        validation = validate_artifact_contract(
            artifact.artifact_type, artifact.schema_version,
            artifact.content_json or {}, artifact.input_baseline_sha256,
            artifact.mission)
    except ArtifactContractError as exc:
        return _error(
            exc.code, str(exc), 409, {'errors': exc.errors})
    artifact.contract_validation_json = validation
    artifact.validated_at = _now()
    artifact.status = 'submitted'
    artifact.submitted_at = _now()
    _audit('submit', artifact, actor, {'status': artifact.status})
    db.session.commit()
    return jsonify(artifact.to_dict())


@api_bp.route('/agent-artifacts/<int:artifact_id>/review', methods=['POST'])
def review_agent_artifact(artifact_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    artifact = _artifact_for_actor(actor, artifact_id, lock=True)
    if not artifact:
        return _error('ARTIFACT_NOT_FOUND', '产物不存在', 404)
    if not _can_review(actor, artifact.project_id):
        return _error('ARTIFACT_REVIEW_DENIED', '无权审核该项目产物', 403)

    data = request.get_json(silent=True) or {}
    decision = str(data.get('decision') or '').strip().lower()
    if decision not in REVIEW_DECISIONS:
        return _error(
            'VALIDATION_FAILED', 'decision must be accepted or rejected', 400)
    comment = str(data.get('comment') or '').strip()
    if len(comment) > 10000:
        return _error(
            'VALIDATION_FAILED', 'comment must not exceed 10000 characters', 400)
    if artifact.status == decision:
        return jsonify(artifact.to_dict())
    if artifact.status != 'submitted':
        return _error(
            'ARTIFACT_REVIEW_CONFLICT', '只有 submitted 产物可以审核', 409,
            {'status': artifact.status})

    if decision == 'accepted':
        try:
            validation = validate_artifact_contract(
                artifact.artifact_type, artifact.schema_version,
                artifact.content_json or {}, artifact.input_baseline_sha256,
                artifact.mission)
        except ArtifactContractError as exc:
            return _error(
                exc.code, str(exc), 409, {'errors': exc.errors})
        artifact.contract_validation_json = validation
        artifact.validated_at = _now()

    artifact.status = decision
    artifact.review_comment = comment
    artifact.reviewed_by_type = actor['type']
    artifact.reviewed_by_id = actor['id']
    artifact.reviewed_by_name = actor['name']
    artifact.reviewed_at = _now()
    _audit('review', artifact, actor, {
        'decision': decision,
        'comment': comment,
    })
    db.session.commit()
    return jsonify(artifact.to_dict())


@api_bp.route('/missions/<int:mission_id>/artifacts', methods=['GET'])
def list_mission_agent_artifacts(mission_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('AUTH_REQUIRED', '未认证', 401)
    mission = db.session.get(WorkflowMission, mission_id)
    if not mission or not _can_access_project(actor, mission.project_id):
        return _error('MISSION_NOT_FOUND', 'Mission 不存在', 404)

    query = AgentArtifact.query.filter_by(mission_id=mission.id)
    status = str(request.args.get('status') or '').strip().lower()
    artifact_type = str(request.args.get('artifact_type') or '').strip()
    stage_id = str(request.args.get('stage_id') or '').strip()
    if status:
        query = query.filter(AgentArtifact.status == status)
    if artifact_type:
        query = query.filter(AgentArtifact.artifact_type == artifact_type)
    if stage_id:
        query = query.filter(AgentArtifact.stage_id == stage_id)
    rows = query.order_by(
        AgentArtifact.stage_id.asc(),
        AgentArtifact.artifact_type.asc(),
        AgentArtifact.artifact_version.desc(),
        AgentArtifact.id.desc(),
    ).limit(500).all()
    return jsonify({
        'mission_id': mission.id,
        'project_id': mission.project_id,
        'items': [row.to_dict() for row in rows],
        'count': len(rows),
    })
