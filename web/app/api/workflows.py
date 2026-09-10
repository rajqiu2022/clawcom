"""Hub Workflow Runbook API.

MVP scope:
- Hub stores definitions/runs/steps/approvals/artifacts.
- Workers/Agents execute steps and report structured results.
- Hub owns gates, approvals, retry/resume, evidence tracking.
"""
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
from uuid import uuid4

from flask import current_app, jsonify, request, session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user, is_admin_user
from app.models import (
    AgentPostAssignment,
    AuditLog,
    ClawSidecarConfig,
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
    TEST_REPORT_TYPES,
    User,
    WorkflowApproval,
    WorkflowArtifact,
    WorkflowDefinition,
    WorkflowDefinitionFavorite,
    WorkflowEvidenceManifest,
    WorkflowOperationIdempotency,
    WorkflowRun,
    WorkflowRunLibrarySnapshot,
    WorkflowRunStep,
    WecomSendLog,
)
from app.services.post_resolver import resolve_post_candidate
from app.services.requirement_coverage import summarize_requirement_coverage
from app.services.workflow_library_snapshots import (
    append_workflow_snapshot_warning,
    freeze_workflow_run_library_snapshot,
    resolve_workflow_library_id,
)
from app.services.workflows import (
    RACINGGO_FLOW25_CONTROLLED_RUNNER,
    RACINGGO_FLOW25_WORKER_OPERATIONS,
    build_heartbeat_fallback_notice,
    build_step_blocked_notice,
    build_workflow_agent_task_payload,
    build_workflow_start_context,
    active_step_claim_state,
    can_claim_step,
    can_claim_step_lease,
    can_accept_step_result_after_blocker,
    can_execute_workflow,
    can_edit_workflow,
    can_manage_workflow,
    can_view_workflow,
    compute_step_health,
    evaluate_step_branches,
    evaluate_step_gates,
    evaluate_notification_authorization,
    validate_notification_result_contract,
    collect_declared_step_outputs,
    validate_step_result_contract,
    merge_workflow_definition_update,
    blocked_dependency_ids,
    normalize_step_lease_seconds,
    normalize_executor_acl,
    normalize_editor_acl,
    paginate_items,
    normalize_workflow_definition,
    ready_step_ids,
    resolve_workflow_run_assignment,
    resolve_start_var_claw_ids,
    resolve_workflow_step_claw_ids,
    materialize_workflow_run_assignment,
    merge_workflow_run_outcomes,
    composite_workflow_terminal_status,
    validate_worker_result_status,
    validate_workflow_finalizer_result,
    workflow_catalog_metadata,
    workflow_outcome_requirements,
    workflow_step_display_state,
    PROPAGATED_UPSTREAM_BLOCKED,
)
from app.services.workflow_result_ingestion import (
    cleanup_workflow_result_records,
    ingest_workflow_result,
    link_report_to_evidence,
)
from app.services.workflow_direct_execution import (
    direct_execution_claim_allowed,
    direct_execution_lease,
    workflow_step_claim_required,
    workflow_step_fencing_required,
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
    if _can_edit_definition(definition):
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


def _run_visible_to_actor(run):
    """Allow the bound Worker to read its Run without granting management."""
    claw = get_current_claw()
    if claw and _run_worker_claw_id(run) == claw.id:
        return True
    return not run.definition or _definition_visible(run.definition)


def _require_actor():
    if get_current_claw() or get_current_user():
        return None
    return jsonify({'error': '未认证'}), 401


def _definition_acl(definition):
    acl = normalize_executor_acl(definition.executor_acl_json or {})
    if definition.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}:
        acl['all'] = True
    return acl


def _definition_editor_acl(definition):
    return normalize_editor_acl(definition.editor_acl_json or {})


def _sync_workflow_create_policy(definition, claw_ids):
    """Mirror Flow Agent grants into the Codex Sidecar policy ceiling.

    Flow ACL remains the authority.  This persisted list is the second,
    fail-closed Worker ceiling, so adding an editor/executor must raise it in
    the same transaction or the UI grant would not become usable.
    """
    normalized_ids = sorted({
        int(value) for value in (claw_ids or [])
        if str(value).isdigit() and int(value) > 0
    })
    if not normalized_ids:
        return []
    synced = []
    configs = ClawSidecarConfig.query.filter(
        ClawSidecarConfig.claw_id.in_(normalized_ids)).all()
    for config in configs:
        policy = dict(config.system_context_policy_json or {})
        allowed = {
            int(value)
            for value in (policy.get(
                'allowed_workflow_create_definition_ids') or [])
            if str(value).isdigit() and int(value) > 0
        }
        changed = definition.id not in allowed
        allowed.add(definition.id)
        policy['allowed_workflow_create_definition_ids'] = sorted(allowed)
        orchestrator = policy.get('codex_orchestrator')
        if isinstance(orchestrator, dict):
            orchestrator = dict(orchestrator)
            allowed_next = {
                int(value)
                for value in (orchestrator.get('allowed_next_flows') or [])
                if str(value).isdigit() and int(value) > 0
            }
            if definition.id not in allowed_next:
                changed = True
            allowed_next.add(definition.id)
            orchestrator['allowed_next_flows'] = sorted(allowed_next)
            policy['codex_orchestrator'] = orchestrator
        if not changed:
            continue
        config.system_context_policy_json = policy
        config.config_version = (config.config_version or 0) + 1
        config.updated_by = 'Workflow permission sync'
        synced.append(config.claw_id)
    return synced


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
        editor_acl=_definition_editor_acl(definition),
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


def _can_edit_definition(definition):
    actor = _actor_identity()
    if not actor:
        return False
    if (definition.owner_type == 'system'
            or definition.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}):
        return is_admin_user()
    if _is_legacy_owner(definition, actor):
        return True
    return can_edit_workflow(
        definition.owner_type,
        definition.owner_id,
        _definition_editor_acl(definition),
        actor['type'],
        actor['id'],
        is_admin=is_admin_user(),
    )


def _definition_payload(definition, brief=False):
    data = definition.to_dict(brief=brief)
    definition_json = (
        definition.definition_json
        if isinstance(definition.definition_json, dict) else {})
    data['workflow_catalog'] = workflow_catalog_metadata(definition_json)
    data['outcome_requirements'] = workflow_outcome_requirements(
        definition_json)
    data['can_execute'] = _can_execute_definition(definition)
    data['can_edit'] = _can_edit_definition(definition)
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


def _workflow_request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex).strip()[:128]


def _definition_requires_worker_binding(definition_json):
    definition_json = (
        definition_json if isinstance(definition_json, dict) else {})
    if definition_json.get('require_worker_binding') is True:
        return True
    context = (
        definition_json.get('context')
        if isinstance(definition_json.get('context'), dict) else {})
    if context.get('require_worker_binding') is True:
        return True
    policy = (
        context.get('executor_operation_policy')
        if isinstance(context.get('executor_operation_policy'), dict) else {})
    return bool(
        policy.get('mode') == 'workflow_run_executor'
        and policy.get('require_worker_binding') is True)


def _workflow_execution_input_snapshot(
        definition_snapshot, start_vars, raw_context,
        definition_context=None, testcase_library_id=None):
    """Freeze the non-secret execution inputs used by a Workflow Run.

    Only an explicit allowlist is copied. Credentials, tokens and arbitrary
    caller context must never become part of the long-lived Run snapshot.
    """
    sources = [
        value for value in (start_vars, raw_context, definition_context)
        if isinstance(value, dict)
    ]

    def first(*names):
        for source in sources:
            for name in names:
                if source.get(name) not in (None, ''):
                    return copy.deepcopy(source.get(name))
        return None

    snapshot = {
        'schema_version': 1,
        'workflow_definition_id': definition_snapshot.get(
            'workflow_definition_id'),
        'workflow_definition_version': definition_snapshot.get('version'),
        'workflow_definition_sha256': definition_snapshot.get('sha256'),
        'template_revision': (
            definition_snapshot.get('template_revision') or
            first('template_revision')),
        'deepflow_release_id': first('deepflow_release_id'),
        'deepflow_release_sha': first(
            'deepflow_release_sha', 'deepflow_release_hash'),
        'baseline_receipt_id': first('baseline_receipt_id'),
        'baseline_receipt_hash': first(
            'baseline_receipt_hash', 'baseline_receipt_sha256'),
        'testcase_library_id': (
            testcase_library_id
            if testcase_library_id is not None
            else first('testcase_library_id', 'library_id')),
        'testcase_library_revision': first(
            'testcase_library_revision', 'library_revision'),
        'testcase_library_hash': first(
            'testcase_library_hash', 'library_hash',
            'testcase_library_sha256'),
        'adapter_capability_schema': first('adapter_capability_schema'),
        'adapter_capability_schema_version': first(
            'adapter_capability_schema_version'),
        'adapter_capability_schema_hash': first(
            'adapter_capability_schema_hash',
            'adapter_capability_schema_sha256'),
        'runner_policy_version': first('runner_policy_version'),
        'runner_policy_hash': first(
            'runner_policy_hash', 'runner_policy_sha256'),
    }
    return snapshot


def _workflow_api_error(code, message, status=400, details=None):
    """Return the new stable error envelope while retaining legacy `error`."""
    payload = {
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': _workflow_request_id(),
    }
    return jsonify(payload), status


def _workflow_lifecycle_conflict(run, step=None, operation='write'):
    """Return a stable, non-retryable contract for writes after terminal state."""
    return _workflow_api_error(
        'HUB_LIFECYCLE_CONFLICT',
        'Workflow Run or Step is already terminal; stop retrying this write and '
        'preserve the local receipt for reconciliation',
        status=409,
        details={
            'workflow_run_id': run.id,
            'run_status': run.status,
            'step_id': step.step_id if step else '',
            'step_status': step.status if step else '',
            'operation': operation,
            'retryable': False,
            'worker_action': 'stop_and_reconcile',
        },
    )


def _run_payload(run, with_steps=False):
    data = run.to_dict(with_steps=with_steps)
    current_step = next(
        (step for step in (run.steps or [])
         if step.step_id == run.current_step_id),
        None,
    )
    context = run.context_json if isinstance(run.context_json, dict) else {}
    catalog = context.get('workflow_catalog_snapshot') if isinstance(
        context.get('workflow_catalog_snapshot'), dict) else None
    if catalog is None:
        catalog = workflow_catalog_metadata(
            run.definition.definition_json
            if run.definition and isinstance(
                run.definition.definition_json, dict) else {})
    requirements = context.get('outcome_requirements_snapshot') if isinstance(
        context.get('outcome_requirements_snapshot'), dict) else None
    if requirements is None:
        requirements = workflow_outcome_requirements(
            run.definition.definition_json
            if run.definition and isinstance(
                run.definition.definition_json, dict) else {})
    data['workflow_catalog'] = catalog
    data['outcome_requirements'] = requirements
    definition = data.get('definition') if isinstance(
        data.get('definition'), dict) else {}
    snapshot = context.get('workflow_definition_snapshot') if isinstance(
        context.get('workflow_definition_snapshot'), dict) else {}
    workflow_name = str(
        getattr(run.definition, 'name', '')
        or definition.get('name')
        or definition.get('workflow_name')
        or '').strip()
    data['workflow_name'] = workflow_name
    data['definition_name'] = workflow_name
    raw_definition_version = (
        snapshot.get('version')
        or getattr(run.definition, 'version', 0)
        or definition.get('version')
        or 1)
    try:
        data['workflow_definition_version'] = int(raw_definition_version)
    except (TypeError, ValueError):
        data['workflow_definition_version'] = 1
    data['current_step_status'] = current_step.status if current_step else None
    data['result_summary'] = (
        context.get('result_summary')
        if context.get('result_summary') is not None
        else (run.summary or '')
    )
    data['output_refs'] = [
        artifact.to_dict()
        for artifact in sorted(
            run.artifacts or [], key=lambda item: (item.created_at or datetime.min, item.id or 0))
    ]
    data['can_delete'] = _can_delete_run(run)
    manifest = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run.id).first()
    data['evidence_manifest'] = manifest.to_dict() if manifest else None
    data['evidence_manifest_status'] = (
        run.evidence_ingest_status
        or ('EVIDENCE_INGEST_INCOMPLETE'
            if run.finished_at and not manifest and requirements.get('evidence')
            else 'EVIDENCE_NOT_REQUIRED'
            if run.finished_at and not requirements.get('evidence')
            else ''))
    data['finding_count'] = len(manifest.finding_ids_json or []) if manifest else 0
    return data


def _workflow_run_create_payload(run, compact=False, idempotent_replay=False):
    """Build a bounded create receipt for Agent callers.

    Definitions can carry large prompts and reference packs.  Returning every
    step after a successful Agent/API start can exceed the Worker proxy limit
    even though the Run was committed.  Web callers retain the historical full
    payload; authenticated Claws receive a stable receipt plus a readback URL.
    """
    if not compact:
        payload = _run_payload(run, with_steps=True)
        payload['idempotent_replay'] = bool(idempotent_replay)
        return payload
    return {
        'ok': True,
        'id': run.id,
        'run_id': run.id,
        'workflow_run_id': run.id,
        'workflow_definition_id': run.definition_id,
        'run_name': run.run_name or '',
        'status': run.status,
        'current_step_id': run.current_step_id or '',
        'worker_claw_id': _run_worker_claw_id(run),
        'readback_url': '/api/v1/workflow-runs/%s' % run.id,
        'steps_url': '/api/v1/workflow-runs/%s' % run.id,
        'idempotent_replay': bool(idempotent_replay),
        'created_at': str(run.created_at) if run.created_at else None,
    }


def _workflow_step_result_payload(run, step, compact=False):
    """Return a bounded commit receipt to authenticated Worker callers."""
    if not compact:
        return _run_payload(run, with_steps=True)
    context = run.context_json if isinstance(run.context_json, dict) else {}
    definition_snapshot = (
        context.get('workflow_definition_snapshot')
        if isinstance(context.get('workflow_definition_snapshot'), dict)
        else {})
    definition_version = definition_snapshot.get('version')
    if definition_version is None and run.definition:
        definition_version = run.definition.version
    return {
        'schema': 'hub.workflow_step_result_receipt@1',
        'ok': True,
        'result_accepted': True,
        'id': run.id,
        'run_id': run.id,
        'workflow_run_id': run.id,
        'status': run.status,
        'current_step_id': run.current_step_id or '',
        'definition_version': definition_version,
        'workflow_definition_version': definition_version,
        'step': {
            'step_id': step.step_id,
            'status': step.status,
            'attempt_no': int(step.attempt_no or 1),
        },
        'readback_url': '/api/v1/workflow-runs/%s' % run.id,
    }


def _parse_workflow_datetime(value, field_name):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError(
            f'{field_name} must be an ISO-8601 datetime') from None
    if parsed.tzinfo is not None:
        cst = timezone(timedelta(hours=8))
        parsed = parsed.astimezone(cst).replace(tzinfo=None)
    return parsed


def _workflow_run_query_from_request():
    q = WorkflowRun.query
    raw_definition_id = (
        request.args.get('workflow_definition_id')
        or request.args.get('definition_id')
    )
    if raw_definition_id not in (None, ''):
        try:
            q = q.filter(WorkflowRun.definition_id == int(raw_definition_id))
        except (TypeError, ValueError):
            return None, _workflow_api_error(
                'INVALID_WORKFLOW_DEFINITION_ID',
                'workflow_definition_id must be an integer')

    statuses = [
        value.strip() for value in str(request.args.get('status') or '').split(',')
        if value.strip()
    ]
    if statuses:
        q = q.filter(WorkflowRun.status.in_(statuses))
    for field, column in (
        ('controller_run_id', WorkflowRun.controller_run_id),
        ('correlation_id', WorkflowRun.correlation_id),
        ('trigger_source', WorkflowRun.trigger_source),
    ):
        value = str(request.args.get(field) or '').strip()
        if value:
            q = q.filter(column == value)
    raw_project_id = request.args.get('project_id')
    if raw_project_id not in (None, ''):
        try:
            q = q.filter(WorkflowRun.project_id == int(raw_project_id))
        except (TypeError, ValueError):
            return None, _workflow_api_error(
                'INVALID_PROJECT_ID', 'project_id must be an integer')
    for field, column in (
        ('created_after', WorkflowRun.created_at),
        ('updated_after', WorkflowRun.updated_at),
    ):
        raw = request.args.get(field)
        if raw in (None, ''):
            continue
        try:
            parsed = _parse_workflow_datetime(raw, field)
        except ValueError as exc:
            return None, _workflow_api_error(
                'INVALID_DATETIME_FILTER', str(exc), details={'field': field})
        q = q.filter(column > parsed)
    catalog_kind = str(request.args.get('catalog_kind') or '').strip().lower()
    if catalog_kind and catalog_kind not in {'business', 'probe', 'legacy'}:
        return None, _workflow_api_error(
            'INVALID_WORKFLOW_CATALOG_KIND',
            'catalog_kind must be business, probe or legacy')
    visible_definition_ids = [
        definition.id
        for definition in WorkflowDefinition.query.filter(
            WorkflowDefinition.status != 'deleted').all()
        if _definition_visible(definition)
        and (not catalog_kind or workflow_catalog_metadata(
            definition.definition_json or {}).get('kind') == catalog_kind)
    ]
    q = q.filter(WorkflowRun.definition_id.in_(visible_definition_ids))
    return q.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()), None


def _workflow_run_create_hash(definition_id, intent):
    canonical = {'workflow_definition_id': int(definition_id)}
    canonical.update(intent)
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


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


def _nested_output_value(outputs, key):
    found = None
    def visit(value):
        nonlocal found
        if not isinstance(value, dict):
            return
        if key in value and value.get(key) not in (None, ''):
            found = value.get(key)
        for child in value.values():
            if isinstance(child, dict):
                visit(child)
    visit(outputs if isinstance(outputs, dict) else {})
    return found


def _verified_notification_report(run, outputs):
    raw_id = _nested_output_value(outputs, 'hub_report_id')
    try:
        report_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    report = TestReport.query.get(report_id)
    if (not report or report.is_deleted or report.project_id != run.project_id
            or not report.is_shared or not report.share_token):
        return None
    expected = '/r/%s' % report.share_token
    supplied = str(_nested_output_value(outputs, 'share_url') or '').strip()
    if not supplied or not (supplied == expected or supplied.endswith(expected)):
        return None
    return report


def _workflow_notification_gate(run):
    outputs = _workflow_outputs_context(run)
    report = _verified_notification_report(run, outputs)
    gate = evaluate_notification_authorization(outputs, report_readback=bool(report))
    gate['hub_report_id'] = report.id if report else None
    gate['share_url'] = '/r/%s' % report.share_token if report else ''
    gate['target'] = '大群2'
    return gate


def _notification_send_audited(run_id, step_id):
    rows = WecomSendLog.query.filter_by(
        workflow_run_id=run_id,
        workflow_step_id=step_id,
        status='sent',
    ).order_by(WecomSendLog.id.desc()).limit(20).all()
    return any(
        isinstance(row.gate_result_json, dict)
        and row.gate_result_json.get('allowed') is True
        and bool(row.request_summary_json)
        and bool(row.template_version)
        and bool(row.message_hash)
        and bool(row.receipt_json)
        for row in rows
    )


def _skip_step_descendants(run, root_step_id, reason):
    rows = WorkflowRunStep.query.filter_by(run_id=run.id).all()
    skipped = {root_step_id}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.status != 'pending':
                continue
            if any(dep in skipped for dep in (row.depends_on_json or [])):
                row.status = 'skipped'
                row.summary = reason
                row.finished_at = datetime.now()
                row.updated_by = 'workflow-notification-gate'
                skipped.add(row.step_id)
                changed = True
    return sorted(skipped - {root_step_id})


def _sync_run_conclusions(run):
    outputs = _workflow_outputs_context(run)
    business = _nested_output_value(outputs, 'business_conclusion')
    automation = _nested_output_value(outputs, 'automation_conclusion')
    business_failure_confirmed = (
        _nested_output_value(outputs, 'business_failure_confirmed') is True)
    business_passed = (
        _nested_output_value(outputs, 'business_passed') is True)
    business_inconclusive = (
        _nested_output_value(outputs, 'business_inconclusive') is True)
    contract_invalid = WorkflowRunStep.query.filter_by(run_id=run.id).filter(
        WorkflowRunStep.contract_result_json.isnot(None)).all()
    has_contract_invalid = any(
        isinstance(row.contract_result_json, dict)
        and row.contract_result_json.get('code') == 'CONTRACT_INVALID'
        for row in contract_invalid)
    automation_error_count = _nested_output_value(outputs, 'automation_error_count')
    try:
        has_automation_error = float(automation_error_count or 0) > 0
    except (TypeError, ValueError):
        has_automation_error = bool(automation_error_count)
    has_automation_error = bool(
        has_automation_error
        or _nested_output_value(outputs, 'automation_failure_confirmed') is True)
    if business not in (None, ''):
        run.business_conclusion = str(business)[:64]
    elif business_failure_confirmed:
        run.business_conclusion = 'CONFIRMED_BUSINESS_FAILURE'
    elif business_passed:
        run.business_conclusion = 'COMPLETED'
    elif business_inconclusive or run.status in ('failed', 'blocked', 'cancelled'):
        # A terminal orchestration/automation state is not proof that game
        # business execution completed. Clear any legacy inferred COMPLETED
        # value and fail closed until a business node reports a verdict.
        run.business_conclusion = 'INCONCLUSIVE'
    else:
        # Generic successful Workflows may not have business semantics. Do not
        # fabricate a business verdict merely because the Run is terminal.
        run.business_conclusion = ''
    if automation not in (None, ''):
        run.automation_conclusion = str(automation)[:64]
    elif (run.status in ('failed', 'blocked')
          and business in (None, '')
          and not business_failure_confirmed
          and not business_passed):
        run.automation_conclusion = 'AUTOMATION_ENV_BLOCKED'
    elif (has_contract_invalid or has_automation_error
          or run.evidence_ingest_status == 'EVIDENCE_INGEST_INCOMPLETE'):
        run.automation_conclusion = 'COMPLETED_WITH_AUTOMATION_ERROR'
    elif run.status in ('succeeded', 'failed', 'blocked', 'cancelled'):
        run.automation_conclusion = run.automation_conclusion or 'COMPLETED'


def _composite_outcomes_enabled(run):
    definition = (
        run.definition.definition_json
        if run and run.definition
        and isinstance(run.definition.definition_json, dict) else {})
    return bool(
        definition.get('outcome_status_mode') == 'composite'
        or definition.get('composite_outcomes') is True)


def _sync_run_outcomes(run, result=None, step=None, report=None, ingestion=None):
    """Persist independent business/automation/evidence/report/etc outcomes."""
    current = dict(run.outcomes_json or {})
    # Evidence is recomputed from current Step attempts, not the historical
    # Run verdict: corrected receipts can close a gap; another case cannot.
    current['evidence'] = ''
    aggregate = {
        'outputs': _workflow_outputs_context(run),
    }
    merged = merge_workflow_run_outcomes(current, aggregate)
    for row in WorkflowRunStep.query.filter_by(run_id=run.id).all():
        merged = merge_workflow_run_outcomes(merged, {
            **((row.contract_result_json or {}).get('outcome_input') or {}),
            'outputs': row.outputs_json or {}, 'metrics': row.metrics_json or {},
            'evidence': row.evidence_json or {},
        }, row.step_config_json)
    manifest = WorkflowEvidenceManifest.query.filter_by(workflow_run_id=run.id).first()
    if manifest:
        merged = merge_workflow_run_outcomes(merged, {
            'evidence_outcome': ('COMPLETE' if manifest.completeness_status == 'complete'
                                 else 'ANALYSIS_INCOMPLETE'),
        })
    if isinstance(result, dict):
        merged = merge_workflow_run_outcomes(
            merged,
            result,
            step.step_config_json if step else None,
        )
    if report is not None:
        merged = merge_workflow_run_outcomes(merged, {
            'report_outcome': (
                'PUBLISHED'
                if str(getattr(report, 'status', '') or '') == 'published'
                else 'DRAFT'
            ),
            'outputs': {'hub_report_id': getattr(report, 'id', None)},
        })
    if isinstance(ingestion, dict):
        ingestion_status = str(
            ingestion.get('completeness_status')
            or ingestion.get('status') or '').upper()
        if ingestion_status:
            merged = merge_workflow_run_outcomes(merged, {
                'evidence_outcome': (
                    'COMPLETE'
                    if ingestion_status in {'COMPLETE', 'COMPLETED'}
                    else 'ANALYSIS_INCOMPLETE'
                )
            })
    deliveries = WorkflowArtifact.query.filter_by(
        run_id=run.id, artifact_type='workflow_notification_delivery').all()
    active_attempts = {row.step_id: row.attempt_no or 1
                       for row in WorkflowRunStep.query.filter_by(run_id=run.id).all()}
    deliveries = [item for item in deliveries if
                  (item.metadata_json or {}).get('attempt_no') == active_attempts.get(item.step_id)]
    if deliveries:
        states = [(item.metadata_json or {}).get('state') for item in deliveries]
        merged['notification'] = ('FAILED' if 'failed' in states else
                                  'PENDING' if 'pending' in states else 'SENT')
    reports = WorkflowArtifact.query.filter_by(
        run_id=run.id, artifact_type='workflow_report').all()
    if reports:
        report_ids = {item.test_report_id for item in reports}
        bound = TestReport.query.filter(TestReport.id.in_(report_ids)).all()
        merged['report'] = ('PUBLISHED' if all(
            item and not item.is_deleted and item.status in ('published', 'revised')
            for item in bound) and len(bound) == len(report_ids) else 'FAILED')
    elif merged.get('report') == 'PUBLISHED':
        merged['report'] = 'FAILED'  # A URL or Agent claim is not a standard binding.
    if run.status in ('succeeded', 'failed', 'blocked', 'cancelled'):
        context = run.context_json if isinstance(run.context_json, dict) else {}
        requirements = context.get('outcome_requirements_snapshot') if isinstance(
            context.get('outcome_requirements_snapshot'), dict) else None
        if requirements is None:
            requirements = workflow_outcome_requirements(
                run.definition.definition_json
                if run.definition and isinstance(
                    run.definition.definition_json, dict) else {})
        catalog = context.get('workflow_catalog_snapshot') if isinstance(
            context.get('workflow_catalog_snapshot'), dict) else None
        if catalog is None:
            catalog = workflow_catalog_metadata(
                run.definition.definition_json
                if run.definition and isinstance(
                    run.definition.definition_json, dict) else {})
        if not requirements.get('evidence') and not manifest:
            merged['evidence'] = 'NOT_REQUIRED'
        if not requirements.get('report') and not reports:
            merged['report'] = 'NOT_APPLICABLE'
        if not requirements.get('notification') and not deliveries:
            merged['notification'] = 'NOT_REQUIRED'
        if not requirements.get('review') and not merged.get('review'):
            merged['review'] = 'NOT_ASSIGNED'
        if (catalog.get('kind') in {'probe', 'legacy'}
                and not requirements.get('business')
                and not merged.get('business')):
            merged['business'] = 'NOT_EXECUTED'
    run.outcomes_json = merged
    legacy_business = {
        'PASSED': 'COMPLETED',
        'FAILED': 'CONFIRMED_BUSINESS_FAILURE',
        'INCONCLUSIVE': 'INCONCLUSIVE',
        'NOT_EXECUTED': 'INCONCLUSIVE',
    }.get(merged.get('business'))
    legacy_automation = {
        'SUCCEEDED': 'COMPLETED',
        'PARTIAL': 'COMPLETED_WITH_AUTOMATION_ERROR',
        'BLOCKED': 'AUTOMATION_ENV_BLOCKED',
        'FAILED': 'AUTOMATION_ENV_BLOCKED',
    }.get(merged.get('automation'))
    explicit_business = str(
        _nested_output_value(
            _workflow_outputs_context(run), 'business_conclusion') or ''
    ).strip().upper()
    if explicit_business == 'COMPLETED_WITH_BUGS' and merged.get(
            'business') == 'FAILED':
        # Preserve the evidence-backed distinction between a completed test
        # that found a product defect and an execution/infrastructure failure.
        run.business_conclusion = 'COMPLETED_WITH_BUGS'
    elif legacy_business:
        run.business_conclusion = legacy_business
    if legacy_automation:
        run.automation_conclusion = legacy_automation
    return merged


def _run_start_vars(run):
    context = run.context_json if run and isinstance(run.context_json, dict) else {}
    return context.get('start_vars') if isinstance(context.get('start_vars'), dict) else {}


def _step_runtime_payload(step):
    data = step.to_dict(with_context=True)
    context = step.run.context_json if step.run and isinstance(step.run.context_json, dict) else {}
    data['context'] = context
    data['start_vars'] = context.get('start_vars') if isinstance(context.get('start_vars'), dict) else {}
    data['outputs'] = _workflow_outputs_context(step.run, current_step=step) if step.run else {}
    snapshot_meta = context.get('testcase_library_snapshot')
    if isinstance(snapshot_meta, dict):
        data['testcase_library_snapshot'] = snapshot_meta
    bound_worker_claw_id = _run_worker_claw_id(step.run if step else None)
    if bound_worker_claw_id:
        data['worker_claw_id'] = bound_worker_claw_id
        data['execution_route'] = {
            'mode': 'single_flow_worker',
            'worker_claw_id': bound_worker_claw_id,
            'acting_claw_id': step.target_claw_id,
            'acting_agent': step.target_agent or '',
            'acting_post': step.target_post or '',
        }
    if step.step_type == 'worker_task':
        data['fencing_token'] = int(step.claim_fencing_token or 0)
        config = (
            step.step_config_json
            if isinstance(step.step_config_json, dict) else {})
        inputs = config.get('inputs') if isinstance(config.get('inputs'), dict) else {}
        operation = str(inputs.get('operation') or '').strip()
        if (step.runner == RACINGGO_FLOW25_CONTROLLED_RUNNER
                and operation in set(RACINGGO_FLOW25_WORKER_OPERATIONS.values())):
            data['worker_contract'] = {
                'runner': RACINGGO_FLOW25_CONTROLLED_RUNNER,
                'operation': operation,
                'protected_target_id': str(
                    inputs.get('protected_target_id') or '').strip(),
                'require_fencing_token': bool(
                    config.get('require_fencing_token')),
            }
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


def _workflow_report_binding_fields(data):
    """Resolve report transport fields from both supported Worker shapes.

    Current Workers project Publisher fields to the top level before submit.
    Older accepted results, including Run #580, stored the same signed facts
    only under ``outputs``. Treat both as transport aliases, but fail closed on
    any disagreement.
    """
    data = data if isinstance(data, dict) else {}
    outputs = data.get('outputs') if isinstance(data.get('outputs'), dict) else {}
    report_payload = _workflow_report_payload(data)
    raw_ids = []
    for container, keys in (
            (data, ('test_report_id', 'workflow_report_id')),
            (report_payload, ('id', 'test_report_id')),
            (outputs, ('test_report_id', 'workflow_report_id', 'hub_report_id'))):
        for key in keys:
            value = container.get(key)
            if value in (None, ''):
                continue
            if isinstance(value, bool):
                return None, None, 'test_report_id 必须是整数'
            try:
                report_id = int(value)
            except (TypeError, ValueError):
                return None, None, 'test_report_id 必须是整数'
            if report_id <= 0:
                return None, None, 'test_report_id 必须是正整数'
            raw_ids.append(report_id)
    if len(set(raw_ids)) > 1:
        return None, None, '顶层与 outputs 中的 test_report_id 冲突'

    manifests = [
        value for value in (
            data.get('workflow_report_manifest'),
            outputs.get('workflow_report_manifest'),
        ) if value is not None
    ]
    if len(manifests) > 1 and any(
            value != manifests[0] for value in manifests[1:]):
        return None, None, '顶层与 outputs 中的 workflow_report_manifest 冲突'
    return (raw_ids[0] if raw_ids else None,
            manifests[0] if manifests else None, '')


def _workflow_step_notification_policy(step):
    """Read the immutable notification policy from the Run step snapshot."""
    config = step.step_config_json if isinstance(
        step.step_config_json, dict) else {}
    inputs = config.get('inputs') if isinstance(config.get('inputs'), dict) else {}
    configured = (
        config.get('notification_policy')
        if isinstance(config.get('notification_policy'), dict)
        else inputs.get('notification_policy'))
    policy = dict(configured) if isinstance(configured, dict) else {}
    for key in (
            'strict_silent', 'notification_required',
            'no_external_notification', 'version'):
        if key in config and key not in policy:
            policy[key] = config[key]
        elif key in inputs and key not in policy:
            policy[key] = inputs[key]
    return policy


def _recover_bound_workflow_report_artifact(run, report_id):
    """Backfill a report link from an already accepted producer result."""
    report = TestReport.query.get(report_id)
    if (not report or report.is_deleted or report.project_id != run.project_id
            or report.status not in ('published', 'revised')):
        return None
    content_sha256 = hashlib.sha256(
        (report.content or '').encode('utf-8')).hexdigest()
    for producer in run.steps or []:
        outputs = (
            producer.outputs_json
            if isinstance(producer.outputs_json, dict) else {})
        manifest = outputs.get('workflow_report_manifest')
        if not isinstance(manifest, dict):
            continue
        expected = {
            'report_id': report.id,
            'run_id': run.id,
            'step_id': producer.step_id,
            'attempt_no': producer.attempt_no or 1,
            'report_type': report.report_type,
            'source_ref_type': report.source_ref_type,
            'source_ref_id': report.source_ref_id,
            'content_sha256': content_sha256,
        }
        if (type(manifest.get('schema_version')) is not int
                or manifest.get('schema_version') != 1
                or any(type(manifest.get(key)) is not type(value)
                       or manifest.get(key) != value
                       for key, value in expected.items())):
            continue
        artifact = _link_workflow_report_artifact(run, producer, report)
        artifact.metadata_json = dict(
            artifact.metadata_json or {}, publisher_manifest=expected)
        db.session.flush()
        return artifact
    return None


def _record_notification_delivery(run, step, payload):
    """Store the authenticated Worker's existing outbox receipt, not a second queue."""
    if not isinstance(payload, dict):
        raise ValueError('notification_delivery outbox receipt is required')
    for name in ('claw_id', 'run_id', 'attempt_no', 'report_id'):
        if type(payload.get(name)) is not int or payload[name] <= 0:
            raise ValueError('notification_delivery IDs must be positive integers')
    claw = get_current_claw()
    if not claw or payload.get('claw_id') != claw.id:
        raise ValueError('notification_delivery must identify the reporting Worker')
    config = step.step_config_json if isinstance(
        step.step_config_json, dict) else {}
    owner_role = str(config.get('notification_owner_role') or '').strip()
    assignment = (
        (run.context_json or {}).get('assignment_snapshot')
        if isinstance(run.context_json, dict) else {})
    if owner_role and isinstance(assignment, dict):
        try:
            owner_claw_id = int(assignment.get(f'{owner_role}_claw_id'))
        except (TypeError, ValueError):
            owner_claw_id = None
        if not owner_claw_id or owner_claw_id != claw.id:
            raise ValueError(
                'notification_delivery does not belong to the frozen '
                f'{owner_role} notification owner')
    if (payload.get('run_id') != run.id or payload.get('step_id') != step.step_id
            or payload.get('attempt_no') != (step.attempt_no or 1)):
        raise ValueError('notification_delivery Run/Step/attempt mismatch')
    key = payload.get('notification_id')
    digest = payload.get('message_sha256')
    if (not isinstance(key, str) or not key.strip() or len(key) > 200
            or not isinstance(digest, str) or len(digest) != 64
            or any(ch not in '0123456789abcdef' for ch in digest)):
        raise ValueError('notification_delivery needs notification_id and message_sha256')
    report_artifact = WorkflowArtifact.query.filter_by(
        run_id=run.id, artifact_type='workflow_report',
        test_report_id=payload.get('report_id')).first()
    if not report_artifact:
        report_artifact = _recover_bound_workflow_report_artifact(
            run, payload.get('report_id'))
    report = TestReport.query.get(payload.get('report_id')) if report_artifact else None
    if (not report or report.is_deleted or report.project_id != run.project_id
            or report.status not in ('published', 'revised')):
        raise ValueError('notification_delivery requires a bound published report')
    bound_manifest = (report_artifact.metadata_json or {}).get('publisher_manifest') or {}
    if bound_manifest and bound_manifest.get('content_sha256') != hashlib.sha256(
            (report.content or '').encode('utf-8')).hexdigest():
        raise ValueError('notification_delivery report changed since manifest binding')
    state = payload.get('state')
    if state not in ('pending', 'failed', 'sent'):
        raise ValueError('notification_delivery state must be pending/failed/sent')
    if state == 'sent':
        log = (WecomSendLog.query.get(payload['wecom_log_id'])
               if type(payload.get('wecom_log_id')) is int else None)
        if (not log or log.status != 'sent' or log.workflow_run_id != run.id
                or log.workflow_step_id != step.step_id or log.message_hash != digest):
            log = None
            receipt = payload.get('delivery_receipt')
            if (not isinstance(receipt, dict) or receipt.get('transport') != 'wecom'
                    or type(receipt.get('errcode')) is not int or receipt['errcode'] != 0
                    or receipt.get('notification_id') != key
                    or receipt.get('message_sha256') != digest
                    or not isinstance(receipt.get('received_at'), str)
                    or not receipt['received_at']):
                raise ValueError('notification_delivery sent requires Hub log or authenticated Worker transport receipt')
    delivery_key = str(payload.get('delivery_key') or (
        f'workflow:{run.id}:report:{payload.get("report_id")}:wecom:owner'
    )).strip()[:240]
    for existing in WorkflowArtifact.query.filter_by(
            run_id=run.id,
            artifact_type='workflow_notification_delivery').all():
        previous = existing.metadata_json or {}
        if previous.get('delivery_key') != delivery_key:
            continue
        if (existing.step_id == step.step_id
                and previous.get('attempt_no') == (step.attempt_no or 1)):
            continue
        if (previous.get('message_sha256') != digest
                or previous.get('report_id') != payload.get('report_id')):
            raise ValueError(
                'notification_delivery dedupe identity conflicts with an '
                'existing delivery')
        # One logical report/target delivery is shared by all roles. Returning
        # the existing receipt prevents executor and reviewer nodes from both
        # creating the same external side effect.
        return existing
    artifact = WorkflowArtifact.query.filter_by(
        run_id=run.id, step_id=step.step_id,
        name='notification-attempt:%s' % (step.attempt_no or 1),
        artifact_type='workflow_notification_delivery').first()
    document = {name: payload.get(name) for name in (
        'claw_id', 'run_id', 'step_id', 'attempt_no', 'notification_id',
        'message_sha256', 'report_id', 'state', 'wecom_log_id')}
    document['delivery_key'] = delivery_key
    document['notification_owner_role'] = owner_role
    if state == 'sent':
        document['receipt_origin'] = 'hub' if log else 'authenticated_worker'
        document['delivery_receipt'] = ({
            key: payload['delivery_receipt'][key] for key in (
                'transport', 'errcode', 'notification_id', 'message_sha256', 'received_at')
        } if not log else {'wecom_log_id': log.id})
    if artifact:
        previous = artifact.metadata_json or {}
        for name in ('claw_id', 'attempt_no', 'notification_id',
                     'message_sha256', 'report_id', 'delivery_key'):
            if previous.get(name) != document[name]:
                raise ValueError('notification_delivery immutable identity conflicts')
        if previous.get('state') == 'sent':
            return artifact
    else:
        artifact = WorkflowArtifact(run_id=run.id, step_id=step.step_id,
            artifact_type='workflow_notification_delivery',
            name='notification-attempt:%s' % (step.attempt_no or 1))
        db.session.add(artifact)
    artifact.metadata_json = document
    return artifact


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/notification-delivery', methods=['POST'])
def update_workflow_notification_delivery(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    step = _locked_workflow_step(run_id, step_id)
    claw = get_current_claw()
    if not claw or not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    if (step.step_config_json or {}).get('notification_delivery_mode') != 'outbox':
        return jsonify({'error': 'Step 未启用 outbox 交付'}), 409
    try:
        artifact = _record_notification_delivery(run, step, request.get_json() or {})
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        return jsonify({'error': str(exc)}), 400
    _sync_run_outcomes(run)
    db.session.commit()
    return jsonify(artifact.metadata_json)


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
    raw_report_id, submitted_manifest, binding_error = (
        _workflow_report_binding_fields(data))
    if binding_error:
        return None, binding_error
    report = None
    if raw_report_id not in (None, ''):
        report = TestReport.query.get(raw_report_id)
        if not report or report.is_deleted:
            return None, 'Workflow 报告不存在'
        if report.project_id != run.project_id:
            return None, 'Workflow 报告必须属于当前 Run 项目'
        if report.report_type != 'workflow':
            manifest = submitted_manifest
            if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
                    or manifest['schema_version'] != 1):
                return None, 'Publisher 报告需要 workflow_report_manifest v1'
            if report.report_type not in TEST_REPORT_TYPES or report.status not in ('published', 'revised'):
                return None, 'Publisher 报告类型不支持或尚未发布'
            if not (report.content or '').strip():
                return None, 'Publisher 报告正文不能为空'
            actor = _actor_identity() or {}
            author = ((actor.get('type') == 'claw' and report.submitter_claw_id == actor.get('id'))
                      or (actor.get('type') == 'user' and report.submitter_user_id == actor.get('id')))
            if not author and not is_admin_user():
                return None, 'Publisher 报告必须由作者或管理员绑定'
            expected = {
                'report_id': report.id, 'run_id': run.id, 'step_id': step.step_id,
                'attempt_no': step.attempt_no or 1, 'report_type': report.report_type,
                'source_ref_type': report.source_ref_type,
                'source_ref_id': report.source_ref_id,
                'content_sha256': hashlib.sha256((report.content or '').encode('utf-8')).hexdigest(),
            }
            if any(type(manifest.get(key)) is not type(value) or manifest.get(key) != value
                   for key, value in expected.items()):
                return None, 'Publisher manifest 与报告内容、来源或当前 Run/Step/attempt 不匹配'
            conflict = WorkflowArtifact.query.filter(
                WorkflowArtifact.test_report_id == report.id,
                WorkflowArtifact.artifact_type == 'workflow_report',
                WorkflowArtifact.run_id != run.id).first()
            if conflict:
                return None, 'Publisher 报告已绑定其他 Run'
        artifact = _link_workflow_report_artifact(run, step, report)
        if report.report_type != 'workflow':
            artifact.metadata_json = dict(artifact.metadata_json or {}, publisher_manifest=expected)
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
        # Hub reconciliation timestamps are not Worker-editable progress fields.
        detail = dict(detail)
        detail.pop('hub_reconciliation', None)
        existing = (step.progress_json or {}).get('hub_reconciliation')
        if existing:
            detail['hub_reconciliation'] = existing
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
    for step in q.populate_existing().with_for_update().all():
        auto_block = _step_auto_block_on_heartbeat_loss(step)
        health = compute_step_health(
            step.status,
            _step_health_signal_at(step),
            now,
            interval_sec=30,
            max_missed=3,
            progress_at=step.progress_at,
            progress_timeout_sec=300,
            auto_fail_on_missed=(auto_block and not (
                step.claim_expires_at and step.claim_expires_at > now)),
        )
        step.health_status = health['status']
        step.missed_heartbeat_count = health['missed_count']
        step.health_checked_at = now
        changed = True
        if health.get('failed'):
            step.status = 'blocked'
            step.finished_at = now
            _clear_step_claim(step)
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
                step.progress_phase = 'no_response_reminded'
                step.progress_message = 'Hub 已提醒模板创建者：节点已派发但执行方长时间未响应'
                reminders['no_response_at'] = str(now)
                reminders['missed_heartbeat_count'] = health.get('missed_count') or 0
                progress['hub_reminders'] = reminders
                step.progress_json = progress
                _dispatch_heartbeat_fallback_task(step.run, step)
            # Silence starts bounded reconciliation; it is not proof that an
            # operation stopped, so retry_max cannot authorize replay here.
            signal_at = _step_health_signal_at(step)
            signal_age = int((now - signal_at).total_seconds()) if signal_at else None
            cfg = step.step_config_json if isinstance(step.step_config_json, dict) else {}
            hard_timeout_enabled = cfg.get('auto_block_on_no_response') is not False
            if (hard_timeout_enabled and signal_age is not None
                    and signal_age > WORKFLOW_NO_RESPONSE_HARD_TIMEOUT_SEC):
                if _reconcile_silent_step(step, now):
                    if step.run:
                        touched_runs[step.run.id] = step.run
    for touched in touched_runs.values():
        _recompute_run_status(touched, 'workflow-heartbeat')
    if changed and commit:
        db.session.commit()
    return changed


def _reconcile_silent_step(step, now):
    """Bound orphan reconciliation without assuming a timeout stopped side effects."""
    from app.models import AgentTask
    if step.claim_expires_at and step.claim_expires_at > now:
        progress = dict(step.progress_json or {})
        progress.pop('hub_reconciliation', None)
        step.progress_json = progress
        return False  # A live runner lease wins over a slow Provider heartbeat.
    progress = dict(step.progress_json or {})
    receipt = dict(progress.get('hub_reconciliation') or {})
    signal = _step_health_signal_at(step)
    signal_key = signal.isoformat() if signal else None
    if (receipt.get('attempt_no') != (step.attempt_no or 1)
            or receipt.get('signal_at') != signal_key):
        receipt = {}
    started = datetime.fromisoformat(receipt['started_at']) if receipt.get('started_at') else now
    last = datetime.fromisoformat(receipt['checked_at']) if receipt.get('checked_at') else None
    if last and (now - last).total_seconds() < 300:
        return False
    task_ids = [step.agent_task_id] if getattr(step, 'agent_task_id', None) else []
    for claw_id in (None, step.target_claw_id, _run_worker_claw_id(step.run)):
        task_ids.append(_workflow_agent_task_id(step.run_id, step.step_id, step.attempt_no, claw_id))
    tasks = AgentTask.query.filter(AgentTask.task_id.in_(task_ids)).all()
    receipt.update({
        'state': 'recovering', 'attempt_no': step.attempt_no or 1,
        'signal_at': signal_key,
        'started_at': started.isoformat(), 'checked_at': now.isoformat(),
        'checks': int(receipt.get('checks') or 0) + 1,
        'task_states': {task.task_id: task.status for task in tasks},
        'claim_expired': True, 'side_effects': 'unknown',
        'worker_action': 'read_journal_and_report_current_attempt',
    })
    progress['hub_reconciliation'] = receipt
    step.progress_json = progress
    step.progress_phase = 'recovering'
    step.progress_message = '执行状态待对账：请 Worker 回读当前 attempt 的 Provider、journal 和回执'
    if (now - started).total_seconds() < 1800:
        return False
    receipt['state'] = 'human_gate'
    step.progress_json = dict(progress, hub_reconciliation=dict(receipt))
    step.status = 'blocked'
    step.finished_at = now
    step.blocker_json = {
        'type': 'workflow_execution_reconciliation_required', 'code': 'HUMAN_GATE',
        'message': '执行失联且对账窗口已耗尽；副作用未知，禁止盲目重放',
        'attempt_no': step.attempt_no or 1, 'requires_reconciliation': True,
        'reconciliation': receipt,
    }
    _expire_workflow_agent_tasks_for_step(step.run_id, step.step_id,
                                         reason='workflow_reconciliation_required')
    _clear_step_claim(step)
    return True


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


def _run_worker_claw_id(run):
    """Return the one physical Worker bound to this Run, if enabled."""
    context = (
        run.context_json
        if run and isinstance(run.context_json, dict) else {})
    workflow_start = (
        context.get('workflow_start')
        if isinstance(context.get('workflow_start'), dict) else {})
    if workflow_start.get('worker_binding_mode') != 'single_flow_worker':
        return None
    try:
        worker_claw_id = int(workflow_start.get('worker_claw_id'))
    except (TypeError, ValueError):
        return None
    return worker_claw_id if worker_claw_id > 0 else None


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
    if config.get('assignment_role') in ('executor', 'reviewer'):
        assigned = resolve_workflow_step_claw_ids(
            step.step_type,
            target_claw_id=step.target_claw_id,
            step_executor_claw_ids=config.get('executor_claw_ids'),
        )
        if assigned:
            return assigned
    bound_worker_claw_id = _run_worker_claw_id(step.run if step else None)
    if bound_worker_claw_id:
        return [bound_worker_claw_id]
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


def _worker_id_from_payload(data, claw):
    return str((data or {}).get('worker_id') or f'claw:{claw.id}').strip()


def _locked_workflow_step(run_id, step_id):
    """Lock the latest Step row before claim/fencing validation and mutation."""
    return (WorkflowRunStep.query
            .filter_by(run_id=run_id, step_id=step_id)
            .populate_existing().with_for_update().first_or_404())


def _clear_step_claim(step):
    step.claimed_by = ''
    step.claimed_at = None
    step.claimed_claw_id = None
    step.claim_expires_at = None
    step.claim_lease_seconds = None


def _claim_error_response(code, step, status=409):
    messages = {
        'claim_conflict': 'Workflow Step 已被其他 worker 认领',
        'claim_required': 'Workflow Step 必须先 claim 后才能写入',
        'claim_owner_mismatch': 'Workflow Step claim owner 不匹配',
        'claim_expired': 'Workflow Step claim 已过期，请重新 claim',
        'missing_worker_id': 'worker_id 不能为空',
        'fencing_token_required': 'Workflow Step 必须携带 fencing_token',
        'fencing_token_stale': 'Workflow Step fencing_token 已失效，请重新 claim',
    }
    return jsonify({
        'error': messages.get(code, code),
        'code': code,
        'claimed_by': step.claimed_by or '',
        'claimed_claw_id': step.claimed_claw_id,
        'claim_expires_at': str(step.claim_expires_at) if step.claim_expires_at else None,
        'lease_seconds': step.claim_lease_seconds or 180,
        'fencing_token': step.claim_fencing_token or 0,
    }), status


def _require_active_step_claim(step, claw, worker_id, fencing_token=None):
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    effective_step_type = (
        'worker_task'
        if workflow_step_claim_required(step.step_type, config)
        else step.step_type
    )
    state = active_step_claim_state(
        effective_step_type,
        getattr(step, 'claimed_claw_id', None),
        step.claimed_by,
        getattr(step, 'claim_expires_at', None),
        claw.id,
        worker_id,
        datetime.now(),
    )
    if not state.get('active'):
        return _claim_error_response(
            state.get('reason') or 'claim_required', step)
    fencing_required = bool(
        workflow_step_fencing_required(step.step_type, config)
        or current_app.config.get('WORKFLOW_FENCING_REQUIRED', False))
    if fencing_token in (None, ''):
        if fencing_required:
            return _claim_error_response('fencing_token_required', step)
        # Compatibility path for old Workers. New/managed Workers include the
        # token and receive stale-write protection without changing Flow #12.
        return None
    try:
        provided = int(fencing_token)
    except (TypeError, ValueError):
        return _claim_error_response('fencing_token_stale', step)
    if provided != int(step.claim_fencing_token or 0):
        return _claim_error_response('fencing_token_stale', step)
    return None


def _renew_step_claim(step, now):
    lease_seconds = normalize_step_lease_seconds(step.claim_lease_seconds)
    step.claim_lease_seconds = lease_seconds
    step.claim_expires_at = now + timedelta(seconds=lease_seconds)


def _idempotency_actor():
    claw = get_current_claw()
    if claw:
        return 'claw', int(claw.id)
    user = get_current_user()
    if user:
        return 'user', int(user.id)
    return None, None


def _workflow_request_hash():
    raw = request.get_data(cache=True) or b''
    digest = hashlib.sha256()
    digest.update(request.method.upper().encode('utf-8'))
    digest.update(b'\n')
    digest.update(request.path.encode('utf-8'))
    digest.update(b'\n')
    digest.update((request.query_string or b''))
    digest.update(b'\n')
    digest.update(raw)
    return digest.hexdigest()


def _workflow_idempotency_begin():
    key = str(request.headers.get('Idempotency-Key') or '').strip()
    if not key:
        return None, None
    if len(key) > 128:
        return None, (jsonify({
            'error': 'Idempotency-Key 长度不能超过 128',
            'code': 'IDEMPOTENCY_KEY_TOO_LONG',
        }), 400)
    actor_type, actor_id = _idempotency_actor()
    if not actor_type:
        return None, None
    request_hash = _workflow_request_hash()
    record = WorkflowOperationIdempotency.query.filter_by(
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=key,
    ).first()
    if record:
        if record.request_hash != request_hash:
            return None, (jsonify({
                'error': 'Idempotency-Key 已被不同请求复用',
                'code': 'IDEMPOTENCY_KEY_REUSED',
            }), 409)
        if record.response_status and record.response_body_json is not None:
            return record, (jsonify(record.response_body_json), int(record.response_status))
        return record, None
    record = WorkflowOperationIdempotency(
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=key,
        method=request.method.upper(),
        path=request.path,
        request_hash=request_hash,
        expires_at=datetime.now() + timedelta(days=7),
    )
    db.session.add(record)
    db.session.flush()
    return record, None


def _workflow_idempotency_store(record, status_code, body):
    if not record:
        return
    record.response_status = int(status_code)
    record.response_body_json = body
    record.updated_at = datetime.now()


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
    bound_worker_claw_id = _run_worker_claw_id(step.run)
    delivery_claw_ids = (
        target_claw_ids
        if config.get('assignment_role') in ('executor', 'reviewer')
        else [bound_worker_claw_id] if bound_worker_claw_id else target_claw_ids)
    for target_claw_id in delivery_claw_ids:
        task_id = (
            base_task_id
            if len(delivery_claw_ids) == 1
            else _workflow_agent_task_id(step.run_id, step.step_id, step.attempt_no or 1, target_claw_id)
        )
        existing = AgentTask.query.filter_by(task_id=task_id, claw_id=target_claw_id).first()
        if not existing:
            task_payload = dict(payload)
            task_payload['task_id'] = task_id
            task = AgentTask(
                claw_id=target_claw_id,
                task_id=task_id,
                task_type='workflow_agent_task',
                command=step.runner or '',
                target_path='',
                payload=jsonify_safe(task_payload),
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
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    if config.get('assignment_role') in ('executor', 'reviewer'):
        assigned = _step_allowed_claw_ids(step)
        if assigned:
            return assigned
    bound_worker_claw_id = _run_worker_claw_id(step.run if step else None)
    if bound_worker_claw_id:
        return [bound_worker_claw_id]
    targets = []
    if step.target_claw_id:
        try:
            targets.append(int(step.target_claw_id))
        except (TypeError, ValueError):
            pass
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
    verification_target = (
        f'workflow_run:{step.run_id}:step:{step.step_id}:'
        f'attempt:{int(step.attempt_no or 1)}'
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
                verification_target=verification_target,
                enabled=True,
                created_by='workflow-blocked',
            ))
        if notify_claw:
            notify_claw(claw_id)


def _expire_step_blocked_todos(run_id, step_id):
    """Disable current and legacy one-shot blocker notices before replay."""
    target_prefix = f'workflow_run:{int(run_id)}:step:{step_id}:'
    legacy_title = f'Workflow Run #{int(run_id)} {step_id}'
    rows = ClawTodo.query.filter(
        ClawTodo.created_by == 'workflow-blocked',
        ClawTodo.enabled.is_(True),
        or_(
            ClawTodo.verification_target.like(target_prefix + '%'),
            ClawTodo.title.contains(legacy_title),
        ),
    ).all()
    for todo in rows:
        todo.enabled = False
    return len(rows)


def _run_definition_snapshot(run, steps):
    """Build dependency policy from the immutable per-Run step snapshot."""
    current = run.definition.definition_json or {}
    current_by_id = {
        step.get('id'): step
        for step in (current.get('steps') or [])
        if isinstance(step, dict) and step.get('id')
    }
    snapshot_steps = []
    for row in steps:
        stored = row.step_config_json if isinstance(
            row.step_config_json, dict) else None
        config = dict(stored or current_by_id.get(row.step_id) or {})
        config['id'] = row.step_id
        if isinstance(row.depends_on_json, list):
            config['depends_on'] = list(row.depends_on_json)
        else:
            config['depends_on'] = list(config.get('depends_on') or [])
        snapshot_steps.append(config)
    return {'steps': snapshot_steps}


_FATAL_WORKFLOW_DEPENDENCY_CODES = {
    'RUNNER_NOT_STARTED',
    'RUNNER_START_FAILED',
    'UPSTREAM_RUNTIME_NOT_READY',
    'WORKFLOW_START_BINDING_REQUIRED',
    'TRUSTED_START_BINDING_REQUIRED',
    'WORKER_BINDING_REQUIRED',
    'RUNTIME_NOT_READY',
}


def _step_has_fatal_dependency_result(step):
    """Identify infrastructure results that make business descendants unsafe."""
    if step.status not in ('blocked', 'failed'):
        return False
    config = step.step_config_json if isinstance(
        step.step_config_json, dict) else {}
    inputs = config.get('inputs') if isinstance(config.get('inputs'), dict) else {}
    policy = str(
        config.get('dependency_failure_policy')
        or inputs.get('dependency_failure_policy') or '').strip().lower()
    if policy == 'continue':
        return False
    if policy == 'fatal':
        return True

    blocker = step.blocker_json if isinstance(step.blocker_json, dict) else {}
    contract = step.contract_result_json if isinstance(
        step.contract_result_json, dict) else {}
    outputs = step.outputs_json if isinstance(step.outputs_json, dict) else {}
    evidence = step.evidence_json if isinstance(step.evidence_json, dict) else {}
    stack = [blocker, contract, outputs, evidence]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        for key in ('code', 'blocker_code', 'error_code'):
            code = str(value.get(key) or '').strip().upper()
            if code in _FATAL_WORKFLOW_DEPENDENCY_CODES:
                return True
        runner_execution = value.get('runner_execution')
        if isinstance(runner_execution, dict):
            state = str(
                runner_execution.get('state')
                or runner_execution.get('status') or '').strip().lower()
            if state in ('not_started', 'start_failed', 'unavailable'):
                return True
        stack.extend(value.values())
    return False


def _propagate_fatal_workflow_dependencies(run, steps, definition, state):
    """Skip unsafe descendants while retaining explicit cleanup/report paths."""
    strict_result_ids = {
        step.step_id for step in steps
        if _step_has_fatal_dependency_result(step)
    }
    for step in steps:
        branch = (
            step.branch_result_json
            if isinstance(step.branch_result_json, dict) else {})
        if isinstance(branch.get('dependency_failure_propagation'), dict):
            state[step.step_id] = PROPAGATED_UPSTREAM_BLOCKED

    step_by_id = {step.step_id: step for step in steps}
    while True:
        blocked = blocked_dependency_ids(
            definition, state,
            strict_result_step_ids=strict_result_ids)
        if not blocked:
            break
        now = datetime.now()
        for step_id, dependency_ids in blocked.items():
            step = step_by_id.get(step_id)
            if not step or step.status != 'pending':
                continue
            propagation = {
                'code': 'UPSTREAM_DEPENDENCY_BLOCKED',
                'upstream_step_ids': list(dependency_ids),
            }
            step.status = 'skipped'
            step.summary = '上游必要步骤未通过，未启动本节点'
            step.outputs_json = {
                'execution_skipped': True,
                'skip_reason_code': 'UPSTREAM_DEPENDENCY_BLOCKED',
                'upstream_step_ids': list(dependency_ids),
            }
            step.branch_result_json = {
                'dependency_failure_propagation': propagation,
            }
            step.finished_at = now
            step.updated_by = 'workflow-dependency-gate'
            step.health_status = 'idle'
            step.health_checked_at = now
            step.progress_at = now
            step.progress_by = 'workflow-dependency-gate'
            step.progress_phase = 'skipped'
            step.progress_message = step.summary
            step.progress_percent = 100
            state[step_id] = PROPAGATED_UPSTREAM_BLOCKED
    return strict_result_ids


def _recompute_run_status(run, actor='system'):
    """Advance pending steps whose dependencies are satisfied."""
    steps = WorkflowRunStep.query.filter_by(run_id=run.id).all()
    state = {s.step_id: s.status for s in steps}
    definition = _run_definition_snapshot(run, steps)
    strict_result_ids = _propagate_fatal_workflow_dependencies(
        run, steps, definition, state)

    for notification_step in steps:
        if (notification_step.step_type != 'notification'
                or notification_step.status != 'waiting_approval'):
            continue
        if not all(state.get(dep) in ('passed', 'skipped')
                   for dep in (notification_step.depends_on_json or [])):
            continue
        notification_gate = _workflow_notification_gate(run)
        if notification_gate.get('allowed'):
            config = dict(notification_step.step_config_json or {})
            config['notification_authorization'] = notification_gate
            config['credential_mode'] = 'hub_brokered'
            config.pop('webhook', None)
            config.pop('webhook_url', None)
            notification_step.step_config_json = config
            continue
        notification_step.status = 'skipped'
        notification_step.outputs_json = {
            'notification_skipped': True,
            'wecom_sent': False,
            'skip_reason': 'business_pass_or_automation_only',
        }
        notification_step.branch_result_json = {
            'notification_gate': notification_gate,
            'skipped_descendants': _skip_step_descendants(
                run, notification_step.step_id,
                '通知硬门禁未通过，业务通过或仅自动化问题，静默结束'),
        }
        notification_step.summary = '通知硬门禁未通过，静默结束'
        notification_step.finished_at = datetime.now()
        notification_step.updated_by = 'workflow-notification-gate'
        _recompute_run_status(run, actor)
        return

    ready_ids = ready_step_ids(
        definition, state,
        strict_result_step_ids=strict_result_ids)
    # A blocked/failed node is terminal for the Run only after explicitly
    # permitted downstream work has drained. This lets safety cleanup retain
    # its real failure result while analysis/reporting still collects evidence.
    # Without a ready, active, or approval-waiting continuation the behavior
    # remains fail-closed.
    has_continuation_work = bool(ready_ids) or any(
        s.status in ('running', 'retrying', 'waiting_approval') for s in steps)

    has_blocked = any(s.status == 'blocked' for s in steps)
    has_failed = any(s.status == 'failed' for s in steps)
    if (has_blocked or has_failed) and not has_continuation_work:
        # Outcome defaults are terminal-state aware.  Set the conservative
        # orchestration result first, then allow composite mode to refine it
        # from the six independent outcome dimensions.
        run.status = 'failed' if has_failed else 'blocked'
        outcomes = _sync_run_outcomes(run)
        run.status = (
            composite_workflow_terminal_status(
                outcomes,
                has_blocked=has_blocked,
                has_failed=has_failed,
            )
            if _composite_outcomes_enabled(run)
            else run.status
        )
        run.finished_at = run.finished_at or datetime.now()
        problem = next((
            s for s in steps if s.status == (
                'failed' if has_failed else 'blocked')), None)
        if run.status == 'succeeded':
            run.current_step_id = ''
            run.blocker_json = {}
        else:
            run.current_step_id = problem.step_id if problem else ''
            run.blocker_json = problem.blocker_json if problem else {}
        _sync_run_conclusions(run)
        return
    if any(s.status == 'waiting_approval' for s in steps):
        run.status = 'waiting_approval'
        run.finished_at = None
        wait = next((s for s in steps if s.status == 'waiting_approval'), None)
        run.current_step_id = wait.step_id if wait else ''
        if wait:
            _create_approval_if_missing(run, wait, actor)
        _sync_run_conclusions(run)
        return
    if steps and all(s.status in ('passed', 'skipped') for s in steps):
        run.status = 'succeeded'
        run.current_step_id = ''
        run.finished_at = run.finished_at or datetime.now()
        _sync_run_outcomes(run)
        _sync_run_conclusions(run)
        return

    step_by_id = {s.step_id: s for s in steps}
    if ready_ids:
        run.status = 'running'
        run.finished_at = None
        run.started_at = run.started_at or datetime.now()
        run.current_step_id = ready_ids[0]
        notification_skipped_now = False
        for sid in ready_ids:
            step = step_by_id.get(sid)
            if not step:
                continue
            config = step.step_config_json or {}
            if step.step_type == 'notification':
                notification_gate = _workflow_notification_gate(run)
                if not notification_gate.get('allowed'):
                    step.status = 'skipped'
                    step.outputs_json = {
                        'notification_skipped': True,
                        'wecom_sent': False,
                        'skip_reason': 'business_pass_or_automation_only',
                    }
                    step.branch_result_json = {
                        'notification_gate': notification_gate,
                        'skipped_descendants': _skip_step_descendants(
                            run, step.step_id,
                            '通知硬门禁未通过，业务通过或仅自动化问题，静默结束'),
                    }
                    step.summary = '通知硬门禁未通过，静默结束'
                    step.finished_at = datetime.now()
                    step.updated_by = 'workflow-notification-gate'
                    notification_skipped_now = True
                    continue
                config = dict(config)
                config['notification_authorization'] = notification_gate
                config['credential_mode'] = 'hub_brokered'
                config.pop('webhook', None)
                config.pop('webhook_url', None)
                step.step_config_json = config
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
        if notification_skipped_now:
            _recompute_run_status(run, actor)
            return
        _sync_run_conclusions(run)
        return

    if any(s.status in ('running', 'retrying') for s in steps):
        run.status = 'running'
        run.finished_at = None
        run.current_step_id = next(
            s.step_id for s in steps if s.status in ('running', 'retrying'))
    else:
        run.status = 'pending'
        run.finished_at = None
    _sync_run_conclusions(run)


@api_bp.route('/workflow-definitions', methods=['GET'])
def list_workflow_definitions():
    err = _require_actor()
    if err:
        return err
    _ensure_default_definition()
    query = WorkflowDefinition.query.filter(
        WorkflowDefinition.status != 'deleted')
    raw_project_id = request.args.get('project_id')
    if raw_project_id not in (None, ''):
        try:
            project_id = int(raw_project_id)
            if project_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _workflow_api_error(
                'INVALID_PROJECT_ID', 'project_id must be a positive integer')
        query = query.filter(WorkflowDefinition.project_id == project_id)
    rows = query.order_by(WorkflowDefinition.updated_at.desc()).all()
    favorite_only = request.args.get('favorite') in ('1', 'true', 'True')
    catalog_kind = str(request.args.get('catalog_kind') or '').strip().lower()
    if catalog_kind and catalog_kind not in {'business', 'probe', 'legacy'}:
        return _workflow_api_error(
            'INVALID_WORKFLOW_CATALOG_KIND',
            'catalog_kind must be business, probe or legacy')
    favorite_ids = _actor_favorite_definition_ids() if favorite_only else None
    visible = []
    for row in rows:
        if favorite_only and row.id not in favorite_ids:
            continue
        if (catalog_kind and workflow_catalog_metadata(
                row.definition_json or {}).get('kind') != catalog_kind):
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
    can_manage = _can_manage_definition(row)
    if not _can_edit_definition(row):
        return jsonify({'error': '只有 Workflow 创建者、编辑者或管理员可以更新模板'}), 403
    data = request.get_json() or {}
    if not can_manage and ({'status', 'visibility_scope'} & set(data)):
        return jsonify({'error': 'Workflow 编辑者不能修改状态或可见性'}), 403
    before_version = int(row.version or 0)
    before_hash = hashlib.sha256(json.dumps(
        row.definition_json or {}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    try:
        definition = merge_workflow_definition_update(row.definition_json or {}, data)
    except ValueError as exc:
        message = str(exc)
        return _workflow_api_error(
            'WORKFLOW_DEFINITION_SCHEMA_INVALID',
            message,
            details={'path': message.split(':', 1)[0]})
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
    after_hash = hashlib.sha256(json.dumps(
        row.definition_json or {}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    db.session.add(AuditLog(
        action='update',
        resource_type='workflow_definition_content',
        resource_id=row.id,
        resource_name=row.name,
        operator=_actor_name(),
        ip_address=request.remote_addr,
        detail=json.dumps({
            'before_version': before_version,
            'after_version': int(row.version or 0),
            'before_definition_sha256': before_hash,
            'after_definition_sha256': after_hash,
            'changed_fields': sorted(data.keys()),
            'actor_can_manage': can_manage,
        }, ensure_ascii=False, sort_keys=True),
    ))
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
    nested_definition = data.get('definition')
    if isinstance(nested_definition, dict):
        definition_input = copy.deepcopy(nested_definition)
        for field in (
                'key', 'id', 'name', 'description', 'version', 'steps',
                'context', 'start_vars_schema'):
            if field in data and field not in definition_input:
                definition_input[field] = data[field]
    else:
        definition_input = data
    try:
        definition = normalize_workflow_definition(definition_input)
    except ValueError as exc:
        message = str(exc)
        return _workflow_api_error(
            'WORKFLOW_DEFINITION_SCHEMA_INVALID',
            message,
            details={'path': message.split(':', 1)[0]})
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
        editor_acl_json=normalize_editor_acl({}),
        visibility_scope=visibility_scope,
    )
    db.session.add(row)
    db.session.flush()
    if actor['type'] == 'claw':
        _sync_workflow_create_policy(row, [actor['id']])
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
        _sync_workflow_create_policy(row, claw_ids)
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
    if actor_type == 'claw':
        _sync_workflow_create_policy(row, [actor_id])
    db.session.commit()
    return jsonify(_definition_payload(row))


@api_bp.route('/workflow-definitions/<int:definition_id>/editors',
              methods=['PUT', 'POST'])
def replace_workflow_definition_editors(definition_id):
    """Replace the multi-Agent/user editor ACL; owner/admin only."""
    err = _require_actor()
    if err:
        return err
    definition = WorkflowDefinition.query.get_or_404(definition_id)
    if not _definition_visible(definition):
        return jsonify({'error': 'workflow definition 不存在'}), 404
    if (definition.owner_type == 'system'
            or definition.workflow_key in {w['key'] for w in BUILTIN_WORKFLOWS}):
        return jsonify({'error': '系统内置 Workflow 模板不能添加编辑者'}), 403
    if not _can_manage_definition(definition):
        return jsonify({'error': '只有 Workflow 创建者或管理员可以管理编辑者'}), 403
    data = request.get_json(silent=True) or {}
    raw_claw_ids = data.get('claw_ids', [])
    raw_user_ids = data.get('user_ids', [])
    if not isinstance(raw_claw_ids, list) or not isinstance(raw_user_ids, list):
        return jsonify({'error': 'claw_ids 和 user_ids 必须是整数数组'}), 400
    try:
        claw_ids = sorted({int(value) for value in raw_claw_ids})
        user_ids = sorted({int(value) for value in raw_user_ids})
    except (TypeError, ValueError):
        return jsonify({'error': 'claw_ids 和 user_ids 必须是整数数组'}), 400
    existing_claw_ids = {
        item.id for item in OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(claw_ids),
            OpenClawInstance.status != 'deleted').all()
    } if claw_ids else set()
    missing_claws = [value for value in claw_ids if value not in existing_claw_ids]
    if missing_claws:
        return jsonify({'error': 'OpenClaw 不存在：%s' %
                        ','.join(map(str, missing_claws))}), 404
    existing_user_ids = {
        item.id for item in User.query.filter(User.id.in_(user_ids)).all()
    } if user_ids else set()
    missing_users = [value for value in user_ids if value not in existing_user_ids]
    if missing_users:
        return jsonify({'error': '用户不存在：%s' %
                        ','.join(map(str, missing_users))}), 404

    before = normalize_editor_acl(definition.editor_acl_json or {})
    after = normalize_editor_acl({
        'claw_ids': claw_ids,
        'user_ids': user_ids,
    })
    definition.editor_acl_json = after
    synced_claw_ids = _sync_workflow_create_policy(
        definition, after.get('claw_ids') or [])
    definition_json = copy.deepcopy(definition.definition_json or {})
    definition.version = max(
        int(definition.version or 0),
        int(definition_json.get('version') or 0),
    ) + 1
    definition_json['version'] = definition.version
    definition.definition_json = definition_json
    db.session.add(AuditLog(
        action='update',
        resource_type='workflow_definition_editors',
        resource_id=definition.id,
        resource_name=definition.name,
        operator=_actor_name(),
        ip_address=request.remote_addr,
        detail=json.dumps({
            'before': before,
            'after': after,
            'version': definition.version,
            'sidecar_policy_synced_claw_ids': synced_claw_ids,
        }, ensure_ascii=False, sort_keys=True),
    ))
    db.session.commit()
    return jsonify(_definition_payload(definition))


@api_bp.route('/workflow-runs', methods=['GET'])
def list_workflow_runs():
    err = _require_actor()
    if err:
        return err
    _refresh_workflow_step_health(commit=True)
    q, query_error = _workflow_run_query_from_request()
    if query_error:
        return query_error
    if any(key in request.args for key in ('page', 'page_size', 'per_page')):
        try:
            page_number = max(1, int(request.args.get('page') or 1))
            page_size = int(
                request.args.get('page_size')
                or request.args.get('per_page')
                or 50)
        except (TypeError, ValueError):
            return _workflow_api_error(
                'INVALID_PAGINATION', 'page and page_size must be integers')
        page_size = max(1, min(page_size, 200))
        total = q.order_by(None).count()
        start = (page_number - 1) * page_size
        rows = q.offset(start).limit(page_size).all()
        return jsonify({
            'items': [_run_payload(row, with_steps=False) for row in rows],
            'total': total,
            'page': page_number,
            'page_size': page_size,
            'pages': (total + page_size - 1) // page_size,
        })
    # Preserve the historical bare-array response when pagination is omitted.
    rows = q.limit(100).all()
    return jsonify([_run_payload(row, with_steps=False) for row in rows])


@api_bp.route('/workflow-runs/latest', methods=['GET'])
def get_latest_workflow_run():
    err = _require_actor()
    if err:
        return err
    _refresh_workflow_step_health(commit=True)
    q, query_error = _workflow_run_query_from_request()
    if query_error:
        return query_error
    run = q.first()
    if not run:
        return _workflow_api_error(
            'WORKFLOW_RUN_NOT_FOUND',
            'No workflow run matches the requested filters',
            status=404)
    return jsonify(_run_payload(run, with_steps=False))


@api_bp.route('/workflow-runs', methods=['POST'])
def create_workflow_run():
    err = _require_actor()
    if err:
        return err
    data = request.get_json() or {}
    definition = None
    legacy_definition_id = data.get('definition_id')
    workflow_definition_id = data.get('workflow_definition_id')
    if (legacy_definition_id not in (None, '')
            and workflow_definition_id not in (None, '')
            and str(legacy_definition_id) != str(workflow_definition_id)):
        return _workflow_api_error(
            'WORKFLOW_DEFINITION_ID_MISMATCH',
            'definition_id and workflow_definition_id must refer to the same definition')
    requested_definition_id = (
        workflow_definition_id
        if workflow_definition_id not in (None, '')
        else legacy_definition_id
    )
    if requested_definition_id not in (None, ''):
        try:
            definition = WorkflowDefinition.query.get(int(requested_definition_id))
        except (TypeError, ValueError):
            return _workflow_api_error(
                'INVALID_WORKFLOW_DEFINITION_ID',
                'workflow_definition_id must be an integer')
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
    body_idempotency_key = str(data.get('idempotency_key') or '').strip()
    header_idempotency_key = str(
        request.headers.get('Idempotency-Key') or '').strip()
    if (body_idempotency_key and header_idempotency_key
            and body_idempotency_key != header_idempotency_key):
        return _workflow_api_error(
            'IDEMPOTENCY_KEY_MISMATCH',
            'Body and header idempotency keys must match')
    idempotency_key = body_idempotency_key or header_idempotency_key or None
    if idempotency_key and len(idempotency_key) > 128:
        return _workflow_api_error(
            'IDEMPOTENCY_KEY_TOO_LONG',
            'idempotency_key must not exceed 128 characters')
    run_metadata = {}
    for field, max_length in (
        ('controller_run_id', 160),
        ('correlation_id', 160),
        ('trigger_source', 64),
    ):
        value = str(data.get(field) or '').strip() or None
        if value and len(value) > max_length:
            return _workflow_api_error(
                'WORKFLOW_RUN_METADATA_TOO_LONG',
                f'{field} must not exceed {max_length} characters',
                details={'field': field, 'max_length': max_length})
        run_metadata[field] = value
    normalized = copy.deepcopy(definition.definition_json or {})
    definition_snapshot_hash = hashlib.sha256(json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
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

    caller_claw = get_current_claw()
    raw_worker_claw_id = (
        data.get('worker_claw_id')
        if data.get('worker_claw_id') not in (None, '')
        else data.get('executor_worker_claw_id'))
    if caller_claw:
        if raw_worker_claw_id not in (None, ''):
            try:
                requested_worker_claw_id = int(raw_worker_claw_id)
            except (TypeError, ValueError):
                return _workflow_api_error(
                    'INVALID_WORKER_CLAW_ID',
                    'worker_claw_id must be an integer')
            if requested_worker_claw_id != caller_claw.id:
                return _workflow_api_error(
                    'WORKER_BINDING_MISMATCH',
                    'A Claw-started Flow must bind to the initiating Worker',
                    status=403,
                    details={
                        'initiating_claw_id': caller_claw.id,
                        'requested_worker_claw_id': requested_worker_claw_id,
                    })
        worker_claw_id = caller_claw.id
    elif raw_worker_claw_id not in (None, ''):
        try:
            worker_claw_id = int(raw_worker_claw_id)
        except (TypeError, ValueError):
            return _workflow_api_error(
                'INVALID_WORKER_CLAW_ID',
                'worker_claw_id must be an integer')
    else:
        worker_claw_id = None

    if worker_claw_id:
        worker_claw = OpenClawInstance.query.filter(
            OpenClawInstance.id == worker_claw_id,
            OpenClawInstance.status != 'deleted',
        ).first()
        if not worker_claw:
            return _workflow_api_error(
                'WORKER_CLAW_NOT_FOUND',
                'The bound Flow Worker does not exist',
                details={'worker_claw_id': worker_claw_id})
    if (_definition_requires_worker_binding(normalized)
            and not worker_claw_id):
        return _workflow_api_error(
            'WORKER_BINDING_REQUIRED',
            'This Workflow requires one bound physical Worker',
            details={'workflow_definition_id': definition.id})

    start_vars = (
        data.get('start_vars')
        if isinstance(data.get('start_vars'), dict)
        else data.get('variables')
        if isinstance(data.get('variables'), dict)
        else data.get('start_parameters')
        if isinstance(data.get('start_parameters'), dict)
        else {}
    )
    try:
        assignment = resolve_workflow_run_assignment(
            normalized,
            start_vars,
            worker_claw_id=worker_claw_id,
            selected_executor_claw_ids=selected_claw_ids,
        )
    except ValueError as exc:
        return _workflow_api_error(
            'WORKFLOW_ASSIGNMENT_INVALID',
            str(exc),
            status=422,
            details={'workflow_definition_id': definition.id},
        )

    assignment_claws = {}
    if assignment:
        assignment_ids = sorted({
            int(assignment['executor_claw_id']),
            *(
                [int(assignment['reviewer_claw_id'])]
                if assignment.get('reviewer_claw_id') else []
            ),
        })
        rows = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(assignment_ids),
            OpenClawInstance.status != 'deleted',
        ).all()
        assignment_claws = {row.id: row for row in rows}
        missing = [cid for cid in assignment_ids if cid not in assignment_claws]
        if missing:
            return _workflow_api_error(
                'WORKFLOW_ASSIGNMENT_CLAW_NOT_FOUND',
                'Assigned executor/reviewer does not exist',
                status=422,
                details={'missing_claw_ids': missing},
            )
        incompatible = [
            claw.id for claw in rows
            if definition.project_id is not None
            and claw.project_id not in (None, definition.project_id)
        ]
        if incompatible:
            return _workflow_api_error(
                'WORKFLOW_ASSIGNMENT_PROJECT_MISMATCH',
                'Assigned executor/reviewer is outside the Workflow project',
                status=422,
                details={'claw_ids': incompatible},
            )
        normalized = materialize_workflow_run_assignment(
            normalized,
            assignment,
            {cid: claw.name for cid, claw in assignment_claws.items()},
        )
        selected_claw_ids = [int(assignment['executor_claw_id'])]
        start_vars = assignment['start_vars']
        worker_claw_id = int(assignment['worker_claw_id'])

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
        for step in (
                (normalized.get('steps') or []) if not assignment else []):
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
    context = build_workflow_start_context(
        start_vars,
        raw_context,
        start_mode=start_mode,
        schedule_cron=schedule_cron,
        executor_claw_ids=selected_claw_ids,
        executor_user_ids=selected_user_ids,
        worker_claw_id=worker_claw_id,
    )
    context['workflow_definition_snapshot'] = {
        'workflow_definition_id': definition.id,
        'version': int(definition.version or 1),
        'sha256': definition_snapshot_hash,
        'template_revision': str(
            (definition.definition_json or {}).get('template_revision')
            or (
                (definition.definition_json or {}).get('context') or {}
            ).get('template_revision')
            or ''),
    }
    context['workflow_catalog_snapshot'] = workflow_catalog_metadata(normalized)
    context['outcome_requirements_snapshot'] = workflow_outcome_requirements(
        normalized)
    if assignment:
        context['assignment_snapshot'] = {
            'schema': assignment['schema'],
            'executor_claw_id': assignment['executor_claw_id'],
            'reviewer_claw_id': assignment.get('reviewer_claw_id'),
            'worker_claw_id': assignment['worker_claw_id'],
            'step_roles': assignment['step_roles'],
            'source': assignment['source'],
        }
    snapshot_library_id, snapshot_input_warning = resolve_workflow_library_id(
        data, context, workflow_key=definition.workflow_key)
    if snapshot_input_warning:
        context = append_workflow_snapshot_warning(
            context, snapshot_input_warning)
    context['execution_input_snapshot'] = _workflow_execution_input_snapshot(
        context['workflow_definition_snapshot'],
        start_vars,
        raw_context,
        normalized.get('context'),
        testcase_library_id=snapshot_library_id,
    )
    run_name = data.get('run_name') or normalized.get('name') or definition.name
    run_project_id = data.get('project_id') or definition.project_id
    idempotency_request_hash = None
    if idempotency_key:
        idempotency_request_hash = _workflow_run_create_hash(
            definition.id,
            {
                'run_name': run_name,
                'project_id': run_project_id,
                'context': context,
                'controller_run_id': run_metadata['controller_run_id'],
                'correlation_id': run_metadata['correlation_id'],
                'trigger_source': run_metadata['trigger_source'],
            },
        )
        existing_run = WorkflowRun.query.filter_by(
            definition_id=definition.id,
            idempotency_key=idempotency_key,
        ).first()
        if existing_run:
            if (existing_run.idempotency_request_hash
                    and existing_run.idempotency_request_hash
                    != idempotency_request_hash):
                return _workflow_api_error(
                    'IDEMPOTENCY_CONFLICT',
                    'The idempotency key was already used with a different payload',
                    status=409,
                    details={'workflow_run_id': existing_run.id})
            payload = _workflow_run_create_payload(
                existing_run, compact=bool(caller_claw),
                idempotent_replay=True)
            return jsonify(payload), 200
    run = WorkflowRun(
        definition_id=definition.id,
        run_name=run_name,
        status='pending',
        project_id=run_project_id,
        triggered_by=_actor_name(),
        idempotency_key=idempotency_key,
        idempotency_request_hash=idempotency_request_hash,
        controller_run_id=run_metadata['controller_run_id'],
        correlation_id=run_metadata['correlation_id'],
        trigger_source=run_metadata['trigger_source'],
        context_json=context,
    )
    db.session.add(run)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        if idempotency_key:
            existing_run = WorkflowRun.query.filter_by(
                definition_id=definition.id,
                idempotency_key=idempotency_key,
            ).first()
            if existing_run:
                if (existing_run.idempotency_request_hash
                        and existing_run.idempotency_request_hash
                        != idempotency_request_hash):
                    return _workflow_api_error(
                        'IDEMPOTENCY_CONFLICT',
                        'The idempotency key was already used with a different payload',
                        status=409,
                        details={'workflow_run_id': existing_run.id})
                payload = _workflow_run_create_payload(
                    existing_run, compact=bool(caller_claw),
                    idempotent_replay=True)
                return jsonify(payload), 200
        raise
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
    if snapshot_library_id is not None:
        snapshot_record = None
        snapshot_warning = None
        try:
            # A SAVEPOINT isolates missing migration tables, malformed legacy
            # content and other snapshot failures from stable Flow creation.
            with db.session.begin_nested():
                snapshot_record, snapshot_warning = (
                    freeze_workflow_run_library_snapshot(
                        run, snapshot_library_id, _actor_name()))
        except Exception as exc:
            current_app.logger.warning(
                'Workflow Run %s testcase library snapshot failed: %s',
                run.id, exc, exc_info=True)
            snapshot_warning = {
                'code': 'TESTCASE_LIBRARY_SNAPSHOT_FAILED',
                'message': (
                    'Failed to freeze testcase library; Run continues with '
                    'its legacy execution path'),
                'details': {
                    'library_id': snapshot_library_id,
                    'error_type': type(exc).__name__,
                },
            }
        run_context = (
            copy.deepcopy(run.context_json)
            if isinstance(run.context_json, dict) else {})
        if snapshot_record is not None:
            run_context['testcase_library_snapshot'] = snapshot_record.to_dict()
        if snapshot_warning:
            run_context = append_workflow_snapshot_warning(
                run_context, snapshot_warning)
        run.context_json = run_context
    db.session.commit()
    payload = _workflow_run_create_payload(
        run, compact=bool(caller_claw), idempotent_replay=False)
    return jsonify(payload), 201


@api_bp.route('/workflow-runs/<int:run_id>', methods=['GET'])
def get_workflow_run(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if not _run_visible_to_actor(run):
        return jsonify({'error': 'workflow run 不存在'}), 404
    _refresh_workflow_step_health(run, commit=True)
    return jsonify(_run_payload(run, with_steps=True))


@api_bp.route(
    '/workflow-runs/<int:run_id>/testcase-library-snapshot', methods=['GET'])
def get_workflow_run_testcase_library_snapshot(run_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if not _run_visible_to_actor(run):
        return jsonify({'error': 'workflow run 不存在'}), 404
    snapshot = WorkflowRunLibrarySnapshot.query.filter_by(
        workflow_run_id=run.id).first()
    if not snapshot:
        return _workflow_api_error(
            'WORKFLOW_RUN_LIBRARY_SNAPSHOT_NOT_FOUND',
            'Workflow Run does not have a frozen testcase library snapshot',
            status=404)
    include_cases = str(request.args.get('include_cases', 'true')).lower() in {
        '1', 'true', 'yes'}
    return jsonify(snapshot.to_dict(with_cases=include_cases))


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
    if not _run_visible_to_actor(run):
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
    try:
        with db.session.begin_nested():
            WorkflowRunLibrarySnapshot.query.filter_by(
                workflow_run_id=run.id).delete(synchronize_session=False)
    except Exception as exc:
        # The snapshot table is an optional additive migration. A partially
        # deployed environment must retain the historical Run deletion path.
        current_app.logger.warning(
            'Workflow Run %s snapshot cleanup skipped: %s',
            run.id, exc, exc_info=True)
    cleanup = cleanup_workflow_result_records(run.id)
    WorkflowArtifact.query.filter_by(run_id=run.id).delete()
    WorkflowApproval.query.filter_by(run_id=run.id).delete()
    WorkflowRunStep.query.filter_by(run_id=run.id).delete()
    db.session.delete(run)
    db.session.commit()
    return jsonify({'ok': True, 'deleted_id': run_id, 'cleanup': cleanup})


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
        WorkflowRunStep.step_type == 'worker_task',
        WorkflowRunStep.status.in_(('running', 'retrying')),
    ).order_by(WorkflowRunStep.updated_at.asc()).limit(1000).all()
    # Production still runs MariaDB 5.5, which has no JSON_EXTRACT.  Keep the
    # query relational-only and apply the run-level worker binding through the
    # same Python authorization helper used by claim/result/progress routes.
    rows = [
        row for row in rows if _step_belongs_to_claw(row, claw.id)
    ][:20]
    return jsonify([_step_runtime_payload(r) for r in rows])


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/claim', methods=['POST'])
def claim_workflow_step(run_id, step_id):
    """Worker claims a running step before executing it."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    idem_record, idem_response = _workflow_idempotency_begin()
    if idem_response:
        return idem_response
    data = request.get_json() or {}
    worker_id = _worker_id_from_payload(data, claw)
    now = datetime.now()
    run = WorkflowRun.query.get_or_404(run_id)
    if run.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Run 当前不可执行'}), 409
    step = _locked_workflow_step(run_id, step_id)
    config = step.step_config_json if isinstance(step.step_config_json, dict) else {}
    direct_policy = direct_execution_lease(step.step_type, config)
    direct_claim = direct_execution_claim_allowed(
        step.step_type, config, data.get('claim_scope'))
    if step.step_type == 'agent_task' and direct_policy and not direct_claim:
        return _workflow_api_error(
            'DIRECT_EXECUTION_SCOPE_REQUIRED',
            'Agent Direct Runner claim requires claim_scope=runner_operation',
            status=409,
            details={
                'step_id': step.step_id,
                'claim_scope': data.get('claim_scope'),
            })
    if step.step_type != 'worker_task' and not direct_claim:
        return _workflow_api_error(
            'INVALID_STEP_TYPE',
            'Only worker_task steps can be claimed by Job Service',
            status=409,
            details={'step_id': step.step_id, 'step_type': step.step_type})
    lease_seconds = normalize_step_lease_seconds(
        data.get('lease_seconds')
        if data.get('lease_seconds') is not None
        else (direct_policy or {}).get('lease_seconds'))
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    claim = can_claim_step_lease(
        getattr(step, 'claimed_claw_id', None),
        step.claimed_by,
        getattr(step, 'claim_expires_at', None),
        claw.id,
        worker_id,
        now,
    )
    if not claim.get('allowed'):
        return _claim_error_response(claim.get('reason') or 'claim_conflict', step)
    same_active_owner = bool(
        step.claimed_claw_id == claw.id
        and step.claimed_by == worker_id
        and step.claim_expires_at
        and step.claim_expires_at > now)
    if not same_active_owner:
        step.claim_fencing_token = int(step.claim_fencing_token or 0) + 1
    step.claimed_by = worker_id
    step.claimed_at = now
    step.claimed_claw_id = claw.id
    step.claim_lease_seconds = lease_seconds
    step.claim_expires_at = now + timedelta(seconds=lease_seconds)
    body = {
        'ok': True,
        'step': _step_runtime_payload(step),
        'lease_seconds': lease_seconds,
        'claim_scope': (
            'runner_operation' if direct_claim else 'worker_task'),
    }
    _workflow_idempotency_store(idem_record, 200, body)
    db.session.commit()
    return jsonify(body)


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/heartbeat', methods=['POST'])
def heartbeat_workflow_step(run_id, step_id):
    """Worker reports liveness while executing a workflow step."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    data = request.get_json() or {}
    worker_id = _worker_id_from_payload(data, claw)
    run = WorkflowRun.query.get_or_404(run_id)
    step = _locked_workflow_step(run_id, step_id)
    if run.status not in ('running', 'retrying'):
        return _workflow_lifecycle_conflict(
            run, step, operation='heartbeat')
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    now = datetime.now()
    claim_error = _require_active_step_claim(
        step, claw, worker_id,
        data.get('fencing_token', data.get('claim_fencing_token')))
    if claim_error:
        return claim_error
    step.heartbeat_at = now
    step.heartbeat_by = worker_id
    step.heartbeat_count = (step.heartbeat_count or 0) + 1
    step.missed_heartbeat_count = 0
    step.health_status = 'healthy'
    step.health_checked_at = now
    _apply_step_progress(step, data, worker_id, now)
    if workflow_step_claim_required(
            step.step_type, step.step_config_json or {}):
        _renew_step_claim(step, now)
    db.session.commit()
    return jsonify({'ok': True, 'step': _step_runtime_payload(step)})


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/progress', methods=['POST'])
def progress_workflow_step(run_id, step_id):
    """Worker/agent reports human-readable progress for a long-running step."""
    claw = get_current_claw()
    if not claw:
        return jsonify({'error': '缺少 OpenClaw Token'}), 401
    data = request.get_json() or {}
    reporter = str(data.get('worker_id') or data.get('reporter') or f'claw:{claw.id}').strip()
    run = WorkflowRun.query.get_or_404(run_id)
    step = _locked_workflow_step(run_id, step_id)
    if run.status not in ('running', 'retrying'):
        return _workflow_lifecycle_conflict(
            run, step, operation='progress')
    if step.status not in ('running', 'retrying'):
        return jsonify({'error': 'Workflow Step 当前不可执行'}), 409
    if not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    now = datetime.now()
    claim_error = _require_active_step_claim(
        step, claw, reporter,
        data.get('fencing_token', data.get('claim_fencing_token')))
    if claim_error:
        return claim_error
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
        if workflow_step_claim_required(
                step.step_type, step.step_config_json or {}):
            _renew_step_claim(step, now)
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
    _clear_step_claim(step)


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
    step = _locked_workflow_step(run_id, step_id)
    if not _can_change_workflow_step_status(run, step):
        return jsonify({'error': '只有节点执行 Agent、owner 或管理员可以修改节点状态'}), 403
    data = request.get_json() or {}
    claw = get_current_claw()
    if claw and workflow_step_claim_required(
            step.step_type, step.step_config_json or {}):
        worker_id = _worker_id_from_payload(data, claw)
        claim_error = _require_active_step_claim(
            step, claw, worker_id,
            data.get('fencing_token', data.get('claim_fencing_token')))
        if claim_error:
            return claim_error
    display_state = str(data.get('display_state') or data.get('state') or '').strip()
    if display_state not in ('todo', 'running', 'blocked', 'done'):
        return jsonify({'error': 'display_state 必须是 todo/running/blocked/done'}), 400
    if display_state == 'done':
        finalizer_contract = validate_workflow_finalizer_result(
            step.step_config_json or {}, data)
        if not finalizer_contract.get('valid'):
            return _workflow_api_error(
                'FINALIZER_RECEIPT_REQUIRED',
                'Workflow finalizer must complete all side effects before '
                'submitting its terminal result',
                status=409,
                details={
                    'workflow_run_id': run.id,
                    'step_id': step.step_id,
                    'missing': finalizer_contract.get('missing') or [],
                },
            )
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
    idem_record, idem_response = _workflow_idempotency_begin()
    if idem_response:
        return idem_response
    run = WorkflowRun.query.get_or_404(run_id)
    step = _locked_workflow_step(run_id, step_id)
    data = request.get_json() or {}
    if (run.status in ('succeeded', 'failed', 'blocked', 'cancelled')
            and not (run.status == 'blocked'
                     and data.get('force_recover') is True)):
        return _workflow_lifecycle_conflict(
            run, step, operation='result')
    if step.status in ('passed', 'failed', 'skipped'):
        return _workflow_lifecycle_conflict(
            run, step, operation='result')
    try:
        status = validate_worker_result_status(data.get('status'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    claw = get_current_claw()
    if claw and not _step_belongs_to_claw(step, claw.id):
        return jsonify({'error': '此 Step 不属于当前 OpenClaw'}), 403
    if claw and workflow_step_claim_required(
            step.step_type, step.step_config_json or {}):
        worker_id = _worker_id_from_payload(data, claw)
        claim_error = _require_active_step_claim(
            step, claw, worker_id,
            data.get('fencing_token', data.get('claim_fencing_token')))
        if claim_error:
            return claim_error
    finalizer_contract = validate_workflow_finalizer_result(
        step.step_config_json or {}, data)
    if not finalizer_contract.get('valid'):
        return _workflow_api_error(
            'FINALIZER_RECEIPT_REQUIRED',
            'Workflow finalizer must complete all side effects before '
            'submitting its terminal result',
            status=409,
            details={
                'workflow_run_id': run.id,
                'step_id': step.step_id,
                'missing': finalizer_contract.get('missing') or [],
            },
        )
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
        run.finished_at = None
        run.current_step_id = step.step_id
        run.blocker_json = {}
        body = _workflow_step_result_payload(
            run, step, compact=bool(claw))
        _workflow_idempotency_store(idem_record, 200, body)
        db.session.commit()
        return jsonify(body)
    step.summary = data.get('summary') or ''
    step.metrics_json = data.get('metrics') if isinstance(data.get('metrics'), dict) else {}
    step.outputs_json = collect_declared_step_outputs(
        step.step_config_json or {}, data)
    step.evidence_json = data.get('evidence') if isinstance(data.get('evidence'), dict) else {}
    step.logs_json = data.get('logs') if isinstance(data.get('logs'), dict) else {}
    step.blocker_json = data.get('blocker') if isinstance(data.get('blocker'), dict) else {}
    step.updated_by = _actor_name()
    step.finished_at = datetime.now()
    report, report_error = _resolve_workflow_report(run, step, data)
    if report_error:
        db.session.rollback()
        return jsonify({'error': report_error}), 400
    if report:
        step.outputs_json.setdefault('hub_report_id', report.id)
        if report.is_shared and report.share_token:
            step.outputs_json.setdefault('share_url', '/r/%s' % report.share_token)
    if (step.step_config_json or {}).get('notification_delivery_mode') == 'outbox':
        try:
            delivery = _record_notification_delivery(run, step, data.get('notification_delivery'))
        except (ValueError, TypeError) as exc:
            db.session.rollback()
            return jsonify({'error': str(exc)}), 400
        step.outputs_json['notification_outcome'] = {
            'pending': 'PENDING', 'failed': 'FAILED', 'sent': 'SENT',
        }[delivery.metadata_json['state']]
    ingestion = ingest_workflow_result(run, step, dict(
        data, outputs=step.outputs_json, evidence=step.evidence_json),
        actor=step.updated_by or 'workflow')
    contract = validate_step_result_contract(step.step_config_json or {}, {
        'metrics': step.metrics_json or {},
        'outputs': step.outputs_json or {},
        'evidence': step.evidence_json or {},
    })
    notification_outputs = _workflow_outputs_context(
        run, step, dict(step.outputs_json or {}, **(step.metrics_json or {})))
    notification_report = _verified_notification_report(
        run, notification_outputs)
    notification_policy = _workflow_step_notification_policy(step)
    notification_authorization = evaluate_notification_authorization(
        notification_outputs,
        report_readback=bool(notification_report),
        notification_policy=notification_policy)
    notification_contract = validate_notification_result_contract({
        'metrics': step.metrics_json or {},
        'outputs': step.outputs_json or {},
    }, notification_authorization, sent_audit=_notification_send_audited(
        run.id, step.step_id), notification_policy=notification_policy)
    if (notification_authorization.get('strict_silent')
            and not notification_authorization.get('allowed')):
        canonical_outputs = dict(step.outputs_json or {})
        canonical_outputs.setdefault(
            'skip_reason_code',
            notification_contract.get('skip_reason_code') or
            notification_authorization.get('skip_reason_code'))
        if notification_contract.get('skip_reason_detail'):
            canonical_outputs.setdefault(
                'skip_reason_detail',
                notification_contract['skip_reason_detail'])
        step.outputs_json = canonical_outputs
    contract['notification'] = notification_contract
    if not notification_contract['valid']:
        contract.update({
            'valid': False,
            'code': 'CONTRACT_INVALID',
            # Notification authorization is a platform hard gate and cannot be
            # weakened by a legacy on_fail=warn contract policy.
            'policy': 'blocked',
        })
        if not notification_authorization.get('allowed'):
            step.outputs_json = dict(step.outputs_json or {}, **{
                'notification_skipped': True,
                'wecom_sent': False,
                'skip_reason': 'business_pass_or_automation_only',
            })
    step.contract_result_json = dict(contract, evidence_ingestion=ingestion,
        outcome_input={key: data[key] for key in (
            'business_outcome', 'business_conclusion', 'business_failure_confirmed',
            'automation_outcome', 'evidence_outcome', 'evidence_ingest_status',
        ) if key in data})
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
    gate['contract'] = contract
    if not contract['valid']:
        gate['warning_only'] = contract['policy'] == 'warn'
        if contract['policy'] != 'warn':
            gate['passed'] = False
            gate['status'] = contract['policy']
    step.gate_result_json = gate
    step.status = gate['status'] if not gate['passed'] else status
    _clear_step_claim(step)
    step.health_status = 'idle'
    step.health_checked_at = datetime.now()
    step.progress_at = step.finished_at
    step.progress_by = step.updated_by
    step.progress_phase = 'completed' if status in ('passed', 'skipped') else status
    step.progress_message = step.summary or '节点已结束'
    if status in ('passed', 'skipped'):
        step.progress_percent = 100
    if step.status not in ('passed', 'failed', 'blocked', 'skipped', 'waiting_approval'):
        step.status = 'blocked'
    if step.status in ('blocked', 'failed') and not step.blocker_json:
        if not contract['valid']:
            step.blocker_json = {
                'type': 'result_contract_invalid',
                'code': 'CONTRACT_INVALID',
                'message': '节点结果缺少必填字段',
                'missing': contract['missing'],
                'notification': contract.get('notification') or {},
            }
        else:
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
    if report:
        link_report_to_evidence(run, report, step.updated_by or 'workflow')
    _sync_run_outcomes(
        run,
        result={
            **data,
            'status': step.status,
            'outputs': step.outputs_json or {},
            'metrics': step.metrics_json or {},
        },
        step=step,
        report=report,
        ingestion=ingestion,
    )
    _recompute_run_status(run, _actor_name())
    body = _workflow_step_result_payload(
        run, step, compact=bool(claw))
    _workflow_idempotency_store(idem_record, 200, body)
    db.session.commit()
    return jsonify(body)


def _unresolved_execution(run_id):
    return next((row for row in WorkflowRunStep.query.filter_by(run_id=run_id)
                 .populate_existing().with_for_update().all()
                 if (row.blocker_json or {}).get('requires_reconciliation')), None)


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/execution-reconciliation', methods=['POST'])
def resolve_workflow_execution_reconciliation(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if not _can_manage_definition(run.definition):
        return jsonify({'error': '只有 Workflow 管理员可以确认副作用已对账'}), 403
    step = _locked_workflow_step(run_id, step_id)
    data = request.get_json() or {}
    if not (step.blocker_json or {}).get('requires_reconciliation'):
        return jsonify({'error': '当前 Step 没有待对账阻断'}), 409
    if (type(data.get('attempt_no')) is not int or data['attempt_no'] != (step.attempt_no or 1)
            or data.get('execution_stopped') is not True
            or data.get('side_effects_reconciled') is not True
            or not isinstance(data.get('receipt_ref'), str)
            or not 1 <= len(data['receipt_ref'].strip()) <= 2048):
        return jsonify({'error': '需要当前 attempt、停止确认、副作用对账确认及 receipt_ref'}), 400
    if step.claim_expires_at and step.claim_expires_at > datetime.now():
        return jsonify({'error': '当前 claim 仍活跃，不能确认停止'}), 409
    resolution = {key: data[key] for key in (
        'attempt_no', 'execution_stopped', 'side_effects_reconciled', 'receipt_ref')}
    step.blocker_json = dict(step.blocker_json, requires_reconciliation=False,
                            resolution=dict(resolution, resolved_by=_actor_name()))
    db.session.add(AuditLog(action='execution_reconciled', resource_type='workflow_run',
        resource_id=run.id, resource_name=run.run_name, operator=_actor_name(),
        detail=json.dumps({'step_id': step_id, 'attempt_no': step.attempt_no,
                           'receipt_ref': data['receipt_ref']}, ensure_ascii=False)))
    db.session.commit()
    return jsonify({'resolved': True, 'step_id': step_id, 'status': step.status})


@api_bp.route('/workflow-runs/<int:run_id>/steps/<step_id>/retry', methods=['POST'])
def retry_workflow_step(run_id, step_id):
    err = _require_actor()
    if err:
        return err
    run = WorkflowRun.query.get_or_404(run_id)
    if _unresolved_execution(run_id):
        return jsonify({'error': '必须先确认旧执行已停止且副作用已对账',
                        'code': 'EXECUTION_RECONCILIATION_REQUIRED'}), 409
    if run.status == 'cancelled':
        return jsonify({'error': 'Workflow Run 已终止，不能重试步骤'}), 409
    step = WorkflowRunStep.query.filter_by(
        run_id=run_id,
        step_id=step_id,
    ).first_or_404()
    if step.status not in ('blocked', 'failed'):
        return jsonify({
            'error': '只有 blocked/failed Step 可以重试',
            'code': 'WORKFLOW_STEP_NOT_RETRYABLE',
            'status': step.status,
        }), 409
    _expire_workflow_agent_tasks_for_step(
        run_id, step_id, reason='workflow_step_retried')
    _expire_step_blocked_todos(run_id, step_id)
    step.status = 'pending'
    step.summary = ''
    step.metrics_json = {}
    step.evidence_json = {}
    step.logs_json = {}
    step.outputs_json = {}
    step.blocker_json = {}
    step.gate_result_json = {}
    step.contract_result_json = {}
    step.branch_result_json = {}
    step.heartbeat_at = None
    step.heartbeat_by = ''
    step.heartbeat_count = 0
    step.missed_heartbeat_count = 0
    step.health_status = 'idle'
    step.health_checked_at = None
    _reset_step_progress(step)
    _clear_step_claim(step)
    step.dispatched_at = None
    step.started_at = None
    step.finished_at = None
    run.status = 'pending'
    run.finished_at = None
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
    if _unresolved_execution(run_id):
        return jsonify({'error': '必须先确认旧执行已停止且副作用已对账',
                        'code': 'EXECUTION_RECONCILIATION_REQUIRED'}), 409
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
                _expire_workflow_agent_tasks_for_step(
                    run.id, step.step_id, reason='workflow_run_resumed')
                _expire_step_blocked_todos(run.id, step.step_id)
                step.status = 'pending'
                step.summary = ''
                step.metrics_json = {}
                step.evidence_json = {}
                step.logs_json = {}
                step.outputs_json = {}
                step.blocker_json = {}
                step.gate_result_json = {}
                step.contract_result_json = {}
                step.branch_result_json = {}
                step.heartbeat_at = None
                step.heartbeat_by = ''
                step.heartbeat_count = 0
                step.missed_heartbeat_count = 0
                step.health_status = 'idle'
                step.health_checked_at = None
                _reset_step_progress(step)
                _clear_step_claim(step)
                step.dispatched_at = None
                step.started_at = None
                step.finished_at = None
    run.status = 'pending'
    run.finished_at = None
    run.blocker_json = {}
    _recompute_run_status(run, _actor_name())
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))


def _workflow_restart_step_snapshot(step):
    def digest(value):
        encoded = json.dumps(
            value or {}, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()
    return {
        'step_id': step.step_id,
        'status': step.status,
        'attempt_no': int(step.attempt_no or 0),
        'summary': (step.summary or '')[:500],
        'result_code': (
            (step.contract_result_json or {}).get('code')
            if isinstance(step.contract_result_json, dict) else ''),
        'outputs_sha256': digest(step.outputs_json),
        'evidence_sha256': digest(step.evidence_json),
    }


def _workflow_definition_snapshot_fingerprint(definition):
    definition_json = copy.deepcopy(
        definition.definition_json if definition is not None else {})
    encoded = json.dumps(
        definition_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    context = (
        definition_json.get('context')
        if isinstance(definition_json.get('context'), dict) else {})
    return {
        'workflow_definition_id': (
            definition.id if definition is not None else None),
        'version': int(
            (definition.version if definition is not None else 0)
            or definition_json.get('version') or 1),
        'sha256': hashlib.sha256(encoded).hexdigest(),
        'template_revision': str(
            definition_json.get('template_revision')
            or context.get('template_revision') or ''),
    }


def _workflow_restart_snapshot_conflict(run):
    context = run.context_json if isinstance(run.context_json, dict) else {}
    stored = context.get('workflow_definition_snapshot')
    live = _workflow_definition_snapshot_fingerprint(run.definition)
    stale_fields = []
    if not isinstance(stored, dict):
        stale_fields.append('snapshot_missing')
        stored = {}
    try:
        stored_version = int(stored.get('version'))
    except (TypeError, ValueError):
        stored_version = None
    if stored_version != live['version']:
        stale_fields.append('version')
    stored_sha = str(stored.get('sha256') or '').strip().lower()
    if not stored_sha or stored_sha != live['sha256']:
        stale_fields.append('sha256')
    if not stale_fields:
        return None
    return {
        'error': (
            'Run 使用的不可变 Definition 快照已落后于线上 Definition，'
            '不能原地重启；必须基于最新 Definition 创建替代 Run。'),
        'code': 'STALE_DEFINITION_SNAPSHOT',
        'run_id': run.id,
        'workflow_definition_id': run.definition_id,
        'stale_fields': stale_fields,
        'run_snapshot': {
            'workflow_definition_id': stored.get(
                'workflow_definition_id', stored.get('id')),
            'version': stored_version,
            'sha256': stored_sha,
            'template_revision': str(
                stored.get('template_revision') or ''),
        },
        'live_definition': live,
        'replacement_required': True,
        'create_run_api': '/api/v1/workflow-runs',
    }


def _reset_workflow_step_for_full_restart(step, definition_step, actor):
    _expire_workflow_agent_tasks_for_step(
        step.run_id, step.step_id, reason='workflow_run_restarted')
    _expire_step_blocked_todos(step.run_id, step.step_id)
    config = copy.deepcopy(
        step.step_config_json if isinstance(step.step_config_json, dict)
        else {})
    source = definition_step if isinstance(definition_step, dict) else {}
    if 'approval_required' in source:
        config['approval_required'] = bool(source.get('approval_required'))
    config.pop('notification_authorization', None)
    config.pop('webhook', None)
    config.pop('webhook_url', None)
    step.step_config_json = config
    step.status = 'pending'
    step.summary = ''
    step.metrics_json = {}
    step.evidence_json = {}
    step.logs_json = {}
    step.outputs_json = {}
    step.blocker_json = {}
    step.gate_result_json = {}
    step.contract_result_json = {}
    step.branch_result_json = {}
    step.dispatched_at = None
    step.started_at = None
    step.finished_at = None
    step.updated_by = actor
    step.heartbeat_at = None
    step.heartbeat_by = ''
    step.heartbeat_count = 0
    step.missed_heartbeat_count = 0
    step.health_status = 'idle'
    step.health_checked_at = None
    _reset_step_progress(step)
    _clear_step_claim(step)


def _can_restart_workflow_run(run):
    if run.definition and _can_execute_definition(run.definition):
        return True
    claw = get_current_claw()
    mission = (
        (run.context_json or {}).get('mission')
        if isinstance(run.context_json, dict) else None)
    return bool(
        claw and run.trigger_source == 'mission_dispatch'
        and isinstance(mission, dict)
        and int(mission.get('main_claw_id') or 0) == int(claw.id))


@api_bp.route('/workflow-runs/<int:run_id>/restart', methods=['POST'])
def restart_workflow_run(run_id):
    """Restart one blocked/failed Run in place instead of creating a new Run."""
    err = _require_actor()
    if err:
        return err
    key = str(request.headers.get('Idempotency-Key') or '').strip()
    if not key:
        return jsonify({
            'error': '完整重启必须携带 Idempotency-Key',
            'code': 'IDEMPOTENCY_KEY_REQUIRED',
        }), 400
    idem_record, idem_response = _workflow_idempotency_begin()
    if idem_response:
        return idem_response
    run = (WorkflowRun.query.filter_by(id=run_id)
           .with_for_update().first_or_404())
    if not _can_restart_workflow_run(run):
        return jsonify({'error': '无权重启该 Workflow Run'}), 403
    if _unresolved_execution(run_id):
        return jsonify({'error': '必须先确认旧执行已停止且副作用已对账',
                        'code': 'EXECUTION_RECONCILIATION_REQUIRED'}), 409
    if run.status not in ('blocked', 'failed'):
        return jsonify({
            'error': '只有 blocked/failed Run 可以完整重启',
            'code': 'WORKFLOW_RUN_NOT_RESTARTABLE',
            'status': run.status,
        }), 409
    snapshot_conflict = _workflow_restart_snapshot_conflict(run)
    if snapshot_conflict is not None:
        _workflow_idempotency_store(idem_record, 409, snapshot_conflict)
        db.session.add(AuditLog(
            action='restart_rejected', resource_type='workflow_run',
            resource_id=run.id, resource_name=run.run_name,
            operator=_actor_name(), ip_address=request.remote_addr,
            detail=json.dumps(snapshot_conflict, ensure_ascii=False,
                              sort_keys=True),
        ))
        db.session.commit()
        return jsonify(snapshot_conflict), 409
    data = request.get_json(silent=True) or {}
    actor = _actor_name()
    now = datetime.now()
    reason = str(data.get('reason') or '失败修复完成，原 Run 从头重启').strip()[:1000]
    steps = sorted(
        WorkflowRunStep.query.filter_by(run_id=run.id).all(),
        key=lambda row: row.position or 0)
    previous_status = run.status
    snapshot = [_workflow_restart_step_snapshot(step) for step in steps]
    context = copy.deepcopy(run.context_json or {})
    restart_count = int(context.get('restart_count') or 0) + 1
    history = list(context.get('restart_history') or [])[-9:]
    history.append({
        'restart_no': restart_count,
        'restarted_at': now.isoformat(),
        'restarted_by': actor,
        'reason': reason,
        'previous_status': previous_status,
        'step_count': len(snapshot),
        'failed_steps': [
            {
                'step_id': item['step_id'],
                'status': item['status'],
                'result_code': item['result_code'],
            }
            for item in snapshot
            if item['status'] in ('blocked', 'failed')
        ],
    })
    context['restart_count'] = restart_count
    context['restart_history'] = history
    run.context_json = context

    definition_steps = {
        str(item.get('id')): item
        for item in ((run.definition.definition_json or {}).get('steps') or [])
        if isinstance(item, dict) and item.get('id')
    } if run.definition else {}
    for step in steps:
        _reset_workflow_step_for_full_restart(
            step, definition_steps.get(step.step_id), actor)

    for approval in WorkflowApproval.query.filter_by(run_id=run.id).filter(
            WorkflowApproval.status.in_(('pending', 'approved'))).all():
        approval.status = 'superseded'
        approval.comment = (
            (approval.comment or '') +
            '\n[完整重启 #%d] 原审批已失效' % restart_count).strip()

    for artifact in WorkflowArtifact.query.filter_by(run_id=run.id).all():
        metadata = copy.deepcopy(artifact.metadata_json or {})
        archives = list(metadata.get('restart_archives') or [])[-9:]
        archives.append({
            'restart_no': restart_count,
            'archived_at': now.isoformat(),
            'reason': reason,
        })
        metadata['restart_archives'] = archives
        metadata['archived_by_restart'] = True
        artifact.metadata_json = metadata

    manifest = WorkflowEvidenceManifest.query.filter_by(
        workflow_run_id=run.id).first()
    if manifest:
        manifest.analysis_run_id = None
        manifest.coverage_json = {}
        manifest.artifacts_json = []
        manifest.completeness_status = 'incomplete'
        manifest.missing_required_json = list(
            manifest.required_evidence_json or [])
        manifest.classification = None
        manifest.analysis_summary_json = {}
        manifest.finding_ids_json = []
        manifest.analyzed_by = ''
        manifest.analyzed_at = None
        manifest.revision = int(manifest.revision or 1) + 1
        manifest.updated_by = actor

    run.status = 'pending'
    run.current_step_id = ''
    run.summary = ''
    run.blocker_json = {}
    run.business_conclusion = ''
    run.automation_conclusion = ''
    run.evidence_ingest_status = 'EVIDENCE_INGEST_INCOMPLETE'
    run.outcomes_json = {}
    run.started_at = None
    run.finished_at = None
    db.session.add(AuditLog(
        action='restart', resource_type='workflow_run',
        resource_id=run.id, resource_name=run.run_name,
        operator=actor, ip_address=request.remote_addr,
        detail=json.dumps({
            'restart_no': restart_count,
            'previous_status': previous_status,
            'reason': reason,
            'same_run_id': True,
            'step_count': len(steps),
            'steps': snapshot,
        }, ensure_ascii=False, sort_keys=True),
    ))
    _recompute_run_status(run, actor)
    payload = run.to_dict(with_steps=True)
    payload['restart'] = {
        'restart_no': restart_count,
        'previous_status': previous_status,
        'same_run_id': True,
        'reason': reason,
    }
    _workflow_idempotency_store(idem_record, 200, payload)
    db.session.commit()
    return jsonify(payload)


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
            _clear_step_claim(step)
    for approval in WorkflowApproval.query.filter_by(run_id=run.id, status='pending').all():
        approval.status = 'skipped'
        approval.approver = actor
        approval.comment = approval.comment or 'Run 已终止，审批自动关闭'
        approval.approved_at = approval.approved_at or now
    db.session.commit()
    return jsonify(run.to_dict(with_steps=True))

