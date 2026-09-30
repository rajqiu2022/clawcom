"""Code analysis specialty: plan -> candidates -> human feedback -> knowledge."""
import json
import re
from flask import jsonify, request, current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app import db
from app.api import api_bp
from app.api.knowledge_notebooks import (_actor, _can_access_project, _create_revision)
from app.api.shift_left import (_idempotency_begin, _commit_payload, _new_finding_event,
                               _add_audit)
from app.models import (CodeAnalysisProject, CodeAnalysisJob, CodeAnalysisDecision,
    SharedResourcePolicy, KnowledgeEntry, OpenClawInstance, Project, Skill,
    ShiftLeftAnalysisRun, ShiftLeftAnalysisFinding, ShiftLeftFinding,
    ShiftLeftFindingFeedback, TestPlan, TestPlanReport, TestReport, WorkflowDefinition, WorkflowRun,
    WorkflowRunStep)
from app.services import code_analysis as domain
from app.services import resource_sharing as sharing
from app.services.shift_left import now_cst_naive, FINDING_SEVERITIES


def error(code, message, status=400):
    return jsonify({'code': code, 'error': message}), status


def access(project_id, write=False):
    actor = _actor()
    if (not sharing.enabled()):
        return None, error('CODE_ANALYSIS_DISABLED', '代码分析专项尚未启用', 404)
    if (not actor or not _can_access_project(actor, project_id)
            or (write and actor['type'] == 'user' and actor['user'].role == 'guest')):
        return None, error('PROJECT_ACCESS_DENIED', '无权访问该项目', 403)
    return actor, None


def integer(value):
    if isinstance(value, bool):
        raise ValueError('ID 必须为正整数')
    number = int(value)
    if number < 1 or str(number) != str(value):
        raise ValueError('ID 必须为正整数')
    return number


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('请求必须为 JSON 对象')
    return data


def config_payload(config):
    return dict({field: getattr(config, field) for field in (
        'project_id', 'knowledge_id', 'skill_id', 'executor_claw_id',
        'definition_id', 'repository_url', 'source_ref', 'revision')},
        knowledge_ids=config.knowledge_ids_json or [])


def job_payload(job, compact=False):
    row = job.analysis
    workflow = db.session.get(WorkflowRun, row.workflow_run_id) if row.workflow_run_id else None
    payload = dict(row.to_dict(), plan_id=job.plan_id, executor_claw_id=job.executor_claw_id,
                workflow_status=workflow.status if workflow else None,
                snapshot=job.snapshot_json)
    if compact:
        payload.pop('context_snapshot', None)
        payload['snapshot'] = {k: job.snapshot_json[k] for k in ('source_ref', 'baseline_ref')}
        payload['snapshot']['plan'] = {'id': job.plan_id, 'name': job.snapshot_json['plan']['name']}
    return payload


@api_bp.route('/code-analysis/options', methods=['GET'])
def code_analysis_options():
    try:
        pid = integer(request.args.get('project_id'))
    except (ValueError, TypeError):
        return error('INVALID_PROJECT', '请选择项目')
    actor, denied = access(pid)
    if denied:
        return denied
    config = db.session.get(CodeAnalysisProject, pid)
    plans = TestPlan.query.filter_by(project_id=pid).order_by(TestPlan.id.desc()).all()
    skills = [s for s in Skill.query.options(defer(Skill.template_content)).order_by(Skill.id).all()
              if sharing.resource_readable('skill', s, actor)]
    entries = [e for e in KnowledgeEntry.query.options(defer(KnowledgeEntry.content)).filter(KnowledgeEntry.archived_at.is_(None)).all()
               if sharing.resource_readable('knowledge', e, actor)]
    return jsonify({'config': config_payload(config) if config else None,
        'plans': [{'id': p.id, 'name': p.name, 'status': p.status} for p in plans],
        'agents': [{'id': c.id, 'name': c.name} for c in OpenClawInstance.query.filter(
            OpenClawInstance.project_id == pid, OpenClawInstance.status != 'deleted').all()],
        'skills': [{'id': s.id, 'title': s.display_name, 'scope': s.scope} for s in skills],
        'knowledge': [{'id': e.id, 'title': e.title, 'project_id': e.project_id,
                       'revision': int(e.current_revision or 0),
                       'sharing': sharing.policy_payload(sharing.policy_for('knowledge', e.id))
                       if sharing.policy_for('knowledge', e.id) else None} for e in entries]})


@api_bp.route('/code-analysis/projects/<int:pid>/initialize', methods=['POST'])
def initialize_code_analysis(pid):
    actor, denied = access(pid, write=True)
    if denied:
        return denied
    if not db.session.get(Project, pid):
        return error('PROJECT_NOT_FOUND', '项目不存在', 404)
    # Serialize singleton creation across projects on MariaDB; no GET creates data.
    Project.query.order_by(Project.id).with_for_update().first()
    config, general = domain.initialize(pid, actor)
    db.session.commit()
    return jsonify({'config': config_payload(config), 'general_knowledge_id': general.id})


@api_bp.route('/code-analysis/projects/<int:pid>', methods=['PUT'])
def save_code_analysis_project(pid):
    actor, denied = access(pid, write=True)
    if denied:
        return denied
    config = CodeAnalysisProject.query.filter_by(project_id=pid).with_for_update().first()
    if not config:
        return error('INITIALIZE_REQUIRED', '请先初始化经验库', 409)
    try:
        data = body()
        if integer(data.get('expected_revision')) != config.revision:
            return error('REVISION_CONFLICT', '配置已更新，请刷新后重试', 409)
        executor = db.session.get(OpenClawInstance, integer(data.get('executor_claw_id')))
        skill = db.session.get(Skill, integer(data.get('skill_id')))
        if not executor or executor.project_id != pid or executor.status == 'deleted':
            raise ValueError('执行 Agent 必须属于当前项目')
        if not skill or not sharing.resource_readable('skill', skill, actor):
            raise ValueError('初始 Skill 不存在或不可读')
        if not sharing.resource_readable('skill', skill, dict(type='claw', id=executor.id,
                name=executor.name, claw=executor, user=None)):
            raise ValueError('执行 Agent 无权拉取该 Skill，请调整共享范围')
        repo = domain.repository(data.get('repository_url'))
        ref = domain.git_ref(data.get('source_ref') or 'main')
        knowledge = db.session.get(KnowledgeEntry, integer(data.get('knowledge_id') or config.knowledge_id))
        if not knowledge or knowledge.project_id != pid or knowledge.archived_at:
            raise ValueError('至少关联一篇本项目的有效知识库')
        extra_ids = data.get('knowledge_ids', [])
        if not isinstance(extra_ids, list) or len(extra_ids) > 30:
            raise ValueError('补充知识必须是最多 30 项的 ID 数组')
        extra_ids = sorted(set(integer(k) for k in extra_ids))
        for kid in extra_ids:
            extra = db.session.get(KnowledgeEntry, kid)
            if not extra or extra.archived_at or not sharing.resource_readable('knowledge', extra,
                    dict(type='claw', id=executor.id, name=executor.name, claw=executor, user=None)):
                raise ValueError('补充知识不存在、已归档或未共享给执行 Agent 的项目')
        if not sharing.policy_for('knowledge', knowledge.id):
            db.session.add(SharedResourcePolicy(resource_kind='knowledge', resource_id=knowledge.id,
                owner_project_id=pid, scope='project', created_by=actor['name']))
            if not knowledge.current_revision:
                _create_revision(knowledge, actor, knowledge.title, knowledge.content or '',
                    '关联代码分析专项，保留原文', 0, 'code-analysis-link:%s' % knowledge.id)
            sharing.invalidate()
        definition = domain.default_definition(pid, actor, executor.id)
        db.session.flush()
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return error('INVALID_CONFIG', str(exc))
    config.skill_id, config.executor_claw_id = skill.id, executor.id
    config.knowledge_id, config.definition_id = knowledge.id, definition.id
    config.knowledge_ids_json = extra_ids
    config.repository_url, config.source_ref = repo, ref
    config.revision += 1
    _add_audit('configure', 'code_analysis_project', pid, repo, actor,
               {'skill_id': skill.id, 'executor_claw_id': executor.id})
    db.session.commit()
    return jsonify(config_payload(config))


@api_bp.route('/code-analysis/runs', methods=['POST'])
def create_code_analysis_job():
    try:
        data = body()
        plan = db.session.get(TestPlan, integer(data.get('plan_id')))
        if not plan:
            return error('PLAN_NOT_FOUND', '测试计划不存在', 404)
        actor, denied = access(plan.project_id, write=True)
        if denied:
            return denied
        config = db.session.get(CodeAnalysisProject, plan.project_id)
        if not config or not config.skill_id or not config.executor_claw_id:
            return error('CONFIG_REQUIRED', '请先配置初始 Skill 和执行 Agent', 409)
        idem, replay = _idempotency_begin(dict(actor, key='%s:%s' % (actor['type'], actor['id'])))
        if replay:
            return replay
        # A retained request key is also protected beyond the generic 7-day receipt.
        key = '%s:%s:%s' % (actor['type'], actor['id'], request.headers['Idempotency-Key'])
        if len(key) > 128:
            raise ValueError('Idempotency-Key 过长')
        fingerprint = domain.digest(data)
        existing = CodeAnalysisJob.query.filter_by(request_key=key).first()
        if existing:
            if existing.request_hash != fingerprint:
                return error('IDEMPOTENCY_KEY_REUSED', '幂等键已用于不同分析', 409)
            return _commit_payload(idem, job_payload(existing))
        baseline = domain.git_ref(data.get('baseline_ref') or '')
        skill = db.session.get(Skill, config.skill_id)
        executor = db.session.get(OpenClawInstance, config.executor_claw_id)
        executor_actor = dict(type='claw', id=executor.id, name=executor.name, claw=executor, user=None)
        if not sharing.resource_readable('skill', skill, executor_actor):
            raise ValueError('初始 Skill 已不可读，需更新配置')
        project_page = db.session.get(KnowledgeEntry, config.knowledge_id)
        general = KnowledgeEntry.query.filter_by(category='code_analysis_general', project_id=None).first()
        if not general or general.archived_at or not sharing.resource_readable('knowledge', general, executor_actor):
            raise ValueError('通用经验库已归档或未共享给执行 Agent，请检查共享配置')
        tasks = [t.to_dict() for t in plan.tasks.order_by('id').all()]
        # Use plan fields explicitly: no credentials or unrelated reports enter a prompt.
        plan_snapshot = {'id': plan.id, 'name': plan.name, 'description': plan.description or '',
                         'iteration_id': plan.iteration_id, 'version_name': plan.version_name,
                         'updated_at': str(plan.updated_at),
                         'tasks': [{'id': t['id'], 'name': t['name'], 'description': t.get('description') or ''}
                                   for t in tasks]}
        prior = (CodeAnalysisDecision.query.join(ShiftLeftFinding,
                 ShiftLeftFinding.id == CodeAnalysisDecision.finding_id)
                 .filter(ShiftLeftFinding.project_id == plan.project_id)
                 .order_by(CodeAnalysisDecision.id.desc()).limit(30).all())
        if not project_page or project_page.archived_at or not sharing.resource_readable('knowledge', project_page, executor_actor):
            raise ValueError('项目知识库已归档或不可读，请更新关联')
        pages = {p.id: p for p in (general, project_page) if p}
        for kid in config.knowledge_ids_json or []:
            extra = db.session.get(KnowledgeEntry, kid)
            if not extra or extra.archived_at or not sharing.resource_readable('knowledge', extra, executor_actor):
                raise ValueError('补充知识已撤销共享、归档或失效，请更新关联')
            pages[kid] = extra
        snapshot = {'project_id': plan.project_id, 'config_revision': config.revision,
            'plan': plan_snapshot, 'repository_url': config.repository_url,
            'source_ref': config.source_ref, 'baseline_ref': baseline,
            'read_only': True, 'skill': domain.skill_snapshot(skill),
            'knowledge': [domain.knowledge_snapshot(p) for p in pages.values()],
            'feedback': [dict(domain.decision_payload(d), finding=d.snapshot_json['finding']) for d in prior]}
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 1024 * 1024:
            raise ValueError('分析上下文超过 1MB，请收窄计划范围或知识正文')
        row = ShiftLeftAnalysisRun(project_id=plan.project_id, iteration_id=plan.iteration_id,
            baseline_fingerprint=domain.digest({'request_key': key, 'snapshot': snapshot}),
            baseline_json={'baseline_ref': baseline, 'source_ref': config.source_ref},
            context_snapshot_json=snapshot, created_by=actor['name'], updated_by=actor['name'])
        db.session.add(row)
        db.session.flush()
        job = CodeAnalysisJob(analysis_run_id=row.id, plan_id=plan.id,
            executor_claw_id=config.executor_claw_id, definition_id=config.definition_id,
            snapshot_json=snapshot, request_key=key, request_hash=fingerprint)
        db.session.add(job)
        db.session.flush()
        _add_audit('create', 'code_analysis', row.id, plan.name, actor, {'plan_id': plan.id})
        return _commit_payload(idem, job_payload(job), 201)
    except (ValueError, TypeError, KeyError) as exc:
        db.session.rollback()
        return error('INVALID_ANALYSIS', str(exc))


def get_job(jid, write=False):
    job = db.session.get(CodeAnalysisJob, jid)
    if not job:
        return None, None, error('ANALYSIS_NOT_FOUND', '分析记录不存在', 404)
    actor, denied = access(job.analysis.project_id, write)
    return job, actor, denied


def start_operation(job, actor, decision=None):
    from app.api.workflows import create_workflow_run_from_data
    if decision and decision.learning_run_id:
        return db.session.get(WorkflowRun, decision.learning_run_id), None
    if not decision and job.analysis.workflow_run_id:
        return db.session.get(WorkflowRun, job.analysis.workflow_run_id), None
    executor = db.session.get(OpenClawInstance, job.executor_claw_id)
    if not executor or executor.status == 'deleted' or executor.project_id != job.analysis.project_id:
        return None, error('EXECUTOR_UNAVAILABLE', '执行 Agent 已删除或不再属于本项目', 409)
    executor_actor = dict(type='claw', id=executor.id, name=executor.name, claw=executor, user=None)
    skill = db.session.get(Skill, job.snapshot_json['skill']['id'])
    if not skill or not sharing.resource_readable('skill', skill, executor_actor):
        return None, error('RESOURCE_ACCESS_REVOKED', '初始 Skill 已撤销共享或失效', 409)
    for source in job.snapshot_json['knowledge']:
        page = db.session.get(KnowledgeEntry, source['id'])
        if not page or page.archived_at or not sharing.resource_readable('knowledge', page, executor_actor):
            return None, error('RESOURCE_ACCESS_REVOKED', '引用知识已撤销共享、归档或失效', 409)
    operation = 'learn' if decision else 'analyze'
    contract = dict(job.snapshot_json, operation=operation, analysis_run_id=job.analysis_run_id)
    contract['result_api'] = ('/api/v1/code-analysis/decisions/%s/learning-result' % decision.id
        if decision else '/api/v1/code-analysis/runs/%s/result' % job.analysis_run_id)
    if decision:
        contract['feedback'] = dict(domain.decision_payload(decision), evidence=decision.snapshot_json)
        page = db.session.get(KnowledgeEntry, decision.snapshot_json['knowledge_id'])
        contract['project_knowledge'] = domain.knowledge_snapshot(page)
    # Project-authorized launchers can use the specialty template without a
    # manager role, active Plan/Mission or additional per-Flow ACL assignment.
    try:
        definition = domain.default_definition(job.analysis.project_id, actor, job.executor_claw_id)
    except ValueError as exc:
        db.session.rollback()
        return None, error('DEFINITION_MISMATCH', str(exc), 409)
    if definition.id != job.definition_id:
        return None, error('DEFINITION_MISMATCH', '分析模板归属不一致', 409)
    db.session.commit()
    response = current_app.make_response(create_workflow_run_from_data({
        'definition_id': job.definition_id, 'project_id': job.analysis.project_id,
        'worker_claw_id': job.executor_claw_id,
        'executor_claw_ids': [job.executor_claw_id],
        'idempotency_key': 'code-analysis:%s:%s' % (operation, decision.id if decision else job.analysis_run_id),
        'trigger_source': 'code_analysis',
        'run_name': ('反馈学习 #%s' % decision.id if decision else
                     '代码分析 #%s · Plan #%s' % (job.analysis_run_id, job.plan_id)),
        'start_vars': {'executor_claw_id': job.executor_claw_id, 'code_analysis': contract},
    }, internal_idempotency=True, trusted_project_dispatch=True))
    if response.status_code >= 400:
        return None, response
    payload = response.get_json()
    rid = payload.get('id') or (payload.get('run') or {}).get('id')
    run = db.session.get(WorkflowRun, rid)
    if not run:
        return None, error('RUN_RECEIPT_INVALID', 'Workflow 没有返回有效 Run ID', 502)
    if decision:
        decision.learning_run_id, decision.learning_status = run.id, 'dispatched'
    else:
        job.analysis.workflow_run_id = run.id
    db.session.commit()
    return run, None


@api_bp.route('/code-analysis/runs/<int:jid>/start', methods=['POST'])
def start_code_analysis(jid):
    job, actor, denied = get_job(jid, True)
    if denied:
        return denied
    run, failed = start_operation(job, actor)
    if failed:
        return failed
    return jsonify(dict(job_payload(job), workflow_run_id=run.id))


@api_bp.route('/code-analysis/runs', methods=['GET'])
def list_code_analysis_jobs():
    try:
        pid = integer(request.args.get('project_id'))
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        return error('INVALID_PROJECT', '请选择项目')
    actor, denied = access(pid)
    if denied:
        return denied
    query = CodeAnalysisJob.query.join(ShiftLeftAnalysisRun).filter(ShiftLeftAnalysisRun.project_id == pid)
    if request.args.get('plan_id'):
        try:
            query = query.filter(CodeAnalysisJob.plan_id == integer(request.args['plan_id']))
        except (ValueError, TypeError):
            return error('INVALID_PLAN', '计划 ID 无效')
    total = query.count()
    rows = query.order_by(CodeAnalysisJob.analysis_run_id.desc()).offset((page - 1) * 20).limit(20).all()
    return jsonify({'items': [job_payload(j, compact=True) for j in rows], 'total': total, 'page': page, 'page_size': 20})


@api_bp.route('/code-analysis/runs/<int:jid>', methods=['GET'])
def get_code_analysis_job(jid):
    job, actor, denied = get_job(jid)
    if denied:
        return denied
    occurrences = ShiftLeftAnalysisFinding.query.filter_by(analysis_run_id=jid).all()
    findings = []
    ids = [o.finding_id for o in occurrences]
    canonical = {f.id: f for f in ShiftLeftFinding.query.filter(ShiftLeftFinding.id.in_(ids)).all()}
    history = {}
    for d in CodeAnalysisDecision.query.filter(CodeAnalysisDecision.finding_id.in_(ids)).order_by(CodeAnalysisDecision.id.desc()).all():
        history.setdefault(d.finding_id, []).append(d)
    for occurrence in occurrences:
        f = canonical[occurrence.finding_id]
        decisions = history.get(f.id, [])
        findings.append(dict(f.to_dict(), observation=occurrence.snapshot_json,
                             decisions=[domain.decision_payload(d) for d in decisions]))
    return jsonify(dict(job_payload(job), findings=findings))


def executor_access(job, actor):
    # Local model claims cannot mark another Agent's learning complete.
    return actor['type'] == 'claw' and actor['id'] == job.executor_claw_id


def current_attempt(run_id, data):
    if not run_id or data.get('workflow_run_id') != run_id:
        return False
    run = db.session.get(WorkflowRun, run_id)
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id='analyze').first()
    return bool(run and run.status not in ('cancelled', 'failed', 'blocked', 'succeeded')
                and step and step.status == 'running'
                and data.get('attempt_no') == step.attempt_no)


@api_bp.route('/code-analysis/runs/<int:jid>/result', methods=['POST'])
def complete_code_analysis_job(jid):
    job, actor, denied = get_job(jid, True)
    if denied:
        return denied
    if not executor_access(job, actor):
        return error('EXECUTOR_REQUIRED', '仅本次分析执行 Agent 可提交结果', 403)
    try:
        data = body()
        idem, replay = _idempotency_begin(dict(actor, key='claw:%s' % actor['id']))
        if replay:
            return replay
        if not current_attempt(job.analysis.workflow_run_id, data):
            return error('EXECUTION_ATTEMPT_STALE', '仅当前运行 attempt 可提交分析结果', 409)
        baseline = data.get('baseline')
        if not isinstance(baseline, dict) or any(not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})',
                str(baseline.get(k) or '')) for k in ('base_sha', 'target_sha')):
            raise ValueError('必须提交真实 base_sha 和 target_sha')
        report = db.session.get(TestReport, integer(data.get('report_id')))
        if (not report or report.project_id != job.analysis.project_id or report.is_deleted
                or report.status != 'published' or report.is_hidden):
            raise ValueError('完整报告必须属于本项目、已发布且未隐藏')
        findings = data.get('findings')
        if not isinstance(findings, list) or len(findings) > 500:
            raise ValueError('findings 必须是最多 500 项的数组')
        seen = set()
        for f in findings:
            if (not isinstance(f, dict) or not isinstance(f.get('title'), str) or not f['title'].strip()
                    or len(f['title']) > 300 or not isinstance(f.get('finding_key'), str)
                    or not 0 < len(f['finding_key']) <= 255 or f['finding_key'] in seen
                    or f.get('severity', 'medium') not in FINDING_SEVERITIES
                    or not isinstance(f.get('code_locations', []), list)):
                raise ValueError('候选问题的标识、标题、严重度或证据格式无效')
            seen.add(f['finding_key'])
        if job.analysis.status.startswith('completed'):
            return error('ANALYSIS_ALREADY_COMPLETED', '结果已保存，不能覆盖历史报告', 409)
        for f in findings:
            row = ShiftLeftFinding.query.filter_by(project_id=job.analysis.project_id,
                                                   finding_key=f['finding_key']).first()
            if not row:
                row = ShiftLeftFinding(project_id=job.analysis.project_id, finding_key=f['finding_key'],
                    analysis_run_id=jid, title=f['title'], created_by=actor['name'],
                    created_by_actor_key='claw:%s' % actor['id'])
                db.session.add(row)
            else:
                row.revision += 1
            row.analysis_run_id, row.title = jid, f['title']
            row.description = str(f.get('description') or '')
            row.module = str(f.get('module') or '')[:160]
            row.severity = f.get('severity', 'medium')
            row.code_locations_json = f.get('code_locations', [])
            row.updated_by, row.last_seen_at = actor['name'], now_cst_naive()
            db.session.flush()
            db.session.add(ShiftLeftAnalysisFinding(analysis_run_id=jid, finding_id=row.id, snapshot_json=f))
            _new_finding_event(row, 'seen', {'analysis_run_id': jid, 'snapshot': f},
                               dict(actor, key='claw:%s' % actor['id']))
        job.analysis.baseline_json = baseline
        job.analysis.report_id = report.id
        job.analysis.status = 'completed_with_findings' if findings else 'completed'
        job.analysis.result_summary_json = {'candidate_count': len(findings), 'summary': str(data.get('summary') or '')}
        job.analysis.finished_at = now_cst_naive()
        job.analysis.revision += 1
        # Existing report center owns the actual content; link it to the plan.
        if not TestPlanReport.query.filter_by(plan_id=job.plan_id, linked_test_report_id=report.id).first():
            db.session.add(TestPlanReport(plan_id=job.plan_id, title=report.title,
                content='', linked_test_report_id=report.id, created_by=actor['name']))
        return _commit_payload(idem, job_payload(job))
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return error('INVALID_RESULT', str(exc))


@api_bp.route('/code-analysis/runs/<int:jid>/findings/<int:fid>/decision', methods=['POST'])
def triage_code_analysis_finding(jid, fid):
    job, actor, denied = get_job(jid, True)
    if denied:
        return denied
    if actor['type'] != 'user':
        return error('HUMAN_REVIEW_REQUIRED', '潜在 Bug 分流需要人工标注', 403)
    try:
        data = body()
        occurrence = ShiftLeftAnalysisFinding.query.filter_by(analysis_run_id=jid, finding_id=fid).first()
        finding = ShiftLeftFinding.query.filter_by(id=fid).with_for_update().first()
        if not finding or not occurrence:
            return error('FINDING_NOT_FOUND', '本次分析中不存在该候选', 404)
        idem, replay = _idempotency_begin(dict(actor, key='user:%s' % actor['id']))
        if replay:
            return replay
        if integer(data.get('expected_revision')) != finding.revision:
            return error('REVISION_CONFLICT', '问题已更新，请刷新后重试', 409)
        action, label, reason = data.get('action'), data.get('truth_label', 'unknown'), str(data.get('reason') or '').strip()
        if action not in ('submit', 'confirm', 'ignore') or label not in (
                'true_positive', 'false_positive', 'duplicate_bug', 'risk_accepted', 'unknown'):
            raise ValueError('处置动作或事实标注无效')
        if not reason or len(reason) > 8000:
            raise ValueError('请填写判断依据（最多 8000 字）')
        if action == 'submit' and label != 'true_positive':
            raise ValueError('直接提单需明确确认是真实 Bug')
        if len('user:%s:%s' % (actor['id'], request.headers['Idempotency-Key'])) > 128:
            raise ValueError('Idempotency-Key 过长')
        decision = CodeAnalysisDecision(finding_id=fid, analysis_run_id=jid, action=action,
            truth_label=label, reason=reason, actor_name=actor['name'],
            request_key='user:%s:%s' % (actor['id'], request.headers['Idempotency-Key']),
            request_hash=domain.digest(data), snapshot_json={
                'finding': occurrence.snapshot_json, 'finding_revision': finding.revision,
                'knowledge_id': next(p['id'] for p in job.snapshot_json['knowledge'] if p['project_id'] == job.analysis.project_id)},
            submission_status='pending' if action == 'submit' else 'not_requested')
        db.session.add(decision)
        if label in ('true_positive', 'false_positive', 'duplicate_bug'):
            feedback = ShiftLeftFindingFeedback(project_id=finding.project_id, finding_id=fid,
                label=label, note=reason, actor_type='user', actor_id=actor['id'],
                actor_key='user:%s' % actor['id'], actor_name=actor['name'],
                idempotency_key=request.headers['Idempotency-Key'], request_hash=domain.digest(data))
            db.session.add(feedback)
            db.session.flush()
            decision.feedback_id = feedback.id
        finding.revision += 1
        # Disposition is deliberately not encoded as true/false finding status.
        _new_finding_event(finding, 'human_disposition', {'action': action, 'truth_label': label, 'reason': reason},
                           dict(actor, key='user:%s' % actor['id']))
        db.session.flush()
        receipt = _commit_payload(idem, domain.decision_payload(decision), 201)
        if current_app.make_response(receipt).status_code >= 400:
            return receipt
        # Durable decision survives a dispatch outage. Retry endpoint uses the
        # same Workflow idempotency key and never repeats the human operation.
        _, failed = start_operation(job, actor, decision)
        payload = domain.decision_payload(decision)
        if failed:
            payload['learning_dispatch_error'] = current_app.make_response(failed).get_json()
        return jsonify(payload), 201
    except (ValueError, TypeError, StopIteration, KeyError) as exc:
        db.session.rollback()
        return error('INVALID_DECISION', str(exc))
    except IntegrityError:
        db.session.rollback()
        return error('DECISION_CONFLICT', '标注发生并发冲突，请刷新', 409)


@api_bp.route('/code-analysis/decisions/<int:did>/learn', methods=['POST'])
def retry_code_analysis_learning(did):
    decision = db.session.get(CodeAnalysisDecision, did)
    if not decision:
        return error('DECISION_NOT_FOUND', '标注不存在', 404)
    job, actor, denied = get_job(decision.analysis_run_id, True)
    if denied:
        return denied
    run, failed = start_operation(job, actor, decision)
    return failed if failed else jsonify(domain.decision_payload(decision))


@api_bp.route('/code-analysis/decisions/<int:did>/learning-result', methods=['POST'])
def complete_code_analysis_learning(did):
    decision = CodeAnalysisDecision.query.filter_by(id=did).with_for_update().first()
    if not decision:
        return error('DECISION_NOT_FOUND', '标注不存在', 404)
    job, actor, denied = get_job(decision.analysis_run_id, True)
    if denied:
        return denied
    if not executor_access(job, actor) or not decision.learning_run_id:
        return error('EXECUTOR_REQUIRED', '需要被派发的分析 Agent 提交学习结果', 403)
    try:
        data = body()
        if decision.learning_status == 'learned':
            if decision.learning_result_hash != domain.digest(data):
                return error('LEARNING_ALREADY_COMPLETED', '学习结果已保存', 409)
            return jsonify(domain.decision_payload(decision))
        if not current_attempt(decision.learning_run_id, data):
            return error('EXECUTION_ATTEMPT_STALE', '仅当前学习 attempt 可写入知识', 409)
        newer = CodeAnalysisDecision.query.filter(CodeAnalysisDecision.finding_id == decision.finding_id,
                                                   CodeAnalysisDecision.id > did).first()
        if newer:
            decision.learning_status = 'superseded'
            db.session.commit()
            return error('FEEDBACK_SUPERSEDED', '人工判断已更新，请学习最新标注', 409)
        content, summary = data.get('project_content'), str(data.get('summary') or '').strip()
        if not isinstance(content, str) or not summary or len(content.encode()) > 512 * 1024:
            raise ValueError('必须提交学习摘要与项目知识正文（最多 512KB）')
        page = KnowledgeEntry.query.filter_by(id=decision.snapshot_json['knowledge_id']).with_for_update().first()
        if not page or page.project_id != job.analysis.project_id or page.archived_at:
            raise ValueError('项目知识已归档或失效')
        revision, failed = _create_revision(page, actor, page.title, content, summary[:500],
            data.get('expected_revision'), 'code-analysis-learning:%s' % did)
        if failed:
            return failed
        decision.learning_status, decision.learned_revision = 'learned', revision.revision_no
        decision.learning_summary = summary
        decision.general_proposal = str(data.get('general_proposal') or '')[:32000]
        decision.learning_result_hash = domain.digest(data)
        _add_audit('learn', 'code_analysis_decision', did, page.title, actor,
                   {'knowledge_id': page.id, 'revision': revision.revision_no})
        db.session.commit()
        return jsonify(domain.decision_payload(decision))
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return error('INVALID_LEARNING_RESULT', str(exc))


@api_bp.route('/code-analysis/decisions/<int:did>/publish-general', methods=['POST'])
def publish_code_analysis_general_experience(did):
    decision = CodeAnalysisDecision.query.filter_by(id=did).with_for_update().first()
    if not decision:
        return error('DECISION_NOT_FOUND', '标注不存在', 404)
    job, actor, denied = get_job(decision.analysis_run_id, True)
    if denied:
        return denied
    if actor['type'] != 'user':
        return error('HUMAN_PUBLICATION_REQUIRED', '通用经验需要人工确认后发布', 403)
    if decision.general_published_revision:
        return jsonify(domain.decision_payload(decision))
    try:
        data = body()
        text = str(data.get('content') or '').strip()
        if not text or len(text) > 32000 or data.get('confirmed') is not True:
            raise ValueError('请确认通用经验已去除项目业务、源码和敏感内容')
        page = KnowledgeEntry.query.filter_by(category='code_analysis_general', project_id=None).with_for_update().one()
        content = (page.content or '') + '\n\n' + text
        revision, failed = _create_revision(page, actor, page.title, content, '发布通用分析经验',
            data.get('expected_revision'), 'code-analysis-general:%s' % did)
        if failed:
            return failed
        decision.general_published_revision = revision.revision_no
        _add_audit('publish_general', 'code_analysis_decision', did, page.title, actor,
                   {'source_project_id': job.analysis.project_id, 'general_revision': revision.revision_no})
        db.session.commit()
        return jsonify(domain.decision_payload(decision))
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return error('INVALID_GENERAL_EXPERIENCE', str(exc))


@api_bp.route('/code-analysis/decisions/<int:did>/submit-bug', methods=['POST'])
def submit_code_analysis_bug(did):
    """Explicit human-approved external write with ambiguous-outcome protection."""
    decision = CodeAnalysisDecision.query.filter_by(id=did).with_for_update().first()
    if not decision:
        return error('DECISION_NOT_FOUND', '标注不存在', 404)
    job, actor, denied = get_job(decision.analysis_run_id, True)
    if denied:
        return denied
    if actor['type'] != 'user' or decision.action != 'submit':
        return error('HUMAN_SUBMISSION_REQUIRED', '需要人工选择直接提单', 403)
    finding = ShiftLeftFinding.query.filter_by(id=decision.finding_id).with_for_update().first()
    latest = CodeAnalysisDecision.query.filter_by(finding_id=finding.id).order_by(CodeAnalysisDecision.id.desc()).first()
    if latest.id != did:
        return error('FEEDBACK_SUPERSEDED', '人工处置已更新，不能提交旧判断', 409)
    previous = CodeAnalysisDecision.query.filter(CodeAnalysisDecision.finding_id == finding.id,
        CodeAnalysisDecision.submission_status.in_(('submitting', 'submitted', 'unknown'))).first()
    if previous:
        if previous.submission_status == 'submitted':
            decision.bug_id, decision.submission_status = previous.bug_id, 'submitted'
            db.session.commit()
            return jsonify(domain.decision_payload(decision))
        return error('SUBMISSION_RECONCILIATION_REQUIRED', '之前提单结果尚未确认，请先关联已有 Bug，禁止重复创建', 409)
    from app.api.tapd import _get_tapd_credentials, _tapd_request, TAPD_API_BASE_URL
    plan = db.session.get(TestPlan, job.plan_id)
    project = db.session.get(Project, job.analysis.project_id)
    workspace = plan.tapd_workspace_id or project.tapd_workspace_id
    if not workspace or not all(_get_tapd_credentials()):
        return error('TAPD_CONFIGURATION_REQUIRED', '请配置本项目 TAPD workspace 和 Hub 凭据', 409)
    data = request.get_json(silent=True) or {}
    if data.get('confirmed') is not True:
        return error('CONFIRMATION_REQUIRED', '提单前需确认 confirmed=true')
    decision.submission_status = 'submitting'
    db.session.commit()
    try:
        from html import escape
        observation = decision.snapshot_json['finding']
        note = '[HubFinding #%s / Analysis #%s]\n%s\n人工依据：%s' % (
            finding.id, job.analysis_run_id, observation.get('description') or '', decision.reason)
        note += '\n代码位置：' + json.dumps(observation.get('code_locations', []), ensure_ascii=False)
        result = _tapd_request('POST', TAPD_API_BASE_URL + '/bugs', data={
            'workspace_id': str(workspace), 'title': observation['title'],
            'description': '<pre>%s</pre>' % escape(note),
            'reporter': actor['name']})
        item = result[0] if isinstance(result, list) and result else result
        bug = item.get('Bug', item) if isinstance(item, dict) else {}
        bid = str(bug.get('id') or '')
        if not re.fullmatch(r'\d{1,64}', bid):
            raise ValueError('TAPD 未返回有效 Bug ID')
        decision.bug_id, decision.submission_status = bid, 'submitted'
        _add_audit('submit_bug', 'code_analysis_decision', did, finding.title, actor, {'bug_id': bid})
        db.session.commit()
        return jsonify(domain.decision_payload(decision))
    except Exception:
        # Timeout/invalid receipt can mean the remote write succeeded. Never
        # blindly retry POST; keep the approval and require a verified Bug ID.
        db.session.rollback()
        decision = db.session.get(CodeAnalysisDecision, did)
        decision.submission_status = 'unknown'
        db.session.commit()
        return error('SUBMISSION_OUTCOME_UNKNOWN', '提单回执不确定，请核对 TAPD 后关联 Bug ID，禁止直接重提', 502)


@api_bp.route('/code-analysis/decisions/<int:did>/reconcile-bug', methods=['POST'])
def reconcile_code_analysis_bug(did):
    decision = CodeAnalysisDecision.query.filter_by(id=did).with_for_update().first()
    if not decision:
        return error('DECISION_NOT_FOUND', '标注不存在', 404)
    job, actor, denied = get_job(decision.analysis_run_id, True)
    if denied:
        return denied
    if actor['type'] != 'user' or decision.action != 'submit':
        return error('HUMAN_SUBMISSION_REQUIRED', '需要人工核验提单', 403)
    try:
        bid = str(body().get('bug_id') or '')
        if not re.fullmatch(r'\d{1,64}', bid):
            raise ValueError('Bug ID 格式无效')
        if decision.submission_status == 'submitted':
            if decision.bug_id != bid:
                return error('BUG_RECEIPT_CONFLICT', '已保存的提单回执不能改为另一个 Bug', 409)
            return jsonify(domain.decision_payload(decision))
        from app.api.tapd import _tapd_request, TAPD_API_BASE_URL
        plan = db.session.get(TestPlan, job.plan_id)
        project = db.session.get(Project, job.analysis.project_id)
        workspace = plan.tapd_workspace_id or project.tapd_workspace_id
        if not workspace:
            raise ValueError('项目尚未配置 TAPD workspace')
        result = _tapd_request('GET', TAPD_API_BASE_URL + '/bugs', params={'workspace_id': workspace, 'id': bid})
        items = result if isinstance(result, list) else [result]
        matching = [i.get('Bug', i) for i in items if isinstance(i, dict)]
        if not any(str(b.get('id')) == bid and str(b.get('workspace_id')) == str(workspace) for b in matching):
            raise ValueError('Bug 不存在或不属于本项目')
        decision.bug_id, decision.submission_status = bid, 'submitted'
        _add_audit('reconcile_bug', 'code_analysis_decision', did, bid, actor, {'bug_id': bid})
        db.session.commit()
        return jsonify(domain.decision_payload(decision))
    except (ValueError, TypeError):
        return error('BUG_RECONCILIATION_FAILED', '无法核验本项目 Bug，请检查 ID 与 TAPD 配置', 409)


@api_bp.route('/resource-sharing/<kind>/<int:rid>', methods=['PUT'])
def configure_resource_sharing(kind, rid):
    actor = _actor()
    if not sharing.enabled() or not actor:
        return error('RESOURCE_SHARING_DISABLED', '共享能力未启用', 404)
    resource = db.session.get(KnowledgeEntry if kind == 'knowledge' else Skill, rid) if kind in ('knowledge', 'skill') else None
    if not resource:
        return error('RESOURCE_NOT_FOUND', '资源不存在', 404)
    policy = SharedResourcePolicy.query.filter_by(resource_kind=kind, resource_id=rid).with_for_update().first()
    if policy:
        allowed = sharing.can_write(policy, actor)
    elif kind == 'knowledge':
        allowed = (_can_access_project(actor, resource.project_id) if resource.project_id
                   else sharing.is_global_admin(actor))
    else:
        from app.api.skills import _can_edit, _get_current_user
        allowed = _can_edit(_get_current_user(), resource)
    if not allowed:
        return error('RESOURCE_OWNER_REQUIRED', '仅来源项目维护者可修改共享范围', 403)
    try:
        data = body()
        scope, projects = data.get('scope'), data.get('project_ids', [])
        if scope not in ('project', 'selected', 'all') or not isinstance(projects, list):
            raise ValueError('共享范围无效')
        projects = sorted(set(integer(p) for p in projects))
        if any(not db.session.get(Project, p) for p in projects) or (scope == 'selected' and not projects):
            raise ValueError('请选择存在的目标项目')
        if (kind == 'skill' and (resource.visibility == 'private' or resource.scope == 'admin'
                                or resource.is_deleted or resource.review_status != 'approved')):
            raise ValueError('私有、待审核或下架 Skill 不能跨项目发布')
        if kind == 'knowledge' and (resource.status != 'approved' or resource.archived_at):
            raise ValueError('仅有效的已通过知识可跨项目发布')
        if policy and data.get('expected_revision') != policy.revision:
            return error('REVISION_CONFLICT', '共享配置已变化，请刷新', 409)
        if not policy:
            owner = resource.project_id if kind == 'knowledge' else None
            if kind == 'skill':
                candidates = resource.applicable_projects or []
                owner = integer(candidates[0]) if len(candidates) == 1 else None
                if not owner and not sharing.is_global_admin(actor):
                    raise ValueError('多项目或通用 Skill 的首次共享配置由平台维护者设置')
            policy = SharedResourcePolicy(resource_kind=kind, resource_id=rid,
                owner_project_id=owner, created_by=actor['name'])
            db.session.add(policy)
        else:
            policy.revision += 1
        policy.scope, policy.project_ids_json = scope, projects if scope == 'selected' else []
        if kind == 'knowledge':
            # Authenticated sharing must not leave an older anonymous token
            # usable after project grants have been narrowed or revoked.
            resource.revoke_share()
            if not resource.current_revision:
                _create_revision(resource, actor, resource.title, resource.content or '',
                    '启用跨项目共享版本记录，保留原文', 0, 'resource-sharing-init:%s' % resource.id)
        _add_audit('share_scope', kind, rid, resource.title if kind == 'knowledge' else resource.name,
                   actor, {'scope': scope, 'project_ids': policy.project_ids_json})
        db.session.commit()
        sharing.invalidate()
        return jsonify(sharing.policy_payload(policy))
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return error('INVALID_SHARING', str(exc))


@api_bp.route('/resource-sharing/<kind>/<int:rid>', methods=['GET'])
def get_resource_sharing(kind, rid):
    actor = _actor()
    if not sharing.enabled() or not actor:
        return error('RESOURCE_SHARING_DISABLED', '共享能力未启用', 404)
    row = db.session.get(KnowledgeEntry if kind == 'knowledge' else Skill, rid) if kind in ('knowledge', 'skill') else None
    if not row or not sharing.resource_readable(kind, row, actor):
        return error('RESOURCE_NOT_FOUND', '资源不存在或不可读', 404)
    policy = sharing.policy_for(kind, rid)
    if policy:
        return jsonify(dict(sharing.policy_payload(policy), can_manage=sharing.can_write(policy, actor)))
    if kind == 'skill':
        from app.api.skills import _can_edit, _get_current_user
        owner = row.applicable_projects[0] if len(row.applicable_projects or []) == 1 else None
        allowed = _can_edit(_get_current_user(), row)
        scope = 'all' if row.scope == 'global' else 'project'
    else:
        owner = row.project_id
        allowed = _can_access_project(actor, owner) if owner else sharing.is_global_admin(actor)
        scope = 'all' if row.scope == 'global' and owner is None else 'project'
    return jsonify({'owner_project_id': owner, 'scope': scope, 'project_ids': [], 'revision': 0, 'can_manage': allowed})
