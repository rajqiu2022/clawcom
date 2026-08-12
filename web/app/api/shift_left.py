"""测试左移、代码分析报告评审与临时 Developer AI 协作 API。

首期仅提供新增数据面；不会自动触发、修改或接管现有 Workflow、报告、
用例评审和测试计划流程。生产可通过 SHIFT_LEFT_ENABLED 灰度开启。
"""

import json
from datetime import timedelta

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
    TestCase,
    TestCaseLibrary,
    TestIteration,
    TestReport,
    Topic,
    WorkflowOperationIdempotency,
    WorkflowRun,
)
from app.services.shift_left import (
    COLLABORATION_SUBJECT_TYPES,
    DEFAULT_CASE_REVIEW_SCOPES,
    EVIDENCE_LEVELS,
    FINDING_SEVERITIES,
    FINDING_STATES,
    build_baseline_fingerprint,
    canonical_json,
    generate_access_token,
    generate_invitation_code,
    normalize_scopes,
    now_cst_naive,
    payload_hash,
    secret_hash,
    transition_target,
    validate_transition_preconditions,
)
from app.services.case_mindmap import MARKS, normalize_mark


ANALYSIS_RESULT_STATUSES = {
    'completed', 'completed_with_findings', 'blocked', 'failed', 'cancelled',
}


def _error(message, status=400, code='INVALID_REQUEST', **extra):
    payload = {'error': message, 'code': code}
    payload.update(extra)
    return jsonify(payload), status


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
            'name': collaboration.agent_identity,
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
        identity = str(actor['collaboration'].agent_identity or '').strip().lower()
        return 'developer_ai:%s' % identity
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


def _idempotency_begin(actor):
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


@api_bp.route('/shift-left/findings', methods=['GET'])
def list_shift_left_findings():
    disabled = _disabled_response()
    if disabled:
        return disabled
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
    return jsonify(payload)


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
    return None


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
    topic_payload = topic.to_dict()
    topic_payload.pop('review_rounds', None)
    return jsonify({
        'topic': topic_payload,
        'project': {'id': project.id, 'name': project.name},
        'library': library.to_dict(with_cases=False, with_mindmap=False),
        'scope': {'module_paths': paths, 'case_ids': sorted(case_ids)},
        'rounds': [row.to_dict(with_comments=True) for row in rounds],
        'open_round_id': next((row.id for row in reversed(rounds)
                               if row.status == 'pending'), None),
        'marks': [row.to_dict() for row in marks],
        'mark_legend': {key: dict(value) for key, value in MARKS.items()},
        'links': {
            'cases': '/api/v1/shift-left/case-reviews/%d/cases' % topic.id,
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
        author_name=actor['name'][:100],
        author_claw_id=actor['id'] if actor['type'] == 'claw' else None,
        author_user_id=actor['id'] if actor['type'] == 'user' else None,
        content=content,
        verdict=verdict,
        score=score,
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
    return _commit_payload(idem, row.to_dict(), 201)


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
    else:
        return 'subject_type 无效'
    return None


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
    project_id = data.get('project_id')
    if project_id is None:
        project_id = _collaboration_subject_project_id(subject_type, subject_id)
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
    agent_identity = str(data.get('agent_identity') or '').strip()
    if not agent_identity:
        return _error('agent_identity 必填', 400, 'AGENT_IDENTITY_REQUIRED')
    try:
        default_scopes = (DEFAULT_CASE_REVIEW_SCOPES
                          if subject_type == 'case_review' else None)
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
        invite_minutes = min(max(int(data.get('invitation_ttl_minutes') or 30), 5), 1440)
        token_minutes = min(max(int(data.get('token_ttl_minutes') or 120), 5), 1440)
        max_calls = min(max(int(data.get('max_calls') or 500), 1), 5000)
    except (TypeError, ValueError):
        return _error('TTL/max_calls 必须是整数', 400,
                      'INVALID_COLLABORATION_LIMIT')

    invitation_code = generate_invitation_code()
    now = now_cst_naive()
    row = CollaborationSession(
        project_id=int(project_id),
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
    effective = row.effective_status(now_cst_naive())
    if effective == 'expired':
        return _error('邀请码已过期', 410, 'INVITATION_EXPIRED')
    if row.status != 'pending':
        return _error('邀请码已使用或已撤销', 409,
                      'INVITATION_ALREADY_USED')

    access_token = generate_access_token()
    now = now_cst_naive()
    row.access_token_hash = secret_hash(access_token)
    row.status = 'active'
    row.exchanged_at = now
    row.last_activity_at = now
    row.token_expires_at = now + timedelta(seconds=row.token_ttl_seconds or 7200)
    db.session.add(CollaborationSessionEvent(
        session_id=row.id,
        event_type='exchanged',
        actor_type='collaboration',
        actor_id=row.id,
        actor_name=row.agent_identity,
        detail_json={'token_expires_at': str(row.token_expires_at)},
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
        'expires_at': str(row.token_expires_at),
        'subject': {'type': row.subject_type, 'id': row.subject_id},
        'agent_identity': row.agent_identity,
        'scopes': row.scopes_json or [],
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
    denied = _require_project(actor, row.project_id)
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
    denied = _require_project(actor, row.project_id, write=True)
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
