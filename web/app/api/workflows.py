"""Hub Workflow Runbook API.

MVP scope:
- Hub stores definitions/runs/steps/approvals/artifacts.
- Workers/Agents execute steps and report structured results.
- Hub owns gates, approvals, retry/resume, evidence tracking.
"""
from datetime import datetime
import copy

from flask import current_app, jsonify, request, session

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user, is_admin_user
from app.models import (
    AgentPostAssignment,
    ClawTodo,
    ClawMessage,
    ExamSession,
    OpenClawInstance,
    Project,
    RequirementItem,
    RequirementReviewVerdict,
    RequirementTestcaseLink,
    TestCaseLibrary,
    TestReport,
    TEST_REPORT_RISK_LEVELS,
    TEST_REPORT_STATUSES,
    User,
    WorkflowApproval,
    WorkflowArtifact,
    WorkflowDefinition,
    WorkflowDefinitionFavorite,
    WorkflowRun,
    WorkflowRunStep,
)
from app.services.post_resolver import resolve_post_candidate
from app.services.requirement_coverage import summarize_requirement_coverage
from app.services.workflows import (
    build_heartbeat_fallback_notice,
    build_step_blocked_notice,
    build_workflow_agent_task_payload,
    build_workflow_start_context,
    can_claim_step,
    can_accept_step_result_after_blocker,
    can_execute_workflow,
    can_manage_workflow,
    can_view_workflow,
    compute_step_health,
    evaluate_step_branches,
    evaluate_step_gates,
    merge_workflow_definition_update,
    normalize_executor_acl,
    paginate_items,
    normalize_workflow_definition,
    ready_step_ids,
    resolve_start_var_claw_ids,
    resolve_workflow_step_claw_ids,
    workflow_step_display_state,
)


# F1：running step 心跳/进度硬超时（秒）。超过后按 retry_max 有限重试，用尽则阻断，
# 避免节点永远挂 running 让执行方反复空转消耗算力（workflow 无限循环根因）。
WORKFLOW_NO_RESPONSE_HARD_TIMEOUT_SEC = 900


DEFAULT_RACINGGO_WORKFLOW = {
    'key': 'racinggo_qa_auto_test',
    'name': 'RacingGO qa_auto_test 全流程',
    'description': 'RacingGO 自动化 QA Runbook：Editor 冒烟、报告、审批、构包、手机冒烟。',
    'steps': [
        {'id': 'precheck', 'name': '前置检查', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.precheck',
         'gates': [
             {'expression': 'metrics.env_ready == true', 'on_fail': 'blocked'},
             {'expression': 'metrics.workspace_clean == true', 'on_fail': 'blocked'},
             {'expression': 'metrics.secrets_ready == true', 'on_fail': 'blocked'},
         ]},
        {'id': 'merge_dev', 'name': '合入 origin/dev', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.merge_dev', 'depends_on': ['precheck'],
         'gates': [
             {'expression': 'status == "passed"', 'on_fail': 'blocked'},
             {'expression': 'metrics.conflict_count == 0', 'on_fail': 'blocked'},
         ]},
        {'id': 'editor_health', 'name': 'Unity Health', 'type': 'worker_task',
         'runner': 'deepflow.unity.health', 'depends_on': ['merge_dev'],
         'gates': [{'expression': 'metrics.red == 0', 'on_fail': 'blocked'}]},
        {'id': 'lib20', 'name': '库20合线门禁', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.editor_lib20', 'depends_on': ['editor_health'],
         'gates': [
             {'expression': 'metrics.failed == 0', 'on_fail': 'blocked'},
             {'expression': 'metrics.missing_screenshot_count == 0', 'on_fail': 'blocked'},
         ]},
        {'id': 'lib21', 'name': '库21关卡矩阵', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.editor_lib21', 'depends_on': ['lib20'],
         'gates': [
             {'expression': 'metrics.failed == 0', 'on_fail': 'blocked'},
             {'expression': 'metrics.missing_screenshot_count == 0', 'on_fail': 'blocked'},
         ]},
        {'id': 'endless', 'name': '无尽专项', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.endless', 'depends_on': ['lib21'],
         'gates': [
             {'expression': 'metrics.ghost_bad == 0', 'on_fail': 'blocked'},
             {'expression': 'metrics.failed == 0', 'on_fail': 'blocked'},
         ]},
        {'id': 'editor_report', 'name': '生成 Hub Editor 总报告', 'type': 'worker_task',
         'runner': 'deepflow.hub.editor_report', 'depends_on': ['endless'],
         'gates': [{'expression': 'metrics.missing_screenshot_count == 0', 'on_fail': 'blocked'}]},
        {'id': 'notify_editor', 'name': '批准推送 Editor 报告', 'type': 'notification',
         'runner': 'deepflow.wecom.release_group', 'approval_required': True,
         'depends_on': ['editor_report']},
        {'id': 'push_and_build', 'name': '批准 Push 并触发蓝盾构包', 'type': 'worker_task',
         'runner': 'deepflow.bkci.trigger_qa_build', 'approval_required': True,
         'depends_on': ['notify_editor'],
         'gates': [
             {'expression': 'metrics.remote_head == metrics.local_head', 'on_fail': 'blocked'},
             {'expression': 'metrics.build_triggered == true', 'on_fail': 'blocked'},
         ]},
        {'id': 'mobile_smoke', 'name': '手机包冒烟', 'type': 'worker_task',
         'runner': 'deepflow.racinggo.mobile_smoke', 'depends_on': ['push_and_build']},
        {'id': 'final_report', 'name': '手机端最终报告与推群', 'type': 'worker_task',
         'runner': 'deepflow.hub.mobile_final_report', 'approval_required': True,
         'depends_on': ['mobile_smoke']},
    ],
}


DEFAULT_AGENT_TEAM_REQ_TO_CASE_WORKFLOW = {
    'key': 'agent_team_req_to_case_loop',
    'name': 'Agent 团队：需求评审到用例评审闭环',
    'description': '四工位 Agent 流水线：需求分析、工程分析、用例设计、独立评审，人只做末端确认。',
    'steps': [
        {
            'id': 'requirement_review',
            'name': '需求分析与逐条评审',
            'type': 'agent_task',
            'target_post': 'requirement_analyst',
            'runner': 'agent.skill.requirement-analysis',
            'prompt': '同步/读取迭代需求，完成质量分析，并为每条需求写入 review-verdict。',
            'outputs': ['reviewed_requirements', 'high_risk_issues'],
        },
        {
            'id': 'engineering_analysis',
            'name': '工程影响面与实现状态分析',
            'type': 'agent_task',
            'target_post': 'engineering_analyst',
            'runner': 'agent.skill.engineering-analysis',
            'depends_on': ['requirement_review'],
            'prompt': '基于需求列表和工程变更，补齐 impl_status、函数级影响面和测试关注点。',
            'outputs': ['impl_status_count', 'impact_modules',
                        'analysis_run_id', 'analysis_report_id'],
            'analysis': {
                'enabled': True,
                'profile': 'requirement_code_joint',
                'create_report': True,
                'report_title_template': '代码分析报告 - {run_name}',
                'report_format': 'html',
                'iteration_id_var': 'iteration_id',
                'report_id_var': 'analysis_report_id',
                'baseline_vars': {
                    'client_repo': 'client_repo',
                    'client_base_sha': 'client_base_sha',
                    'client_target_sha': 'client_target_sha',
                    'server_repo': 'server_repo',
                    'server_base_sha': 'server_base_sha',
                    'server_target_sha': 'server_target_sha',
                    'requirement_revision': 'requirement_revision',
                },
                'require_independent_review': True,
            },
        },
        {
            'id': 'case_design',
            'name': '用例设计与覆盖补齐',
            'type': 'agent_task',
            'target_post': 'case_designer',
            'runner': 'agent.skill.testcase-manager',
            'depends_on': ['engineering_analysis'],
            'prompt': '基于验收标准、测试关注点、工程影响面和历史 pitfall 设计用例，并建立需求到用例关联。',
            'outputs': ['created_cases', 'coverage'],
        },
        {
            'id': 'independent_review',
            'name': '独立评审与风险汇总',
            'type': 'agent_task',
            'target_post': 'independent_reviewer',
            'runner': 'agent.skill.case-review',
            'depends_on': ['case_design'],
            'prompt': '交叉评审需求问题清单与用例质量，不能评审自己的产出；输出最终风险和整改建议。',
            'outputs': ['review_comments', 'risk_summary'],
        },
        {
            'id': 'human_confirm',
            'name': '人工一次确认',
            'type': 'approval',
            'depends_on': ['independent_review'],
            'approval_required': True,
        },
    ],
}


BUILTIN_WORKFLOWS = [
    DEFAULT_RACINGGO_WORKFLOW,
    DEFAULT_AGENT_TEAM_REQ_TO_CASE_WORKFLOW,
]


def _shift_left_enabled():
    value = current_app.config.get('SHIFT_LEFT_ENABLED', False)
    if isinstance(value, str):
        return value.lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _actor_name():
    claw = get_current_claw()
    if claw:
        return claw.name
    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            return user.display_name or user.username
    user = get_current_user()
    return getattr(user, 'username', None) or 'system'


def _actor_identity():
    claw = get_current_claw()
    if claw:
        return {'type': 'claw', 'id': claw.id, 'name': claw.name}
    uid = session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user:
            return {'type': 'user', 'id': user.id, 'name': user.display_name or user.username}
    user = get_current_user()
    if user:
        return {'type': 'user', 'id': user.id, 'name': getattr(user, 'username', '')}
    return None


def _actor_project_ids():
    claw = get_current_claw()
    if claw:
        ids = []
        if claw.project_id:
            ids.append(int(claw.project_id))
        elif claw.project_name:
            project = Project.query.filter_by(name=claw.project_name).first()
            if project:
                ids.append(int(project.id))
        return sorted(set(ids))
    user = get_current_user()
    if not user:
        uid = session.get('user_id')
        user = User.query.get(uid) if uid else None
    ids = set()
    if user:
        for pid in (getattr(user, 'managed_projects', None) or []):
            try:
                ids.add(int(pid))
            except (TypeError, ValueError):
                continue
        bound_claw_id = getattr(user, 'bound_claw_id', None)
        if bound_claw_id:
            claw = OpenClawInstance.query.get(bound_claw_id)
            if claw and claw.project_id:
                ids.add(int(claw.project_id))
            elif claw and claw.project_name:
                project = Project.query.filter_by(name=claw.project_name).first()
                if project:
                    ids.add(int(project.id))
    return sorted(ids)


def _definition_project_actor_options(definition):
    """Return project-scoped Agents and owners for the start dialog."""
    project_id = definition.project_id
    project_name = ''
    if project_id:
        project = Project.query.get(project_id)
        project_name = project.name if project else ''
    agents_query = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if project_id:
        agents_query = agents_query.filter(
            db.or_(
                OpenClawInstance.project_id == project_id,
                OpenClawInstance.project_name == project_name if project_name else db.text('1=0'),
            )
        )
    else:
        actor_projects = _actor_project_ids()
        if actor_projects:
            agents_query = agents_query.filter(OpenClawInstance.project_id.in_(actor_projects))
    agents = agents_query.order_by(OpenClawInstance.name.asc()).all()
    agent_ids = {a.id for a in agents}

    owners = []
    for user in User.query.order_by(User.display_name.asc(), User.username.asc()).all():
        include = False
        if project_id:
            try:
                include = int(project_id) in [int(x) for x in (user.managed_projects or [])]
            except Exception:
                include = False
        if not include and user.bound_claw_id and user.bound_claw_id in agent_ids:
            include = True
        if include:
            owners.append({
                'id': user.id,
                'username': user.username,
                'display_name': user.display_name or user.username,
                'bound_claw_id': user.bound_claw_id,
            })

    return {
        'project_id': project_id,
        'project_name': project_name,
        'agents': [{
            'id': a.id,
            'name': a.name,
            'safe_name': a.safe_name,
            'claw_tag': a.claw_tag,
            'status': a.status,
            'role': a.role,
            'project_id': a.project_id,
            'project_name': a.project_name,
        } for a in agents],
        'owners': owners,
    }


def _actor_favorite_definition_ids():
    actor = _actor_identity()
    if not actor:
        return set()
    q = WorkflowDefinitionFavorite.query
    if actor['type'] == 'claw':
        q = q.filter_by(claw_id=actor['id'])
    else:
        q = q.filter_by(user_id=actor['id'])
    return {row.definition_id for row in q.all()}


def _definition_visible(definition):
    actor = _actor_identity()
    if not actor:
        return False
    if _is_legacy_owner(definition, actor):
        return True
    return can_view_workflow(
        definition.visibility_scope or 'project',
        definition.owner_type,
        definition.owner_id,
        definition.project_id,
        actor['type'],
        actor['id'],
        _actor_project_ids(),
        is_admin=is_admin_user(),
    )


def _require_actor():
    if get_current_claw() or get_current_user():
        return None
    return jsonify({'error': '未认证'}), 401


def _definition_acl(definition):
    acl = normalize_executor_acl(definition.executor_acl_json or {})
    if definition.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}:
        acl['all'] = True
    return acl


def _is_legacy_owner(definition, actor):
    return bool(
        actor and
        not definition.owner_type and
        not definition.owner_id and
        definition.created_by and
        definition.created_by == actor.get('name')
    )


def _can_execute_definition(definition):
    actor = _actor_identity()
    if not actor:
        return False
    if _is_legacy_owner(definition, actor):
        return True
    return can_execute_workflow(
        definition.owner_type,
        definition.owner_id,
        _definition_acl(definition),
        actor['type'],
        actor['id'],
        is_admin=is_admin_user(),
    )


def _can_manage_definition(definition):
    actor = _actor_identity()
    if not actor:
        return False
    if _is_legacy_owner(definition, actor):
        return True
    return can_manage_workflow(
        definition.owner_type,
        definition.owner_id,
        _definition_acl(definition),
        actor['type'],
        actor['id'],
        is_admin=is_admin_user(),
    )


def _definition_payload(definition, brief=False):
    data = definition.to_dict(brief=brief)
    data['can_execute'] = _can_execute_definition(definition)
    data['can_manage'] = _can_manage_definition(definition)
    data['can_delete'] = data['can_manage'] and definition.owner_type != 'system'
    data['can_change_visibility'] = data['can_manage'] and definition.owner_type != 'system'
    data['is_favorite'] = definition.id in _actor_favorite_definition_ids()
    return data


def _can_delete_run(run):
    actor = _actor_identity()
    if not actor:
        return False
    if is_admin_user():
        return True
    if run.triggered_by and run.triggered_by == actor.get('name'):
        return True
    if run.definition and _can_manage_definition(run.definition):
        return True
    return False


def _run_payload(run, with_steps=False):
    data = run.to_dict(with_steps=with_steps)
    data['can_delete'] = _can_delete_run(run)
    return data


def _resolve_definition_project_id(data):
    raw = data.get('project_id')
    if raw not in (None, ''):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    ids = _actor_project_ids()
    return ids[0] if len(ids) == 1 else None


def _ensure_builtin_definition(raw_definition):
    """Create one built-in workflow lazily, without overwriting edits."""
    existing = WorkflowDefinition.query.filter_by(
        workflow_key=raw_definition['key']).first()
    if existing:
        changed = False
        if not existing.owner_type:
            existing.owner_type = 'system'
            changed = True
        if not existing.executor_acl_json:
            existing.executor_acl_json = {'all': True, 'claw_ids': [], 'user_ids': []}
            changed = True
        if not existing.visibility_scope:
            existing.visibility_scope = 'project'
            changed = True
        # Compatible metadata backfill: only add a built-in analysis contract
        # when the matching node has never been configured by an operator.
        raw_analysis = {
            step.get('id'): step.get('analysis')
            for step in (raw_definition.get('steps') or [])
            if isinstance(step, dict) and isinstance(step.get('analysis'), dict)
        } if _shift_left_enabled() else {}
        raw_outputs = {
            step.get('id'): list(step.get('outputs') or [])
            for step in (raw_definition.get('steps') or [])
            if isinstance(step, dict) and step.get('id') in raw_analysis
        }
        definition = copy.deepcopy(existing.definition_json or {})
        definition_steps = definition.get('steps') or []
        definition_changed = False
        for step in definition_steps:
            if (isinstance(step, dict) and step.get('id') in raw_analysis
                    and not isinstance(step.get('analysis'), dict)):
                step['analysis'] = copy.deepcopy(raw_analysis[step['id']])
                outputs = list(step.get('outputs') or [])
                for output_name in raw_outputs.get(step['id'], []):
                    if output_name not in outputs:
                        outputs.append(output_name)
                step['outputs'] = outputs
                definition_changed = True
        if definition_changed:
            existing.definition_json = definition
            changed = True
        if changed:
            db.session.commit()
        return existing
    definition_source = copy.deepcopy(raw_definition)
    if not _shift_left_enabled():
        for step in (definition_source.get('steps') or []):
            if isinstance(step, dict):
                step.pop('analysis', None)
    definition = normalize_workflow_definition(definition_source)
    row = WorkflowDefinition(
        workflow_key=definition['key'],
        name=definition['name'],
        description=definition['description'],
        definition_json=definition,
        status='active',
        version=definition['version'],
        created_by='system',
        owner_type='system',
        executor_acl_json={'all': True, 'claw_ids': [], 'user_ids': []},
        visibility_scope='project',
    )
    db.session.add(row)
    db.session.commit()
    return row


def _ensure_default_definition():
    """Create built-in workflows lazily, return the historical RacingGO one."""
    created = None
    for raw in BUILTIN_WORKFLOWS:
        row = _ensure_builtin_definition(raw)
        if raw['key'] == DEFAULT_RACINGGO_WORKFLOW['key']:
            created = row
    return created


def _step_config_map(run):
    return {
        step['id']: step
        for step in ((run.definition.definition_json or {}).get('steps') or [])
    }


def _create_approval_if_missing(run, step, created_by):
    existing = WorkflowApproval.query.filter_by(
        run_id=run.id,
        step_id=step.step_id,
        status='pending',
    ).first()
    if existing:
        return existing
    approval = WorkflowApproval(
        run_id=run.id,
        step_id=step.step_id,
        action='approve',
        status='pending',
        created_by=created_by,
    )
    db.session.add(approval)
    return approval


def _workflow_outputs_context(run, current_step=None, current_outputs=None):
    outputs = {}
    for row in WorkflowRunStep.query.filter_by(run_id=run.id).all():
        if row.outputs_json:
            outputs[row.step_id] = row.outputs_json or {}
    if current_step is not None:
        outputs[current_step.step_id] = current_outputs or {}
    return outputs


def _run_start_vars(run):
    context = run.context_json if run and isinstance(run.context_json, dict) else {}
    return context.get('start_vars') if isinstance(context.get('start_vars'), dict) else {}


def _step_runtime_payload(step):
    data = step.to_dict(with_context=True)
    context = step.run.context_json if step.run and isinstance(step.run.context_json, dict) else {}
    data['context'] = context
    data['start_vars'] = context.get('start_vars') if isinstance(context.get('start_vars'), dict) else {}
    data['outputs'] = _workflow_outputs_context(step.run, current_step=step) if step.run else {}
    return data


def _workflow_report_artifact(run_id, step_id):
    return WorkflowArtifact.query.filter_by(
        run_id=run_id,
        step_id=step_id,
        artifact_type='workflow_report',
    ).first()


def _link_workflow_report_artifact(run, step, report):
    artifact = _workflow_report_artifact(run.id, step.step_id)
    if not artifact:
        artifact = WorkflowArtifact(
            run_id=run.id,
            step_id=step.step_id,
            artifact_type='workflow_report',
        )
        db.session.add(artifact)
    artifact.name = report.title
    artifact.url = '/test-reports/%s' % report.id
    artifact.local_path = ''
    artifact.test_report_id = report.id
    artifact.metadata_json = {
        'report_type': report.report_type,
        'is_hidden': bool(report.is_hidden),
        'status': report.status or '',
    }
    return artifact


def _workflow_report_payload(data):
    report = data.get('workflow_report')
    return report if isinstance(report, dict) else {}


def _report_submitter_fields():
    actor = _actor_identity() or {}
    if actor.get('type') == 'claw':
        return {
            'submitter_type': 'openclaw',
            'submitter_claw_id': actor.get('id'),
            'submitter_user_id': None,
            'submitter_name': actor.get('name') or 'OpenClaw',
        }
    return {
        'submitter_type': 'user',
        'submitter_user_id': actor.get('id'),
        'submitter_claw_id': None,
        'submitter_name': actor.get('name') or 'Workflow',
    }


def _resolve_workflow_report(run, step, data):
    """Create or bind a workflow report for a step result, if requested."""
    report_payload = _workflow_report_payload(data)
    raw_report_id = (
        data.get('test_report_id') or data.get('workflow_report_id') or
        report_payload.get('id') or report_payload.get('test_report_id')
    )
    report = None
    if raw_report_id not in (None, ''):
        try:
            report = TestReport.query.get(int(raw_report_id))
        except (TypeError, ValueError):
            return None, 'test_report_id 必须是整数'
        if not report or report.is_deleted:
            return None, 'Workflow 报告不存在'
        if report.report_type != 'workflow':
            return None, 'Workflow 节点只能绑定 report_type=workflow 的报告'
        _link_workflow_report_artifact(run, step, report)
        return report, ''

    if not report_payload:
        return None, ''
    if not run.project_id:
        return None, 'Workflow Run 未关联项目，无法创建 Workflow 报告'

    artifact = _workflow_report_artifact(run.id, step.step_id)
    if artifact and artifact.test_report_id:
        report = TestReport.query.get(artifact.test_report_id)

    title = (report_payload.get('title') or '').strip()[:200]
    if not title:
        title = '%s / %s' % (run.run_name or 'Workflow Run #%s' % run.id, step.name)
        title = title[:200]
    fmt = (report_payload.get('format') or 'markdown').lower()
    if fmt not in ('markdown', 'html'):
        fmt = 'markdown'
    risk_level = (report_payload.get('risk_level') or 'tbd').strip()
    if risk_level not in TEST_REPORT_RISK_LEVELS:
        risk_level = 'tbd'
    status = (report_payload.get('status') or 'published').strip()
    if status not in TEST_REPORT_STATUSES:
        status = 'published'
    submitter = _report_submitter_fields()
    if not report:
        report = TestReport(
            project_id=run.project_id,
            report_type='workflow',
            source_ref_type='workflow',
            source_ref_id=run.id,
            **submitter,
        )
        db.session.add(report)
    report.title = title
    report.remark = (report_payload.get('remark') or 'Workflow 节点报告')[:500]
    report.content = report_payload.get('content') or ''
    report.format = fmt
    report.risk_level = risk_level
    report.status = status
    report.is_hidden = bool(report_payload.get('is_hidden', True))
    report.version_name = (report_payload.get('version_name') or '')[:100]
    report.iteration_id = None
    db.session.flush()
    _link_workflow_report_artifact(run, step, report)
    return report, ''


def _step_health_signal_at(step):
    # Do not use updated_at here: Hub's own health scan can refresh it, which
    # would mask a genuinely silent executor.
    signals = [
        step.heartbeat_at,
        step.progress_at,
        step.dispatched_at,
        step.started_at,
    ]
    signals = [value for value in signals if value]
    return max(signals) if signals else None


def _apply_step_progress(step, data, reporter, now):
    """Persist optional progress payload for a running workflow step."""
    data = data if isinstance(data, dict) else {}
    if not any(k in data for k in ('phase', 'message', 'percent', 'progress', 'detail')):
        return False
    step.progress_at = now
    step.progress_by = reporter or ''
    if 'phase' in data:
        step.progress_phase = str(data.get('phase') or '').strip()[:80]
    if 'message' in data:
        step.progress_message = str(data.get('message') or '').strip()
    if 'percent' in data:
        raw_percent = data.get('percent')
        if raw_percent in (None, ''):
            step.progress_percent = None
        else:
            try:
                step.progress_percent = max(0, min(100, int(raw_percent)))
            except (TypeError, ValueError):
                step.progress_percent = None
    detail = data.get('progress') if isinstance(data.get('progress'), dict) else {}
    if not detail and isinstance(data.get('detail'), dict):
        detail = data.get('detail')
    if detail:
        step.progress_json = detail
    return True


def _reset_step_progress(step):
    step.progress_at = None
    step.progress_by = ''
    step.progress_phase = ''
    step.progress_message = ''
    step.progress_percent = None
    step.progress_json = {}


def _step_auto_block_on_heartbeat_loss(step):
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    return bool(
        config.get('auto_block_on_heartbeat_loss')
        or config.get('heartbeat_auto_block')
        or config.get('auto_block_on_no_response')
    )


def _dispatch_heartbeat_fallback_task(run, step):
    definition = run.definition if run else None
    if not definition or definition.owner_type != 'claw' or not definition.owner_id:
        if step.status == 'blocked':
            step.blocker_json = dict(step.blocker_json or {}, fallback_message='模板创建者不是 Agent，需管理员或创建者手工介入')
        else:
            step.progress_message = step.progress_message or '模板创建者不是 Agent，未响应提醒需管理员或创建者手工介入'
        return False
    claw = OpenClawInstance.query.filter(
        OpenClawInstance.id == definition.owner_id,
        OpenClawInstance.status != 'deleted',
    ).first()
    if not claw:
        if step.status == 'blocked':
            step.blocker_json = dict(step.blocker_json or {}, fallback_message='模板创建者 Agent 不存在或已删除，需管理员介入')
        else:
            step.progress_message = step.progress_message or '模板创建者 Agent 不存在或已删除，未响应提醒需管理员介入'
        return False
    try:
        from app.api.agent_client import AgentTask, notify_claw
    except Exception as exc:
        if step.status == 'blocked':
            step.blocker_json = dict(step.blocker_json or {}, fallback_message='AgentTask 兜底通道不可用：%s' % exc)
        else:
            step.progress_message = step.progress_message or ('AgentTask 提醒通道不可用：%s' % exc)
        return False
    create_agent_task = step.status == 'blocked'
    task_id = 'workflow_heartbeat_fallback_%s_%s_%s' % (
        step.run_id,
        step.step_id,
        step.attempt_no or 1,
    )
    existing = AgentTask.query.filter_by(task_id=task_id, claw_id=claw.id).first()
    if create_agent_task and not existing:
        payload = build_workflow_agent_task_payload(
            run.to_dict(with_steps=False),
            step.to_dict(),
            _workflow_outputs_context(run),
        )
        if not _shift_left_enabled():
            payload.pop('analysis', None)
        payload.update({
            'kind': 'workflow_no_response_reminder',
            'prompt': (
                'Workflow 节点已派发但长时间未收到执行方响应。'
                '请作为模板创建者 Agent 排查目标 Agent/worker 是否在线、是否已领取任务，'
                '根据现场情况提醒执行方回写 progress/result，或通知管理员介入。'
            ),
            'heartbeat': {
                'health_status': step.health_status or '',
                'heartbeat_at': str(step.heartbeat_at) if step.heartbeat_at else None,
                'heartbeat_by': step.heartbeat_by or '',
                'missed_heartbeat_count': step.missed_heartbeat_count or 0,
                'health_checked_at': str(step.health_checked_at) if step.health_checked_at else None,
            },
            'blocker': step.blocker_json or {},
        })
        task = AgentTask(
            claw_id=claw.id,
            task_id=task_id,
            task_type='workflow_agent_task',
            command=step.runner or '',
            target_path='',
            payload=jsonify_safe(payload),
            status='pending',
        )
        db.session.add(task)
    notice = build_heartbeat_fallback_notice(
        run_id=step.run_id,
        step_id=step.step_id,
        step_name=step.name,
        blocker=step.blocker_json or {},
    )
    msg_exists = ClawMessage.query.filter_by(
        claw_id=claw.id,
        msg_type='task_delegate',
        content=notice['content'],
    ).first()
    if not msg_exists:
        db.session.add(ClawMessage(
            claw_id=claw.id,
            sender_name='Workflow',
            msg_type='task_delegate',
            content=notice['content'],
        ))
    todo_exists = ClawTodo.query.filter_by(
        openclaw_id=claw.id,
        title=notice['title'],
        created_by='workflow-heartbeat',
    ).first()
    if not todo_exists:
        db.session.add(ClawTodo(
            openclaw_id=claw.id,
            title=notice['title'][:200],
            description=notice['content'],
            schedule_type='once',
            urgency_level='interrupt',
            priority='P0',
            task_category='routine',
            enabled=True,
            created_by='workflow-heartbeat',
        ))
    notify_claw(claw.id)
    return True


def _expire_workflow_agent_tasks_for_step(run_id, step_id, reason='workflow_step_terminated'):
    """把某个 workflow step 名下仍未结束(pending/running)的 AgentTask 置 failed。

    防止：离线 agent 重连后被塞入这些陈旧任务、再跑一遍 LLM 空转（无限循环放大器）。
    """
    try:
        from app.api.agent_client import AgentTask
    except Exception:
        return 0
    prefix = 'workflow_%s_%s_' % (run_id, step_id)
    rows = AgentTask.query.filter(
        AgentTask.task_id.like(prefix + '%'),
        AgentTask.status.in_(('pending', 'running')),
    ).all()
    n = 0
    for t in rows:
        t.status = 'failed'
        t.error = reason
        t.completed_at = datetime.now()
        n += 1
    return n


def _refresh_workflow_step_health(run=None, commit=False):
    now = datetime.now()
    q = WorkflowRunStep.query.join(WorkflowRun, WorkflowRun.id == WorkflowRunStep.run_id).filter(
        WorkflowRun.status.in_(('running', 'retrying')),
        WorkflowRunStep.status.in_(('running', 'retrying')),
    )
    if run is not None:
        q = q.filter(WorkflowRunStep.run_id == run.id)
    changed = False
    touched_runs = {}
    for step in q.all():
        auto_block = _step_auto_block_on_heartbeat_loss(step)
        health = compute_step_health(
            step.status,
            _step_health_signal_at(step),
            now,
            interval_sec=30,
            max_missed=3,
            progress_at=step.progress_at,
            progress_timeout_sec=300,
            auto_fail_on_missed=auto_block,
        )
        step.health_status = health['status']
        step.missed_heartbeat_count = health['missed_count']
        step.health_checked_at = now
        changed = True
        if health.get('failed'):
            step.status = 'blocked'
            step.finished_at = now
            step.claimed_by = ''
            step.claimed_at = None
            step.blocker_json = {
                'type': 'workflow_step_heartbeat_lost',
                'message': 'Workflow 节点连续 3 次未收到心跳，已判定执行失败',
                'last_heartbeat_at': str(step.heartbeat_at) if step.heartbeat_at else None,
                'heartbeat_by': step.heartbeat_by or '',
                'missed_heartbeat_count': step.missed_heartbeat_count or 0,
                'suggested_action': '检查 workflow worker/runner 是否仍在运行，必要时重试或转人工处理',
            }
            _dispatch_heartbeat_fallback_task(step.run, step)
            if step.run:
                touched_runs[step.run.id] = step.run
        elif (health.get('missed_count') or 0) >= 3:
            progress = step.progress_json if isinstance(step.progress_json, dict) else {}
            reminders = progress.get('hub_reminders') if isinstance(progress.get('hub_reminders'), dict) else {}
            if not reminders.get('no_response_at'):
                step.progress_at = step.progress_at or now
                step.progress_phase = 'no_response_reminded'
                step.progress_message = 'Hub 已提醒模板创建者：节点已派发但执行方长时间未响应'
                reminders['no_response_at'] = str(now)
                reminders['missed_heartbeat_count'] = health.get('missed_count') or 0
                progress['hub_reminders'] = reminders
                step.progress_json = progress
                _dispatch_heartbeat_fallback_task(step.run, step)
            # F1：无响应硬超时兜底（默认开启）。running step 的心跳/进度停滞超过硬阈值，
            # 说明执行方一直没回结果（feedback-no-auto-report / CLI 崩溃 / 上下文压缩退出等），
            # 若一直挂 running，agent 会靠日常节奏/提醒/session 重置反复空转烧 LLM。
            # → 按 retry_max 有限重试，用尽则阻断，让 run 收敛、停止反复触发执行方。
            signal_at = _step_health_signal_at(step)
            signal_age = int((now - signal_at).total_seconds()) if signal_at else None
            if signal_age is not None and signal_age > WORKFLOW_NO_RESPONSE_HARD_TIMEOUT_SEC:
                cfg = step.step_config_json if isinstance(step.step_config_json, dict) else {}
                retry_max = int(cfg.get('retry_max') or 0)
                attempts_used = int(step.attempt_no or 1)
                timeout_min = WORKFLOW_NO_RESPONSE_HARD_TIMEOUT_SEC // 60
                if attempts_used <= retry_max:
                    # 有限重试：先失效旧任务，再重派（task_id 含新 attempt_no）
                    _expire_workflow_agent_tasks_for_step(
                        step.run_id, step.step_id, reason='workflow_step_no_response_retry')
                    step.attempt_no = attempts_used + 1
                    step.started_at = now
                    step.dispatched_at = now
                    step.heartbeat_at = None
                    step.heartbeat_by = ''
                    step.heartbeat_count = 0
                    step.missed_heartbeat_count = 0
                    step.health_status = 'stale'
                    step.health_checked_at = now
                    _reset_step_progress(step)
                    step.progress_at = now
                    step.progress_by = 'workflow'
                    step.progress_phase = 'dispatched'
                    step.progress_message = (
                        'Hub 无响应超时（%d分钟），自动重试派发（第 %d 次）'
                        % (timeout_min, step.attempt_no))
                    _dispatch_step_message(step)
                    if step.run:
                        touched_runs[step.run.id] = step.run
                else:
                    # 重试用尽 → 阻断，停止无限挂起并关掉陈旧任务
                    step.status = 'blocked'
                    step.finished_at = now
                    step.claimed_by = ''
                    step.claimed_at = None
                    step.blocker_json = {
                        'type': 'workflow_step_no_response',
                        'message': (
                            'Workflow 节点派发后超过 %d 分钟无任何心跳/进度，已自动阻断'
                            '（避免执行方反复空转消耗算力）' % timeout_min),
                        'last_heartbeat_at': str(step.heartbeat_at) if step.heartbeat_at else None,
                        'missed_heartbeat_count': step.missed_heartbeat_count or 0,
                        'attempt_no': step.attempt_no or 1,
                        'suggested_action': '检查执行 Agent 是否在线/是否回写 result；修复后在页面重试该节点',
                    }
                    _expire_workflow_agent_tasks_for_step(
                        step.run_id, step.step_id, reason='workflow_step_no_response_blocked')
                    _dispatch_heartbeat_fallback_task(step.run, step)
                    if step.run:
                        touched_runs[step.run.id] = step.run
    for touched in touched_runs.values():
        _recompute_run_status(touched, 'workflow-heartbeat')
    if changed and commit:
        db.session.commit()
    return changed


def _apply_step_branches(run, step, branch_context):
    branch = evaluate_step_branches(step.step_config_json or {}, branch_context)
    if not branch.get('decisions'):
        step.branch_result_json = {}
        return branch
    step.branch_result_json = branch
    selected = set(branch.get('selected') or [])
    for target_id in branch.get('skipped') or []:
        if target_id in selected:
            continue
        target = WorkflowRunStep.query.filter_by(
            run_id=run.id,
            step_id=target_id,
        ).first()
        if target and target.status == 'pending':
            target.status = 'skipped'
            target.summary = target.summary or '分支条件未命中，自动跳过'
            target.finished_at = datetime.now()
            target.updated_by = step.updated_by or _actor_name()
    return branch


def _resolve_step_target_claw(step):
    if step.target_claw_id:
        return step.target_claw_id
    target_agent = (step.target_agent or '').strip()
    if not target_agent:
        return None
    claw = OpenClawInstance.query.filter(
        OpenClawInstance.status != 'deleted',
        db.or_(
            OpenClawInstance.name == target_agent,
            OpenClawInstance.safe_name == target_agent,
            OpenClawInstance.claw_tag == target_agent,
        )
    ).first()
    return claw.id if claw else None


def _step_target_post(step):
    if getattr(step, 'target_post', None):
        return (step.target_post or '').strip()
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    return str(config.get('target_post') or '').strip()


def _run_project_id(run):
    definition = getattr(run, 'definition', None) if run else None
    return getattr(definition, 'project_id', None)


def _active_step_counts(claw_ids, current_step_id=None):
    if not claw_ids:
        return {}
    q = WorkflowRunStep.query.filter(
        WorkflowRunStep.target_claw_id.in_(claw_ids),
        WorkflowRunStep.status.in_(('running', 'retrying', 'waiting_approval')),
    )
    if current_step_id:
        q = q.filter(WorkflowRunStep.id != current_step_id)
    counts = {}
    for row in q.with_entities(
            WorkflowRunStep.target_claw_id,
            db.func.count(WorkflowRunStep.id)).group_by(
                WorkflowRunStep.target_claw_id).all():
        counts[int(row[0])] = int(row[1] or 0)
    return counts


def _assignment_exam_passed(post, assignment):
    profile = getattr(post, 'profile', None)
    exam_paper_id = getattr(profile, 'exam_paper_id', None)
    if not exam_paper_id:
        return True
    if not assignment.exam_session_id:
        return False
    session_row = ExamSession.query.get(assignment.exam_session_id)
    return bool(
        session_row
        and session_row.paper_id == exam_paper_id
        and session_row.examinee_claw_id == assignment.claw_id
        and session_row.passed
    )


def _post_candidate_rows(post_key, project_id, current_step_id=None):
    rows = AgentPostAssignment.query.filter_by(status='active').all()
    claw_ids = [r.claw_id for r in rows if r.claw_id]
    active_counts = _active_step_counts(claw_ids, current_step_id=current_step_id)
    candidates = []
    for assignment in rows:
        post = getattr(assignment, 'post', None)
        claw = getattr(assignment, 'claw', None)
        if not post or not claw:
            continue
        if post.status != 'active':
            continue
        if post.post_key != post_key:
            continue
        if project_id is not None and post.project_id not in (None, project_id):
            continue
        candidates.append({
            'claw_id': claw.id,
            'post_key': post.post_key,
            'project_id': post.project_id,
            'profile_version': assignment.profile_version or 1,
            'required_profile_version': post.required_profile_version or 1,
            'exam_passed': _assignment_exam_passed(post, assignment),
            'status': claw.status or '',
            'last_activity': claw.last_activity,
            'active_steps': active_counts.get(claw.id, 0),
        })
    return candidates


def _resolve_step_target_post(step, exclude_claw_ids=None):
    post_key = _step_target_post(step)
    if not post_key:
        return None
    result = resolve_post_candidate(
        _post_candidate_rows(
            post_key,
            _run_project_id(step.run),
            current_step_id=step.id,
        ),
        post_key=post_key,
        project_id=_run_project_id(step.run),
        now=datetime.now(),
        exclude_claw_ids=exclude_claw_ids or set(),
    )
    claw_id = result.get('claw_id')
    if claw_id:
        step.target_claw_id = claw_id
        step.target_post = post_key
        return claw_id
    step.status = 'blocked'
    step.blocker_json = {
        'type': 'no_available_agent_post',
        'message': '工位 %s 当前没有可用 Agent' % post_key,
    }
    step.finished_at = datetime.now()
    return None


def _owner_fallback_claw_id(step):
    definition = step.run.definition if step.run else None
    if not definition:
        return None
    if definition.owner_type == 'claw' and definition.owner_id:
        return definition.owner_id
    if definition.owner_type == 'user' and definition.owner_id:
        user = User.query.get(definition.owner_id)
        if user and user.bound_claw_id:
            return user.bound_claw_id
    return None


def _resolve_step_target_claw_ids(step):
    config = step.step_config_json or {}
    start_vars = _run_start_vars(step.run) if step.run else {}
    if isinstance(config, dict):
        for key in ('executor_claw_ids_var', 'target_claw_ids_var', 'target_claw_id_var'):
            var_path = config.get(key)
            if not var_path:
                continue
            raw_value = _dig_start_var(start_vars, var_path)
            if raw_value in (None, '', []):
                continue
            resolved = resolve_start_var_claw_ids(start_vars, var_path, strict=True)
            if resolved:
                return resolved
            raise ValueError('启动参数 %s 不是有效 Agent ID：%s' % (var_path, raw_value))
    raw_ids = config.get('executor_claw_ids') if isinstance(config, dict) else None
    result = []
    if isinstance(raw_ids, list):
        for item in raw_ids:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
    if result:
        return sorted(set(result))
    single = _resolve_step_target_claw(step)
    if not single:
        single = _resolve_step_target_post(step)
    if not single:
        single = _owner_fallback_claw_id(step)
    return [single] if single else []


def _dig_start_var(start_vars, path):
    current = start_vars if isinstance(start_vars, dict) else {}
    for part in str(path or '').split('.'):
        part = part.strip()
        if not part:
            return None
        if not isinstance(current, dict) or part not in current:
            return None
        current = current.get(part)
    return current


def _step_allowed_claw_ids(step):
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    run_context = (
        step.run.context_json
        if step.run and isinstance(step.run.context_json, dict)
        else {}
    )
    workflow_start = (
        run_context.get('workflow_start')
        if isinstance(run_context.get('workflow_start'), dict)
        else {}
    )
    return resolve_workflow_step_claw_ids(
        step.step_type,
        target_claw_id=step.target_claw_id,
        step_executor_claw_ids=config.get('executor_claw_ids'),
        run_executor_claw_ids=workflow_start.get('executor_claw_ids'),
        target_post=getattr(step, 'target_post', ''),
        target_agent=getattr(step, 'target_agent', ''),
    )


def _step_belongs_to_claw(step, claw_id):
    allowed = _step_allowed_claw_ids(step)
    if allowed:
        return int(claw_id) in allowed
    owner_claw_id = _owner_fallback_claw_id(step)
    return bool(owner_claw_id and int(claw_id) == int(owner_claw_id))


def _existing_claws_by_id(claw_ids):
    ids = sorted(set(int(x) for x in claw_ids if x))
    if not ids:
        return {}
    claws = OpenClawInstance.query.filter(
        OpenClawInstance.id.in_(ids),
        OpenClawInstance.status != 'deleted',
    ).all()
    return {c.id: c for c in claws}


def _workflow_agent_task_id(run_id, step_id, attempt_no, target_claw_id=None):
    raw = 'workflow_%s_%s_%s' % (run_id, step_id, attempt_no or 1)
    if target_claw_id:
        raw = '%s_%s' % (raw, target_claw_id)
    if len(raw) <= 64:
        return raw
    import hashlib
    digest = hashlib.md5(str(step_id).encode('utf-8')).hexdigest()[:12]
    return 'workflow_%s_%s_%s_%s' % (run_id, digest, attempt_no or 1, target_claw_id or 'task')


def _dispatch_agent_start_message(step, target_claw_id, task_id):
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    if not config.get('notify_agent_on_start'):
        return
    refs = _format_step_reference_lines(config)
    content = (
        f'Workflow Step #{step.run_id}/{step.step_id} 已开始\n'
        f'名称：{step.name}\n'
        f'Runner：{step.runner or "-"}\n'
        f'任务：{task_id}\n'
        f'请领取并执行该节点；执行过程中可回写 progress，完成后回写 /api/v1/workflow-runs/{step.run_id}/steps/{step.step_id}/result'
        + (('\n' + '\n'.join(refs)) if refs else '')
    )
    exists = ClawMessage.query.filter_by(
        claw_id=target_claw_id,
        msg_type='task_delegate',
        content=content,
    ).first()
    if exists:
        return
    db.session.add(ClawMessage(
        claw_id=target_claw_id,
        sender_name='Workflow',
        msg_type='task_delegate',
        content=content,
    ))


def _format_step_reference_lines(config):
    refs = config.get('references') if isinstance(config, dict) and isinstance(config.get('references'), list) else []
    if not refs:
        return []
    lines = ['参考内容：']
    for ref in refs[:8]:
        if not isinstance(ref, dict):
            continue
        label = ref.get('title') or ref.get('url') or ref.get('path') or ref.get('id') or '-'
        lines.append('- %s: %s' % (ref.get('type') or 'ref', label))
    return lines if len(lines) > 1 else []


def _dispatch_agent_task(step):
    try:
        target_claw_ids = _resolve_step_target_claw_ids(step)
    except ValueError as exc:
        step.status = 'blocked'
        step.blocker_json = {
            'type': 'invalid_target_agent',
            'message': str(exc),
        }
        step.finished_at = datetime.now()
        return
    if not target_claw_ids:
        step.status = 'blocked'
        step.blocker_json = {
            'type': 'missing_target_agent',
            'message': 'agent_task 必须配置 target_claw_id，或 target_agent 匹配 OpenClaw 名称',
        }
        step.finished_at = datetime.now()
        return
    claws_by_id = _existing_claws_by_id(target_claw_ids)
    missing_ids = [cid for cid in target_claw_ids if cid not in claws_by_id]
    if missing_ids:
        step.status = 'blocked'
        step.blocker_json = {
            'type': 'missing_target_agent',
            'message': '执行 Agent 不存在：%s' % ','.join(map(str, missing_ids)),
        }
        step.finished_at = datetime.now()
        return
    step.target_claw_id = target_claw_ids[0] if len(target_claw_ids) == 1 else None
    step.target_agent = (
        claws_by_id[target_claw_ids[0]].name
        if len(target_claw_ids) == 1
        else '多执行人：' + '、'.join(claws_by_id[cid].name for cid in target_claw_ids)
    )
    try:
        from app.api.agent_client import AgentTask, notify_claw
    except Exception as exc:
        step.status = 'blocked'
        step.blocker_json = {
            'type': 'agent_task_dispatch_unavailable',
            'message': 'Hub AgentTask 通道不可用：%s' % exc,
        }
        step.finished_at = datetime.now()
        return
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    config['executor_claw_ids'] = target_claw_ids
    step.step_config_json = config
    base_task_id = _workflow_agent_task_id(step.run_id, step.step_id, step.attempt_no or 1)
    payload = build_workflow_agent_task_payload(
        step.run.to_dict(with_steps=False) if step.run else {'id': step.run_id},
        step.to_dict(with_context=True),
        _workflow_outputs_context(step.run) if step.run else {},
    )
    if not _shift_left_enabled():
        payload.pop('analysis', None)
    for target_claw_id in target_claw_ids:
        task_id = (
            base_task_id
            if len(target_claw_ids) == 1
            else _workflow_agent_task_id(step.run_id, step.step_id, step.attempt_no or 1, target_claw_id)
        )
        existing = AgentTask.query.filter_by(task_id=task_id, claw_id=target_claw_id).first()
        if not existing:
            task = AgentTask(
                claw_id=target_claw_id,
                task_id=task_id,
                task_type='workflow_agent_task',
                command=step.runner or '',
                target_path='',
                payload=jsonify_safe(payload),
                status='pending',
            )
            db.session.add(task)
        _dispatch_agent_start_message(step, target_claw_id, task_id)
        notify_claw(target_claw_id)


def jsonify_safe(payload):
    import json
    return json.dumps(payload, ensure_ascii=False)


def _dispatch_step_message(step):
    if step.step_type == 'agent_task':
        _dispatch_agent_task(step)
        return
    target_claw_ids = _step_allowed_claw_ids(step)
    if not target_claw_ids:
        target_claw_id = (
            step.target_claw_id
            or _resolve_step_target_post(step)
            or _owner_fallback_claw_id(step)
        )
        target_claw_ids = [target_claw_id] if target_claw_id else []
    if not target_claw_ids:
        step.progress_message = step.progress_message or '节点已进入执行中，但未找到可通知的 Agent/owner'
        return
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    refs = _format_step_reference_lines(config)
    content = (
        f'Workflow Step #{step.run_id}/{step.step_id}\n'
        f'名称：{step.name}\n'
        f'Runner：{step.runner}\n'
        f'请执行后回写 /api/v1/workflow-runs/{step.run_id}/steps/{step.step_id}/result'
        + (('\n' + '\n'.join(refs)) if refs else '')
    )
    for target_claw_id in target_claw_ids:
        db.session.add(ClawMessage(
            claw_id=target_claw_id,
            sender_name='Workflow',
            msg_type='task_delegate',
            content=content,
        ))
        try:
            from app.api.agent_client import notify_claw
            notify_claw(target_claw_id)
        except Exception:
            pass


def _step_notice_target_claw_ids(step):
    """Resolve Agent ids that should receive step handling notices."""
    targets = []
    if step.target_claw_id:
        try:
            targets.append(int(step.target_claw_id))
        except (TypeError, ValueError):
            pass
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    raw_ids = config.get('executor_claw_ids') if isinstance(config.get('executor_claw_ids'), list) else []
    for raw in raw_ids:
        try:
            targets.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not targets:
        owner_id = _owner_fallback_claw_id(step)
        if owner_id:
            targets.append(int(owner_id))
    return sorted(set(targets))


def _dispatch_step_blocked_notice(step):
    if step.status not in ('blocked', 'failed'):
        return
    target_claw_ids = _step_notice_target_claw_ids(step)
    if not target_claw_ids:
        step.progress_message = step.progress_message or '节点已阻断，但未找到可通知的 Agent/owner'
        return
    notice = build_step_blocked_notice(
        run_id=step.run_id,
        step_id=step.step_id,
        step_name=step.name,
        status=step.status,
        summary=step.summary,
        blocker=step.blocker_json or {},
    )
    try:
        from app.api.agent_client import notify_claw
    except Exception:
        notify_claw = None
    for claw_id in target_claw_ids:
        msg_exists = ClawMessage.query.filter_by(
            claw_id=claw_id,
            msg_type='task_delegate',
            content=notice['content'],
        ).first()
        if not msg_exists:
            db.session.add(ClawMessage(
                claw_id=claw_id,
                sender_name='Workflow',
                msg_type='task_delegate',
                content=notice['content'],
            ))
        todo_exists = ClawTodo.query.filter_by(
            openclaw_id=claw_id,
            title=notice['title'],
            created_by='workflow-blocked',
        ).first()
        if not todo_exists:
            db.session.add(ClawTodo(
                openclaw_id=claw_id,
                title=notice['title'][:200],
                description=notice['content'],
                schedule_type='once',
                urgency_level='interrupt',
                priority='P0',
                task_category='routine',
                enabled=True,
                created_by='workflow-blocked',
            ))
        if notify_claw:
            notify_claw(claw_id)


def _recompute_run_status(run, actor='system'):
    """Advance pending steps whose dependencies are satisfied."""
    steps = WorkflowRunStep.query.filter_by(run_id=run.id).all()
    state = {s.step_id: s.status for s in steps}
    definition = run.definition.definition_json or {}

    if any(s.status == 'blocked' for s in steps):
        run.status = 'blocked'
        blocked = next((s for s in steps if s.status == 'blocked'), None)
        run.current_step_id = blocked.step_id if blocked else ''
        run.blocker_json = blocked.blocker_json if blocked else {}
        return
    if any(s.status == 'failed' for s in steps):
        run.status = 'failed'
        failed = next((s for s in steps if s.status == 'failed'), None)
        run.current_step_id = failed.step_id if failed else ''
        return
    if any(s.status == 'waiting_approval' for s in steps):
        run.status = 'waiting_approval'
        wait = next((s for s in steps if s.status == 'waiting_approval'), None)
        run.current_step_id = wait.step_id if wait else ''
        if wait:
            _create_approval_if_missing(run, wait, actor)
        return
    if steps and all(s.status in ('passed', 'skipped') for s in steps):
        run.status = 'succeeded'
        run.current_step_id = ''
        run.finished_at = datetime.now()
        return

    ready_ids = ready_step_ids(definition, state)
    step_by_id = {s.step_id: s for s in steps}
    if ready_ids:
        run.status = 'running'
        run.started_at = run.started_at or datetime.now()
        run.current_step_id = ready_ids[0]
        for sid in ready_ids:
            step = step_by_id.get(sid)
            if not step:
                continue
            config = step.step_config_json or {}
            if config.get('approval_required'):
                step.status = 'waiting_approval'
                _create_approval_if_missing(run, step, actor)
                run.status = 'waiting_approval'
                run.current_step_id = step.step_id
                break
            step.status = 'running'
            step.attempt_no = (step.attempt_no or 0) + 1
            step.started_at = datetime.now()
            step.dispatched_at = datetime.now()
            step.heartbeat_at = None
            step.heartbeat_by = ''
            step.heartbeat_count = 0
            step.missed_heartbeat_count = 0
            step.health_status = 'stale'
            step.health_checked_at = step.dispatched_at
            _reset_step_progress(step)
            step.progress_at = step.dispatched_at
            step.progress_by = 'workflow'
            step.progress_phase = 'dispatched'
            step.progress_message = '节点已派发，等待执行器心跳与进度回传'
            _dispatch_step_message(step)
            if step.status == 'blocked':
                run.status = 'blocked'
                run.current_step_id = step.step_id
                run.blocker_json = step.blocker_json or {}
                break
        return

    if any(s.status in ('running', 'retrying') for s in steps):
        run.status = 'running'
        run.current_step_id = next(
            s.step_id for s in steps if s.status in ('running', 'retrying'))
    else:
        run.status = 'pending'


@api_bp.route('/workflow-definitions', methods=['GET'])
def list_workflow_definitions():
    err = _require_actor()
    if err:
        return err
    _ensure_default_definition()
    rows = WorkflowDefinition.query.filter(
        WorkflowDefinition.status != 'deleted'
    ).order_by(
        WorkflowDefinition.updated_at.desc()).all()
    favorite_only = request.args.get('favorite') in ('1', 'true', 'True')
    favorite_ids = _actor_favorite_definition_ids() if favorite_only else None
    visible = []
    for row in rows:
        if favorite_only and row.id not in favorite_ids:
            continue
        if _definition_visible(row):
            visible.append(_definition_payload(row, brief=True))
    if 'page' in request.args or 'per_page' in request.args:
        page = paginate_items(
            visible,
            page=request.args.get('page'),
            per_page=request.args.get('per_page'),
        )
        return jsonify(page)
    return jsonify(visible)


@api_bp.route('/workflow-definitions/<int:definition_id>', methods=['GET'])
def get_workflow_definition(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if row.status == 'deleted':
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions/<int:definition_id>/start-options', methods=['GET'])
def get_workflow_definition_start_options(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if row.status == 'deleted':
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    data = _definition_payload(row)
    data['start_options'] = _definition_project_actor_options(row)
    return jsonify(data)


@api_bp.route('/workflow-definitions/<int:definition_id>', methods=['PATCH', 'PUT'])
def update_workflow_definition(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if row.status == 'deleted':
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _can_manage_definition(row):
        return jsonify({'error': '只有 Workflow 创建者或管理员可以更新模板'}), 403
    data = request.get_json() or {}
    try:
        definition = merge_workflow_definition_update(row.definition_json or {}, data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    row.name = data.get('name') or definition.get('name') or row.name
    row.description = data.get('description') if 'description' in data else definition.get('description', row.description)
    row.definition_json = definition
    row.version = definition.get('version') or row.version or 1
    if 'status' in data:
        status = data.get('status') or 'active'
        if status not in ('active', 'disabled', 'draft'):
            return jsonify({'error': 'status 必须是 active/disabled/draft'}), 400
        row.status = status
    if 'visibility_scope' in data:
        visibility_scope = data.get('visibility_scope') or 'project'
        if visibility_scope not in ('project', 'private'):
            return jsonify({'error': 'visibility_scope 必须是 project 或 private'}), 400
        row.visibility_scope = visibility_scope
    db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions', methods=['POST'])
def create_workflow_definition():
    err = _require_actor()
    if err:
        return err
    actor = _actor_identity()
    if not actor:
        return jsonify({'error': '未认证'}), 401
    data = request.get_json() or {}
    try:
        definition = normalize_workflow_definition(data.get('definition') or data)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if WorkflowDefinition.query.filter_by(workflow_key=definition['key']).first():
        return jsonify({'error': 'workflow_key 已存在'}), 409
    visibility_scope = data.get('visibility_scope') or definition.get('visibility_scope') or 'project'
    if visibility_scope not in ('project', 'private'):
        visibility_scope = 'project'
    row = WorkflowDefinition(
        workflow_key=definition['key'],
        name=definition['name'],
        description=definition.get('description') or '',
        project_id=_resolve_definition_project_id(data),
        definition_json=definition,
        status=data.get('status') or 'active',
        version=definition.get('version') or 1,
        created_by=_actor_name(),
        owner_type=actor['type'],
        owner_id=actor['id'],
        executor_acl_json=normalize_executor_acl({
            'claw_ids': [actor['id']] if actor['type'] == 'claw' else [],
            'user_ids': [actor['id']] if actor['type'] == 'user' else [],
        }),
        visibility_scope=visibility_scope,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(_definition_payload(row)), 201


@api_bp.route('/workflow-definitions/<int:definition_id>/visibility', methods=['PATCH', 'POST'])
def update_workflow_definition_visibility(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if row.owner_type == 'system' or row.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}:
        return jsonify({'error': '系统内置 Workflow 模板不能修改可见性'}), 403
    if not _can_manage_definition(row):
        return jsonify({'error': '只有 Workflow 创建者或管理员可以修改可见性'}), 403
    data = request.get_json() or {}
    visibility_scope = data.get('visibility_scope') or data.get('scope')
    if visibility_scope not in ('project', 'private'):
        return jsonify({'error': 'visibility_scope 必须是 project 或 private'}), 400
    row.visibility_scope = visibility_scope
    db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions/<int:definition_id>/favorite', methods=['POST'])
def favorite_workflow_definition(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if row.status == 'deleted' or not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    actor = _actor_identity()
    filters = {'definition_id': row.id}
    values = {'definition_id': row.id}
    if actor['type'] == 'claw':
        filters['claw_id'] = actor['id']
        values['claw_id'] = actor['id']
    else:
        filters['user_id'] = actor['id']
        values['user_id'] = actor['id']
    favorite = WorkflowDefinitionFavorite.query.filter_by(**filters).first()
    if not favorite:
        db.session.add(WorkflowDefinitionFavorite(**values))
        db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions/<int:definition_id>/favorite', methods=['DELETE'])
def unfavorite_workflow_definition(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    actor = _actor_identity()
    filters = {'definition_id': row.id}
    if actor['type'] == 'claw':
        filters['claw_id'] = actor['id']
    else:
        filters['user_id'] = actor['id']
    favorite = WorkflowDefinitionFavorite.query.filter_by(**filters).first()
    if favorite:
        db.session.delete(favorite)
        db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions/<int:definition_id>', methods=['DELETE'])
def delete_workflow_definition(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if row.owner_type == 'system' or row.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}:
        return jsonify({'error': '系统内置 Workflow 模板不能删除'}), 403
    if not _can_manage_definition(row):
        return jsonify({'error': '只有 Workflow 创建者或管理员可以删除模板'}), 403
    original_key = row.workflow_key
    row.status = 'deleted'
    row.workflow_key = f'{original_key}__deleted_{row.id}'
    row.name = f'{row.name}（已删除）'
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': row.id})


@api_bp.route('/workflow-definitions/<int:definition_id>/executors', methods=['POST'])
def add_workflow_definition_executor(definition_id):
    err = _require_actor()
    if err:
        return err
    row = WorkflowDefinition.query.get_or_404(definition_id)
    if not _definition_visible(row):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _can_manage_definition(row):
        return jsonify({'error': '只有 Workflow 创建者或管理员可以添加执行者'}), 403
    data = request.get_json() or {}
    raw_claw_ids = data.get('claw_ids')
    raw_user_ids = data.get('user_ids')
    if isinstance(raw_claw_ids, list) or isinstance(raw_user_ids, list):
        claw_ids = []
        user_ids = []
        for raw in raw_claw_ids or []:
            try:
                claw_ids.append(int(raw))
            except (TypeError, ValueError):
                return jsonify({'error': 'claw_ids 必须是整数数组'}), 400
        for raw in raw_user_ids or []:
            try:
                user_ids.append(int(raw))
            except (TypeError, ValueError):
                return jsonify({'error': 'user_ids 必须是整数数组'}), 400
        claw_ids = sorted(set(claw_ids))
        user_ids = sorted(set(user_ids))
        if not claw_ids and not user_ids:
            return jsonify({'error': '至少选择一个执行者'}), 400
        existing_claw_ids = {
            row.id for row in OpenClawInstance.query.filter(OpenClawInstance.id.in_(claw_ids)).all()
        } if claw_ids else set()
        missing_claws = [cid for cid in claw_ids if cid not in existing_claw_ids]
        if missing_claws:
            return jsonify({'error': 'OpenClaw 不存在：%s' % ','.join(map(str, missing_claws))}), 404
        existing_user_ids = {
            row.id for row in User.query.filter(User.id.in_(user_ids)).all()
        } if user_ids else set()
        missing_users = [uid for uid in user_ids if uid not in existing_user_ids]
        if missing_users:
            return jsonify({'error': '用户不存在：%s' % ','.join(map(str, missing_users))}), 404

        acl = normalize_executor_acl(row.executor_acl_json or {})
        acl['claw_ids'] = sorted(set((acl.get('claw_ids') or []) + claw_ids))
        acl['user_ids'] = sorted(set((acl.get('user_ids') or []) + user_ids))
        row.executor_acl_json = acl
        db.session.commit()
        return jsonify(_definition_payload(row))

    actor_type = data.get('actor_type')
    actor_id = data.get('actor_id')
    if data.get('claw_id'):
        actor_type = 'claw'
        actor_id = data.get('claw_id')
    if data.get('user_id'):
        actor_type = 'user'
        actor_id = data.get('user_id')
    if actor_type not in ('claw', 'user'):
        return jsonify({'error': 'actor_type 必须是 claw 或 user'}), 400
    try:
        actor_id = int(actor_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'actor_id 必须是整数'}), 400

    if actor_type == 'claw' and not OpenClawInstance.query.get(actor_id):
        return jsonify({'error': 'OpenClaw 不存在'}), 404
    if actor_type == 'user' and not User.query.get(actor_id):
        return jsonify({'error': '用户不存在'}), 404

    acl = normalize_executor_acl(row.executor_acl_json or {})
    key = 'claw_ids' if actor_type == 'claw' else 'user_ids'
    acl[key] = sorted(set((acl.get(key) or []) + [actor_id]))
    row.executor_acl_json = acl
    db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-runs', methods=['GET'])
def list_workflow_runs():
    err = _require_actor()
    if err:
        return err
    _refresh_workflow_step_health(commit=True)
    q = WorkflowRun.query
    status = request.args.get('status')
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(WorkflowRun.created_at.desc()).limit(100).all()
    rows = [r for r in rows if r.definition and _definition_visible(r.definition)]
    payload = [_run_payload(r, with_steps=False) for r in rows]
    if 'page' in request.args or 'per_page' in request.args:
        page = paginate_items(
            payload,
            page=request.args.get('page'),
            per_page=request.args.get('per_page'),
        )
        return jsonify(page)
    return jsonify(payload)


@api_bp.route('/workflow-runs', methods=['POST'])
def create_workflow_run():
    err = _require_actor()
    if err:
        return err
    data = request.get_json() or {}
    definition = None
    if data.get('definition_id'):
        definition = WorkflowDefinition.query.get(data.get('definition_id'))
    elif data.get('workflow_key'):
        definition = WorkflowDefinition.query.filter_by(
            workflow_key=data.get('workflow_key')).first()
    if not definition and data.get('workflow_key') in {w['key'] for w in BUILTIN_WORKFLOWS}:
        _ensure_default_definition()
        definition = WorkflowDefinition.query.filter_by(
            workflow_key=data.get('workflow_key')).first()
    if not definition:
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _definition_visible(definition):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if not _can_execute_definition(definition):
        return jsonify({'error': '无权启动此 Workflow，请联系创建者添加执行权限'}), 403
    normalized = copy.deepcopy(definition.definition_json or {})
    selected_claw_ids = []
    invalid_claw_ids = []
    for raw in (
        data.get('executor_claw_ids') or
        data.get('target_claw_ids') or
        data.get('selected_claw_ids') or []
    ):
        try:
            selected_claw_ids.append(int(raw))
        except (TypeError, ValueError):
            invalid_claw_ids.append(str(raw))
    if invalid_claw_ids:
        return jsonify({'error': '执行 Agent ID 非法：%s' % ','.join(invalid_claw_ids)}), 400
    selected_claw_ids = sorted(set(selected_claw_ids))
    selected_user_ids = []
    for raw in (data.get('executor_user_ids') or data.get('selected_user_ids') or []):
        try:
            selected_user_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    selected_user_ids = sorted(set(selected_user_ids))

    selected_claws = []
    if selected_claw_ids:
        selected_claws = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(selected_claw_ids),
            OpenClawInstance.status != 'deleted',
        ).all()
        existing_ids = {c.id for c in selected_claws}
        missing = [cid for cid in selected_claw_ids if cid not in existing_ids]
        if missing:
            return jsonify({'error': '执行 Agent 不存在：%s' % ','.join(map(str, missing))}), 400
        selected_claw_names = {c.id: c.name for c in selected_claws}
        for step in normalized.get('steps') or []:
            if step.get('type') != 'agent_task':
                continue
            step['executor_claw_ids'] = selected_claw_ids
            if len(selected_claw_ids) == 1:
                cid = selected_claw_ids[0]
                step['target_claw_id'] = cid
                step['target_agent'] = selected_claw_names.get(cid) or str(cid)
            else:
                step['target_claw_id'] = None
                step['target_agent'] = '多执行人：' + '、'.join(
                    selected_claw_names.get(cid) or str(cid) for cid in selected_claw_ids
                )

    start_mode = data.get('start_mode') or 'immediate'
    schedule_cron = (data.get('schedule_cron') or '').strip()
    raw_context = data.get('context') if isinstance(data.get('context'), dict) else {}
    start_vars = (
        data.get('start_vars')
        if isinstance(data.get('start_vars'), dict)
        else data.get('variables')
        if isinstance(data.get('variables'), dict)
        else data.get('start_parameters')
        if isinstance(data.get('start_parameters'), dict)
        else {}
    )
    context = build_workflow_start_context(
        start_vars,
        raw_context,
        start_mode=start_mode,
        schedule_cron=schedule_cron,
        executor_claw_ids=selected_claw_ids,
        executor_user_ids=selected_user_ids,
    )
    run = WorkflowRun(
        definition_id=definition.id,
        run_name=data.get('run_name') or normalized.get('name') or definition.name,
        status='pending',
        project_id=data.get('project_id') or definition.project_id,
        triggered_by=_actor_name(),
        context_json=context,
    )
    db.session.add(run)
    db.session.flush()
    for idx, step in enumerate(normalized.get('steps') or []):
        status = 'pending'
        if step.get('approval_required') and not step.get('depends_on'):
            status = 'waiting_approval'
        row = WorkflowRunStep(
            run_id=run.id,
            step_id=step['id'],
            position=idx,
            name=step['name'],
            step_type=step.get('type') or 'worker_task',
            runner=step.get('runner') or '',
            target_claw_id=step.get('target_claw_id'),
            target_agent=step.get('target_agent') or '',
            target_post=step.get('target_post') or '',
            status=status,
            depends_on_json=step.get('depends_on') or [],
            step_config_json=step,
        )
        db.session.add(row)
    db.session.flush()
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True)), 201


@api_bp.route('/workflow-runs/<int:run_id>', methods=['GET'])
def get_workflow_run(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    _refresh_workflow_step_health(run, commit=True)
    return jsonify(_run_payload(run, with_steps=True))


def _run_start_value(run, key):
    ctx = run.context_json if isinstance(run.context_json, dict) else {}
    start_vars = ctx.get('start_vars') if isinstance(ctx.get('start_vars'), dict) else {}
    return start_vars.get(key) or ctx.get(key)


def _agent_team_delivery_payload(run):
    iteration_id = _run_start_value(run, 'iteration_id')
    review_key = str(run.id)
    reqs = []
    verdicts = []
    coverage = None
    if iteration_id:
        reqs = RequirementItem.query.filter_by(
            iteration_id=iteration_id).order_by(RequirementItem.id.asc()).all()
        req_ids = [r.id for r in reqs]
        verdicts = RequirementReviewVerdict.query.filter_by(
            iteration_id=iteration_id, review_key=review_key).all()
        links = []
        if req_ids:
            links = RequirementTestcaseLink.query.filter(
                RequirementTestcaseLink.requirement_item_id.in_(req_ids)).all()
        coverage = summarize_requirement_coverage(reqs, links)
    ctx = run.context_json if isinstance(run.context_json, dict) else {}
    return {
        'run': _run_payload(run, with_steps=True),
        'iteration_id': iteration_id,
        'review_key': review_key,
        'requirement_count': len(reqs),
        'review_verdict_count': len(verdicts),
        'review_verdicts': [v.to_dict() for v in verdicts],
        'coverage': coverage,
        'confirmation': ctx.get('agent_team_delivery') or {},
    }


@api_bp.route('/workflow-runs/<int:run_id>/agent-team-delivery',
              methods=['GET'])
def get_agent_team_delivery(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    return jsonify(_agent_team_delivery_payload(run))


@api_bp.route('/workflow-runs/<int:run_id>/agent-team-delivery/confirm',
              methods=['POST'])
def confirm_agent_team_delivery(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    data = request.get_json() or {}
    decision = str(data.get('decision') or 'approved').strip()
    if decision not in ('approved', 'rejected'):
        return jsonify({'error': 'decision 必须是 approved/rejected'}), 400
    ctx = copy.deepcopy(run.context_json) if isinstance(run.context_json, dict) else {}
    ctx['agent_team_delivery'] = {
        'decision': decision,
        'operator': _actor_name(),
        'confirmed_at': datetime.now().isoformat(),
        'note': str(data.get('note') or '').strip(),
        'external_actions': data.get('external_actions') if isinstance(data.get('external_actions'), list) else [],
    }
    run.context_json = ctx
    if decision == 'approved':
        library_id = data.get('test_case_library_id') or _run_start_value(run, 'test_case_library_id')
        if library_id:
            library = TestCaseLibrary.query.get(library_id)
            if library:
                library.review_status = 'approved'
    db.session.commit()
    log_action('confirm', 'workflow_agent_team_delivery', run.id,
               'decision=%s' % decision, operator=_actor_name())
    return jsonify(_agent_team_delivery_payload(run))


@api_bp.route('/workflow-runs/<int:run_id>', methods=['DELETE'])
def delete_workflow_run(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    if not _can_delete_run(run):
        return jsonify({'error': '只有 Run 发起者、模板创建者或管理员可以删除运行记录'}), 403
    WorkflowArtifact.query.filter_by(run_id=run.id).delete()
    WorkflowApproval.query.filter_by(run_id=run.id).delete()
    WorkflowRunStep.query.filter_by(run_id=run.id).delete()
    db.session.delete(run)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': run_id})


@api_bp.route('/workflow-runs/worker/tasks', methods=['GET'])
def list_worker_workflow_tasks():
    """Bearer Agent/worker pulls currently running workflow steps."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    _refresh_workflow_step_health(commit=True)
    rows = WorkflowRunStep.query.join(
        WorkflowRun,
        WorkflowRun.id == WorkflowRunStep.run_id,
    ).filter(
        WorkflowRun.status.in_(('running', 'retrying')),
        WorkflowRunStep.status.in_(('running', 'retrying')),
        db.or_(
            WorkflowRunStep.target_claw_id == claw.id,
            WorkflowRunStep.target_claw_id.is_(None),
        ),
    ).order_by(WorkflowRunStep.updated_at.asc()).limit(20).all()
    rows = [row for row in rows if _step_belongs_to_claw(row, claw.id)]
    return jsonify([_step_runtime_payload(r) for r in rows])


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/claim', methods=['POST'])
def claim_workflow_step(run_id, step_id):
    """Worker claims a running step before executing it."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    data = request.get_json() or {}
    worker_id = (data.get('worker_id') or f'claw:{claw.id}').strip()
    lease_seconds = int(data.get('lease_seconds') or 180)
    now = datetime.now()
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Run 当前不可执行'}), 409
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    if not can_claim_step(step.claimed_by, step.claimed_at, worker_id, now, lease_seconds):
        return jsonify({
            'error': 'Workflow Step 已被其他 worker 认领',
            'claimed_by': step.claimed_by or '',
            'claimed_at': str(step.claimed_at) if step.claimed_at else None,
        }), 409
    step.claimed_by = worker_id
    step.claimed_at = now
    db.session.commit()
    return jsonify({'ok': True, 'step': _step_runtime_payload(step), 'lease_seconds': lease_seconds})


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/heartbeat', methods=['POST'])
def heartbeat_workflow_step(run_id, step_id):
    """Worker reports liveness while executing a workflow step."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    data = request.get_json() or {}
    worker_id = (data.get('worker_id') or f'claw:{claw.id}').strip()
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Run 当前不可执行'}), 409
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    now = datetime.now()
    step.heartbeat_at = now
    step.heartbeat_by = worker_id
    step.heartbeat_count = (step.heartbeat_count or 0) + 1
    step.missed_heartbeat_count = 0
    step.health_status = 'healthy'
    step.health_checked_at = now
    _apply_step_progress(step, data, worker_id, now)
    if not step.claimed_by:
        step.claimed_by = worker_id
        step.claimed_at = now
    db.session.commit()
    return jsonify({'ok': True, 'step': _step_runtime_payload(step)})


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/progress', methods=['POST'])
def progress_workflow_step(run_id, step_id):
    """Worker/agent reports human-readable progress for a long-running step."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    data = request.get_json() or {}
    reporter = (data.get('worker_id') or data.get('reporter') or f'claw:{claw.id}').strip()
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Run 当前不可执行'}), 409
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    now = datetime.now()
    updated = _apply_step_progress(step, data, reporter, now)
    if not updated:
        return jsonify({'error': '缺少 progress 字段：phase/message/percent/progress 至少一个'}), 400
    if data.get('heartbeat') is True:
        step.heartbeat_at = now
        step.heartbeat_by = reporter
        step.heartbeat_count = (step.heartbeat_count or 0) + 1
        step.missed_heartbeat_count = 0
        step.health_status = 'healthy'
        step.health_checked_at = now
    db.session.commit()
    return jsonify({'ok': True, 'step': _step_runtime_payload(step)})


def _can_change_workflow_step_status(run, step):
    if run.definition and _can_manage_definition(run.definition):
        return True
    if is_admin_user():
        return True
    claw = get_current_claw()
    if not claw:
        return False
    allowed = _step_allowed_claw_ids(step)
    if allowed:
        return int(claw.id) in allowed
    owner_claw_id = _owner_fallback_claw_id(step)
    return bool(owner_claw_id and int(claw.id) == int(owner_claw_id))


def _apply_manual_step_display_status(step, display_state, data, previous_display_state=None):
    now = datetime.now()
    summary = data.get('summary') or ''
    actor = _actor_name()
    if display_state == 'todo':
        step.status = 'pending'
        step.started_at = None
        step.finished_at = None
        step.blocker_json = {}
        step.health_status = 'idle'
        _reset_step_progress(step)
    elif display_state == 'running':
        step.status = 'running'
        if previous_display_state != 'running':
            step.attempt_no = (step.attempt_no or 0) + 1
        step.started_at = step.started_at or now
        step.finished_at = None
        step.dispatched_at = step.dispatched_at or now
        step.blocker_json = {}
        step.health_status = 'stale'
        step.health_checked_at = now
        step.progress_at = now
        step.progress_by = actor
        step.progress_phase = 'manual_running'
        step.progress_message = summary or '已手动设为执行中'
    elif display_state == 'blocked':
        step.status = 'blocked'
        step.finished_at = now
        step.blocker_json = data.get('blocker') if isinstance(data.get('blocker'), dict) else {
            'type': 'manual_blocked',
            'message': summary or '节点被手动标记为阻断',
        }
        step.progress_at = now
        step.progress_by = actor
        step.progress_phase = 'blocked'
        step.progress_message = summary or step.blocker_json.get('message') or '节点已阻断'
    elif display_state == 'done':
        step.status = 'passed'
        step.finished_at = now
        step.blocker_json = {}
        step.progress_at = now
        step.progress_by = actor
        step.progress_phase = 'completed'
        step.progress_message = summary or '节点已结束'
        step.progress_percent = 100
        if isinstance(data.get('metrics'), dict):
            step.metrics_json = data.get('metrics')
        if isinstance(data.get('outputs'), dict):
            step.outputs_json = data.get('outputs')
    step.summary = summary
    step.updated_by = actor
    step.claimed_by = ''
    step.claimed_at = None


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/status', methods=['POST'])
def update_workflow_step_display_status(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.definition and not _definition_visible(run.definition):
        return jsonify({'error': 'workflow run 不存在'}), 404
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能修改节点状态'}), 409
    step = WorkflowRunStep.query.filter_by(run_id=run_id, step_id=step_id).first_or_404()
    if not _can_change_workflow_step_status(run, step):
        return jsonify({'error': '只有节点执行 Agent、owner 或管理员可以修改节点状态'}), 403
    data = request.get_json() or {}
    display_state = str(data.get('display_state') or data.get('state') or '').strip()
    if display_state not in ('todo', 'running', 'blocked', 'done'):
        return jsonify({'error': 'display_state 必须是 todo/running/blocked/done'}), 400
    previous_display_state = workflow_step_display_state(step.status)
    _apply_manual_step_display_status(step, display_state, data, previous_display_state)
    if display_state == 'running' and previous_display_state != 'running':
        _dispatch_step_message(step)
    if display_state == 'done':
        outputs_context = _workflow_outputs_context(run, step, step.outputs_json or {})
        _apply_step_branches(run, step, {
            'status': step.status,
            'summary': step.summary,
            'metrics': step.metrics_json or {},
            'outputs': outputs_context,
            'evidence': step.evidence_json or {},
            'logs': step.logs_json or {},
            'blocker': step.blocker_json or {},
            'context': run.context_json or {},
            'start_vars': _run_start_vars(run),
        })
    run.blocker_json = {}
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(_run_payload(run, with_steps=True))


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/result', methods=['POST'])
def report_workflow_step_result(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能回写步骤结果'}), 409
    step = WorkflowRunStep.query.filter_by(
        run_id=run_id,
        step_id=step_id,
    ).first_or_404()
    data = request.get_json() or {}
    claw = get_current_claw()
    if claw and not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    accept = can_accept_step_result_after_blocker(
        step.status,
        step.blocker_json or {},
        force_recover=bool(data.get('force_recover')),
        can_manage=_can_manage_definition(run.definition) if run.definition else is_admin_user(),
    )
    if not accept.get('accepted'):
        return jsonify({
            'error': 'Workflow Step 已因心跳丢失阻断，拒绝迟到结果覆盖',
            'reason': accept.get('reason'),
            'blocker': step.blocker_json or {},
        }), 409
    if data.get('force_recover') and accept.get('reason') != 'force_recover':
        return jsonify({'error': '只有模板创建者或管理员可以强制恢复心跳阻断步骤'}), 403
    blocker = data.get('blocker') if isinstance(data.get('blocker'), dict) else {}
    blocker_message = str(blocker.get('message') or data.get('summary') or '')
    if (
        (data.get('status') or '') == 'blocked'
        and blocker.get('type') == 'agent_invocation_failed'
        and 'timeout' in blocker_message.lower()
    ):
        now = datetime.now()
        step.status = 'running'
        step.summary = data.get('summary') or 'AgentTask 执行通道超时，等待 Agent 或 owner 继续处理'
        step.logs_json = data.get('logs') if isinstance(data.get('logs'), dict) else {}
        step.logs_json = dict(step.logs_json or {}, agent_task_timeout=blocker_message)
        step.blocker_json = {}
        step.updated_by = _actor_name()
        step.health_status = 'stale'
        step.health_checked_at = now
        step.progress_at = now
        step.progress_by = step.updated_by
        step.progress_phase = 'agent_task_timeout'
        step.progress_message = 'AgentTask 执行通道超时，节点保持执行中；请 Agent 或 owner 继续处理'
        run.status = 'running'
        run.current_step_id = step.step_id
        run.blocker_json = {}
        db.session.commit()
        return jsonify(_run_payload(run, with_steps=True))
    step.summary = data.get('summary') or ''
    step.metrics_json = data.get('metrics') if isinstance(data.get('metrics'), dict) else {}
    step.outputs_json = data.get('outputs') if isinstance(data.get('outputs'), dict) else {}
    step.evidence_json = data.get('evidence') if isinstance(data.get('evidence'), dict) else {}
    step.logs_json = data.get('logs') if isinstance(data.get('logs'), dict) else {}
    step.blocker_json = data.get('blocker') if isinstance(data.get('blocker'), dict) else {}
    step.updated_by = _actor_name()
    step.finished_at = datetime.now()
    status = data.get('status') or 'passed'
    gate = evaluate_step_gates(step.step_config_json or {}, {
        'status': status,
        'summary': step.summary,
        'metrics': step.metrics_json or {},
        'outputs': step.outputs_json or {},
        'evidence': step.evidence_json or {},
        'logs': step.logs_json or {},
        'blocker': step.blocker_json or {},
        'context': run.context_json or {},
        'start_vars': _run_start_vars(run),
    })
    step.gate_result_json = gate
    step.status = gate['status'] if not gate['passed'] else status
    step.claimed_by = ''
    step.claimed_at = None
    step.health_status = 'idle'
    step.health_checked_at = datetime.now()
    step.progress_at = step.finished_at
    step.progress_by = step.updated_by
    step.progress_phase = 'completed' if status in ('passed', 'skipped') else status
    step.progress_message = step.summary or '节点已结束'
    if status in ('passed', 'skipped'):
        step.progress_percent = 100
    if step.status not in ('passed', 'failed', 'blocked', 'skipped', 'waiting_approval'):
        step.status = 'passed'
    if step.status in ('blocked', 'failed') and not step.blocker_json:
        step.blocker_json = {'message': step.summary or 'Workflow step 未通过门禁'}
    if step.status in ('blocked', 'failed'):
        _dispatch_step_blocked_notice(step)
    if step.status in ('passed', 'skipped'):
        outputs_context = _workflow_outputs_context(run, step, step.outputs_json or {})
        _apply_step_branches(run, step, {
            'status': step.status,
            'summary': step.summary,
            'metrics': step.metrics_json or {},
            'outputs': outputs_context,
            'evidence': step.evidence_json or {},
            'logs': step.logs_json or {},
            'blocker': step.blocker_json or {},
            'context': run.context_json or {},
            'start_vars': _run_start_vars(run),
        })

    # Save evidence links as artifacts for browsing/report generation.
    for key, value in (step.evidence_json or {}).items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is None:
                continue
            db.session.add(WorkflowArtifact(
                run_id=run.id,
                step_id=step.step_id,
                artifact_type=key,
                name=str(key),
                url=str(item) if str(item).startswith(('http://', 'https://', '/')) else '',
                local_path=str(item) if not str(item).startswith(('http://', 'https://', '/')) else '',
            ))
    report, report_error = _resolve_workflow_report(run, step, data)
    if report_error:
        db.session.rollback()
        return jsonify({'error': report_error}), 400
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/retry', methods=['POST'])
def retry_workflow_step(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能重试步骤'}), 409
    step = WorkflowRunStep.query.filter_by(
        run_id=run_id,
        step_id=step_id,
    ).first_or_404()
    step.status = 'pending'
    step.blocker_json = {}
    step.gate_result_json = {}
    step.branch_result_json = {}
    step.heartbeat_at = None
    step.heartbeat_by = ''
    step.heartbeat_count = 0
    step.missed_heartbeat_count = 0
    step.health_status = 'idle'
    step.health_checked_at = None
    _reset_step_progress(step)
    step.finished_at = None
    run.status = 'pending'
    run.blocker_json = {}
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))


@api_bp.route('/workflow-runs/<int:run_id>/resume', methods=['POST'])
def resume_workflow_run(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能恢复执行'}), 409
    data = request.get_json() or {}
    from_step = data.get('from_step_id') or run.current_step_id
    if from_step:
        steps = WorkflowRunStep.query.filter_by(run_id=run.id).all()
        reset = False
        for step in sorted(steps, key=lambda s: s.position or 0):
            if step.step_id == from_step:
                reset = True
            if reset:
                step.status = 'pending'
                step.blocker_json = {}
                step.gate_result_json = {}
                step.branch_result_json = {}
                step.heartbeat_at = None
                step.heartbeat_by = ''
                step.heartbeat_count = 0
                step.missed_heartbeat_count = 0
                step.health_status = 'idle'
                step.health_checked_at = None
                _reset_step_progress(step)
                step.finished_at = None
    run.status = 'pending'
    run.blocker_json = {}
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))


@api_bp.route('/workflow-runs/<int:run_id>/approvals/<int:approval_id>/approve', methods=['POST'])
def approve_workflow(run_id, approval_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能继续审批'}), 409
    approval = WorkflowApproval.query.filter_by(
        id=approval_id,
        run_id=run_id,
    ).first_or_404()
    data = request.get_json() or {}
    approval.status = 'approved'
    approval.approver = _actor_name()
    approval.comment = data.get('comment') or ''
    approval.approved_at = datetime.now()
    step = WorkflowRunStep.query.filter_by(
        run_id=run_id,
        step_id=approval.step_id,
    ).first()
    if step and step.status == 'waiting_approval':
        step.status = 'pending'
        config = step.step_config_json or {}
        config['approval_required'] = False
        step.step_config_json = config
    run.status = 'pending'
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))


@api_bp.route('/workflow-runs/<int:run_id>/cancel', methods=['POST'])
def cancel_workflow_run(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    actor = _actor_name()
    now = datetime.now()
    run.status = 'cancelled'
    run.current_step_id = ''
    run.finished_at = now
    run.summary = (request.get_json() or {}).get('summary') or run.summary or '已终止'
    for step in WorkflowRunStep.query.filter_by(run_id=run.id).all():
        if step.status in ('pending', 'running', 'retrying', 'waiting_approval'):
            step.status = 'skipped'
            step.summary = step.summary or 'Run 已终止，未完成步骤已跳过'
            step.finished_at = step.finished_at or now
            step.updated_by = actor
            step.health_status = 'idle'
            step.health_checked_at = now
            step.progress_at = now
            step.progress_by = actor
            step.progress_phase = 'cancelled'
            step.progress_message = step.summary
    for approval in WorkflowApproval.query.filter_by(run_id=run.id, status='pending').all():
        approval.status = 'skipped'
        approval.approver = actor
        approval.comment = approval.comment or 'Run 已终止，审批自动关闭'
        approval.approved_at = approval.approved_at or now
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))

