"""测试左移、代码分析报告评审与临时 Developer AI 协作 API。

首期仅提供新增数据面；不会自动触发、修改或接管现有 Workflow、报告、
用例评审和测试计划流程。生产可通过 SHIFT_LEFT_ENABLED 灰度开启。
"""

import json
from datetime import datetime, timedelta, timezone

from flask import current_app, g, jsonify, request, session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user
from app.models import (
    AuditLog,
    CaseReviewComment,
    CaseReviewNodeMark,
    CaseReviewRound,
    CollaborationSession,
    CollaborationSessionEvent,
    Project,
    ShiftLeftAnalysisFinding,
    ShiftLeftAnalysisRun,
    ShiftLeftFinding,
    ShiftLeftFindingEvent,
    ShiftLeftFindingFeedback,
    TestCase,
    TestCaseLibrary,
    TestIteration,
    TestReport,
    Topic,
    WorkflowOperationIdempotency,
    WorkflowEvidenceManifest,
    WorkflowRun,
)
from app.services.shift_left import (
    COLLABORATION_SUBJECT_TYPES,
    DEFAULT_CASE_REVIEW_SCOPES,
    DEFAULT_TOPIC_SCOPES,
    EVIDENCE_LEVELS,
    FINDING_SEVERITIES,
    FINDING_STATES,
    FINDING_FEEDBACK_LABELS,
    build_baseline_fingerprint,
    canonical_json,
    collaboration_submission_identity,
    generate_access_token,
    generate_invitation_code,
    normalize_scopes,
    now_cst_naive,
    payload_hash,
    secret_hash,
    transition_target,
    validate_transition_preconditions,
)
from app.services.evidence_manifests import (
    normalize_analysis_summary,
    normalize_classification,
    normalize_manifest,
)
from app.services.entity_relations import best_effort_upsert_relations
from app.services.case_mindmap import MARKS, normalize_mark


ANALYSIS_RESULT_STATUSES = {
    'completed', 'completed_with_findings', 'blocked', 'failed', 'cancelled',
}

COLLABORATION_MAX_TTL_MINUTES = 72 * 60
_CST = timezone(timedelta(hours=8))


def _error(message, status=400, code='INVALID_REQUEST', **extra):
    payload = {'error': message, 'code': code}
    payload.update(extra)
    return jsonify(payload), status


def _parse_collaboration_deadline(raw, now=None):
    """Parse a collaboration deadline and enforce the rolling 72-hour cap."""
    text = str(raw or '').strip()
    if not text:
        raise ValueError('expires_at 必填')
    try:
        deadline = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('expires_at 格式无效') from exc
    if deadline.tzinfo is not None:
        deadline = deadline.astimezone(_CST).replace(tzinfo=None)
    now = now or now_cst_naive()
    if deadline <= now:
        raise ValueError('截止时间必须晚于当前时间')
    if deadline > now + timedelta(minutes=COLLABORATION_MAX_TTL_MINUTES):
        raise ValueError('截止时间最长只能延至当前时间后 72 小时')
    return deadline


def _enabled():
    value = current_app.config.get('SHIFT_LEFT_ENABLED', False)
    if isinstance(value, str):
        return value.lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _disabled_response():
    if _enabled():
        return None
    return _error('测试左移能力尚未开启', 404, 'SHIFT_LEFT_DISABLED')


def _actor():
    collaboration = getattr(g, '_collaboration_session', None)
    if collaboration:
        return {
            'type': 'collaboration',
            'id': collaboration.id,
            'key': 'collaboration:%s' % collaboration.id,
            'name': 'external-participant:%s' % collaboration.id,
            'collaboration': collaboration,
        }

    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw',
            'id': claw.id,
            'key': 'claw:%s' % claw.id,
            'name': claw.name,
            'claw': claw,
        }

    user = get_current_user()
    if user:
        return {
            'type': 'user',
            'id': user.id,
            'key': 'user:%s' % user.id,
            'name': user.username,
            'user': user,
        }
    return None


def _independence_key(actor):
    """Stable reviewer/fixer identity across renewed collaboration sessions."""
    if actor['type'] == 'collaboration':
        return 'developer_ai_session:%s' % actor['collaboration'].id
    return actor['key']


def _can_access_project(actor, project_id, write=False):
    if not actor:
        return False
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return False

    if actor['type'] == 'collaboration':
        return actor['collaboration'].project_id == project_id

    if actor['type'] == 'claw':
        claw = actor['claw']
        if claw.role == 'admin' and claw.project_id is None:
            return True
        return claw.project_id == project_id

    user = actor['user']
    if write and getattr(user, 'role', None) == 'guest':
        return False
    if getattr(user, 'role', None) in ('super_admin', 'admin'):
        return True
    ids = set()
    for raw in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        from app.models import OpenClawInstance
        bound = db.session.get(OpenClawInstance, bound_claw_id)
        if bound and bound.project_id:
            ids.add(int(bound.project_id))
    return project_id in ids


def _require_project(actor, project_id, write=False):
    if _can_access_project(actor, project_id, write=write):
        return None
    return _error('无权访问该项目', 403, 'PROJECT_ACCESS_DENIED')


def _require_collaboration_scope(actor, scope):
    if actor and actor['type'] == 'collaboration':
        collaboration = actor['collaboration']
        if not collaboration.has_scope(scope):
            return _error('临时协作会话缺少 scope: %s' % scope,
                          403, 'COLLABORATION_SCOPE_DENIED')
    return None


def _collaboration_allows_report(collaboration, report_id):
    if collaboration.subject_type == 'analysis_report':
        return collaboration.subject_id == report_id
    if collaboration.subject_type == 'analysis_run':
        row = db.session.get(ShiftLeftAnalysisRun, collaboration.subject_id)
        return bool(row and row.report_id == report_id)
    if collaboration.subject_type == 'finding':
        finding = db.session.get(ShiftLeftFinding, collaboration.subject_id)
        if not finding:
            return False
        run = db.session.get(ShiftLeftAnalysisRun, finding.analysis_run_id)
        return bool(run and run.report_id == report_id)
    return False


def _collaboration_allows_finding(collaboration, finding):
    if collaboration.subject_type == 'finding':
        return collaboration.subject_id == finding.id
    if collaboration.subject_type == 'analysis_run':
        return collaboration.subject_id == finding.analysis_run_id
    if collaboration.subject_type == 'analysis_report':
        occurrence = (ShiftLeftAnalysisFinding.query
                      .join(ShiftLeftAnalysisRun,
                            ShiftLeftAnalysisFinding.analysis_run_id ==
                            ShiftLeftAnalysisRun.id)
                      .filter(
                          ShiftLeftAnalysisFinding.finding_id == finding.id,
                          ShiftLeftAnalysisRun.report_id == collaboration.subject_id,
                      ).first())
        return occurrence is not None
    return False


def _require_finding_access(actor, finding, write=False, scope='finding:read'):
    denied = _require_project(actor, finding.project_id, write=write)
    if denied:
        return denied
    if actor['type'] == 'collaboration':
        if not _collaboration_allows_finding(actor['collaboration'], finding):
            return _error('该 Finding 不在临时协作范围内', 403,
                          'COLLABORATION_SUBJECT_DENIED')
        return _require_collaboration_scope(actor, scope)
    return None


def _add_audit(action, resource_type, resource_id, resource_name,
               actor, detail=None):
    db.session.add(AuditLog(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_name=(resource_name or '')[:200],
        operator=actor['name'],
        ip_address=request.remote_addr,
        detail=canonical_json(detail or {}),
    ))


def _idempotency_begin(actor, create=True):
    key = str(request.headers.get('Idempotency-Key') or '').strip()
    if not key:
        return None, _error('写请求必须携带 Idempotency-Key', 400,
                            'IDEMPOTENCY_KEY_REQUIRED')
    if len(key) > 128:
        return None, _error('Idempotency-Key 不能超过 128 字符', 400,
                            'IDEMPOTENCY_KEY_TOO_LONG')
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
                '同一 Idempotency-Key 已用于不同请求', 409,
                'IDEMPOTENCY_KEY_REUSED')
        if row.response_status is None:
            return None, _error('相同写请求仍在处理中', 409,
                                'IDEMPOTENCY_IN_PROGRESS')
        return None, (jsonify(row.response_body_json or {}), row.response_status)

    if not create:
        return None, None

    row = WorkflowOperationIdempotency(
        actor_type=actor['type'],
        actor_id=actor['id'],
        idempotency_key=key,
        method=request.method,
        path=request.path,
        request_hash=request_fingerprint,
        expires_at=now_cst_naive() + timedelta(days=7),
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = WorkflowOperationIdempotency.query.filter_by(
            actor_type=actor['type'], actor_id=actor['id'],
            idempotency_key=key).first()
        if (existing and existing.method == request.method
                and existing.path == request.path
                and existing.request_hash == request_fingerprint
                and existing.response_status is not None):
            return None, (jsonify(existing.response_body_json or {}),
                          existing.response_status)
        return None, _error('幂等请求发生并发冲突', 409,
                            'IDEMPOTENCY_CONFLICT')
    return row, None


def _commit_payload(idempotency_row, payload, status=200):
    try:
        if idempotency_row is not None:
            idempotency_row.response_status = status
            idempotency_row.response_body_json = payload
        db.session.commit()
        return jsonify(payload), status
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('shift-left transaction failed')
        return _error('保存失败，请稍后重试', 500, 'SHIFT_LEFT_WRITE_FAILED')


def _new_finding_event(finding, event_type, payload, actor, idempotency_key=None):
    row = ShiftLeftFindingEvent(
        finding_id=finding.id,
        event_type=event_type,
        payload_json=payload,
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_key=actor['key'],
        actor_name=actor['name'],
        idempotency_key=idempotency_key,
    )
    db.session.add(row)
    return row


def _report_or_404(report_id):
    report = TestReport.query.filter_by(id=report_id, is_deleted=False).first()
    if not report:
        return None, _error('报告不存在', 404, 'REPORT_NOT_FOUND')
    return report, None


@api_bp.route('/shift-left/analysis-runs', methods=['POST'])
def create_shift_left_analysis_run():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    data = request.get_json(silent=True) or {}
    project_id = data.get('project_id')
    if not db.session.get(Project, project_id):
        return _error('project_id 无效', 400, 'PROJECT_NOT_FOUND')
    denied = _require_project(actor, project_id, write=True)
    if denied:
        return denied
    if not data.get('client_target_sha') or not data.get('server_target_sha'):
        return _error('client_target_sha 和 server_target_sha 必填', 400,
                      'ANALYSIS_TARGET_SHA_REQUIRED')
    iteration_id = data.get('iteration_id')
    if iteration_id is not None:
        try:
            iteration_id = int(iteration_id)
        except (TypeError, ValueError):
            return _error('iteration_id 必须是整数', 400,
                          'INVALID_ITERATION_ID')
        iteration = db.session.get(TestIteration, iteration_id)
        if not iteration or iteration.project_id != int(project_id):
            return _error('iteration_id 不属于该项目', 400,
                          'ITERATION_PROJECT_MISMATCH')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay

    fingerprint, baseline = build_baseline_fingerprint(data)
    force_rerun = bool(data.get('force_rerun'))
    existing = (ShiftLeftAnalysisRun.query
                .filter_by(project_id=project_id,
                           baseline_fingerprint=fingerprint)
                .order_by(ShiftLeftAnalysisRun.rerun_sequence.asc()).first())
    if existing and not force_rerun:
        payload = existing.to_dict()
        payload['reused'] = True
        return _commit_payload(idem, payload, 200)
    if force_rerun and not str(data.get('rerun_reason') or '').strip():
        db.session.rollback()
        return _error('显式 rerun 必须提供 rerun_reason', 400,
                      'RERUN_REASON_REQUIRED')

    rerun_sequence = 0
    rerun_of_id = None
    if existing:
        rerun_of_id = existing.id
        rerun_sequence = int(db.session.query(
            func.max(ShiftLeftAnalysisRun.rerun_sequence)).filter_by(
                project_id=project_id,
                baseline_fingerprint=fingerprint).scalar() or 0) + 1

    workflow_run_id = data.get('workflow_run_id')
    if workflow_run_id:
        workflow_run = db.session.get(WorkflowRun, workflow_run_id)
        if not workflow_run or workflow_run.project_id != int(project_id):
            db.session.rollback()
            return _error('workflow_run_id 不属于该项目', 400,
                          'WORKFLOW_PROJECT_MISMATCH')
    report_id = data.get('report_id')
    if report_id:
        report = db.session.get(TestReport, report_id)
        if not report or report.project_id != int(project_id):
            db.session.rollback()
            return _error('report_id 不属于该项目', 400,
                          'REPORT_PROJECT_MISMATCH')

    row = ShiftLeftAnalysisRun(
        project_id=int(project_id),
        iteration_id=iteration_id,
        workflow_run_id=workflow_run_id,
        report_id=report_id,
        baseline_fingerprint=fingerprint,
        rerun_sequence=rerun_sequence,
        rerun_of_id=rerun_of_id,
        rerun_reason=str(data.get('rerun_reason') or '')[:500],
        status='pending',
        baseline_json=baseline,
        context_snapshot_json=data.get('context_snapshot') or {},
        output_schema_version=baseline['output_schema_version'],
        created_by=actor['name'],
        updated_by=actor['name'],
    )
    db.session.add(row)
    db.session.flush()
    _add_audit('create', 'shift_left_analysis_run', row.id,
               'Analysis Run #%s' % row.id, actor,
               {'baseline_fingerprint': fingerprint,
                'workflow_run_id': workflow_run_id, 'report_id': report_id})
    payload = row.to_dict()
    payload['reused'] = False
    return _commit_payload(idem, payload, 201)


@api_bp.route('/shift-left/analysis-runs/<int:run_id>', methods=['GET'])
def get_shift_left_analysis_run(run_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    row = db.session.get(ShiftLeftAnalysisRun, run_id)
    if not row:
        return _error('Analysis Run 不存在', 404, 'ANALYSIS_RUN_NOT_FOUND')
    denied = _require_project(actor, row.project_id)
    if denied:
        return denied
    if actor['type'] == 'collaboration':
        collaboration = actor['collaboration']
        if not (collaboration.subject_type == 'analysis_run'
                and collaboration.subject_id == row.id):
            return _error('Analysis Run 不在临时协作范围内', 403,
                          'COLLABORATION_SUBJECT_DENIED')
    return jsonify(row.to_dict())


@api_bp.route('/shift-left/analysis-runs/<int:run_id>/result', methods=['POST'])
def update_shift_left_analysis_result(run_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    row = db.session.get(ShiftLeftAnalysisRun, run_id)
    if not row:
        return _error('Analysis Run 不存在', 404, 'ANALYSIS_RUN_NOT_FOUND')
    denied = _require_project(actor, row.project_id, write=True)
    if denied:
        return denied
    if actor['type'] == 'collaboration':
        return _error('临时协作会话不能回写 Analysis Run', 403,
                      'COLLABORATION_SCOPE_DENIED')
    data = request.get_json(silent=True) or {}
    status = str(data.get('status') or '').strip()
    if status not in ANALYSIS_RESULT_STATUSES:
        return _error('status 必须是 %s' % '/'.join(sorted(ANALYSIS_RESULT_STATUSES)),
                      400, 'INVALID_ANALYSIS_STATUS')
    from_revision = data.get('from_revision')
    try:
        parsed_revision = int(from_revision) if from_revision is not None else None
    except (TypeError, ValueError):
        return _error('from_revision 必须是整数', 400, 'INVALID_REVISION')
    if parsed_revision is not None and parsed_revision != row.revision:
        return _error('Analysis Run revision 冲突', 409, 'REVISION_CONFLICT',
                      current_revision=row.revision)
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    row.status = status
    row.result_summary_json = data.get('summary') or {}
    row.updated_by = actor['name']
    row.revision = (row.revision or 1) + 1
    if data.get('report_id') is not None:
        report = db.session.get(TestReport, data['report_id'])
        if not report or report.project_id != row.project_id:
            db.session.rollback()
            return _error('report_id 不属于该项目', 400,
                          'REPORT_PROJECT_MISMATCH')
        row.report_id = report.id
    if status in ANALYSIS_RESULT_STATUSES:
        row.finished_at = now_cst_naive()
    _add_audit('result', 'shift_left_analysis_run', row.id,
               'Analysis Run #%s' % row.id, actor,
               {'status': status, 'revision': row.revision})
    return _commit_payload(idem, row.to_dict(), 200)


def _workflow_run_for_evidence(run_id, actor, write=False):
    row = ((WorkflowRun.query.filter_by(id=run_id).with_for_update().first())
           if write else db.session.get(WorkflowRun, run_id))
    if not row or row.project_id is None:
        return None, _error(
            'Workflow Run 不存在或未关联项目', 404, 'WORKFLOW_RUN_NOT_FOUND')
    denied = _require_project(actor, row.project_id, write=write)
    if denied:
        return None, denied
    if actor['type'] == 'collaboration':
        return None, _error(
            '临时协作会话请通过报告上下文读取证据', 403,
            'COLLABORATION_SCOPE_DENIED')
    return row, None


def _expected_manifest_revision(data, current):
    raw = data.get('expected_version')
    if raw is None:
        if current is None:
            return 0
        raise ValueError('更新 Evidence Manifest 必须提供 expected_version')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError('expected_version 必须是整数') from None
    if value < 0:
        raise ValueError('expected_version 不能小于 0')
    return value


def _manifest_relation(manifest):
    return {
        'from_type': 'workflow_run',
        'from_id': str(manifest.workflow_run_id),
        'relation_type': 'has_evidence',
        'to_type': 'evidence_manifest',
        'to_id': str(manifest.id),
        'metadata': {
            'completeness_status': manifest.completeness_status,
            'manifest_revision': manifest.revision,
        },
    }


@api_bp.route('/evidence-manifests', methods=['GET'])
def list_workflow_evidence_manifests():
    """Read-only cross-Run evidence index, available before write rollout."""
    actor = _actor()
    project_id = request.args.get('project_id', type=int)
    if not project_id:
        return _error('project_id 必填', 400, 'PROJECT_ID_REQUIRED')
    denied = _require_project(actor, project_id)
    if denied:
        return denied
    query = WorkflowEvidenceManifest.query.filter_by(project_id=project_id)
    if request.args.get('completeness_status'):
        query = query.filter_by(
            completeness_status=request.args['completeness_status'])
    if request.args.get('classification'):
        query = query.filter_by(classification=request.args['classification'])
    if request.args.get('workflow_run_id', type=int):
        query = query.filter_by(
            workflow_run_id=request.args.get('workflow_run_id', type=int))
    page = max(request.args.get('page', 1, type=int), 1)
    page_size = min(max(request.args.get('page_size', 50, type=int), 1), 200)
    pagination = query.order_by(
        WorkflowEvidenceManifest.updated_at.desc(),
        WorkflowEvidenceManifest.id.desc(),
    ).paginate(page=page, per_page=page_size, error_out=False)
    return jsonify({
        'items': [row.to_dict() for row in pagination.items],
        'total': pagination.total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/workflow-runs/<int:run_id>/evidence-manifest', methods=['GET'])
def get_workflow_evidence_manifest(run_id):
    # Reading evidence cannot affect an existing Flow. Keep writes gated, but
    # allow agents and the closed-loop dashboard to inspect deployed data.
    actor = _actor()
    _, error = _workflow_run_for_evidence(run_id, actor)
    if error:
        return error
    row = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run_id).first()
    if not row:
        return _error(
            'Evidence Manifest 不存在', 404, 'EVIDENCE_MANIFEST_NOT_FOUND')
    return jsonify(row.to_dict())


@api_bp.route('/workflow-runs/<int:run_id>/evidence-manifest', methods=['PUT'])
def upsert_workflow_evidence_manifest(run_id):
    # Worker result ingestion is always available for existing Workflows, so
    # the explicit equivalent follows the same compatibility contract.
    actor = _actor()
    workflow_run, error = _workflow_run_for_evidence(run_id, actor, write=True)
    if error:
        return error
    # Replay must win over state-dependent validation. A create without
    # expected_version is valid while no Manifest exists; its identical retry
    # must return the saved 201 after that Manifest has been created.
    _, replay = _idempotency_begin(actor, create=False)
    if replay:
        return replay
    data = request.get_json(silent=True) or {}
    try:
        normalized = normalize_manifest(data)
    except ValueError as exc:
        return _error(str(exc), 400, 'EVIDENCE_MANIFEST_VALIDATION_FAILED')
    row = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=workflow_run.id).first()
    try:
        expected_version = _expected_manifest_revision(data, row)
    except ValueError as exc:
        return _error(str(exc), 400, 'INVALID_EXPECTED_VERSION')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    current_version = int(row.revision or 1) if row else 0
    if expected_version != current_version:
        db.session.rollback()
        return _error(
            'Evidence Manifest revision 冲突', 409, 'REVISION_CONFLICT',
            current_revision=current_version)
    if (row and row.classification == 'NO_RISK_FOUND'
            and normalized['completeness_status'] != 'complete'):
        db.session.rollback()
        return _error(
            '现有结论是 NO_RISK_FOUND；证据降级前必须先重新分析', 409,
            'EVIDENCE_REGRESSION_REQUIRES_REANALYSIS',
            missing_required=normalized['missing_required'])

    analysis_run_id = data.get('analysis_run_id')
    if analysis_run_id is None and row is not None:
        analysis_run_id = row.analysis_run_id
    if analysis_run_id is not None:
        try:
            analysis_run_id = int(analysis_run_id)
        except (TypeError, ValueError):
            db.session.rollback()
            return _error(
                'analysis_run_id 必须是整数', 400, 'INVALID_ANALYSIS_RUN_ID')
        analysis_run = db.session.get(ShiftLeftAnalysisRun, analysis_run_id)
        if (not analysis_run or analysis_run.project_id != workflow_run.project_id
                or (analysis_run.workflow_run_id is not None
                    and analysis_run.workflow_run_id != workflow_run.id)):
            db.session.rollback()
            return _error(
                'analysis_run_id 不属于该 Workflow Run 的项目或绑定了其他 Run',
                400, 'ANALYSIS_RUN_MISMATCH')

    created = row is None
    if created:
        row = WorkflowEvidenceManifest(
            project_id=workflow_run.project_id,
            workflow_run_id=workflow_run.id,
            revision=1,
            created_by=actor['name'],
        )
        db.session.add(row)
    else:
        row.revision = current_version + 1
    row.analysis_run_id = analysis_run_id
    row.coverage_json = normalized['coverage']
    row.artifacts_json = normalized['artifacts']
    row.required_evidence_json = normalized['required_evidence']
    row.completeness_status = normalized['completeness_status']
    row.missing_required_json = normalized['missing_required']
    row.updated_by = actor['name']
    db.session.flush()
    best_effort_upsert_relations(
        workflow_run.project_id, [_manifest_relation(row)], actor['name'])
    _add_audit(
        'create' if created else 'update', 'workflow_evidence_manifest', row.id,
        'Workflow Run #%s Evidence Manifest' % workflow_run.id, actor, {
            'workflow_run_id': workflow_run.id,
            'revision': row.revision,
            'completeness_status': row.completeness_status,
            'missing_required': row.missing_required_json or [],
        })
    return _commit_payload(idem, row.to_dict(), 201 if created else 200)


def _normalize_finding_ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('finding_ids must be an array with at most 100 items')
    result = []
    seen = set()
    for raw in value:
        try:
            finding_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError('finding_ids must contain integers') from None
        if finding_id <= 0:
            raise ValueError('finding_ids must contain positive integers')
        if finding_id not in seen:
            seen.add(finding_id)
            result.append(finding_id)
    return result


@api_bp.route('/workflow-runs/<int:run_id>/post-run-analysis', methods=['POST'])
def save_workflow_post_run_analysis(run_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    workflow_run, error = _workflow_run_for_evidence(run_id, actor, write=True)
    if error:
        return error
    manifest = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=workflow_run.id).first()
    if not manifest:
        return _error(
            '请先保存 Evidence Manifest', 409, 'EVIDENCE_MANIFEST_REQUIRED')
    data = request.get_json(silent=True) or {}
    try:
        classification = normalize_classification(data.get('classification'))
        summary = normalize_analysis_summary(data.get('summary'))
        finding_ids = _normalize_finding_ids(data.get('finding_ids'))
        expected_version = _expected_manifest_revision(data, manifest)
    except ValueError as exc:
        return _error(str(exc), 400, 'POST_RUN_ANALYSIS_VALIDATION_FAILED')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    if expected_version != int(manifest.revision or 1):
        db.session.rollback()
        return _error(
            'Evidence Manifest revision 冲突', 409, 'REVISION_CONFLICT',
            current_revision=manifest.revision)
    if (classification == 'NO_RISK_FOUND'
            and manifest.completeness_status != 'complete'):
        db.session.rollback()
        return _error(
            '证据不完整时不能保存 NO_RISK_FOUND，必须使用 ANALYSIS_INCOMPLETE',
            409, 'EVIDENCE_INCOMPLETE_FOR_NO_RISK',
            missing_required=manifest.missing_required_json or [])

    findings = []
    if finding_ids:
        findings = ShiftLeftFinding.query.filter(
            ShiftLeftFinding.id.in_(finding_ids),
            ShiftLeftFinding.project_id == workflow_run.project_id,
            ShiftLeftFinding.is_archived.is_(False),
        ).all()
        found_ids = {row.id for row in findings}
        missing_ids = [value for value in finding_ids if value not in found_ids]
        if missing_ids:
            db.session.rollback()
            return _error(
                'Finding 不存在或不属于该项目', 400, 'FINDING_PROJECT_MISMATCH',
                finding_ids=missing_ids)

    manifest.classification = classification
    manifest.analysis_summary_json = summary
    manifest.finding_ids_json = finding_ids
    manifest.analyzed_by = actor['name']
    manifest.analyzed_at = now_cst_naive()
    manifest.updated_by = actor['name']
    manifest.revision = int(manifest.revision or 1) + 1
    if manifest.analysis_run_id is not None:
        for finding in findings:
            snapshot = {
                'title': finding.title,
                'severity': finding.severity,
                'confidence': finding.confidence,
                'evidence_level': finding.evidence_level,
                'status': finding.status,
                'module': finding.module,
                'classification': classification,
                'evidence_manifest_id': manifest.id,
            }
            occurrence = ShiftLeftAnalysisFinding.query.filter_by(
                analysis_run_id=manifest.analysis_run_id,
                finding_id=finding.id,
            ).first()
            if occurrence is None:
                db.session.add(ShiftLeftAnalysisFinding(
                    analysis_run_id=manifest.analysis_run_id,
                    finding_id=finding.id,
                    snapshot_json=snapshot,
                ))
            else:
                occurrence.snapshot_json = snapshot
    db.session.flush()
    relations = [_manifest_relation(manifest)]
    for finding in findings:
        relations.extend([{
            'from_type': 'workflow_run',
            'from_id': str(workflow_run.id),
            'relation_type': 'produced',
            'to_type': 'finding',
            'to_id': str(finding.id),
            'metadata': {
                'classification': classification,
                'evidence_manifest_id': manifest.id,
            },
        }, {
            'from_type': 'evidence_manifest',
            'from_id': str(manifest.id),
            'relation_type': 'supports',
            'to_type': 'finding',
            'to_id': str(finding.id),
            'metadata': {'manifest_revision': manifest.revision},
        }])
    best_effort_upsert_relations(
        workflow_run.project_id, relations, actor['name'])
    _add_audit(
        'classify', 'workflow_evidence_manifest', manifest.id,
        'Workflow Run #%s Post-run Analysis' % workflow_run.id, actor, {
            'workflow_run_id': workflow_run.id,
            'classification': classification,
            'finding_ids': finding_ids,
            'revision': manifest.revision,
        })
    return _commit_payload(idem, manifest.to_dict(), 200)


@api_bp.route('/findings', methods=['GET'])
@api_bp.route('/shift-left/findings', methods=['GET'])
def list_shift_left_findings():
    # Compatibility/read route is safe while SHIFT_LEFT_ENABLED remains off;
    # all mutations below stay behind the feature gate.
    actor = _actor()
    if actor['type'] == 'collaboration':
        denied = _require_collaboration_scope(actor, 'finding:read')
        if denied:
            return denied
        collaboration = actor['collaboration']
        if collaboration.subject_type == 'finding':
            query = ShiftLeftFinding.query.filter_by(id=collaboration.subject_id)
        elif collaboration.subject_type == 'analysis_run':
            query = ShiftLeftFinding.query.join(
                ShiftLeftAnalysisFinding,
                ShiftLeftAnalysisFinding.finding_id == ShiftLeftFinding.id,
            ).filter(
                ShiftLeftAnalysisFinding.analysis_run_id == collaboration.subject_id)
        elif collaboration.subject_type == 'analysis_report':
            query = (ShiftLeftFinding.query
                     .join(ShiftLeftAnalysisFinding,
                           ShiftLeftAnalysisFinding.finding_id == ShiftLeftFinding.id)
                     .join(ShiftLeftAnalysisRun,
                           ShiftLeftAnalysisRun.id ==
                           ShiftLeftAnalysisFinding.analysis_run_id)
                     .filter(ShiftLeftAnalysisRun.report_id ==
                             collaboration.subject_id))
        else:
            return _error('该协作对象不支持 Finding 列表', 403,
                          'COLLABORATION_SUBJECT_DENIED')
    else:
        project_id = request.args.get('project_id', type=int)
        if not project_id:
            return _error('project_id 必填', 400, 'PROJECT_ID_REQUIRED')
        denied = _require_project(actor, project_id)
        if denied:
            return denied
        query = ShiftLeftFinding.query.filter_by(project_id=project_id)

    if request.args.get('status'):
        query = query.filter(ShiftLeftFinding.status == request.args['status'])
    if request.args.get('severity'):
        query = query.filter(ShiftLeftFinding.severity == request.args['severity'])
    if request.args.get('module'):
        query = query.filter(ShiftLeftFinding.module == request.args['module'])
    if request.args.get('analysis_run_id', type=int):
        query = query.join(
            ShiftLeftAnalysisFinding,
            ShiftLeftAnalysisFinding.finding_id == ShiftLeftFinding.id,
        ).filter(ShiftLeftAnalysisFinding.analysis_run_id ==
                 request.args.get('analysis_run_id', type=int))
    if request.args.get('iteration_id', type=int):
        iteration_finding_ids = (db.session.query(
            ShiftLeftAnalysisFinding.finding_id)
            .join(ShiftLeftAnalysisRun,
                  ShiftLeftAnalysisRun.id ==
                  ShiftLeftAnalysisFinding.analysis_run_id)
            .filter(ShiftLeftAnalysisRun.iteration_id ==
                    request.args.get('iteration_id', type=int)))
        query = query.filter(ShiftLeftFinding.id.in_(iteration_finding_ids))
    query = query.filter(ShiftLeftFinding.is_archived.is_(False)).distinct()
    page = max(request.args.get('page', 1, type=int), 1)
    page_size = min(max(request.args.get('page_size', 50, type=int), 1), 200)
    pagination = query.order_by(
        ShiftLeftFinding.last_seen_at.desc(), ShiftLeftFinding.id.desc()
    ).paginate(page=page, per_page=page_size, error_out=False)
    return jsonify({
        'items': [row.to_dict() for row in pagination.items],
        'total': pagination.total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/shift-left/findings:upsert', methods=['PUT'])
@api_bp.route('/shift-left/findings/upsert', methods=['POST'])
def upsert_shift_left_finding():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if actor['type'] == 'collaboration':
        return _error('临时协作会话不能创建或覆盖 Finding', 403,
                      'COLLABORATION_SCOPE_DENIED')
    data = request.get_json(silent=True) or {}
    project_id = data.get('project_id')
    denied = _require_project(actor, project_id, write=True)
    if denied:
        return denied
    analysis_run = db.session.get(ShiftLeftAnalysisRun, data.get('analysis_run_id'))
    if not analysis_run or analysis_run.project_id != int(project_id):
        return _error('analysis_run_id 不属于该项目', 400,
                      'ANALYSIS_PROJECT_MISMATCH')
    finding_key = str(data.get('finding_key') or '').strip()
    title = str(data.get('title') or '').strip()
    if not finding_key or not title:
        return _error('finding_key 和 title 必填', 400,
                      'FINDING_IDENTITY_REQUIRED')
    if len(finding_key) > 255 or len(title) > 300:
        return _error('finding_key 或 title 超长', 400, 'FINDING_FIELD_TOO_LONG')
    severity = str(data.get('severity') or 'medium').strip()
    evidence_level = str(data.get('evidence_level') or 'hypothesis').strip()
    if severity not in FINDING_SEVERITIES:
        return _error('severity 无效', 400, 'INVALID_FINDING_SEVERITY')
    if evidence_level not in EVIDENCE_LEVELS:
        return _error('evidence_level 无效', 400, 'INVALID_EVIDENCE_LEVEL')
    try:
        confidence = max(0.0, min(float(data.get('confidence') or 0), 1.0))
    except (TypeError, ValueError):
        return _error('confidence 必须为 0~1 数字', 400,
                      'INVALID_FINDING_CONFIDENCE')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay

    now = now_cst_naive()
    row = ShiftLeftFinding.query.filter_by(
        project_id=int(project_id), finding_key=finding_key).first()
    created = row is None
    if created:
        row = ShiftLeftFinding(
            project_id=int(project_id),
            analysis_run_id=analysis_run.id,
            finding_key=finding_key,
            title=title,
            first_seen_at=now,
            created_by=actor['name'],
            created_by_actor_key=_independence_key(actor),
        )
        db.session.add(row)
    row.analysis_run_id = analysis_run.id
    row.title = title
    row.module = str(data.get('module') or '')[:160]
    row.severity = severity
    row.confidence = confidence
    row.evidence_level = evidence_level
    row.source_type = str(data.get('source_type') or 'agent')[:40]
    row.description = data.get('description') or ''
    row.business_impact = data.get('business_impact') or ''
    row.context_quality = str(data.get('context_quality') or 'fresh')[:20]
    row.code_locations_json = data.get('code_locations') or []
    row.associations_json = data.get('associations') or {}
    row.owner = str(data.get('owner') or row.owner or '')[:120]
    row.last_seen_at = now
    row.updated_by = actor['name']
    if not created:
        row.revision = (row.revision or 1) + 1
    db.session.flush()

    snapshot = {
        'title': row.title,
        'severity': row.severity,
        'confidence': row.confidence,
        'evidence_level': row.evidence_level,
        'status': row.status,
        'module': row.module,
    }
    occurrence = ShiftLeftAnalysisFinding.query.filter_by(
        analysis_run_id=analysis_run.id, finding_id=row.id).first()
    if not occurrence:
        occurrence = ShiftLeftAnalysisFinding(
            analysis_run_id=analysis_run.id,
            finding_id=row.id,
            snapshot_json=snapshot,
        )
        db.session.add(occurrence)
    else:
        occurrence.snapshot_json = snapshot
    idempotency_key = request.headers.get('Idempotency-Key')
    _new_finding_event(row, 'seen', {
        'analysis_run_id': analysis_run.id,
        'created': created,
        'snapshot': snapshot,
    }, actor, idempotency_key=idempotency_key)
    evidence = data.get('evidence')
    if evidence:
        _new_finding_event(row, 'evidence', {
            'evidence': evidence,
            'evidence_level': evidence_level,
        }, actor)
    _add_audit('create' if created else 'upsert', 'shift_left_finding',
               row.id, row.title, actor,
               {'finding_key': finding_key, 'analysis_run_id': analysis_run.id})
    payload = row.to_dict()
    payload['created'] = created
    return _commit_payload(idem, payload, 201 if created else 200)


@api_bp.route('/shift-left/findings/<int:finding_id>', methods=['GET'])
def get_shift_left_finding(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding)
    if denied:
        return denied
    payload = finding.to_dict()
    payload['events'] = [event.to_dict() for event in (
        ShiftLeftFindingEvent.query.filter_by(finding_id=finding.id)
        .order_by(ShiftLeftFindingEvent.created_at.asc(),
                  ShiftLeftFindingEvent.id.asc()).all())]
    payload['feedback'] = [row.to_dict() for row in (
        ShiftLeftFindingFeedback.query.filter_by(finding_id=finding.id)
        .order_by(ShiftLeftFindingFeedback.id.asc()).all())]
    return jsonify(payload)


@api_bp.route('/shift-left/findings/<int:finding_id>/feedback', methods=['GET'])
def list_shift_left_finding_feedback(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding)
    if denied:
        return denied
    rows = (ShiftLeftFindingFeedback.query.filter_by(finding_id=finding.id)
            .order_by(ShiftLeftFindingFeedback.id.asc()).all())
    counts = {}
    for row in rows:
        counts[row.label] = counts.get(row.label, 0) + 1
    return jsonify({
        'finding_id': finding.id,
        'items': [row.to_dict() for row in rows],
        'counts': counts,
        'total': len(rows),
    })


@api_bp.route('/shift-left/findings/<int:finding_id>/feedback', methods=['POST'])
def add_shift_left_finding_feedback(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(
        actor, finding, write=True, scope='finding:review')
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    label = str(data.get('label') or '').strip().lower()
    if label not in FINDING_FEEDBACK_LABELS:
        return _error(
            'feedback label 无效', 400, 'INVALID_FINDING_FEEDBACK_LABEL',
            allowed_labels=list(FINDING_FEEDBACK_LABELS))
    try:
        from_revision = int(data.get('from_revision'))
    except (TypeError, ValueError):
        return _error(
            'from_revision 必须是整数', 400, 'INVALID_REVISION')
    if from_revision != int(finding.revision or 1):
        return _error(
            'Finding revision 冲突', 409, 'REVISION_CONFLICT',
            current_revision=finding.revision,
            current_status=finding.status)
    note = str(data.get('note') or '').strip()
    refs = data.get('evidence_refs') or []
    if not isinstance(refs, list) or len(refs) > 100:
        return _error(
            'evidence_refs 必须是最多 100 项的数组', 400,
            'INVALID_FEEDBACK_EVIDENCE_REFS')
    normalized_refs = []
    for index, value in enumerate(refs):
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return _error(
                    'evidence_refs 不能包含空值', 400,
                    'INVALID_FEEDBACK_EVIDENCE_REFS')
            normalized_refs.append(value[:1000])
        elif isinstance(value, dict):
            normalized_refs.append(value)
        else:
            return _error(
                'evidence_refs[%s] 必须是字符串或对象' % index, 400,
                'INVALID_FEEDBACK_EVIDENCE_REFS')
    if not note and not normalized_refs:
        return _error(
            'note 或 evidence_refs 至少提供一项', 400,
            'FINDING_FEEDBACK_EVIDENCE_REQUIRED')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    idempotency_key = str(request.headers.get('Idempotency-Key') or '').strip()
    request_hash = payload_hash({
        'finding_id': finding.id,
        'label': label,
        'from_revision': from_revision,
        'note': note,
        'evidence_refs': normalized_refs,
    })
    feedback = ShiftLeftFindingFeedback(
        project_id=finding.project_id,
        finding_id=finding.id,
        label=label,
        note=note,
        evidence_refs_json=normalized_refs,
        source_type=(
            'human' if actor['type'] == 'user'
            else 'developer_ai' if actor['type'] == 'collaboration'
            else 'system'),
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_key=_independence_key(actor),
        actor_name=actor['name'],
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(feedback)
    db.session.flush()
    _new_finding_event(
        finding, 'feedback', {
            'feedback_id': feedback.id,
            'label': label,
            'finding_revision': from_revision,
            'note': note,
            'evidence_refs': normalized_refs,
        }, actor)
    _add_audit(
        'feedback', 'shift_left_finding', finding.id, finding.title, actor,
        {'feedback_id': feedback.id, 'label': label,
         'finding_revision': from_revision})
    return _commit_payload(idem, feedback.to_dict(), 201)


@api_bp.route('/shift-left/findings/<int:finding_id>/evidence', methods=['GET'])
def list_shift_left_finding_evidence(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding)
    if denied:
        return denied
    rows = ShiftLeftFindingEvent.query.filter_by(
        finding_id=finding.id, event_type='evidence').order_by(
            ShiftLeftFindingEvent.created_at.asc()).all()
    return jsonify({'items': [row.to_dict() for row in rows]})


@api_bp.route('/shift-left/findings/<int:finding_id>/evidence', methods=['POST'])
def add_shift_left_finding_evidence(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    if actor['type'] == 'collaboration':
        return _error('临时协作会话不能新增或提升证据等级', 403,
                      'COLLABORATION_SCOPE_DENIED')
    denied = _require_finding_access(actor, finding, write=True,
                                     scope='finding:review')
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if not data.get('summary') and not data.get('artifact_refs'):
        return _error('summary 或 artifact_refs 至少提供一项', 400,
                      'EVIDENCE_CONTENT_REQUIRED')
    level = str(data.get('evidence_level') or finding.evidence_level).strip()
    if level not in EVIDENCE_LEVELS:
        return _error('evidence_level 无效', 400, 'INVALID_EVIDENCE_LEVEL')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    event = _new_finding_event(
        finding, 'evidence', {
            'summary': data.get('summary') or '',
            'evidence_level': level,
            'artifact_refs': data.get('artifact_refs') or [],
            'references': data.get('references') or [],
        }, actor, request.headers.get('Idempotency-Key'))
    if EVIDENCE_LEVELS.index(level) > EVIDENCE_LEVELS.index(finding.evidence_level):
        finding.evidence_level = level
        finding.revision = (finding.revision or 1) + 1
        finding.updated_by = actor['name']
    db.session.flush()
    _add_audit('evidence', 'shift_left_finding', finding.id,
               finding.title, actor, {'event_id': event.id, 'level': level})
    return _commit_payload(idem, event.to_dict(), 201)


@api_bp.route('/shift-left/findings/<int:finding_id>/comments', methods=['GET'])
def list_shift_left_finding_comments(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding)
    if denied:
        return denied
    rows = ShiftLeftFindingEvent.query.filter_by(
        finding_id=finding.id, event_type='comment').order_by(
            ShiftLeftFindingEvent.created_at.asc(),
            ShiftLeftFindingEvent.id.asc()).all()
    return jsonify({'items': [row.to_dict() for row in rows]})


@api_bp.route('/shift-left/findings/<int:finding_id>/comments', methods=['POST'])
def add_shift_left_finding_comment(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding, write=True,
                                     scope='finding:comment')
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    content = str(data.get('content') or '').strip()
    if not content:
        return _error('content 不能为空', 400, 'COMMENT_CONTENT_REQUIRED')
    if len(content) > 20000:
        return _error('content 不能超过 20000 字符', 400,
                      'COMMENT_CONTENT_TOO_LONG')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    event = _new_finding_event(finding, 'comment', {
        'content': content,
        'comment_type': str(data.get('comment_type') or 'comment')[:40],
        'references': data.get('references') or [],
    }, actor, request.headers.get('Idempotency-Key'))
    db.session.flush()
    _add_audit('comment', 'shift_left_finding', finding.id,
               finding.title, actor, {'event_id': event.id})
    return _commit_payload(idem, event.to_dict(), 201)


@api_bp.route('/shift-left/findings/<int:finding_id>/review-decisions',
              methods=['POST'])
def add_shift_left_review_decision(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    denied = _require_finding_access(actor, finding, write=True,
                                     scope='finding:review')
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    decision = str(data.get('decision') or '').strip()
    if decision not in ('confirmed', 'rejected', 'needs_clarification', 'comment'):
        return _error('decision 无效', 400, 'INVALID_REVIEW_DECISION')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    payload = {
        'decision': decision,
        'root_cause': data.get('root_cause') or '',
        'suggested_fix': data.get('suggested_fix') or '',
        'risk': data.get('risk') or '',
        'confidence': data.get('confidence'),
        'references': data.get('references') or [],
    }
    event = _new_finding_event(
        finding, 'review_decision', payload, actor,
        request.headers.get('Idempotency-Key'))
    db.session.flush()
    _add_audit('review', 'shift_left_finding', finding.id,
               finding.title, actor,
               {'event_id': event.id, 'decision': decision})
    return _commit_payload(idem, event.to_dict(), 201)


@api_bp.route('/shift-left/findings/<int:finding_id>/transitions',
              methods=['POST'])
def transition_shift_left_finding(finding_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    finding = db.session.get(ShiftLeftFinding, finding_id)
    if not finding or finding.is_archived:
        return _error('Finding 不存在', 404, 'FINDING_NOT_FOUND')
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip()
    scope = 'finding:transition:%s' % action
    denied = _require_finding_access(actor, finding, write=True, scope=scope)
    if denied:
        return denied
    if data.get('from_revision') is None:
        return _error('from_revision 必填', 400, 'REVISION_REQUIRED')
    try:
        from_revision = int(data['from_revision'])
    except (TypeError, ValueError):
        return _error('from_revision 必须是整数', 400, 'INVALID_REVISION')
    if from_revision != finding.revision:
        return _error('Finding revision 冲突', 409, 'REVISION_CONFLICT',
                      current_revision=finding.revision,
                      current_status=finding.status)
    try:
        target = transition_target(finding.status, action)
    except ValueError as exc:
        return _error(str(exc), 409, 'INVALID_FINDING_TRANSITION')
    precondition_error = validate_transition_preconditions(finding, action, data)
    if precondition_error:
        return _error(precondition_error, 409,
                      'FINDING_TRANSITION_PRECONDITION_FAILED')
    if actor['type'] == 'collaboration' and action not in (
            'acknowledge', 'start_fix', 'submit_fix'):
        return _error('临时协作会话不能执行该状态动作', 403,
                      'COLLABORATION_SCOPE_DENIED')
    actor_identity = _independence_key(actor)
    if (action == 'confirm' and actor['type'] != 'user' and
            finding.created_by_actor_key == actor_identity):
        return _error('生成该问题的 Agent 不能自行确认，需要独立评审者', 403,
                      'INDEPENDENT_CONFIRMATION_REQUIRED')
    if (action == 'close' and actor['type'] != 'user' and
            finding.fix_actor_key and finding.fix_actor_key == actor_identity):
        return _error('执行修复的 Agent 不能自行验收关闭，需要独立验证者', 403,
                      'INDEPENDENT_VERIFICATION_REQUIRED')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay

    previous = finding.status
    finding.status = target
    finding.status_reason = str(data.get('reason') or data.get('blocker') or '')
    finding.updated_by = actor['name']
    finding.revision = (finding.revision or 1) + 1
    if data.get('fix_ref'):
        finding.fix_ref = str(data['fix_ref'])[:500]
    if data.get('verification_ref'):
        finding.verification_ref = str(data['verification_ref'])[:500]
    if action in ('start_fix', 'submit_fix'):
        finding.fix_actor_key = actor_identity
    if action == 'close':
        finding.verification_actor_key = actor_identity
    event = _new_finding_event(finding, 'transition', {
        'action': action,
        'from_status': previous,
        'to_status': target,
        'from_revision': from_revision,
        'to_revision': finding.revision,
        'reason': data.get('reason') or '',
        'counter_evidence': data.get('counter_evidence') or '',
        'blocker': data.get('blocker') or '',
        'fix_ref': data.get('fix_ref') or '',
        'verification_ref': data.get('verification_ref') or '',
    }, actor, request.headers.get('Idempotency-Key'))
    db.session.flush()
    _add_audit('transition', 'shift_left_finding', finding.id,
               finding.title, actor,
               {'event_id': event.id, 'action': action,
                'from': previous, 'to': target})
    payload = finding.to_dict()
    payload['transition_event'] = event.to_dict()
    return _commit_payload(idem, payload, 200)


@api_bp.route('/test-reports/<int:report_id>/analysis-context', methods=['GET'])
def get_analysis_report_context(report_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    report, error = _report_or_404(report_id)
    if error:
        return error
    denied = _require_project(actor, report.project_id)
    if denied:
        return denied
    if actor['type'] == 'collaboration':
        scope_denied = _require_collaboration_scope(actor, 'report:read')
        if scope_denied:
            return scope_denied
        if not _collaboration_allows_report(actor['collaboration'], report.id):
            return _error('报告不在临时协作范围内', 403,
                          'COLLABORATION_SUBJECT_DENIED')
    runs = ShiftLeftAnalysisRun.query.filter_by(report_id=report.id).order_by(
        ShiftLeftAnalysisRun.id.desc()).all()
    workflow_run_ids = {
        row.workflow_run_id for row in runs if row.workflow_run_id is not None}
    manifests = (WorkflowEvidenceManifest.query.filter(
        WorkflowEvidenceManifest.workflow_run_id.in_(workflow_run_ids)
    ).order_by(WorkflowEvidenceManifest.id.desc()).all()
                 if workflow_run_ids else [])
    return jsonify({
        'report': {
            'id': report.id,
            'title': report.title,
            'project_id': report.project_id,
            'iteration_id': report.iteration_id,
            'report_type': report.report_type,
            'status': report.status,
            'format': report.format,
            'content': report.content or '',
            'updated_at': str(report.updated_at) if report.updated_at else None,
        },
        'analysis_runs': [row.to_dict() for row in runs],
        'evidence_manifests': [row.to_dict() for row in manifests],
        'latest_analysis_run_id': runs[0].id if runs else None,
    })


@api_bp.route('/test-reports/<int:report_id>/findings', methods=['GET'])
def list_analysis_report_findings(report_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    report, error = _report_or_404(report_id)
    if error:
        return error
    denied = _require_project(actor, report.project_id)
    if denied:
        return denied
    if actor['type'] == 'collaboration':
        scope_denied = _require_collaboration_scope(actor, 'finding:read')
        if scope_denied:
            return scope_denied
        if not _collaboration_allows_report(actor['collaboration'], report.id):
            return _error('报告不在临时协作范围内', 403,
                          'COLLABORATION_SUBJECT_DENIED')

    rows = (db.session.query(ShiftLeftFinding, ShiftLeftAnalysisFinding)
            .join(ShiftLeftAnalysisFinding,
                  ShiftLeftAnalysisFinding.finding_id == ShiftLeftFinding.id)
            .join(ShiftLeftAnalysisRun,
                  ShiftLeftAnalysisRun.id ==
                  ShiftLeftAnalysisFinding.analysis_run_id)
            .filter(ShiftLeftAnalysisRun.report_id == report.id,
                    ShiftLeftFinding.is_archived.is_(False))
            .order_by(ShiftLeftFinding.last_seen_at.desc()).all())
    items = []
    seen = set()
    for finding, occurrence in rows:
        if finding.id in seen:
            continue
        seen.add(finding.id)
        payload = finding.to_dict()
        payload['report_snapshot'] = occurrence.snapshot_json or {}
        items.append(payload)
    return jsonify({'report_id': report.id, 'items': items, 'total': len(items)})


def _case_review_project(topic):
    if not topic or topic.board != 'case_review' or topic.status == 'deleted':
        return None
    project_name = ''
    if topic.review_library:
        project_name = topic.review_library.project_name or ''
    project_name = project_name or topic.project_name or ''
    return Project.query.filter_by(name=project_name).first() if project_name else None


def _topic_project(topic):
    if not topic or topic.status == 'deleted':
        return None
    project_name = str(topic.project_name or '').strip()
    return Project.query.filter_by(name=project_name).first() if project_name else None


def _can_manage_topic_collaboration(actor, topic):
    if not actor or actor['type'] == 'collaboration' or not topic:
        return False
    if actor['type'] == 'claw':
        claw = actor['claw']
        return bool(
            claw.role == 'admin'
            or (topic.author_claw_id
                and int(topic.author_claw_id) == int(claw.id)))
    user = actor['user']
    return bool(
        user.role in ('super_admin', 'admin')
        or (topic.author_user_id
            and int(topic.author_user_id) == int(user.id)))


def _require_topic_collaboration_manager(actor, topic):
    if _can_manage_topic_collaboration(actor, topic):
        return None
    return _error(
        '只有课题发起人或管理员可以管理对外协作链接', 403,
        'TOPIC_COLLABORATION_MANAGE_DENIED')


def _collaboration_subject_project_id(subject_type, subject_id):
    if subject_type == 'analysis_report':
        row = db.session.get(TestReport, subject_id)
        return row.project_id if row and not row.is_deleted else None
    if subject_type == 'analysis_run':
        row = db.session.get(ShiftLeftAnalysisRun, subject_id)
        return row.project_id if row else None
    if subject_type == 'finding':
        row = db.session.get(ShiftLeftFinding, subject_id)
        return row.project_id if row else None
    if subject_type == 'case_review':
        project = _case_review_project(db.session.get(Topic, subject_id))
        return project.id if project else None
    if subject_type == 'topic':
        project = _topic_project(db.session.get(Topic, subject_id))
        return project.id if project else None
    return None


def _require_collaboration_session_manager(actor, row, write=False):
    if row.subject_type == 'topic':
        topic = db.session.get(Topic, row.subject_id)
        if not topic or topic.status == 'deleted':
            return _error('课题不存在', 404, 'TOPIC_NOT_FOUND')
        return _require_topic_collaboration_manager(actor, topic)
    return _require_project(actor, row.project_id, write=write)


def _case_review_scope(topic):
    library = db.session.get(TestCaseLibrary, topic.review_library_id)
    if not library:
        return None, [], set()
    paths = []
    for value in (topic.review_module_paths or []):
        value = str(value or '').strip().strip('/')
        if value not in paths:
            paths.append(value)
    case_ids = set()
    for value in (topic.review_case_ids or []):
        try:
            case_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return library, paths, case_ids


def _case_in_review_scope(case, paths, case_ids):
    if case_ids:
        return case.id in case_ids
    if not paths:
        return True
    value = str(case.module_path or '').strip().strip('/')
    return any(value == root or (root and value.startswith(root + '/'))
               for root in paths)


def _case_review_or_error(topic_id, actor, scope='case_review:read', write=False):
    if not actor or actor['type'] != 'collaboration':
        return None, None, _error(
            '该接口仅供临时 Developer AI 协作凭据调用', 403,
            'COLLABORATION_TOKEN_REQUIRED')
    topic = db.session.get(Topic, topic_id)
    if not topic or topic.board != 'case_review' or topic.status == 'deleted':
        return None, None, _error('用例评审不存在', 404, 'CASE_REVIEW_NOT_FOUND')
    project = _case_review_project(topic)
    if not project:
        return None, None, _error('用例评审未关联有效项目', 400,
                                  'CASE_REVIEW_PROJECT_NOT_FOUND')
    denied = _require_project(actor, project.id, write=write)
    if denied:
        return None, None, denied
    collaboration = actor['collaboration']
    if not (collaboration.subject_type == 'case_review'
            and collaboration.subject_id == topic.id):
        return None, None, _error('用例评审不在临时协作范围内', 403,
                                  'COLLABORATION_SUBJECT_DENIED')
    scope_denied = _require_collaboration_scope(actor, scope)
    if scope_denied:
        return None, None, scope_denied
    return topic, project, None


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/context', methods=['GET'])
def get_case_review_context(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, project, denied = _case_review_or_error(topic_id, actor)
    if denied:
        return denied
    library, paths, case_ids = _case_review_scope(topic)
    if not library:
        return _error('关联的用例库不存在', 404, 'CASE_LIBRARY_NOT_FOUND')
    rounds = (CaseReviewRound.query.filter_by(topic_id=topic.id)
              .filter(db.or_(CaseReviewRound.is_deleted.is_(False),
                             CaseReviewRound.is_deleted.is_(None)))
              .order_by(CaseReviewRound.round_number.asc()).all())
    marks = CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()
    active_records = (CaseReviewComment.query.join(
        CaseReviewRound, CaseReviewComment.round_id == CaseReviewRound.id)
        .filter(CaseReviewRound.topic_id == topic.id)
        .filter(db.or_(CaseReviewComment.status == 'active',
                       CaseReviewComment.status.is_(None))).count())
    topic_payload = topic.to_dict()
    topic_payload.pop('review_rounds', None)
    open_round_id = next((row.id for row in reversed(rounds)
                          if row.status == 'pending'), None)
    return jsonify({
        'review_summary': {
            'topic_id': topic.id,
            'title': topic.title,
            'status': topic.review_status or 'reviewing',
            'round_count': len(rounds),
            'open_round_id': open_round_id,
            'submitted_review_count': active_records,
        },
        'topic': topic_payload,
        'project': {'id': project.id, 'name': project.name},
        'library': library.to_dict(with_cases=False, with_mindmap=False),
        'scope': {'module_paths': paths, 'case_ids': sorted(case_ids)},
        'rounds': [row.to_dict(with_comments=True) for row in rounds],
        'open_round_id': open_round_id,
        'marks': [row.to_dict() for row in marks],
        'mark_legend': {key: dict(value) for key, value in MARKS.items()},
        'links': {
            'cases': '/api/v1/shift-left/case-reviews/%d/cases' % topic.id,
            'reviews': '/api/v1/shift-left/case-reviews/%d/reviews' % topic.id,
            'comments': '/api/v1/shift-left/case-reviews/%d/comments' % topic.id,
            'marks': '/api/v1/shift-left/case-reviews/%d/marks' % topic.id,
        },
    })


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/cases', methods=['GET'])
def list_case_review_cases(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(topic_id, actor)
    if denied:
        return denied
    library, paths, case_ids = _case_review_scope(topic)
    if not library:
        return _error('关联的用例库不存在', 404, 'CASE_LIBRARY_NOT_FOUND')
    try:
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('page_size', 100)), 1), 200)
    except (TypeError, ValueError):
        return _error('page/page_size 必须是整数', 400, 'INVALID_PAGINATION')
    query = (TestCase.query.filter_by(library_id=library.id)
             .filter(db.or_(TestCase.is_placeholder.is_(False),
                            TestCase.is_placeholder.is_(None))))
    if case_ids:
        query = query.filter(TestCase.id.in_(case_ids))
    elif paths:
        predicates = []
        for root in paths:
            if root:
                predicates.extend((TestCase.module_path == root,
                                   TestCase.module_path.like(root + '/%')))
            else:
                predicates.extend((TestCase.module_path == '',
                                   TestCase.module_path.is_(None)))
        query = query.filter(db.or_(*predicates))
    total = query.count()
    rows = (query.order_by(TestCase.module_path.asc(), TestCase.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return jsonify({'items': [row.to_dict() for row in rows], 'total': total,
                    'page': page, 'page_size': page_size})


def _case_review_record_payload(row, actor, topic):
    payload = row.to_dict()
    collaboration_id = (
        actor['collaboration'].id
        if actor and actor['type'] == 'collaboration' else None)
    owned = bool(collaboration_id
                 and row.collaboration_session_id == collaboration_id)
    payload['owned_by_me'] = owned
    payload['can_modify'] = bool(
        owned and topic.review_status != 'closed'
        and row.round and row.round.status == 'pending'
        and (row.status or 'active') == 'active')
    return payload


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/reviews',
              methods=['GET'])
def list_case_review_records(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(topic_id, actor)
    if denied:
        return denied
    try:
        page = max(int(request.args.get('page', 1)), 1)
        page_size = min(max(int(request.args.get('page_size', 100)), 1), 200)
    except (TypeError, ValueError):
        return _error('page/page_size 必须是整数', 400,
                      'INVALID_PAGINATION')
    query = (CaseReviewComment.query.join(
        CaseReviewRound, CaseReviewComment.round_id == CaseReviewRound.id)
        .filter(CaseReviewRound.topic_id == topic.id)
        .filter(db.or_(CaseReviewComment.status == 'active',
                       CaseReviewComment.status.is_(None))))
    round_id = request.args.get('round_id')
    if round_id not in (None, ''):
        try:
            query = query.filter(CaseReviewComment.round_id == int(round_id))
        except (TypeError, ValueError):
            return _error('round_id 必须是整数', 400, 'INVALID_ROUND_ID')
    total = query.count()
    rows = (query.order_by(CaseReviewComment.created_at.asc(),
                           CaseReviewComment.id.asc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return jsonify({
        'items': [_case_review_record_payload(row, actor, topic)
                  for row in rows],
        'total': total,
        'page': page,
        'page_size': page_size,
    })


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/reviews',
              methods=['POST'])
@api_bp.route('/shift-left/case-reviews/<int:topic_id>/comments', methods=['POST'])
def create_case_review_comment(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(
        topic_id, actor, scope='case_review:comment', write=True)
    if denied:
        return denied
    if topic.review_status == 'closed':
        return _error('评审已关闭，不能增加意见', 409, 'CASE_REVIEW_CLOSED')
    data = request.get_json(silent=True) or {}
    content = str(data.get('content') or '').strip()
    if not content:
        return _error('评审意见不能为空', 400, 'COMMENT_REQUIRED')
    if len(content) > 20000:
        return _error('评审意见不能超过 20000 字符', 400, 'COMMENT_TOO_LONG')
    display_identity = actor['name']
    if actor['type'] == 'collaboration':
        try:
            display_identity = collaboration_submission_identity(
                actor['collaboration'], data, max_length=100)
        except ValueError as exc:
            return _error(str(exc), 400, 'INVALID_SUBMISSION_IDENTITY')
    verdict = str(data.get('verdict') or 'comment').strip().lower()
    if verdict not in ('comment', 'approve', 'reject'):
        return _error('verdict 仅支持 comment/approve/reject', 400,
                      'INVALID_VERDICT')
    if verdict != 'comment' and actor['type'] == 'collaboration':
        scope_denied = _require_collaboration_scope(actor, 'case_review:decision')
        if scope_denied:
            return scope_denied
    score = data.get('score')
    if score is not None:
        try:
            score = int(score)
        except (TypeError, ValueError):
            return _error('score 必须是 1-10 的整数', 400, 'INVALID_SCORE')
        if not 1 <= score <= 10:
            return _error('score 必须是 1-10 的整数', 400, 'INVALID_SCORE')
    round_id = data.get('round_id')
    if round_id is not None:
        try:
            round_id = int(round_id)
        except (TypeError, ValueError):
            return _error('round_id 必须是整数', 400, 'INVALID_ROUND_ID')
        review_round = CaseReviewRound.query.filter_by(
            id=round_id, topic_id=topic.id, status='pending').first()
    else:
        review_round = (CaseReviewRound.query.filter_by(
            topic_id=topic.id, status='pending')
            .order_by(CaseReviewRound.round_number.desc()).first())
    if not review_round:
        return _error('当前没有开放的评审轮次', 409, 'NO_OPEN_REVIEW_ROUND')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    row = CaseReviewComment(
        round_id=review_round.id,
        author_name=display_identity[:100],
        author_claw_id=actor['id'] if actor['type'] == 'claw' else None,
        author_user_id=actor['id'] if actor['type'] == 'user' else None,
        collaboration_session_id=(
            actor['collaboration'].id
            if actor['type'] == 'collaboration' else None),
        content=content,
        verdict=verdict,
        score=score,
        status='active',
    )
    db.session.add(row)
    if verdict in ('approve', 'reject'):
        review_round.status = 'approved' if verdict == 'approve' else 'rejected'
        from app.api.topics import _sync_library_review_status
        _sync_library_review_status(topic, review_round.status)
    db.session.flush()
    _add_audit('comment', 'case_review', topic.id, topic.title, actor,
               {'round_id': review_round.id, 'comment_id': row.id,
                'verdict': verdict})
    return _commit_payload(
        idem, _case_review_record_payload(row, actor, topic), 201)


def _owned_case_review_record_or_error(topic, review_id, actor):
    row = (CaseReviewComment.query.join(
        CaseReviewRound, CaseReviewComment.round_id == CaseReviewRound.id)
        .filter(CaseReviewComment.id == review_id,
                CaseReviewRound.topic_id == topic.id)
        .first())
    if not row or (row.status or 'active') != 'active':
        return None, _error('评审记录不存在', 404,
                            'CASE_REVIEW_RECORD_NOT_FOUND')
    if row.collaboration_session_id != actor['collaboration'].id:
        return None, _error('只能修改或删除当前临时会话提交的评审记录', 403,
                            'CASE_REVIEW_RECORD_NOT_OWNER')
    if topic.review_status == 'closed' or not row.round \
            or row.round.status != 'pending':
        return None, _error('评审已关闭或所属轮次已结束', 409,
                            'CASE_REVIEW_RECORD_LOCKED')
    return row, None


@api_bp.route(
    '/shift-left/case-reviews/<int:topic_id>/reviews/<int:review_id>',
    methods=['PATCH', 'PUT'])
def update_case_review_record(topic_id, review_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(
        topic_id, actor, scope='case_review:comment', write=True)
    if denied:
        return denied
    row, denied = _owned_case_review_record_or_error(topic, review_id, actor)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if not any(key in data for key in ('identity', 'agent_identity',
                                       'author_name', 'content', 'score')):
        return _error('至少提供 identity、content 或 score', 400,
                      'REVIEW_RECORD_UPDATE_REQUIRED')
    new_content = None
    new_score = row.score
    if 'content' in data:
        content = str(data.get('content') or '').strip()
        if not content:
            return _error('评审意见不能为空', 400, 'COMMENT_REQUIRED')
        if len(content) > 20000:
            return _error('评审意见不能超过 20000 字符', 400,
                          'COMMENT_TOO_LONG')
        new_content = content
    if 'score' in data:
        score = data.get('score')
        if score is None:
            new_score = None
        else:
            try:
                score = int(score)
            except (TypeError, ValueError):
                return _error('score 必须是 1-10 的整数', 400,
                              'INVALID_SCORE')
            if not 1 <= score <= 10:
                return _error('score 必须是 1-10 的整数', 400,
                              'INVALID_SCORE')
            new_score = score
    new_identity = None
    if actor['type'] == 'collaboration' and any(
            key in data for key in ('identity', 'agent_identity',
                                    'author_name')):
        try:
            new_identity = collaboration_submission_identity(
                actor['collaboration'], data, max_length=100)
        except ValueError as exc:
            return _error(str(exc), 400, 'INVALID_SUBMISSION_IDENTITY')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    if new_content is not None:
        row.content = new_content
    if 'score' in data:
        row.score = new_score
    if new_identity is not None:
        row.author_name = new_identity
    row.is_edited = True
    row.updated_at = now_cst_naive()
    _add_audit('update_review_record', 'case_review', topic.id, topic.title,
               actor, {'round_id': row.round_id, 'review_id': row.id})
    return _commit_payload(
        idem, _case_review_record_payload(row, actor, topic), 200)


@api_bp.route(
    '/shift-left/case-reviews/<int:topic_id>/reviews/<int:review_id>',
    methods=['DELETE'])
def delete_case_review_record(topic_id, review_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(
        topic_id, actor, scope='case_review:comment', write=True)
    if denied:
        return denied
    row, denied = _owned_case_review_record_or_error(topic, review_id, actor)
    if denied:
        return denied
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    row.status = 'deleted'
    row.deleted_at = now_cst_naive()
    row.updated_at = row.deleted_at
    _add_audit('delete_review_record', 'case_review', topic.id, topic.title,
               actor, {'round_id': row.round_id, 'review_id': row.id})
    return _commit_payload(idem, {
        'id': row.id,
        'deleted': True,
        'deleted_at': str(row.deleted_at),
    }, 200)


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/marks', methods=['GET'])
def list_case_review_marks(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(topic_id, actor)
    if denied:
        return denied
    rows = CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()
    return jsonify({'items': [row.to_dict() for row in rows],
                    'total': len(rows),
                    'mark_legend': {key: dict(value)
                                    for key, value in MARKS.items()}})


@api_bp.route('/shift-left/case-reviews/<int:topic_id>/marks', methods=['PUT'])
def update_case_review_marks(topic_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    topic, _project, denied = _case_review_or_error(
        topic_id, actor, scope='case_review:mark', write=True)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    items = data.get('marks')
    if not isinstance(items, list) or len(items) > 500:
        return _error('marks 必须是数组且单次不超过 500 项', 400, 'INVALID_MARKS')
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    library, paths, case_ids = _case_review_scope(topic)
    if not library:
        return _error('关联的用例库不存在', 404, 'CASE_LIBRARY_NOT_FOUND')
    existing = {row.node_id: row for row in
                CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()}
    scoped_cases = [case for case in TestCase.query.filter_by(library_id=library.id).all()
                    if _case_in_review_scope(case, paths, case_ids)]
    operator = actor['name'][:100]
    applied = 0
    cleared = 0
    skipped = []
    for item in items:
        if not isinstance(item, dict):
            skipped.append({'reason': 'NOT_OBJECT'})
            continue
        node_id = str(item.get('node_id') or '').strip()
        if node_id.startswith('case:') and node_id[5:].isdigit():
            node_type, node_key = 'case', node_id[5:]
            case = db.session.get(TestCase, int(node_key))
            valid = bool(case and case.library_id == library.id
                         and _case_in_review_scope(case, paths, case_ids))
        elif node_id.startswith('mod:'):
            node_type = 'module'
            node_key = node_id[4:].strip().strip('/')
            valid = any(((not node_key and not (case.module_path or ''))
                             or (case.module_path or '') == node_key
                             or (node_key and (case.module_path or '').startswith(node_key + '/')))
                        for case in scoped_cases)
        else:
            skipped.append({'node_id': node_id, 'reason': 'BAD_NODE_ID'})
            continue
        if not valid:
            skipped.append({'node_id': node_id, 'reason': 'OUT_OF_SCOPE'})
            continue
        row = existing.get(node_id)
        mark = normalize_mark(item.get('mark'))
        if mark is None:
            if row and (not row.marked_by or row.marked_by == operator):
                db.session.delete(row)
                existing.pop(node_id, None)
                cleared += 1
            elif row:
                skipped.append({'node_id': node_id, 'reason': 'NOT_YOUR_MARK'})
            continue
        if row and row.marked_by and row.marked_by != operator:
            skipped.append({'node_id': node_id, 'reason': 'NOT_YOUR_MARK'})
            continue
        note = str(item.get('note') or '').strip()[:500]
        if row:
            row.mark = mark
            row.note = note
            row.marked_by = operator
        else:
            row = CaseReviewNodeMark(topic_id=topic.id, node_type=node_type,
                                     node_key=node_key, mark=mark, note=note,
                                     marked_by=operator)
            db.session.add(row)
            existing[node_id] = row
        applied += 1
    db.session.flush()
    rows = CaseReviewNodeMark.query.filter_by(topic_id=topic.id).all()
    payload = {'applied': applied, 'cleared': cleared, 'skipped': skipped,
               'items': [row.to_dict() for row in rows]}
    _add_audit('mark', 'case_review', topic.id, topic.title, actor,
               {'applied': applied, 'cleared': cleared,
                'skipped_count': len(skipped)})
    return _commit_payload(idem, payload, 200)


def _validate_collaboration_subject(project_id, subject_type, subject_id):
    if subject_type == 'analysis_report':
        row = db.session.get(TestReport, subject_id)
        if not row or row.is_deleted or row.project_id != project_id:
            return '分析报告不存在或不属于该项目'
    elif subject_type == 'analysis_run':
        row = db.session.get(ShiftLeftAnalysisRun, subject_id)
        if not row or row.project_id != project_id:
            return 'Analysis Run 不存在或不属于该项目'
    elif subject_type == 'finding':
        row = db.session.get(ShiftLeftFinding, subject_id)
        if not row or row.project_id != project_id:
            return 'Finding 不存在或不属于该项目'
    elif subject_type == 'case_review':
        topic = db.session.get(Topic, subject_id)
        project = _case_review_project(topic)
        if not topic or not project or project.id != project_id:
            return '用例评审不存在或不属于该项目'
    elif subject_type == 'topic':
        topic = db.session.get(Topic, subject_id)
        project = _topic_project(topic)
        if not topic:
            return '课题不存在'
        if topic.board == 'case_review':
            return '用例评审课题必须使用 subject_type=case_review'
        if project and int(project.id) != int(project_id or 0):
            return '课题不属于该项目'
        if not project and project_id is not None:
            return '未关联项目的课题不能临时绑定其他项目'
    else:
        return 'subject_type 无效'
    return None


def _case_review_bootstrap(row, access_token):
    """Return a self-contained, machine-readable handoff for any external AI."""
    if row.subject_type != 'case_review':
        return None
    topic_id = int(row.subject_id)
    api_base = request.url_root.rstrip('/')
    context_url = '%s/api/v1/shift-left/case-reviews/%d/context' % (
        api_base, topic_id)
    return {
        'schema_version': 'case-review-bootstrap.v1',
        'api_base_url': api_base,
        'subject': {'type': 'case_review', 'id': topic_id},
        'web_path': '/topics/%d' % topic_id,
        'web_url': '%s/topics/%d' % (api_base, topic_id),
        'agent_identity': None,
        'identity_mode': 'per_submission',
        'participant_session_id': row.id,
        'expires_at': str(row.token_expires_at),
        'max_calls': row.max_calls,
        'scopes': row.scopes_json or [],
        'authorization': {
            'type': 'bearer',
            'header': 'Authorization: Bearer <access_token>',
            'access_token': access_token,
            'browser_required': False,
            'token_reuse': 'Reuse this token for all writes owned by this participant session.',
            'storage_warning': 'Do not store this token in source code, reports, or logs.',
        },
        'endpoints': {
            'context': context_url,
            'cases': '%s/api/v1/shift-left/case-reviews/%d/cases' % (
                api_base, topic_id),
            'reviews': '%s/api/v1/shift-left/case-reviews/%d/reviews' % (
                api_base, topic_id),
            'comments': '%s/api/v1/shift-left/case-reviews/%d/comments' % (
                api_base, topic_id),
            'marks': '%s/api/v1/shift-left/case-reviews/%d/marks' % (
                api_base, topic_id),
        },
        'workflow': [
            'GET context first and obey its scope, open round, mark legend, and links.',
            'GET cases page by page; review only cases returned by this endpoint.',
            'GET reviews to read all currently submitted review records.',
            'POST one evidence-based record with identity, content, and score 1-10.',
            'PATCH or DELETE only records whose owned_by_me flag is true.',
            'PUT node marks only when useful; every write needs a unique Idempotency-Key.',
            'Do not approve or reject unless case_review:decision is explicitly present.',
            'Return a concise summary of findings, residual risks, and submitted actions.',
        ],
        'review_contract': {
            'dimensions': [
                'coverage', 'executability', 'expected_results',
                'priority', 'consistency',
            ],
            'create_body': {
                'identity': '<display identity chosen for this submission>',
                'content': '<evidence-based review in Markdown>',
                'score': '<integer 1-10>',
                'round_id': '<open_round_id from context>',
            },
            'update_body': {
                'identity': '<updated display identity; optional>',
                'content': '<updated review in Markdown; optional>',
                'score': '<integer 1-10 or null; optional>',
            },
            'delete_rule': (
                'DELETE endpoints.reviews/<review_id>; only owned_by_me=true '
                'records may be changed or deleted.'),
            'mark_body': {
                'marks': [{
                    'node_id': 'case:<case_id> or mod:<module_path>',
                    'mark': '<key from context.mark_legend>',
                    'note': '<short evidence>',
                }],
            },
        },
    }


def _topic_bootstrap(row, access_token):
    """Return a machine-readable handoff for one ordinary discussion topic."""
    if row.subject_type != 'topic':
        return None
    topic_id = int(row.subject_id)
    api_base = request.url_root.rstrip('/')
    topic_url = '%s/api/v1/topics/%d' % (api_base, topic_id)
    replies_url = topic_url + '/replies'
    return {
        'schema_version': 'topic-discussion-bootstrap.v1',
        'api_base_url': api_base,
        'subject': {'type': 'topic', 'id': topic_id},
        'web_path': '/topics/%d' % topic_id,
        'web_url': '%s/topics/%d' % (api_base, topic_id),
        'agent_identity': None,
        'identity_mode': 'per_submission',
        'participant_session_id': row.id,
        'expires_at': str(row.token_expires_at),
        'max_calls': row.max_calls,
        'scopes': row.scopes_json or [],
        'authorization': {
            'type': 'bearer',
            'header': 'Authorization: Bearer <access_token>',
            'access_token': access_token,
            'browser_required': False,
            'token_reuse': (
                'Reuse this token for all replies owned by this participant session.'),
            'storage_warning': (
                'Do not store this token in source code, reports, or logs.'),
        },
        'endpoints': {
            'topic': topic_url,
            'replies': replies_url,
            'reply': replies_url,
            'owned_reply': replies_url + '/<reply_id>',
        },
        'workflow': [
            'GET endpoints.topic first and read the topic plus existing replies.',
            'Reply only when it adds relevant evidence, analysis, or a clear question.',
            'POST endpoints.reply with identity, content, and a unique Idempotency-Key.',
            'PATCH or DELETE only replies whose owned_by_me flag is true.',
            'Every PATCH or DELETE also needs a unique Idempotency-Key.',
            'Do not browse other topics or expand beyond this invitation subject.',
            'Return a concise summary of what you read and wrote back to Hub.',
        ],
        'reply_contract': {
            'create_body': {
                'identity': '<display identity chosen for this reply>',
                'content': '<Markdown reply>',
                'reply_to_id': '<optional existing reply id>',
            },
            'update_body': {
                'identity': '<updated display identity; optional>',
                'content': '<updated Markdown reply; optional>',
            },
            'delete_rule': (
                'DELETE endpoints.owned_reply after replacing <reply_id>; '
                'only owned_by_me=true replies may be changed or deleted.'),
        },
    }


def _collaboration_bootstrap(row, access_token):
    return (_case_review_bootstrap(row, access_token)
            or _topic_bootstrap(row, access_token))


@api_bp.route('/collaboration-sessions', methods=['GET'])
def list_collaboration_sessions():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor or actor['type'] == 'collaboration':
        return _error('临时协作 Token 不能读取会话管理信息', 403,
                      'COLLABORATION_SCOPE_DENIED')
    subject_type = str(request.args.get('subject_type') or '').strip()
    if subject_type not in COLLABORATION_SUBJECT_TYPES:
        return _error('subject_type 无效', 400, 'INVALID_SUBJECT_TYPE')
    try:
        subject_id = int(request.args.get('subject_id'))
    except (TypeError, ValueError):
        return _error('subject_id 必须是整数', 400, 'INVALID_SUBJECT_ID')
    if subject_type == 'topic':
        topic = db.session.get(Topic, subject_id)
        if not topic or topic.status == 'deleted':
            return _error('课题不存在', 404, 'TOPIC_NOT_FOUND')
        denied = _require_topic_collaboration_manager(actor, topic)
    else:
        project_id = _collaboration_subject_project_id(
            subject_type, subject_id)
        if not project_id:
            return _error('协作对象不存在或未关联项目', 404,
                          'COLLABORATION_SUBJECT_NOT_FOUND')
        denied = _require_project(actor, project_id)
    if denied:
        return denied
    rows = (CollaborationSession.query.filter_by(
        subject_type=subject_type, subject_id=subject_id,
        parent_invite_id=None)
        .order_by(CollaborationSession.created_at.desc())
        .limit(100).all())
    counts = {}
    if rows:
        counts = dict(db.session.query(
            CollaborationSession.parent_invite_id,
            func.count(CollaborationSession.id),
        ).filter(
            CollaborationSession.parent_invite_id.in_(
                [row.id for row in rows])
        ).group_by(CollaborationSession.parent_invite_id).all())
    items = []
    for row in rows:
        item = row.to_dict()
        item['participant_count'] = int(counts.get(row.id, 0))
        items.append(item)
    return jsonify({'items': items,
                    'total': len(rows)})


@api_bp.route('/collaboration-sessions', methods=['POST'])
def create_collaboration_session():
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor or actor['type'] == 'collaboration':
        return _error('临时协作会话不能创建新的协作会话', 403,
                      'COLLABORATION_CHAIN_DENIED')
    data = request.get_json(silent=True) or {}
    subject_type = str(data.get('subject_type') or '').strip()
    if subject_type not in COLLABORATION_SUBJECT_TYPES:
        return _error('subject_type 无效', 400, 'INVALID_SUBJECT_TYPE')
    try:
        subject_id = int(data.get('subject_id'))
    except (TypeError, ValueError):
        return _error('subject_id 必须是整数', 400, 'INVALID_SUBJECT_ID')
    topic = db.session.get(Topic, subject_id) if subject_type == 'topic' else None
    if subject_type == 'topic':
        if not topic or topic.status == 'deleted':
            return _error('课题不存在', 404, 'TOPIC_NOT_FOUND')
        if topic.board == 'case_review':
            return _error(
                '用例评审课题请使用 subject_type=case_review', 400,
                'TOPIC_SUBJECT_TYPE_MISMATCH')
        denied = _require_topic_collaboration_manager(actor, topic)
        project = _topic_project(topic)
        project_id = project.id if project else None
    else:
        project_id = data.get('project_id')
        if project_id is None:
            project_id = _collaboration_subject_project_id(
                subject_type, subject_id)
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            return _error('无法从协作对象确定 project_id', 400,
                          'COLLABORATION_PROJECT_REQUIRED')
        denied = _require_project(actor, project_id, write=True)
    if denied:
        return denied
    subject_error = _validate_collaboration_subject(
        project_id, subject_type, subject_id)
    if subject_error:
        return _error(subject_error, 400, 'INVALID_COLLABORATION_SUBJECT')
    # This is an optional invitation label for managers. Participant display
    # identity is supplied with each reply/review and never authorizes access.
    agent_identity = str(data.get('agent_identity') or '').strip()
    if subject_type not in ('case_review', 'topic') and not agent_identity:
        return _error('agent_identity 必填', 400, 'AGENT_IDENTITY_REQUIRED')
    if len(agent_identity) > 160:
        return _error('agent_identity 不能超过 160 字符', 400,
                      'AGENT_IDENTITY_TOO_LONG')
    try:
        default_scopes = (
            DEFAULT_CASE_REVIEW_SCOPES
            if subject_type == 'case_review'
            else (DEFAULT_TOPIC_SCOPES if subject_type == 'topic' else None))
        scopes = normalize_scopes(data.get('scopes'), default_scopes)
    except ValueError as exc:
        return _error(str(exc), 400, 'INVALID_COLLABORATION_SCOPE')
    key = str(request.headers.get('Idempotency-Key') or '').strip()
    if not key:
        return _error('写请求必须携带 Idempotency-Key', 400,
                      'IDEMPOTENCY_KEY_REQUIRED')
    if len(key) > 128:
        return _error('Idempotency-Key 不能超过 128 字符', 400,
                      'IDEMPOTENCY_KEY_TOO_LONG')
    request_fingerprint = payload_hash(data)
    existing = CollaborationSession.query.filter_by(
        creator_actor_key=actor['key'], create_idempotency_key=key).first()
    if existing:
        if existing.create_request_hash != request_fingerprint:
            return _error('同一 Idempotency-Key 已用于不同请求', 409,
                          'IDEMPOTENCY_KEY_REUSED')
        payload = existing.to_dict()
        payload.update({
            'invitation_code': None,
            'invitation_returned_once': True,
        })
        return jsonify(payload), 200
    try:
        invite_minutes = int(data.get('invitation_ttl_minutes') or 30)
        token_minutes = int(data.get('token_ttl_minutes') or 120)
        max_calls = min(max(int(data.get('max_calls') or 500), 1), 5000)
    except (TypeError, ValueError):
        return _error('TTL/max_calls 必须是整数', 400,
                      'INVALID_COLLABORATION_LIMIT')
    if (not 5 <= invite_minutes <= COLLABORATION_MAX_TTL_MINUTES
            or not 5 <= token_minutes <= COLLABORATION_MAX_TTL_MINUTES):
        return _error('邀请和Token有效期必须在5分钟到72小时之间', 400,
                      'COLLABORATION_TTL_OUT_OF_RANGE')

    invitation_code = generate_invitation_code()
    now = now_cst_naive()
    row = CollaborationSession(
        project_id=(int(project_id) if project_id is not None else None),
        subject_type=subject_type,
        subject_id=subject_id,
        agent_identity=agent_identity[:160],
        scopes_json=scopes,
        invitation_hash=secret_hash(invitation_code),
        status='pending',
        invitation_expires_at=now + timedelta(minutes=invite_minutes),
        token_ttl_seconds=token_minutes * 60,
        max_calls=max_calls,
        created_by_type=actor['type'],
        created_by_id=actor['id'],
        created_by_name=actor['name'],
        creator_actor_key=actor['key'],
        create_idempotency_key=key,
        create_request_hash=request_fingerprint,
    )
    db.session.add(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return _error('协作会话创建发生幂等冲突，请重试查询', 409,
                      'IDEMPOTENCY_CONFLICT')
    db.session.add(CollaborationSessionEvent(
        session_id=row.id,
        event_type='created',
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        detail_json={'subject_type': subject_type, 'subject_id': subject_id,
                     'scopes': scopes},
    ))
    _add_audit('create', 'collaboration_session', row.id,
               '%s:%s' % (subject_type, subject_id), actor,
               {'agent_identity': agent_identity, 'scopes': scopes,
                'invitation_expires_at': str(row.invitation_expires_at)})
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('create collaboration session failed')
        return _error('协作会话创建失败', 500,
                      'COLLABORATION_CREATE_FAILED')
    payload = row.to_dict()
    payload.update({
        'invitation_code': invitation_code,
        'exchange_url': '/api/v1/collaboration-sessions/exchange',
        'invitation_path': '/developer-ai/collaborate',
        'invitation_returned_once': True,
    })
    return jsonify(payload), 201


@api_bp.route('/collaboration-sessions/preview', methods=['POST'])
def preview_collaboration_session():
    """Resolve the subject page for an existing link without issuing a token."""
    disabled = _disabled_response()
    if disabled:
        return disabled
    data = request.get_json(silent=True) or {}
    invitation_code = str(data.get('invitation_code') or '').strip()
    if not invitation_code.startswith('hub_ci_'):
        return _error('邀请码无效', 400, 'INVALID_INVITATION_CODE')
    row = CollaborationSession.query.filter_by(
        invitation_hash=secret_hash(invitation_code),
        parent_invite_id=None,
    ).first()
    if not row:
        return _error('邀请码无效', 404, 'INVITATION_NOT_FOUND')
    now = now_cst_naive()
    if row.invitation_expires_at <= now:
        return _error('协作链接已过期', 410, 'INVITATION_EXPIRED')
    if row.status not in ('pending', 'active'):
        return _error('协作链接已撤销或结束', 409,
                      'INVITATION_NOT_ACTIVE')
    if row.subject_type not in ('topic', 'case_review'):
        return _error('该协作对象没有帖子页面', 404,
                      'COLLABORATION_SUBJECT_PAGE_UNAVAILABLE')
    topic = db.session.get(Topic, row.subject_id)
    if not topic or topic.status == 'deleted':
        return _error('课题不存在', 404, 'TOPIC_NOT_FOUND')
    web_path = '/topics/%d' % topic.id
    return jsonify({
        'subject': {
            'type': row.subject_type,
            'id': topic.id,
            'title': topic.title,
        },
        'web_path': web_path,
        'web_url': request.url_root.rstrip('/') + web_path,
        'invitation_expires_at': str(row.invitation_expires_at),
        'token_issued': False,
    })


@api_bp.route('/collaboration-sessions/exchange', methods=['POST'])
def exchange_collaboration_session():
    disabled = _disabled_response()
    if disabled:
        return disabled
    data = request.get_json(silent=True) or {}
    invitation_code = str(data.get('invitation_code') or '').strip()
    if not invitation_code.startswith('hub_ci_'):
        return _error('邀请码无效', 400, 'INVALID_INVITATION_CODE')
    row = (CollaborationSession.query
           .filter_by(invitation_hash=secret_hash(invitation_code))
           .with_for_update().first())
    if not row:
        return _error('邀请码无效', 404, 'INVITATION_NOT_FOUND')
    now = now_cst_naive()
    if row.invitation_expires_at <= now:
        return _error('协作链接已过期', 410, 'INVITATION_EXPIRED')
    if row.status not in ('pending', 'active'):
        return _error('邀请码已使用或已撤销', 409,
                      'INVITATION_ALREADY_USED')
    if row.subject_type not in ('case_review', 'topic'):
        token_rotated = row.status == 'active'
        access_token = generate_access_token()
        row.access_token_hash = secret_hash(access_token)
        row.status = 'active'
        row.exchanged_at = row.exchanged_at or now
        row.last_activity_at = now
        row.token_expires_at = now + timedelta(
            seconds=row.token_ttl_seconds or 7200)
        db.session.add(CollaborationSessionEvent(
            session_id=row.id,
            event_type=('token_rotated' if token_rotated else 'exchanged'),
            actor_type='collaboration',
            actor_id=row.id,
            actor_name=row.agent_identity,
            detail_json={
                'token_expires_at': str(row.token_expires_at),
                'previous_token_invalidated': token_rotated,
            },
        ))
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception(
                'exchange collaboration session failed')
            return _error('邀请码兑换失败', 500,
                          'COLLABORATION_EXCHANGE_FAILED')
        return jsonify({
            'access_token': access_token,
            'expires_at': str(row.token_expires_at),
            'subject': {'type': row.subject_type, 'id': row.subject_id},
            'agent_identity': row.agent_identity,
            'scopes': row.scopes_json or [],
            'token_rotated': token_rotated,
            'previous_token_invalidated': token_rotated,
            'link_expires_at': str(row.invitation_expires_at),
            'bootstrap': _collaboration_bootstrap(row, access_token),
        })
    access_token = generate_access_token()
    token_expires_at = now + timedelta(
        seconds=row.token_ttl_seconds or 7200)
    participant_nonce = generate_invitation_code()
    participant = CollaborationSession(
        project_id=row.project_id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        # Display identity belongs to each submission, not the invitation or
        # bearer token. Keep the participant session intentionally anonymous.
        agent_identity='',
        scopes_json=list(row.scopes_json or []),
        invitation_hash=secret_hash('participant:' + participant_nonce),
        access_token_hash=secret_hash(access_token),
        status='active',
        invitation_expires_at=row.invitation_expires_at,
        token_expires_at=token_expires_at,
        token_ttl_seconds=row.token_ttl_seconds or 7200,
        max_calls=row.max_calls or 500,
        call_count=0,
        created_by_type=row.created_by_type,
        created_by_id=row.created_by_id,
        created_by_name=row.created_by_name,
        creator_actor_key='invite:%s' % row.id,
        create_idempotency_key='participant:%s' % secret_hash(
            participant_nonce)[:32],
        create_request_hash=payload_hash({
            'invite_id': row.id,
            'participant_nonce': participant_nonce,
        }),
        parent_invite_id=row.id,
        exchanged_at=now,
        last_activity_at=now,
    )
    db.session.add(participant)
    db.session.flush()
    row.status = 'active'
    row.exchanged_at = row.exchanged_at or now
    row.last_activity_at = now
    db.session.add(CollaborationSessionEvent(
        session_id=row.id,
        event_type='participant_issued',
        actor_type='collaboration',
        actor_id=participant.id,
        actor_name='',
        detail_json={
            'participant_session_id': participant.id,
            'token_expires_at': str(token_expires_at),
            'link_expires_at': str(row.invitation_expires_at),
            'previous_token_invalidated': False,
        },
    ))
    db.session.add(CollaborationSessionEvent(
        session_id=participant.id,
        event_type='token_issued',
        actor_type='collaboration',
        actor_id=participant.id,
        actor_name='',
        detail_json={'invite_id': row.id,
                     'token_expires_at': str(token_expires_at)},
    ))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('exchange collaboration session failed')
        return _error('邀请码兑换失败', 500,
                      'COLLABORATION_EXCHANGE_FAILED')
    return jsonify({
        'access_token': access_token,
        'expires_at': str(token_expires_at),
        'subject': {'type': row.subject_type, 'id': row.subject_id},
        'agent_identity': None,
        'identity_mode': 'per_submission',
        'participant_session_id': participant.id,
        'invite_id': row.id,
        'independent_participant': True,
        'scopes': row.scopes_json or [],
        'token_rotated': False,
        'previous_token_invalidated': False,
        'link_expires_at': str(row.invitation_expires_at),
        'bootstrap': _collaboration_bootstrap(participant, access_token),
    })


@api_bp.route('/collaboration-sessions/<int:session_id>', methods=['GET'])
def get_collaboration_session(session_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if actor['type'] == 'collaboration':
        return _error('临时协作 Token 不能读取会话管理信息', 403,
                      'COLLABORATION_SCOPE_DENIED')
    row = db.session.get(CollaborationSession, session_id)
    if not row:
        return _error('协作会话不存在', 404, 'COLLABORATION_SESSION_NOT_FOUND')
    denied = _require_collaboration_session_manager(actor, row)
    if denied:
        return denied
    payload = row.to_dict()
    payload['events'] = [event.to_dict() for event in (
        CollaborationSessionEvent.query.filter_by(session_id=row.id)
        .order_by(CollaborationSessionEvent.created_at.asc()).all())]
    return jsonify(payload)


@api_bp.route('/collaboration-sessions/<int:session_id>/revoke',
              methods=['POST'])
def revoke_collaboration_session(session_id):
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if actor['type'] == 'collaboration':
        return _error('临时协作 Token 不能撤销会话', 403,
                      'COLLABORATION_SCOPE_DENIED')
    row = db.session.get(CollaborationSession, session_id)
    if not row:
        return _error('协作会话不存在', 404, 'COLLABORATION_SESSION_NOT_FOUND')
    denied = _require_collaboration_session_manager(actor, row, write=True)
    if denied:
        return denied
    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    data = request.get_json(silent=True) or {}
    now = now_cst_naive()
    row.status = 'revoked'
    row.revoked_at = now
    row.revoked_by = actor['name']
    row.revoke_reason = str(data.get('reason') or 'manual_revoke')[:500]
    children = CollaborationSession.query.filter_by(
        parent_invite_id=row.id).all()
    for child in children:
        if child.status not in ('revoked', 'completed'):
            child.status = 'revoked'
            child.revoked_at = now
            child.revoked_by = actor['name']
            child.revoke_reason = 'parent_invite_revoked'
    db.session.add(CollaborationSessionEvent(
        session_id=row.id,
        event_type='revoked',
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        detail_json={'reason': row.revoke_reason},
    ))
    _add_audit('revoke', 'collaboration_session', row.id,
               '%s:%s' % (row.subject_type, row.subject_id), actor,
               {'reason': row.revoke_reason})
    return _commit_payload(idem, row.to_dict(), 200)


@api_bp.route('/collaboration-sessions/<int:session_id>/deadline',
              methods=['PATCH'])
def update_collaboration_session_deadline(session_id):
    """Extend a supported collaboration session without rotating its secret.

    The deadline belongs to the reusable invitation. Already issued participant
    tokens keep their own expiry; an Agent can exchange the valid link again
    when it needs a new independent token.
    """
    disabled = _disabled_response()
    if disabled:
        return disabled
    actor = _actor()
    if not actor:
        return _error('未登录', 401, 'AUTH_REQUIRED')
    if actor['type'] == 'collaboration':
        return _error('临时协作 Token 不能修改会话截止时间', 403,
                      'COLLABORATION_SCOPE_DENIED')
    row = db.session.get(CollaborationSession, session_id)
    if not row:
        return _error('协作会话不存在', 404,
                      'COLLABORATION_SESSION_NOT_FOUND')
    denied = _require_collaboration_session_manager(actor, row, write=True)
    if denied:
        return denied
    if row.subject_type not in ('case_review', 'topic'):
        return _error('当前资源类型不支持协作链接延期', 400,
                      'COLLABORATION_DEADLINE_UNSUPPORTED')
    if row.status in ('revoked', 'completed'):
        return _error('已撤销或已完成的会话不能延期', 409,
                      'COLLABORATION_SESSION_TERMINAL')
    if row.max_calls and (row.call_count or 0) >= row.max_calls:
        return _error('调用额度已用尽，不能仅通过延期恢复', 409,
                      'COLLABORATION_CALLS_EXHAUSTED')

    idem, replay = _idempotency_begin(actor)
    if replay:
        return replay
    data = request.get_json(silent=True) or {}
    now = now_cst_naive()
    try:
        deadline = _parse_collaboration_deadline(data.get('expires_at'), now)
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), 400, 'INVALID_COLLABORATION_DEADLINE')

    if row.parent_invite_id is not None:
        db.session.rollback()
        return _error('参与 Token 会话不能作为邀请链接延期', 409,
                      'COLLABORATION_PARTICIPANT_NOT_EXTENDABLE')
    if row.status in ('pending', 'active'):
        target = 'invitation_expires_at'
        old_deadline = row.invitation_expires_at
        row.invitation_expires_at = deadline
    else:
        db.session.rollback()
        return _error('当前会话状态不支持延期', 409,
                      'COLLABORATION_SESSION_NOT_EXTENDABLE')

    detail = {
        'target': target,
        'old_expires_at': str(old_deadline) if old_deadline else None,
        'expires_at': str(deadline),
        'link_rotated': False,
    }
    db.session.add(CollaborationSessionEvent(
        session_id=row.id,
        event_type='deadline_updated',
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        detail_json=detail,
    ))
    _add_audit('update_deadline', 'collaboration_session', row.id,
               '%s:%s' % (row.subject_type, row.subject_id), actor, detail)
    payload = row.to_dict()
    payload.update({
        'deadline_field': target,
        'expires_at': str(deadline),
        'link_rotated': False,
    })
    return _commit_payload(idem, payload, 200)
