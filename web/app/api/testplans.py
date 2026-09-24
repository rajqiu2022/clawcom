# -*- coding: utf-8 -*-
"""
测试计划排期 & 测试任务管理 API
支持 OpenClaw Token 认证操作
"""
import json
import re
from datetime import datetime, date
from flask import request, jsonify, session
from sqlalchemy import func
from app import db
from app.models import (TestPlan, TestPlanReport, TestTask, TestTaskCase, TestTaskOccurrence, TestIteration,
                        TestIterationTab,
                        TestTaskChain, TestTaskChainStep,
                        TestTaskReport, TestTaskBugReport,
                        Project, OpenClawInstance, TestCaseLibrary, TestCase, User,
                        ClawMessage, ClawTodo, WorkflowDefinition)
from app.api import api_bp
from app.services.iteration_tabs import (
    append_chart,
    append_row,
    can_edit_tab_cell,
    normalize_tab_payload,
    normalize_flow_steps_payload,
    should_preserve_existing_rows,
    update_cell_value,
)
from app.services.tapd_bug_case_link import (
    build_tapd_bug_url,
    build_case_info_snapshot,
    parse_tapd_bug_url,
    sync_task_case_bug_link,
)
from app.services.test_task_case_sync import (
    apply_sync as apply_task_case_sync,
    build_sync_plan as build_task_case_sync_plan,
    case_snapshot as build_task_case_source_snapshot,
    enrich_case_filter,
    restore_backup as restore_task_case_backup,
    select_library_cases,
)
from app.services.test_task_references import (
    normalize_task_references,
    task_reference_options,
)


def _get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）"""
    from app.api.skills import _get_current_user as _orig
    return _orig()


def _actor_name(user):
    if not user:
        return 'system'
    return (getattr(user, '_claw_name', None)
            or getattr(user, 'username', None)
            or getattr(user, 'name', None)
            or 'system')


def _caller_project_ids(user):
    from app.api.auth_utils import user_project_ids
    ids = set(user_project_ids(user))
    claw_id = getattr(user, '_claw_id', None)
    if claw_id:
        claw = OpenClawInstance.query.get(claw_id)
        if claw and claw.project_id:
            ids.add(int(claw.project_id))
    return ids


def _can_edit_iteration_tabs(user, iteration):
    """项目内成员可共建迭代页签；全局管理员兜底全权。"""
    if not user:
        return False
    role = getattr(user, 'role', '')
    if role == 'super_admin':
        return True
    project_ids = _caller_project_ids(user)
    if role == 'admin' and not project_ids:
        return True
    if iteration.project_id:
        return int(iteration.project_id) in project_ids
    return role == 'admin'


def _can_edit_plan(user, plan):
    """检查用户是否有权编辑测试计划"""
    if not user:
        return False
    if plan.team_id:
        from app.services.agent_team_plans import access
        from app.services.agent_teams import load_team
        return access(load_team(plan.team_id))[1]
    if user.role in ('super_admin', 'admin'):
        return True
    return plan.created_by == user.username


def _can_edit_task_conclusion(user, plan, task):
    """A plan editor or the assigned executor may maintain the one-line truth."""
    from app.api.auth_utils import get_current_claw
    claw = get_current_claw()
    if claw and task.assignee_claw_id == claw.id:
        return True
    if _can_edit_plan(user, plan):
        return True
    claw_id = getattr(user, '_claw_id', None) if user else None
    if claw_id and task.assignee_claw_id == claw_id:
        return True
    username = getattr(user, 'username', '') if user else ''
    return bool(username and task.assignee_username == username)


def _resolve_assignee_claw_id(data):
    """支持按 Claw ID 或 Claw 对应用户指派测试任务。
    返回 claw_id 或 None（直接分配给人时由 assignee_username 处理）。
    """
    # 前端传 claw:ID 格式时，值已经是整数
    claw_id = data.get('assignee_claw_id')
    if claw_id is not None and claw_id != '' and claw_id != 0:
        return int(claw_id)

    # 如果明确指定了 assignee_username，说明直接分给人，不查 claw
    if data.get('assignee_username'):
        return None

    owner = (data.get('assignee_owner') or '').strip()
    if not owner:
        return None

    user = (User.query.filter_by(username=owner).first()
            or User.query.filter_by(display_name=owner).first())
    if user and user.bound_claw_id:
        return user.bound_claw_id

    claw = (OpenClawInstance.query
            .filter(OpenClawInstance.status != 'deleted',
                    OpenClawInstance.owner == owner)
            .order_by(OpenClawInstance.id.asc())
            .first())
    return claw.id if claw else None


def _resolve_assignee_username(data):
    """解析直接分配给真人的用户名。"""
    if data.get('assignee_username'):
        return data['assignee_username'].strip()
    # 如果没有指定 claw 也没有指定 username，但有 assignee_owner，
    # 且找不到对应 claw，则视为直接分配给人
    if not data.get('assignee_claw_id'):
        owner = (data.get('assignee_owner') or '').strip()
        if owner:
            user = (User.query.filter_by(username=owner).first()
                    or User.query.filter_by(display_name=owner).first())
            if user and not user.bound_claw_id:
                claw = (OpenClawInstance.query
                        .filter(OpenClawInstance.status != 'deleted',
                                OpenClawInstance.owner == owner)
                        .first())
                if not claw:
                    return user.username
    return ''


def _create_test_task_notification(task, plan, action='assigned', message=''):
    """Create a one-time notification todo for the agent when test task status changes.

    This is the "event notification layer" of Plan B:
    - Each status change creates a once/flexible todo as a notification
    - Agent processes (acknowledges) it and marks complete
    - Long-term visibility is handled by the test-tasks API, not todos
    """
    if not task.assignee_claw_id:
        return

    plan_name = plan.name if plan else '未知计划'
    title = f'[测试任务] {task.name} - {action}'

    reference_count = sum((
        len(task.reference_skill_ids_json or []),
        len(task.reference_knowledge_ids_json or []),
        len(task.reference_report_ids_json or []),
    ))
    reference_note = (
        f'\n📎 执行参考：{reference_count} 项，开始前必须从任务详情按需读取'
        if reference_count else '')
    desc = (f'{message}\n\n'
            f'📋 所属计划：{plan_name}\n'
            f'📌 任务优先级：{task.priority}\n'
            f'📅 排期：{task.start_date or "未设置"} ~ {task.end_date or "未设置"}'
            f'{reference_note}\n'
            f'🔗 查看详情：GET /api/v1/test-plans/{task.plan_id}/tasks/{task.id}')

    todo = ClawTodo(
        openclaw_id=task.assignee_claw_id,
        title=title,
        description=desc,
        schedule_type='once',
        urgency_level='flexible',
        priority=task.priority or 'P1',
        task_category='test_task',
        ref_task_id=task.id,
        enabled=True,
        created_by='system:testplan',
    )
    db.session.add(todo)


# ==================== 测试迭代 CRUD ====================

@api_bp.before_request
def authorize_team_plan_request():
    """Apply project/manager boundaries to newly team-owned plans only."""
    plan_id = (request.view_args or {}).get('plan_id')
    if not plan_id or not request.path.startswith('/api/v1/test-plans/'):
        return
    plan = db.session.get(TestPlan, plan_id)
    if not plan or not plan.team_id:
        return
    from app.services.agent_team_plans import access
    from app.services.agent_teams import load_team
    actor, manager = access(load_team(plan.team_id))
    root = '/api/v1/test-plans/%s' % plan_id
    if (request.path == root and request.method in ('PUT', 'DELETE')) or (
            request.path == root + '/tasks' and request.method == 'POST'):
        access(load_team(plan.team_id), write=True)


@api_bp.route('/test-iterations', methods=['GET'])
def list_test_iterations():
    """获取测试迭代列表

    查询参数：
    - project_id: 按项目筛选
    - status: 按状态筛选
    - search: 搜索名称
    """
    query = TestIteration.query

    project_id = request.args.get('project_id', type=int)
    status = request.args.get('status')
    search = request.args.get('search')

    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    if status:
        query = query.filter_by(status=status)
    if search:
        query = query.filter(TestIteration.name.ilike(f'%{search}%'))

    iterations = query.order_by(TestIteration.created_at.desc()).all()
    return jsonify([it.to_dict() for it in iterations])


@api_bp.route('/test-iterations', methods=['POST'])
def create_test_iteration():
    """创建测试迭代"""
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    from datetime import date as date_type
    start_date = None
    end_date = None
    if data.get('start_date'):
        try:
            start_date = date_type.fromisoformat(data['start_date'])
        except (ValueError, TypeError):
            pass
    if data.get('end_date'):
        try:
            end_date = date_type.fromisoformat(data['end_date'])
        except (ValueError, TypeError):
            pass

    user = _get_current_user()
    created_by = ''
    if user:
        created_by = getattr(user, 'username', '') or getattr(user, 'name', '')

    iteration = TestIteration(
        name=data['name'],
        description=data.get('description', ''),
        version_name=data.get('version_name', ''),
        version_type=data.get('version_type', 'regular'),
        start_date=start_date,
        end_date=end_date,
        project_id=data.get('project_id'),
        tapd_iteration_ids=data.get('tapd_iteration_ids', []),
        tapd_iteration_names=data.get('tapd_iteration_names', []),
        tapd_workspace_id=data.get('tapd_workspace_id'),
        status=data.get('status', 'draft'),
        created_by=created_by,
    )
    db.session.add(iteration)
    db.session.commit()
    return jsonify(iteration.to_dict()), 201


@api_bp.route('/test-iterations/<int:iteration_id>', methods=['GET'])
def get_test_iteration(iteration_id):
    """获取测试迭代详情（含计划和任务）"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    return jsonify(iteration.to_dict(with_plans=True))


@api_bp.route('/test-iterations/<int:iteration_id>', methods=['PUT'])
def update_test_iteration(iteration_id):
    """更新测试迭代"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    data = request.get_json()

    updatable_fields = ['name', 'description', 'version_name', 'version_type',
                        'project_id', 'tapd_iteration_ids', 'tapd_iteration_names',
                        'tapd_workspace_id', 'status']
    for field in updatable_fields:
        if field in data:
            setattr(iteration, field, data[field])

    # 日期字段单独处理（支持传 null 清除）
    from datetime import date as date_type
    for date_field in ('start_date', 'end_date'):
        if date_field in data:
            try:
                setattr(iteration, date_field, date_type.fromisoformat(data[date_field]) if data[date_field] else None)
            except (ValueError, TypeError):
                pass

    _recalc_iteration_stats(iteration)
    db.session.commit()
    return jsonify(iteration.to_dict())


@api_bp.route('/test-iterations/<int:iteration_id>', methods=['DELETE'])
def delete_test_iteration(iteration_id):
    """删除测试迭代（级联删除下属计划和任务）"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    db.session.delete(iteration)
    db.session.commit()
    return jsonify({'message': f'测试迭代 "{iteration.name}" 已删除'})


@api_bp.route('/test-iterations/<int:iteration_id>/tabs', methods=['GET'])
def list_iteration_tabs(iteration_id):
    """获取迭代动态页签。"""
    TestIteration.query.get_or_404(iteration_id)
    tabs = (TestIterationTab.query
            .filter_by(iteration_id=iteration_id)
            .order_by(TestIterationTab.created_at.asc())
            .all())
    return jsonify([tab.to_dict() for tab in tabs])


@api_bp.route('/test-iterations/<int:iteration_id>/tabs', methods=['POST'])
def upsert_iteration_tab(iteration_id):
    """创建或整体更新迭代动态页签。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    if not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '仅项目内成员可修改迭代页签'}), 403
    data = request.get_json() or {}
    payload = normalize_tab_payload(data)
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=payload['tab_key'],
    ).first()
    actor = _actor_name(user)
    if tab:
        status_code = 200
    else:
        tab = TestIterationTab(
            iteration_id=iteration_id,
            tab_key=payload['tab_key'],
            created_by=actor,
        )
        db.session.add(tab)
        status_code = 201
    if should_preserve_existing_rows(tab.rows_json or [], payload['rows'], data):
        payload['rows'] = tab.rows_json or []
    tab.title = payload['title']
    tab.tab_type = payload['tab_type']
    tab.view_mode = payload['view_mode']
    tab.columns_json = payload['columns']
    tab.rows_json = payload['rows']
    tab.charts_json = payload['charts']
    tab.updated_by = actor
    db.session.commit()
    return jsonify(tab.to_dict()), status_code


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>', methods=['GET'])
def get_iteration_tab(iteration_id, tab_key):
    """获取单个迭代动态页签。"""
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first_or_404()
    return jsonify(tab.to_dict())


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>/cells', methods=['PATCH'])
def patch_iteration_tab_cell(iteration_id, tab_key):
    """更新指定行/列单元格值。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    if not can_edit_tab_cell(user) or not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '无权修改单元格'}), 403
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first_or_404()
    data = request.get_json() or {}
    row_id = data.get('row_id')
    column_key = data.get('column_key')
    if not row_id or not column_key:
        return jsonify({'error': 'row_id 和 column_key 为必填项'}), 400
    try:
        tab.rows = update_cell_value(
            tab.rows or [],
            row_id,
            column_key,
            data.get('value'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    tab.updated_by = _actor_name(user)
    db.session.commit()
    return jsonify(tab.to_dict())


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>/rows', methods=['POST'])
def append_iteration_tab_row(iteration_id, tab_key):
    """追加一行数据。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    if not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '仅项目内成员可追加行'}), 403
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first_or_404()
    data = request.get_json() or {}
    row = data.get('row') if isinstance(data.get('row'), dict) else data
    try:
        tab.rows = append_row(tab.rows or [], row)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    tab.updated_by = _actor_name(user)
    db.session.commit()
    return jsonify(tab.to_dict()), 201


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>/charts', methods=['POST'])
def append_iteration_tab_chart(iteration_id, tab_key):
    """追加一个图表配置。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    if not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '仅项目内成员可追加图表'}), 403
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first_or_404()
    data = request.get_json() or {}
    chart = data.get('chart') if isinstance(data.get('chart'), dict) else data
    try:
        tab.charts = append_chart(tab.charts or [], chart)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if tab.view_mode == 'table':
        tab.view_mode = 'table_chart'
    tab.updated_by = _actor_name(user)
    db.session.commit()
    return jsonify(tab.to_dict()), 201


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>', methods=['DELETE'])
def delete_iteration_tab(iteration_id, tab_key):
    """删除迭代动态页签。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first_or_404()
    if not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '仅项目内成员可删除页签'}), 403
    db.session.delete(tab)
    db.session.commit()
    return jsonify({'message': f'页签 "{tab.title}" 已删除'})


@api_bp.route('/test-iterations/<int:iteration_id>/tabs/<tab_key>/flow', methods=['POST'])
def upsert_flow_progress(iteration_id, tab_key):
    """Agent 上报流程进度：按步骤合并更新，页签不存在则自动创建。"""
    iteration = TestIteration.query.get_or_404(iteration_id)
    user = _get_current_user()
    if not user:
        return jsonify({'error': '未认证'}), 401
    if not _can_edit_iteration_tabs(user, iteration):
        return jsonify({'error': '仅项目内成员可上报流程进度'}), 403
    data = request.get_json() or {}
    actor = _actor_name(user)

    tab = TestIterationTab.query.filter_by(
        iteration_id=iteration_id,
        tab_key=tab_key,
    ).first()
    if tab:
        status_code = 200
    else:
        tab = TestIterationTab(
            iteration_id=iteration_id,
            tab_key=tab_key,
            created_by=actor,
        )
        db.session.add(tab)
        status_code = 201

    tab.title = str(data.get('title') or tab.title or '流程进度')
    tab.tab_type = 'custom'
    tab.view_mode = 'flow_progress'
    tab.rows = normalize_flow_steps_payload(data, tab.rows, actor)
    tab.updated_by = actor
    db.session.commit()
    return jsonify(tab.to_dict()), status_code


def _recalc_iteration_stats(iteration):
    """重新计算迭代的统计数字"""
    plans = TestPlan.query.filter_by(iteration_id=iteration.id).all()
    iteration.total_plans = len(plans)
    total_tasks = 0
    completed_tasks = 0
    total_bugs = 0
    for p in plans:
        total_tasks += p.total_tasks or 0
        completed_tasks += p.completed_tasks or 0
        total_bugs += p.total_bugs or 0
    iteration.total_tasks = total_tasks
    iteration.completed_tasks = completed_tasks
    iteration.total_bugs = total_bugs


# ==================== 测试计划 CRUD ====================

@api_bp.route('/test-plans', methods=['GET'])
def list_test_plans():
    """获取测试计划列表

    查询参数：
    - project_id: 按项目筛选
    - status: 按状态筛选
    - version_type: 按版本类型筛选
    - search: 搜索名称
    """
    query = TestPlan.query
    if request.args.get('team_id') is not None:
        from app.services.agent_teams import load_team
        from app.services.agent_team_plans import access
        team = load_team(request.args.get('team_id', type=int))
        access(team)
        query = query.filter_by(team_id=team.id)

    project_id = request.args.get('project_id', type=int)
    status = request.args.get('status')
    version_type = request.args.get('version_type')
    search = request.args.get('search')

    if project_id is not None:
        query = query.filter_by(project_id=project_id)
    if status:
        query = query.filter_by(status=status)
    if version_type:
        query = query.filter_by(version_type=version_type)
    if search:
        query = query.filter(TestPlan.name.ilike(f'%{search}%'))

    plans = query.order_by(TestPlan.start_date.desc()).all()

    # 非管理员只看自己项目的
    user = _get_current_user()
    if user and user.role not in ('super_admin', 'admin'):
        user_project_ids = set()
        if user.managed_projects:
            user_project_ids.update(user.managed_projects)
        if user.bound_claw_id:
            claw = OpenClawInstance.query.get(user.bound_claw_id)
            if claw and claw.project_id:
                user_project_ids.add(claw.project_id)
        if user_project_ids:
            plans = [p for p in plans if p.project_id in user_project_ids]
        else:
            plans = []

    return jsonify([p.to_dict() for p in plans])


@api_bp.route('/test-plans', methods=['POST'])
def create_test_plan(team_id=None):
    """创建测试计划"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求须为 JSON 对象'}), 400
    data = dict(data)
    if team_id is not None:
        if data.get('team_id', team_id) != team_id:
            return jsonify({'error': 'team_id 与 URL 不一致'}), 400
        data['team_id'] = team_id
    from app.services import agent_team_plans as team_plans
    team = team_plans.bind(data)
    if team:
        team_plans.validate_dates(data)
    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400
    if not data.get('start_date') or not data.get('end_date'):
        return jsonify({'error': 'start_date 和 end_date 为必填项'}), 400

    from datetime import date as date_type
    try:
        start_date = date_type.fromisoformat(data['start_date'])
        end_date = date_type.fromisoformat(data['end_date'])
    except (ValueError, TypeError):
        return jsonify({'error': '日期格式无效，请使用 YYYY-MM-DD'}), 400

    if end_date < start_date:
        return jsonify({'error': '结束日期不能早于开始日期'}), 400

    user = _get_current_user()
    created_by = ''
    if user:
        created_by = getattr(user, 'username', '') or getattr(user, 'name', '')

    plan = TestPlan(
        team_id=team.id if team else None,
        name=data['name'],
        description=data.get('description', ''),
        iteration_id=data.get('iteration_id'),
        version_type=data.get('version_type', 'regular'),
        version_name=data.get('version_name', ''),
        start_date=start_date,
        end_date=end_date,
        project_id=data.get('project_id'),
        tapd_iteration_ids=data.get('tapd_iteration_ids', []),
        tapd_iteration_names=data.get('tapd_iteration_names', []),
        tapd_workspace_id=data.get('tapd_workspace_id'),
        status=data.get('status', 'draft'),
        created_by=created_by,
    )
    db.session.add(plan)
    db.session.flush()
    wake_target = None
    if team and plan.status == 'active':
        from app.services import plan_supervision as plan_guard
        if plan_guard.team_enabled(team.id):
            _, result = plan_guard.bootstrap(
                plan, team.id, team.primary_manager_claw_id, {
                    'command_key': 'auto-active-plan:%s' % plan.id,
                    'team_id': team.id,
                    'orchestrator_claw_id': team.primary_manager_claw_id,
                })
            wake_target = result.pop('wake_claw_id', None)
    db.session.commit()
    if wake_target:
        plan_guard.wake(wake_target)

    # 更新迭代统计
    if plan.iteration_id:
        iteration = TestIteration.query.get(plan.iteration_id)
        if iteration:
            _recalc_iteration_stats(iteration)
            db.session.commit()

    return jsonify(plan.to_dict()), 201


@api_bp.route('/test-plans/<int:plan_id>', methods=['GET'])
def get_test_plan(plan_id):
    """获取测试计划详情（含任务列表）"""
    plan = TestPlan.query.get_or_404(plan_id)
    return jsonify(plan.to_dict(with_tasks=True))


@api_bp.route('/test-plans/<int:plan_id>', methods=['PUT'])
def update_test_plan(plan_id):
    """更新测试计划"""
    from app.services import plan_supervision as plan_guard
    from app.models_plan_supervision import PlanSupervisor
    if plan_guard.enabled() and db.session.get(PlanSupervisor, plan_id):
        from app.api.plan_supervision import load
        _, supervisor, caller_claw = load(plan_id, write=True)
        if caller_claw:
            plan_guard.require_lease(supervisor, caller_claw.id,
                (request.get_json(silent=True) or {}).get('plan_supervision') or {})
    plan = TestPlan.query.get_or_404(plan_id)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '请求须为 JSON 对象'}), 400
    data = dict(data)
    plan = TestPlan.query.filter_by(id=plan_id).populate_existing().with_for_update().first_or_404()
    from app.services import agent_team_plans as team_plans
    team = team_plans.bind(data, plan)
    if team:
        team_plans.validate_dates(data, plan)
        plan.team_id = team.id

    old_iteration_id = plan.iteration_id
    updatable_fields = ['name', 'description', 'version_type', 'version_name',
                        'start_date', 'end_date', 'project_id', 'iteration_id',
                        'tapd_iteration_ids', 'tapd_iteration_names', 'tapd_workspace_id', 'status']
    for field in updatable_fields:
        if field in data:
            if field in ('start_date', 'end_date') and data[field]:
                from datetime import date as date_type
                try:
                    setattr(plan, field, date_type.fromisoformat(data[field]))
                except (ValueError, TypeError):
                    pass
            else:
                setattr(plan, field, data[field])

    _recalc_plan_stats(plan)

    # 联动更新迭代统计（新旧迭代都要更新）
    new_iteration_id = plan.iteration_id
    iter_ids_to_update = set()
    if old_iteration_id:
        iter_ids_to_update.add(old_iteration_id)
    if new_iteration_id:
        iter_ids_to_update.add(new_iteration_id)
    for iid in iter_ids_to_update:
        it = TestIteration.query.get(iid)
        if it:
            _recalc_iteration_stats(it)

    wake_target = None
    if (team and plan.status == 'active'
            and plan_guard.team_enabled(team.id)
            and not db.session.get(PlanSupervisor, plan.id)):
        _, result = plan_guard.bootstrap(
            plan, team.id, team.primary_manager_claw_id, {
                'command_key': 'auto-active-plan:%s' % plan.id,
                'team_id': team.id,
                'orchestrator_claw_id': team.primary_manager_claw_id,
            })
        wake_target = result.pop('wake_claw_id', None)
    db.session.commit()
    if wake_target:
        plan_guard.wake(wake_target)
    return jsonify(plan.to_dict())


@api_bp.route('/test-plans/<int:plan_id>', methods=['DELETE'])
def delete_test_plan(plan_id):
    """删除测试计划"""
    plan = TestPlan.query.get_or_404(plan_id)
    if plan.team_id:
        from app.services.agent_team_plans import access
        from app.services.agent_teams import load_team
        access(load_team(plan.team_id), write=True)
    iteration_id = plan.iteration_id
    db.session.delete(plan)

    # 联动更新迭代统计
    if iteration_id:
        iteration = TestIteration.query.get(iteration_id)
        if iteration:
            _recalc_iteration_stats(iteration)

    db.session.commit()
    return jsonify({'message': f'测试计划 "{plan.name}" 已删除'})


@api_bp.route('/test-plans/<int:plan_id>/stats', methods=['GET'])
def get_test_plan_stats(plan_id):
    """获取测试计划统计信息"""
    plan = TestPlan.query.get_or_404(plan_id)
    _recalc_plan_stats(plan)
    db.session.commit()

    # 按任务类型统计
    type_stats = db.session.query(
        TestTask.task_type,
        func.count(TestTask.id),
        func.sum(db.case([(TestTask.status == 'completed', 1)], else_=0))
    ).filter_by(plan_id=plan_id).group_by(TestTask.task_type).all()

    # 按状态统计
    status_stats = db.session.query(
        TestTask.status,
        func.count(TestTask.id)
    ).filter_by(plan_id=plan_id).group_by(TestTask.status).all()

    # Bug 统计
    bug_total = db.session.query(func.sum(TestTask.bug_count)).filter_by(plan_id=plan_id).scalar() or 0

    return jsonify({
        'id': plan.id,
        'name': plan.name,
        'total_tasks': plan.total_tasks,
        'completed_tasks': plan.completed_tasks,
        'progress': plan.to_dict()['progress'],
        'total_bugs': int(bug_total),
        'by_type': {row[0]: {'total': row[1], 'completed': int(row[2] or 0)} for row in type_stats},
        'by_status': {row[0]: row[1] for row in status_stats},
    })


def _extract_report_title(content, plan_name=''):
    """从报告内容提取标题（优先 markdown 一级标题）"""
    text = (content or '').strip()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('# '):
            return line[2:][:200]
    if text:
        return text[:40]
    return f'{plan_name} 测试报告'.strip()


@api_bp.route('/test-plans/<int:plan_id>/report', methods=['GET'])
def get_test_plan_report(plan_id):
    """获取测试计划报告（兼容旧接口，优先返回最新一份）"""
    plan = TestPlan.query.get_or_404(plan_id)
    latest = TestPlanReport.query.filter_by(plan_id=plan_id).order_by(
        TestPlanReport.created_at.desc()
    ).first()
    if latest:
        d = latest.to_dict()
        return jsonify({
            'plan_id': plan.id,
            'plan_name': plan.name,
            'content': d.get('content', ''),
            'format': d.get('format', 'markdown'),
            'updated_by': d.get('created_by', ''),
            'updated_at': d.get('created_at'),
            'has_report': True,
        })
    return jsonify({
        'plan_id': plan.id,
        'plan_name': plan.name,
        'content': plan.report_content or '',
        'format': plan.report_format or 'markdown',
        'updated_by': plan.report_updated_by or '',
        'updated_at': str(plan.report_updated_at) if plan.report_updated_at else None,
        'has_report': bool(plan.report_content),
    })


@api_bp.route('/test-plans/<int:plan_id>/report', methods=['POST', 'PUT'])
def save_test_plan_report(plan_id):
    """保存测试计划报告。报告由 Agent/API 写入，Web 页面只查看。"""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权更新该测试计划报告'}), 403

    data = request.get_json()
    if not data or not isinstance(data.get('content'), str):
        return jsonify({'error': 'content 为必填字符串'}), 400

    report_format = (data.get('format') or 'markdown').lower()
    if report_format not in ('markdown', 'html'):
        return jsonify({'error': 'format 仅支持 markdown/html'}), 400

    author = (
        getattr(user, 'username', '') or
        getattr(user, 'name', '') or
        getattr(user, '_claw_name', '') or
        'agent'
    )

    plan.report_content = data['content']
    plan.report_format = report_format
    plan.report_updated_at = datetime.now()
    plan.report_updated_by = author

    # 同步写入多份报告表（保留历史）
    report = TestPlanReport(
        plan_id=plan.id,
        title=(data.get('title') or _extract_report_title(data['content'], plan.name)),
        content=data['content'],
        format=report_format,
        created_by=author,
    )
    db.session.add(report)
    db.session.commit()
    return jsonify({
        'plan_id': plan.id,
        'plan_name': plan.name,
        'format': plan.report_format,
        'updated_by': plan.report_updated_by,
        'updated_at': str(plan.report_updated_at) if plan.report_updated_at else None,
        'has_report': bool(plan.report_content),
    })


# ==================== 测试计划报告（多份）====================

@api_bp.route('/test-plans/<int:plan_id>/reports', methods=['GET'])
def list_test_plan_reports(plan_id):
    """获取测试计划下的报告列表（兼容旧 report_content）"""
    plan = TestPlan.query.get_or_404(plan_id)
    reports = TestPlanReport.query.filter_by(plan_id=plan_id).order_by(
        TestPlanReport.created_at.desc()
    ).all()
    
    # 兼容旧数据：如果 TestPlanReport 表为空但有 report_content，自动迁移
    if not reports and plan.report_content and plan.report_content.strip():
        # 尝试从内容中提取标题（取第一行非空内容）
        text = re.sub(r'#+\s+', '', plan.report_content)[:200]
        text = text.strip().split('\n')[0] if text.strip() else ''
        if not text or len(text) > 40:
            text = f'{plan.name} 测试报告'
        title = text[:40]
        
        migrated = TestPlanReport(
            plan_id=plan.id,
            title=title,
            content=plan.report_content,
            format=plan.report_format or 'markdown',
            created_by=plan.report_updated_by or 'unknown',
            created_at=plan.report_updated_at or datetime.now(),
            updated_at=plan.report_updated_at or datetime.now(),
        )
        db.session.add(migrated)
        db.session.commit()
        reports = [migrated]
    
    return jsonify({
        'items': [r.to_dict() for r in reports],
        'total': len(reports),
    })


@api_bp.route('/test-plans/<int:plan_id>/reports', methods=['POST'])
def create_test_plan_report(plan_id):
    """创建测试计划报告"""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作该测试计划'}), 403

    data = request.get_json()
    if not data or not data.get('title') or not isinstance(data.get('content'), str):
        return jsonify({'error': 'title 和 content 为必填项'}), 400

    report_format = (data.get('format') or 'markdown').lower()
    if report_format not in ('markdown', 'html'):
        return jsonify({'error': 'format 仅支持 markdown/html'}), 400

    user_name = ''
    if user:
        user_name = getattr(user, 'username', '') or getattr(user, 'name', '') or ''

    report = TestPlanReport(
        plan_id=plan_id,
        title=data['title'],
        content=data['content'],
        format=report_format,
        created_by=user_name,
    )
    db.session.add(report)
    db.session.commit()
    return jsonify(report.to_dict()), 201


@api_bp.route('/test-plans/<int:plan_id>/reports/<int:report_id>', methods=['GET'])
def get_test_plan_report_detail(plan_id, report_id):
    """获取单份报告详情"""
    report = TestPlanReport.query.filter_by(id=report_id, plan_id=plan_id).first_or_404()
    return jsonify(report.to_dict())


@api_bp.route('/test-plans/<int:plan_id>/reports/<int:report_id>', methods=['DELETE'])
def delete_test_plan_report(plan_id, report_id):
    """删除测试计划报告"""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作该测试计划'}), 403

    report = TestPlanReport.query.filter_by(id=report_id, plan_id=plan_id).first()
    if not report:
        return jsonify({'error': '报告不存在'}), 404
    db.session.delete(report)
    db.session.commit()
    return jsonify({'message': '已删除'})


# ==================== 测试任务报告 ====================

@api_bp.route(
    '/test-plans/<int:plan_id>/tasks/<int:task_id>/conclusion',
    methods=['GET', 'PUT'])
def task_conclusion(plan_id, task_id):
    """Read or replace the task's single lightweight conclusion.

    This updates TestTask.result_summary only.  It intentionally does not
    create TestTaskReport/TestReport rows; callers needing multiple immutable
    documents must use the report APIs instead.
    """
    plan = TestPlan.query.get_or_404(plan_id)
    task = TestTask.query.filter_by(
        plan_id=plan_id, id=task_id).first_or_404()
    user = _get_current_user()
    can_edit = _can_edit_task_conclusion(user, plan, task)
    if request.method == 'PUT':
        if not can_edit:
            return jsonify({'error': '仅计划管理者或任务执行人可更新任务结论'}), 403
        data = request.get_json(silent=True)
        content = data.get('content') if isinstance(data, dict) else None
        if not isinstance(content, str):
            return jsonify({'error': 'content 必须为字符串'}), 400
        if len(content) > 20000:
            return jsonify({'error': '任务结论不能超过 20000 字符'}), 400
        task.result_summary = content
        db.session.commit()
    return jsonify({
        'task_id': task.id,
        'content': task.result_summary or '',
        'updated_at': str(task.updated_at) if task.updated_at else None,
        'can_edit': can_edit,
        'report_created': False,
    })

@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/reports', methods=['GET'])
def list_task_reports(plan_id, task_id):
    """获取测试任务的报告列表"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    query = TestTaskReport.query.filter_by(task_id=task_id)
    occurrence_id = request.args.get('occurrence_id', type=int)
    if occurrence_id:
        query = query.filter_by(occurrence_id=occurrence_id)
    reports = query.order_by(
        TestTaskReport.created_at.desc()
    ).all()
    return jsonify({'items': [r.to_dict() for r in reports], 'total': len(reports)})


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/reports', methods=['POST'])
def create_task_report(plan_id, task_id):
    """创建测试任务报告"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作该测试任务'}), 403

    data = request.get_json()
    if not data or not data.get('title') or not isinstance(data.get('content'), str):
        return jsonify({'error': 'title 和 content 为必填项'}), 400

    report_format = (data.get('format') or 'markdown').lower()
    if report_format not in ('markdown', 'html'):
        return jsonify({'error': 'format 仅支持 markdown/html'}), 400

    user_name = ''
    if user:
        user_name = getattr(user, 'username', '') or getattr(user, 'name', '') or getattr(user, '_claw_name', '') or ''

    occurrence_id = data.get('occurrence_id')
    occurrence = None
    if occurrence_id is not None:
        if type(occurrence_id) is not int:
            return jsonify({'error': 'occurrence_id 必须为整数'}), 400
        occurrence = TestTaskOccurrence.query.filter_by(
            id=occurrence_id, plan_id=plan_id,
            test_task_id=task_id).first()
        if not occurrence:
            return jsonify({'error': '执行实例不存在或不属于该任务'}), 400

    report = TestTaskReport(
        task_id=task_id,
        occurrence_id=occurrence.id if occurrence else None,
        title=data['title'],
        content=data['content'],
        format=report_format,
        created_by=user_name,
    )
    db.session.add(report)
    db.session.commit()
    return jsonify(report.to_dict()), 201


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/reports/<int:report_id>', methods=['GET'])
def get_task_report_detail(plan_id, task_id, report_id):
    """获取测试任务单份报告详情"""
    report = TestTaskReport.query.filter_by(id=report_id, task_id=task_id).first_or_404()
    return jsonify(report.to_dict())


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/reports/<int:report_id>', methods=['DELETE'])
def delete_task_report(plan_id, task_id, report_id):
    """删除测试任务报告"""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作'}), 403

    report = TestTaskReport.query.filter_by(id=report_id, task_id=task_id).first()
    if not report:
        return jsonify({'error': '报告不存在'}), 404
    db.session.delete(report)
    db.session.commit()
    return jsonify({'message': '已删除'})


# ==================== 测试任务 Bug 上报 ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/bug-reports', methods=['GET'])
def list_task_bug_reports(plan_id, task_id):
    """获取测试任务的 Bug 上报列表"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    reports = TestTaskBugReport.query.filter_by(task_id=task_id).order_by(
        TestTaskBugReport.created_at.desc()
    ).all()
    # 计算汇总
    total = sum(r.total_bugs for r in reports)
    return jsonify({'items': [r.to_dict() for r in reports], 'total': len(reports), 'total_bugs': total})


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/bug-reports', methods=['POST'])
def create_task_bug_report(plan_id, task_id):
    """上报测试任务 Bug 列表

    请求体：
    {
        "total_bugs": 5,
        "content": "## Bug列表\n1. xxx\n2. yyy",
        "format": "markdown"
    }
    """
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作该测试任务'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'error': '请求体为空'}), 400

    total_bugs = data.get('total_bugs', 0)
    content = data.get('content', '')
    report_format = (data.get('format') or 'markdown').lower()
    if report_format not in ('markdown', 'html'):
        return jsonify({'error': 'format 仅支持 markdown/html'}), 400

    user_name = ''
    if user:
        user_name = getattr(user, 'username', '') or getattr(user, 'name', '') or getattr(user, '_claw_name', '') or ''

    bug_report = TestTaskBugReport(
        task_id=task_id,
        total_bugs=total_bugs,
        content=content,
        format=report_format,
        created_by=user_name,
    )
    db.session.add(bug_report)

    # 同步更新 task.bug_count 为最新上报的 total_bugs
    task.bug_count = total_bugs
    _recalc_plan_stats(plan)

    db.session.commit()
    return jsonify(bug_report.to_dict()), 201


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/bug-reports/<int:report_id>', methods=['DELETE'])
def delete_task_bug_report(plan_id, task_id, report_id):
    """删除 Bug 上报记录"""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '无权操作'}), 403

    report = TestTaskBugReport.query.filter_by(id=report_id, task_id=task_id).first()
    if not report:
        return jsonify({'error': '记录不存在'}), 404
    db.session.delete(report)
    db.session.commit()
    return jsonify({'message': '已删除'})


def _recalc_plan_stats(plan):
    """重新计算计划的统计数字"""
    tasks = TestTask.query.filter_by(plan_id=plan.id).all()
    plan.total_tasks = len(tasks)
    plan.completed_tasks = sum(1 for t in tasks if t.status == 'completed')
    plan.total_bugs = sum(t.bug_count for t in tasks)
    plan.resolved_bugs = sum(t.bug_count for t in tasks if t.status == 'completed')


def _normalize_task_schedule(data, plan, current=None):
    """Validate the opt-in Hub-owned recurring execution contract."""
    enabled = data.get(
        'schedule_enabled', current.schedule_enabled if current else False)
    if type(enabled) is not bool:
        raise ValueError('schedule_enabled 必须为布尔值')
    recurrence = str(data.get(
        'recurrence_type', current.recurrence_type if current else 'once')
                     or 'once').strip().lower()
    if recurrence not in ('once', 'daily', 'weekly'):
        raise ValueError('recurrence_type 仅支持 once/daily/weekly')
    timezone_name = str(data.get(
        'schedule_timezone', current.schedule_timezone if current
        else 'Asia/Shanghai') or 'Asia/Shanghai').strip()
    if timezone_name != 'Asia/Shanghai':
        raise ValueError('当前仅支持 Asia/Shanghai 时区')

    def clock(name, default=''):
        value = str(data.get(name, getattr(current, name, default)
                             if current else default) or '').strip()
        if value and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
            raise ValueError('%s 必须为 HH:MM' % name)
        return value

    weekdays = data.get(
        'recurrence_weekdays', current.recurrence_weekdays_json
        if current else []) or []
    if (not isinstance(weekdays, list)
            or any(type(value) is not int or value < 0 or value > 6
                   for value in weekdays)):
        raise ValueError('recurrence_weekdays 必须为 0-6 的数组')
    weekdays = sorted(set(weekdays))
    if enabled and recurrence == 'weekly' and not weekdays:
        raise ValueError('每周任务至少选择一个星期')
    auto_dispatch = data.get(
        'auto_dispatch', current.auto_dispatch if current else False)
    if type(auto_dispatch) is not bool:
        raise ValueError('auto_dispatch 必须为布尔值')
    if not enabled:
        auto_dispatch = False
    execution_role = str(data.get(
        'execution_role', current.execution_role if current
        else 'member_work') or 'member_work').strip()
    if execution_role not in ('member_work', 'manager_work'):
        raise ValueError('execution_role 仅支持 member_work/manager_work')
    effective_raw = data.get(
        'schedule_effective_from', current.schedule_effective_from
        if current else None)
    try:
        effective_from = (
            effective_raw if isinstance(effective_raw, date)
            else date.fromisoformat(str(effective_raw))
            if effective_raw else None)
    except (TypeError, ValueError):
        raise ValueError('schedule_effective_from 必须为 YYYY-MM-DD')
    if effective_from and plan and plan.end_date and effective_from > plan.end_date:
        raise ValueError('schedule_effective_from 不能晚于计划结束日期')
    raw_definition_id = data.get(
        'workflow_definition_id', current.workflow_definition_id
        if current else None)
    if raw_definition_id in (None, ''):
        workflow_definition_id = None
    else:
        if isinstance(raw_definition_id, bool):
            raise ValueError('workflow_definition_id 必须为正整数')
        try:
            workflow_definition_id = int(raw_definition_id)
        except (TypeError, ValueError):
            raise ValueError('workflow_definition_id 必须为正整数')
        if workflow_definition_id <= 0:
            raise ValueError('workflow_definition_id 必须为正整数')
        definition = db.session.get(WorkflowDefinition, workflow_definition_id)
        if (not definition or definition.status != 'active'
                or not plan
                or int(definition.project_id or 0) != int(plan.project_id or 0)):
            raise ValueError('workflow_definition_id 必须是同项目的有效 Flow')
        if not enabled:
            raise ValueError('仅 Hub 周期调度任务可绑定 workflow_definition_id')
    start_vars = data.get(
        'workflow_start_vars', current.workflow_start_vars_json
        if current else {}) or {}
    if not isinstance(start_vars, dict):
        raise ValueError('workflow_start_vars 必须为 JSON 对象')
    if len(json.dumps(start_vars, ensure_ascii=False)) > 16000:
        raise ValueError('workflow_start_vars 不能超过 16000 字节')
    if not workflow_definition_id and start_vars:
        raise ValueError('workflow_start_vars 仅可用于已绑定 Flow 的周期任务')

    mode_default = (
        'workflow' if workflow_definition_id else
        current.execution_mode if current else 'ordinary_agent_task')
    execution_mode = str(data.get('execution_mode', mode_default)
                         or '').strip().lower()
    if execution_mode not in ('ordinary_agent_task', 'workflow'):
        raise ValueError('execution_mode 仅支持 ordinary_agent_task/workflow')
    if workflow_definition_id and execution_mode != 'workflow':
        raise ValueError('已绑定 workflow_definition_id 的任务必须使用 workflow 模式')
    if execution_mode == 'workflow' and not workflow_definition_id:
        raise ValueError('workflow 模式必须绑定 workflow_definition_id')

    def string_list(name, current_value=None):
        value = data.get(name, current_value if current_value is not None else []) or []
        if (not isinstance(value, list) or len(value) > 50
                or any(not isinstance(item, str) or not item.strip()
                       or len(item.strip()) > 120 for item in value)):
            raise ValueError('%s 必须为不超过 50 项的非空字符串数组' % name)
        return sorted(set(item.strip() for item in value))

    raw_fallbacks = data.get(
        'allowed_fallback_claw_ids',
        current.allowed_fallback_claw_ids_json if current else []) or []
    if (not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > 50
            or any(isinstance(item, bool) for item in raw_fallbacks)):
        raise ValueError('allowed_fallback_claw_ids 必须为正整数数组')
    try:
        fallback_ids = sorted(set(int(item) for item in raw_fallbacks))
    except (TypeError, ValueError):
        raise ValueError('allowed_fallback_claw_ids 必须为正整数数组')
    if any(item <= 0 for item in fallback_ids):
        raise ValueError('allowed_fallback_claw_ids 必须为正整数数组')
    if fallback_ids:
        valid_ids = {row.id for row in OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(fallback_ids),
            OpenClawInstance.project_id == plan.project_id,
            OpenClawInstance.status != 'deleted').all()}
        if valid_ids != set(fallback_ids):
            raise ValueError('allowed_fallback_claw_ids 必须属于当前项目且未删除')

    required_capabilities = string_list(
        'required_capabilities',
        current.required_capabilities_json if current else [])
    required_resources = data.get(
        'required_resources', current.required_resources_json
        if current else {}) or {}
    if (not isinstance(required_resources, dict)
            or len(json.dumps(required_resources, ensure_ascii=False)) > 16000):
        raise ValueError('required_resources 必须为不超过 16000 字节的 JSON 对象')
    return {
        'schedule_enabled': enabled,
        'recurrence_type': recurrence,
        'recurrence_weekdays_json': weekdays,
        'schedule_timezone': timezone_name,
        'schedule_effective_from': effective_from,
        'not_before_time': clock('not_before_time', '00:00'),
        'due_time': clock('due_time', '23:59'),
        'auto_dispatch': auto_dispatch,
        'execution_role': execution_role,
        'execution_mode': execution_mode,
        'workflow_definition_id': workflow_definition_id,
        'workflow_start_vars_json': start_vars,
        'allowed_fallback_claw_ids_json': fallback_ids,
        'required_capabilities_json': required_capabilities,
        'required_resources_json': required_resources,
    }


# ==================== 测试任务 CRUD ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks', methods=['GET'])
def list_test_tasks(plan_id):
    """获取测试计划下的任务列表"""
    plan = TestPlan.query.get_or_404(plan_id)

    query = TestTask.query.filter_by(plan_id=plan_id)

    task_type = request.args.get('task_type')
    status = request.args.get('status')
    assignee_claw_id = request.args.get('assignee_claw_id', type=int)
    assignee_owner = request.args.get('assignee_owner') or request.args.get('assignee_username')

    if task_type:
        query = query.filter_by(task_type=task_type)
    if status:
        query = query.filter_by(status=status)
    if assignee_claw_id:
        query = query.filter_by(assignee_claw_id=assignee_claw_id)
    elif assignee_owner:
        claw_ids = [c.id for c in OpenClawInstance.query.filter(
            OpenClawInstance.status != 'deleted',
            OpenClawInstance.owner == assignee_owner
        ).all()]
        if not claw_ids:
            user = (User.query.filter_by(username=assignee_owner).first()
                    or User.query.filter_by(display_name=assignee_owner).first())
            if user and user.bound_claw_id:
                claw_ids = [user.bound_claw_id]
        query = query.filter(TestTask.assignee_claw_id.in_(claw_ids or [-1]))

    tasks = query.order_by(TestTask.priority, TestTask.created_at).all()
    return jsonify([t.to_dict() for t in tasks])


@api_bp.route('/test-plans/<int:plan_id>/task-reference-options', methods=['GET'])
def list_test_task_reference_options(plan_id):
    """Return project/team-scoped resources a manager may bind to tasks."""
    plan = TestPlan.query.get_or_404(plan_id)
    user = _get_current_user()
    if not _can_edit_plan(user, plan):
        return jsonify({'error': '仅测试经理或计划管理员可选择任务参考资料'}), 403
    return jsonify(task_reference_options(plan))


@api_bp.route('/test-plans/<int:plan_id>/tasks', methods=['POST'])
def create_test_task(plan_id):
    """创建测试任务"""
    plan = TestPlan.query.get_or_404(plan_id)
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    user = _get_current_user()
    created_by = ''
    if user:
        created_by = getattr(user, 'username', '') or getattr(user, 'name', '')

    from datetime import date as date_type
    start_date = None
    end_date = None
    if data.get('start_date'):
        try:
            start_date = date_type.fromisoformat(data['start_date'])
        except (ValueError, TypeError):
            pass
    if data.get('end_date'):
        try:
            end_date = date_type.fromisoformat(data['end_date'])
        except (ValueError, TypeError):
            pass

    try:
        schedule = _normalize_task_schedule(data, plan)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    resolved_assignee_claw_id = _resolve_assignee_claw_id(data)
    if schedule['schedule_enabled'] and not resolved_assignee_claw_id:
        return jsonify({'error': 'Hub 周期调度必须指派团队 Agent'}), 400
    if schedule['schedule_enabled']:
        start_date = start_date or plan.start_date
        end_date = end_date or plan.end_date or start_date

    normalized_case_filter = enrich_case_filter(
        data.get('library_id'), data.get('case_filter'))
    try:
        references = normalize_task_references(data, plan)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    task = TestTask(
        plan_id=plan_id,
        name=data['name'],
        description=data.get('description', ''),
        task_type=data.get('task_type', 'functional'),
        assignee_claw_id=resolved_assignee_claw_id,
        assignee_username=_resolve_assignee_username(data),
        start_date=start_date,
        end_date=end_date,
        priority=data.get('priority', 'P2'),
        library_id=data.get('library_id'),
        case_filter=normalized_case_filter,
        status=data.get('status', 'assigned'),
        created_by=created_by,
        **references,
        **schedule,
    )
    db.session.add(task)
    db.session.flush()

    # Create notification todo for the assigned agent
    if task.assignee_claw_id:
        _create_test_task_notification(
            task, plan, action='assigned',
            message=f'你被分配了新的测试任务「{task.name}」，当前状态为【新分配】，请等待环境就绪后开始执行。'
        )

    # 如果关联了用例库，根据 case_filter 筛选用例导入到 task_cases
    if task.library_id:
        cases = select_library_cases(task.library_id, task.case_filter)
        for case in cases:
            tc = TestTaskCase(
                task_id=task.id,
                case_id=case.id,
                status='pending',
                case_source_snapshot=build_task_case_source_snapshot(case),
            )
            db.session.add(tc)
        task.total_cases = len(cases)

    from app.models_plan_supervision import PlanSupervisor
    from app.services import plan_supervision as plan_guard
    supervisor = plan_guard.locked(plan.id)
    if supervisor:
        plan_guard.sync_plan_stages(supervisor)

    _recalc_plan_stats(plan)
    db.session.commit()

    return jsonify(task.to_dict(with_cases=True)), 201


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>', methods=['GET'])
def get_test_task(plan_id, task_id):
    """获取测试任务详情"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    return jsonify(task.to_dict(with_cases=True))


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>', methods=['PUT'])
def update_test_task(plan_id, task_id):
    """更新测试任务"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    plan = TestPlan.query.get_or_404(plan_id)
    data = request.get_json()

    reference_fields = {
        'reference_skill_ids', 'reference_knowledge_ids',
        'reference_report_ids'}
    if reference_fields.intersection(data or {}):
        user = _get_current_user()
        if not _can_edit_plan(user, plan):
            return jsonify({'error': '仅测试经理或计划管理员可绑定任务参考资料'}), 403
    try:
        references = normalize_task_references(data or {}, plan, task)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    old_library_id = task.library_id
    old_case_filter = json.dumps(
        task.case_filter or {}, ensure_ascii=False, sort_keys=True)
    updatable_fields = ['name', 'description', 'task_type',
                        'priority', 'status', 'progress',
                        'result_summary', 'bug_count']
    old_status = task.status
    try:
        schedule = _normalize_task_schedule(data, TestPlan.query.get(plan_id), task)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    assignment_changed = any(
        key in data for key in (
            'assignee_claw_id', 'assignee_owner', 'assignee_username'))
    proposed_assignee_claw_id = (
        _resolve_assignee_claw_id(data) if assignment_changed
        else task.assignee_claw_id)
    proposed_assignee_username = (
        _resolve_assignee_username(data) if assignment_changed
        else task.assignee_username)
    if schedule['schedule_enabled'] and not proposed_assignee_claw_id:
        return jsonify({'error': 'Hub 周期调度必须指派团队 Agent'}), 400
    if data.get('status') == 'in_progress' and old_status != 'in_progress':
        from app.services import plan_supervision as plan_guard
        plan_guard.require_task_dispatch_receipt(task)
    for field in updatable_fields:
        if field in data:
            setattr(task, field, data[field])
    if assignment_changed:
        task.assignee_claw_id = proposed_assignee_claw_id
        task.assignee_username = proposed_assignee_username

    # Notify agent on status change
    new_status = data.get('status')
    if new_status and new_status != old_status and task.assignee_claw_id:
        plan = TestPlan.query.get(plan_id)
        status_labels = {
            'assigned': '新分配', 'pending': '待开始',
            'in_progress': '进行中', 'completed': '已完成',
            'blocked': '阻塞', 'skipped': '跳过'
        }
        label = status_labels.get(new_status, new_status)
        if new_status == 'pending':
            message = f'测试任务「{task.name}」已变为【待开始】，测试环境已就绪，请开始执行测试。'
        else:
            message = f'测试任务「{task.name}」状态已变更为【{label}】，请及时关注。'
        _create_test_task_notification(task, plan, action=new_status, message=message)

    # 日期字段
    from datetime import date as date_type
    if 'start_date' in data:
        try:
            task.start_date = date_type.fromisoformat(data['start_date']) if data['start_date'] else None
        except (ValueError, TypeError):
            pass
    if 'end_date' in data:
        try:
            task.end_date = date_type.fromisoformat(data['end_date']) if data['end_date'] else None
        except (ValueError, TypeError):
            pass
    if schedule['schedule_enabled']:
        plan_for_schedule = TestPlan.query.get(plan_id)
        task.start_date = task.start_date or plan_for_schedule.start_date
        task.end_date = task.end_date or plan_for_schedule.end_date or task.start_date
    for field, value in schedule.items():
        setattr(task, field, value)
    for field, value in references.items():
        setattr(task, field, value)

    # tapd_bug_ids
    if 'tapd_bug_ids' in data:
        task.tapd_bug_ids = data['tapd_bug_ids']
        task.bug_count = len(data['tapd_bug_ids']) if data['tapd_bug_ids'] else 0

    if 'library_id' in data or 'case_filter' in data:
        task.library_id = data.get('library_id', task.library_id)
        task.case_filter = enrich_case_filter(
            task.library_id, data.get('case_filter', task.case_filter))
        new_case_filter = json.dumps(
            task.case_filter or {}, ensure_ascii=False, sort_keys=True)
        if (task.library_id != old_library_id
                or new_case_filter != old_case_filter):
            apply_task_case_sync(task, TestPlan.query.get(plan_id))
            backup = dict(task.case_sync_backup_json or {})
            backup['library_id'] = old_library_id
            backup['case_filter'] = json.loads(old_case_filter)
            task.case_sync_backup_json = backup

    # 重算用例统计
    _recalc_task_case_stats(task)

    if plan:
        _recalc_plan_stats(plan)

    from app.models_plan_supervision import PlanSupervisor
    from app.services import plan_supervision as plan_guard
    supervisor = plan_guard.locked(plan_id)
    if supervisor:
        plan_guard.sync_plan_stages(supervisor)

    db.session.commit()
    return jsonify(task.to_dict(with_cases=True))


@api_bp.route(
    '/test-plans/<int:plan_id>/tasks/<int:task_id>/occurrences',
    methods=['GET'])
def list_test_task_occurrences(plan_id, task_id):
    """Return immutable dated executions, newest first."""
    TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    limit = max(1, min(request.args.get('limit', 30, type=int), 100))
    rows = (TestTaskOccurrence.query.filter_by(
        plan_id=plan_id, test_task_id=task_id)
        .order_by(TestTaskOccurrence.occurrence_date.desc(),
                  TestTaskOccurrence.id.desc()).limit(limit).all())
    return jsonify({'items': [row.to_dict() for row in rows],
                    'total': TestTaskOccurrence.query.filter_by(
                        plan_id=plan_id, test_task_id=task_id).count()})


def _task_case_sync_payload(task, sync_plan):
    payload = dict(sync_plan['summary'])
    payload.update({
        'task_id': task.id,
        'library_id': task.library_id,
        'library_name': task.library.name if task.library else '',
        'backup_available': bool(
            task.case_sync_backup_json
            and not task.case_sync_backup_restored_at),
        'samples': {
            'added': [case.title for case in sync_plan['added'][:8]],
            'removed': [((row.case.title if row.case else '')
                         or ('用例#%s' % row.case_id))
                        for row in sync_plan['removed'][:8]],
            'changed': [item['case'].title for item in sync_plan['matches']
                        if item['changed']][:8],
        },
    })
    return payload


@api_bp.route(
    '/test-plans/<int:plan_id>/tasks/<int:task_id>/sync-library-preview',
    methods=['GET'])
def preview_test_task_library_sync(plan_id, task_id):
    """Preview latest-library reconciliation without changing task data."""
    task = TestTask.query.filter_by(
        plan_id=plan_id, id=task_id).first_or_404()
    if not task.library_id:
        return jsonify({'error': '任务未关联用例库'}), 400
    return jsonify(_task_case_sync_payload(
        task, build_task_case_sync_plan(task)))


@api_bp.route(
    '/test-plans/<int:plan_id>/tasks/<int:task_id>/sync-library',
    methods=['POST'])
def sync_test_task_library(plan_id, task_id):
    """Backup and reconcile task cases with the latest library content."""
    task = TestTask.query.filter_by(
        plan_id=plan_id, id=task_id).first_or_404()
    data = request.get_json(silent=True) or {}
    if data.get('confirmed') is not True:
        return jsonify({
            'error': '同步前必须确认可能影响已执行用例结果',
            'code': 'TASK_CASE_SYNC_CONFIRMATION_REQUIRED',
        }), 409
    if not task.library_id:
        return jsonify({'error': '任务未关联用例库'}), 400
    plan = TestPlan.query.get(plan_id)
    sync_plan = apply_task_case_sync(task, plan)
    _recalc_task_case_stats(task)
    if plan:
        _recalc_plan_stats(plan)
    db.session.commit()
    payload = _task_case_sync_payload(task, sync_plan)
    payload.update({
        'message': '已同步最新用例库，并保存同步前备份',
        'backup_available': True,
        'backup_created_at': str(task.case_sync_backup_created_at),
    })
    return jsonify(payload)


@api_bp.route(
    '/test-plans/<int:plan_id>/tasks/<int:task_id>/sync-library-restore',
    methods=['POST'])
def restore_test_task_library_sync(plan_id, task_id):
    """Use the single restore opportunity created by the latest sync."""
    task = TestTask.query.filter_by(
        plan_id=plan_id, id=task_id).first_or_404()
    try:
        result = restore_task_case_backup(task)
        _recalc_task_case_stats(task)
        plan = TestPlan.query.get(plan_id)
        if plan:
            _recalc_plan_stats(plan)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify({
            'error': str(exc),
            'code': 'TASK_CASE_SYNC_RESTORE_UNAVAILABLE',
        }), 409
    return jsonify(dict({
        'message': '已还原到最近一次同步前的数据；本次还原机会已使用',
        'backup_available': False,
    }, **result))


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>', methods=['DELETE'])
def delete_test_task(plan_id, task_id):
    """删除测试任务"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    db.session.delete(task)

    plan = TestPlan.query.get(plan_id)
    if plan:
        _recalc_plan_stats(plan)
    db.session.commit()

    return jsonify({'message': f'任务 "{task.name}" 已删除'})


def _recalc_task_case_stats(task):
    """重算任务的用例执行统计"""
    tcs = TestTaskCase.query.filter_by(task_id=task.id).all()
    task.total_cases = len(tcs)
    task.passed_cases = sum(1 for tc in tcs if tc.status == 'passed')
    task.failed_cases = sum(1 for tc in tcs if tc.status == 'failed')
    task.blocked_cases = sum(1 for tc in tcs if tc.status == 'blocked')
    task.skipped_cases = sum(1 for tc in tcs if tc.status == 'skipped')
    # 自动计算进度
    if task.total_cases > 0:
        done = task.passed_cases + task.failed_cases + task.skipped_cases
        task.progress = round(done / task.total_cases * 100)
    elif task.status == 'completed':
        task.progress = 100


def _merge_task_bug_id(task, bug_id):
    bug_id = str(bug_id or '').strip()
    if not task or not bug_id:
        return
    current = list(task.tapd_bug_ids or [])
    if bug_id not in current:
        current.append(bug_id)
    task.tapd_bug_ids = current
    task.bug_count = len(current)


def _apply_case_bug_sync(tc, task, plan, bug_url):
    parsed = parse_tapd_bug_url(bug_url)
    if not parsed:
        return {'status': 'none'}
    workspace_id = (
        parsed['workspace_id']
        or (getattr(plan, 'tapd_workspace_id', '') or '')
        or (getattr(getattr(plan, 'project', None), 'tapd_workspace_id', '') or '')
    )
    tc.tapd_bug_id = parsed['bug_id']
    tc.tapd_bug_url = (
        parsed.get('url')
        or (build_tapd_bug_url(workspace_id, parsed['bug_id']) if workspace_id else '')
    )
    tc.case_info_snapshot = build_case_info_snapshot(tc, task, plan)
    _merge_task_bug_id(task, parsed['bug_id'])
    try:
        result = sync_task_case_bug_link(tc, task, plan, bug_url)
    except Exception as exc:
        tc.bug_sync_status = 'failed'
        tc.bug_sync_error = str(exc)
        return {'status': 'failed', 'error': str(exc)}

    tc.tapd_bug_id = result['bug_id']
    tc.tapd_bug_url = result['bug_url']
    tc.case_info_snapshot = result['case_info_snapshot']
    tc.bug_sync_status = 'synced'
    tc.bug_sync_error = ''
    tc.bug_synced_at = datetime.now()
    _merge_task_bug_id(task, result['bug_id'])
    return {'status': 'synced'}


# ==================== 用例执行状态 ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases', methods=['GET'])
def list_task_cases(plan_id, task_id):
    """获取任务下的用例执行列表"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()

    status = request.args.get('status')
    page = request.args.get('page', type=int)
    page_size = request.args.get('page_size', type=int)
    query = TestTaskCase.query.filter_by(task_id=task_id)
    if status:
        query = query.filter_by(status=status)

    query = query.order_by(TestTaskCase.id.asc())
    if page or page_size:
        page = max(page or 1, 1)
        page_size = min(max(page_size or 50, 1), 200)
        total = query.count()
        cases = query.offset((page - 1) * page_size).limit(page_size).all()
        return jsonify({
            'items': [tc.to_dict() for tc in cases],
            'total': total,
            'page': page,
            'page_size': page_size,
            'pages': (total + page_size - 1) // page_size if page_size else 0,
        })

    cases = query.all()
    return jsonify([tc.to_dict() for tc in cases])


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases', methods=['POST'])
def add_cases_to_task(plan_id, task_id):
    """向已有任务手动关联用例

    请求体：
    {
        "case_ids": [1, 2, 3],                    // 方式1：直接指定用例ID列表
        "library_id": 5,                           // 方式2：从用例库筛选导入
        "case_filter": {                           // 配合 library_id 使用
            "module_paths": ["模块A/子模块"],
            "priorities": ["P0", "P1"],
            "types": ["可自动化"],
            "tags": ["冒烟"]
        },
        "replace": false                           // 是否清空已有关联再导入（默认 false=追加）
    }
    """
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    data = request.get_json()

    if not data:
        return jsonify({'error': '请求体不能为空'}), 400

    case_ids = data.get('case_ids', [])
    library_id = data.get('library_id')
    case_filter = data.get('case_filter', {})
    replace = data.get('replace', False)

    # 方式2：从用例库筛选
    if library_id and not case_ids:
        case_filter = enrich_case_filter(library_id, case_filter)
        cases = select_library_cases(library_id, case_filter)
        case_ids = [c.id for c in cases]

    if not case_ids:
        return jsonify({'error': '未指定任何用例（case_ids 为空或筛选结果为空）'}), 400

    # 是否清空已有关联
    if replace:
        TestTaskCase.query.filter_by(task_id=task.id).delete()

    # 获取已关联的 case_id 集合（避免重复）
    existing_ids = set(
        row[0] for row in db.session.query(TestTaskCase.case_id)
        .filter_by(task_id=task.id).all()
    )

    added = 0
    skipped = 0
    case_rows = {case.id: case for case in TestCase.query.filter(
        TestCase.id.in_(case_ids)).all()}
    for cid in case_ids:
        if cid in existing_ids:
            skipped += 1
            continue
        tc = TestTaskCase(
            task_id=task.id, case_id=cid, status='pending',
            case_source_snapshot=(
                build_task_case_source_snapshot(case_rows[cid])
                if cid in case_rows else None))
        db.session.add(tc)
        existing_ids.add(cid)
        added += 1

    # 更新任务的 library_id（如果通过用例库导入）
    if library_id:
        task.library_id = library_id
        if case_filter:
            task.case_filter = enrich_case_filter(library_id, case_filter)

    _recalc_task_case_stats(task)
    plan = TestPlan.query.get(plan_id)
    if plan:
        _recalc_plan_stats(plan)

    db.session.commit()
    return jsonify({
        'message': f'已关联 {added} 条用例（{skipped} 条已存在跳过）',
        'added': added,
        'skipped': skipped,
        'total_cases': task.total_cases,
    }), 201


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/remove', methods=['POST'])
def remove_cases_from_task(plan_id, task_id):
    """从任务中移除关联的用例

    请求体：
    {
        "case_ids": [1, 2, 3],     // 要移除的用例ID列表
        "tc_ids": [10, 11, 12]     // 或按 TestTaskCase.id 移除
    }
    """
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    data = request.get_json()

    if not data:
        return jsonify({'error': '请求体不能为空'}), 400

    case_ids = data.get('case_ids', [])
    tc_ids = data.get('tc_ids', [])

    removed = 0
    if tc_ids:
        removed = TestTaskCase.query.filter(
            TestTaskCase.task_id == task.id,
            TestTaskCase.id.in_(tc_ids)
        ).delete(synchronize_session=False)
    elif case_ids:
        removed = TestTaskCase.query.filter(
            TestTaskCase.task_id == task.id,
            TestTaskCase.case_id.in_(case_ids)
        ).delete(synchronize_session=False)
    else:
        return jsonify({'error': '需要提供 case_ids 或 tc_ids'}), 400

    _recalc_task_case_stats(task)
    plan = TestPlan.query.get(plan_id)
    if plan:
        _recalc_plan_stats(plan)

    db.session.commit()
    return jsonify({
        'message': f'已移除 {removed} 条用例关联',
        'removed': removed,
        'total_cases': task.total_cases,
    })


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/<int:tc_id>', methods=['PUT'])
def update_task_case(plan_id, task_id, tc_id):
    """更新用例执行状态（OpenClaw 核心接口）

    请求体：
    {
        "status": "passed|failed|blocked|skipped",  // 可选，备注可局部保存
        "note": "失败原因等备注",
        "tapd_bug_id": "关联的 TAPD Bug ID",
        "tapd_bug_url": "关联的 TAPD Bug 链接"
    }
    """
    tc = TestTaskCase.query.filter_by(task_id=task_id, id=tc_id).first_or_404()
    task = TestTask.query.get_or_404(task_id)
    plan = TestPlan.query.get(plan_id)
    data = request.get_json(silent=True) or {}

    if not data:
        return jsonify({'error': '请求体不能为空'}), 400

    valid_statuses = ('pending', 'passed', 'failed', 'blocked', 'skipped')
    if data.get('status') and data['status'] not in valid_statuses:
        return jsonify({'error': f'status 必须是: {", ".join(valid_statuses)}'}), 400

    bug_url = None
    if 'tapd_bug_url' in data:
        bug_url = (data.get('tapd_bug_url') or '').strip()
        effective_status = data.get('status') or tc.status
        if bug_url and effective_status not in ('failed', 'blocked'):
            return jsonify({'error': '只有失败/阻塞用例允许关联 Bug 链接'}), 400
        if bug_url:
            try:
                parse_tapd_bug_url(bug_url)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400

    if data.get('status'):
        tc.status = data['status']
        tc.executed_at = datetime.now()

        user = _get_current_user()
        if user:
            tc.executed_by = getattr(user, 'username', '') or getattr(user, 'name', '')
    if 'note' in data:
        tc.note = data['note']
    if 'tapd_bug_id' in data:
        tc.tapd_bug_id = data['tapd_bug_id']
        _merge_task_bug_id(task, data['tapd_bug_id'])
    sync_result = {'status': tc.bug_sync_status or 'none'}
    if 'tapd_bug_url' in data:
        if bug_url:
            sync_result = _apply_case_bug_sync(tc, task, plan, bug_url)
        else:
            tc.tapd_bug_url = ''
            tc.bug_sync_status = 'none'
            tc.bug_sync_error = ''

    # 重算任务统计
    if task:
        _recalc_task_case_stats(task)
        if plan:
            _recalc_plan_stats(plan)

    db.session.commit()
    result = tc.to_dict()
    result['bug_sync_result'] = sync_result
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/<int:tc_id>/bug-sync/retry', methods=['POST'])
def retry_task_case_bug_sync(plan_id, task_id, tc_id):
    """重试单条用例的 TAPD Bug 用例信息同步。"""
    tc = TestTaskCase.query.filter_by(task_id=task_id, id=tc_id).first_or_404()
    task = TestTask.query.get_or_404(task_id)
    plan = TestPlan.query.get(plan_id)
    if not tc.tapd_bug_url:
        return jsonify({'error': '该用例未关联 Bug 链接'}), 400
    if tc.status not in ('failed', 'blocked'):
        return jsonify({'error': '只有失败/阻塞用例允许同步 Bug 用例信息'}), 400

    try:
        parse_tapd_bug_url(tc.tapd_bug_url)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    sync_result = _apply_case_bug_sync(tc, task, plan, tc.tapd_bug_url)
    _recalc_task_case_stats(task)
    if plan:
        _recalc_plan_stats(plan)
    db.session.commit()
    result = tc.to_dict()
    result['bug_sync_result'] = sync_result
    return jsonify(result)


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/batch', methods=['POST'])
def batch_update_task_cases(plan_id, task_id):
    """批量更新用例执行状态

    请求体：
    {
        "updates": [
            {"tc_id": 1, "status": "passed"},
            {"tc_id": 2, "status": "failed", "note": "原因..."},
            ...
        ]
    }
    """
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()
    data = request.get_json()

    if not data or 'updates' not in data:
        return jsonify({'error': 'updates 为必填项'}), 400

    updated = 0
    skipped = []
    for item in data['updates']:
        tc = None
        tc_id = item.get('tc_id')
        case_id = item.get('case_id')
        # 优先按 tc_id（TestTaskCase.id）匹配
        if tc_id:
            tc = TestTaskCase.query.filter_by(task_id=task_id, id=tc_id).first()
        # 其次按 case_id（TestCase.id）匹配
        if not tc and case_id:
            tc = TestTaskCase.query.filter_by(task_id=task_id, case_id=case_id).first()
        if not tc:
            skipped.append({'tc_id': tc_id, 'case_id': case_id, 'reason': 'not_found'})
            continue
        if item.get('status'):
            tc.status = item['status']
            tc.executed_at = datetime.now()
            user = _get_current_user()
            if user:
                tc.executed_by = getattr(user, 'username', '') or getattr(user, 'name', '')
        if item.get('note'):
            tc.note = item['note']
        if item.get('tapd_bug_id'):
            tc.tapd_bug_id = item['tapd_bug_id']
        updated += 1

    _recalc_task_case_stats(task)
    plan = TestPlan.query.get(plan_id)
    if plan:
        _recalc_plan_stats(plan)

    db.session.commit()
    result = {'message': f'已更新 {updated} 条用例状态', 'updated': updated}
    if skipped:
        result['skipped'] = skipped
        result['skipped_count'] = len(skipped)
    return jsonify(result)


# ==================== OpenClaw API（Token 认证） ====================

@api_bp.route('/openclaws/<int:claw_id>/test-tasks', methods=['GET'])
def get_claw_test_tasks(claw_id):
    """获取指派给某个 OpenClaw 的测试任务（支持 Token 认证）

    查询参数：
    - status: 按状态筛选
    - plan_id: 按计划筛选
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)

    query = TestTask.query.filter_by(assignee_claw_id=claw_id)
    status = request.args.get('status')
    plan_id = request.args.get('plan_id', type=int)
    if status:
        query = query.filter_by(status=status)
    if plan_id:
        query = query.filter_by(plan_id=plan_id)

    tasks = query.order_by(TestTask.priority, TestTask.start_date).all()

    result = []
    for t in tasks:
        d = t.to_dict(with_cases=True)
        d['plan_name'] = t.plan.name if t.plan else None
        result.append(d)

    return jsonify({'tasks': result, 'count': len(result)})


@api_bp.route('/openclaws/<int:claw_id>/test-tasks/<int:task_id>/report', methods=['POST'])
def report_task_progress(claw_id, task_id):
    """OpenClaw 上报任务进度（支持 Token 认证）

    请求体：
    {
        "status": "in_progress|completed|blocked",
        "progress": 75,
        "result_summary": "执行摘要...",
        "case_updates": [
            {"case_id": 1, "status": "passed"},
            {"case_id": 2, "status": "failed", "note": "断言失败"},
            ...
        ],
        "tapd_bug_ids": ["12345", "12346"]
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    # 优先匹配自己指派的任务；管理员 claw 可操作任何任务
    task = TestTask.query.filter_by(id=task_id, assignee_claw_id=claw_id).first()
    if not task:
        # admin 角色的 claw 可以代报任何任务
        if claw.role in ('admin', 'super_admin'):
            task = TestTask.query.get(task_id)
        if not task:
            return jsonify({'error': '任务不存在或未指派给该 OpenClaw'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': '上报数据为空'}), 400

    # 更新任务状态
    if data.get('status'):
        if data['status'] == 'in_progress' and task.status != 'in_progress':
            from app.services import plan_supervision as plan_guard
            plan_guard.require_task_dispatch_receipt(task)
        task.status = data['status']
    if data.get('progress') is not None:
        task.progress = data['progress']
    if data.get('result_summary'):
        task.result_summary = data['result_summary']

    # 更新用例执行状态（支持 tc_id 或 case_id 匹配）
    case_updates = data.get('case_updates', [])
    for cu in case_updates:
        tc = None
        # 优先按 tc_id（TestTaskCase.id）匹配
        if cu.get('tc_id'):
            tc = TestTaskCase.query.filter_by(task_id=task_id, id=cu['tc_id']).first()
        # 其次按 case_id（TestCase.id）匹配
        if not tc and cu.get('case_id'):
            tc = TestTaskCase.query.filter_by(task_id=task_id, case_id=cu['case_id']).first()
        if not tc:
            continue
        if cu.get('status'):
            tc.status = cu['status']
            tc.executed_at = datetime.now()
            tc.executed_by = claw.name
            if cu.get('note'):
                tc.note = cu['note']
            if cu.get('tapd_bug_id'):
                tc.tapd_bug_id = cu['tapd_bug_id']

    # 更新 Bug 关联
    if data.get('tapd_bug_ids'):
        existing = task.tapd_bug_ids or []
        new_ids = list(set(existing + data['tapd_bug_ids']))
        task.tapd_bug_ids = new_ids
        task.bug_count = len(new_ids)

    _recalc_task_case_stats(task)
    plan = TestPlan.query.get(task.plan_id)
    if plan:
        _recalc_plan_stats(plan)

    # Deadlock retry
    for _retry in range(3):
        try:
            db.session.commit()
            break
        except Exception as e:
            if 'Deadlock' in str(e) and _retry < 2:
                db.session.rollback()
                import time; time.sleep(0.3)
                continue
            raise
    return jsonify(task.to_dict())


# ==================== TAPD 迭代关联 ====================

@api_bp.route('/test-plans/tapd-iterations', methods=['GET'])
def get_tapd_iterations_for_plans():
    """获取可关联的 TAPD 迭代列表（根据项目绑定的 workspace_id）

    重构（2026-04）：Hub 不再直连 TAPD，改读 tapd_iterations_cache 本地缓存。
    缓存由 OpenClaw Agent 经 mcporter-internal/MCP 推送维护。
    """
    from app.models import TapdIterationsCache
    from sqlalchemy import desc as _desc, func as _func

    project_id = request.args.get('project_id', type=int)
    if project_id is None:
        projects = Project.query.filter(
            Project.tapd_workspace_id.isnot(None),
            Project.tapd_workspace_id != ''
        ).all()
    else:
        project = Project.query.get(project_id)
        projects = [project] if project and project.tapd_workspace_id else []

    result = []
    for p in projects:
        rows = (TapdIterationsCache.query
                .filter_by(tapd_workspace_id=p.tapd_workspace_id)
                .order_by(_desc(TapdIterationsCache.startdate))
                .all())
        last_synced = (db.session.query(_func.max(TapdIterationsCache.last_synced_at))
                       .filter(TapdIterationsCache.tapd_workspace_id == p.tapd_workspace_id)
                       .scalar())
        iterations = [{
            'id': r.tapd_iteration_id,
            'name': r.name or '',
            'status': r.status,
            'startdate': str(r.startdate) if r.startdate else None,
            'enddate': str(r.enddate) if r.enddate else None,
        } for r in rows]
        result.append({
            'project_id': p.id,
            'project_name': p.name,
            'workspace_id': p.tapd_workspace_id,
            'iterations': iterations,
            'data_source': 'local_cache',
            'last_synced_at': str(last_synced) if last_synced else None,
        })

    return jsonify(result)


# ==================== TAPD Bug 关联查询 ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/tapd-bugs', methods=['GET'])
def get_task_tapd_bugs(plan_id, task_id):
    """获取任务关联的 TAPD Bug 详情"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()

    if not task.tapd_bug_ids:
        return jsonify({'bugs': [], 'count': 0})

    # 尝试从 TAPD 拉取 Bug 详情
    plan = TestPlan.query.get(plan_id)
    workspace_id = task.tapd_bug_ids  # 不直接用，需要从项目获取 workspace_id
    ws_id = plan.tapd_workspace_id if plan else None

    if not ws_id:
        # 尝试从项目获取
        if plan and plan.project and plan.project.tapd_workspace_id:
            ws_id = plan.project.tapd_workspace_id

    if not ws_id:
        return jsonify({'bugs': [], 'count': len(task.tapd_bug_ids),
                        'error': '未配置 TAPD workspace_id'})

    try:
        from app.api.tapd import TAPD_API_BASE_URL, _tapd_request
        bugs = []
        for bug_id in task.tapd_bug_ids:
            try:
                data = _tapd_request('GET', f'{TAPD_API_BASE_URL}/bugs',
                                     {'workspace_id': ws_id, 'id': bug_id})
                for item in data:
                    bug = item.get('Bug', {})
                    bugs.append({
                        'id': bug.get('id'),
                        'title': bug.get('title'),
                        'status': bug.get('status'),
                        'severity': bug.get('severity'),
                        'priority': bug.get('priority'),
                        'current_owner': bug.get('current_owner'),
                    })
            except Exception:
                bugs.append({'id': bug_id, 'error': '获取详情失败'})

        return jsonify({'bugs': bugs, 'count': len(bugs)})
    except Exception as e:
        return jsonify({'error': str(e), 'bugs': [], 'count': 0})


# ==================== 用例库目录树 & 筛选 API ====================

@api_bp.route('/testcase-libraries/<int:library_id>/tree', methods=['GET'])
def get_library_tree(library_id):
    """获取用例库的目录树结构（基于 module_path 聚合）"""
    lib = TestCaseLibrary.query.get_or_404(library_id)

    # 查询所有非占位用例的 module_path
    rows = db.session.query(
        TestCase.module_path, func.count(TestCase.id)
    ).filter(
        TestCase.library_id == library_id,
        TestCase.is_placeholder != True
    ).group_by(TestCase.module_path).all()

    # 构建目录树
    tree = {}  # path -> {name, count, children}
    for path, count in rows:
        if not path:
            path = ''
        parts = path.split('/') if path else []
        current = tree
        for i, part in enumerate(parts):
            if part not in current:
                current[part] = {'_name': part, '_count': 0, '_children': {}}
            if i == len(parts) - 1:
                current[part]['_count'] += count
            else:
                current[part]['_count'] += count  # 父节点累加
            current = current[part]['_children']

    # 查询各优先级用例数（用于 API 筛选）
    priority_counts = {}
    for row in db.session.query(
        TestCase.priority, func.count(TestCase.id)
    ).filter(
        TestCase.library_id == library_id,
        TestCase.is_placeholder != True
    ).group_by(TestCase.priority).all():
        priority_counts[row[0]] = row[1]

    # 递归转成列表结构
    def _tree_to_list(nodes):
        result = []
        for name, node in sorted(nodes.items()):
            item = {
                'name': name,
                'path': name,  # 会被上层拼接
                'count': node['_count'],
            }
            if node['_children']:
                item['children'] = _tree_to_list(node['_children'])
            result.append(item)
        return result

    nodes = _tree_to_list(tree)

    # 重新构建完整路径的树
    all_paths = []
    for path, count in rows:
        if path:
            all_paths.append(path)

    # 生成唯一目录路径列表（用于前端多选）
    unique_dirs = sorted(set(all_paths))

    return jsonify({
        'library_id': library_id,
        'library_name': lib.name,
        'total_cases': lib.cases.count(),
        'directories': unique_dirs,
        'priority_counts': priority_counts,
        'tree': nodes,
    })


@api_bp.route('/testcase-libraries/<int:library_id>/cases', methods=['GET'])
def get_library_cases_filtered(library_id):
    """获取用例库的用例列表（支持筛选，API 创建任务时使用）"""
    lib = TestCaseLibrary.query.get_or_404(library_id)

    query = TestCase.query.filter_by(
        library_id=library_id
    ).filter(TestCase.is_placeholder != True)

    # 筛选条件
    module_path = request.args.get('module_path')
    if module_path:
        query = query.filter(db.or_(
            TestCase.module_path == module_path,
            TestCase.module_path.like(module_path + '/%')
        ))

    priorities = request.args.getlist('priority')
    if priorities:
        query = query.filter(TestCase.priority.in_(priorities))

    types = request.args.getlist('type')
    if types:
        query = query.filter(TestCase.type.in_(types))

    case_ids = request.args.getlist('case_id')
    if case_ids:
        query = query.filter(TestCase.id.in_([int(c) for c in case_ids]))

    cases = query.order_by(TestCase.priority, TestCase.case_id).all()
    return jsonify([c.to_dict() for c in cases])


# ==================== 任务链 CRUD ====================

@api_bp.route('/test-plans/<int:plan_id>/task-chains', methods=['GET'])
def list_task_chains(plan_id):
    """获取测试计划下的所有任务链"""
    plan = TestPlan.query.get_or_404(plan_id)
    chains = TestTaskChain.query.filter_by(plan_id=plan_id).order_by(
        TestTaskChain.created_at.desc()).all()
    return jsonify([c.to_dict(with_steps=True) for c in chains])


@api_bp.route('/test-plans/<int:plan_id>/task-chains', methods=['POST'])
def create_task_chain(plan_id):
    """创建任务链

    请求体：
    {
        "name": "商业化活动模块用例设计",
        "description": "从代码分析到用例设计的完整流程",
        "priority": "P1",
        "steps": [
            {
                "name": "服务端代码分析",
                "description": "分析商业化模块服务端代码",
                "task_type": "other",
                "assignee_claw_id": 3
            },
            {
                "name": "客户端代码分析",
                "description": "分析商业化模块客户端代码",
                "task_type": "other",
                "assignee_claw_id": 5
            }
        ]
    }
    """
    plan = TestPlan.query.get_or_404(plan_id)
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400
    if not data.get('steps') or len(data['steps']) < 2:
        return jsonify({'error': '任务链至少需要 2 个步骤'}), 400

    user = _get_current_user()
    created_by = ''
    if user:
        created_by = getattr(user, 'username', '') or getattr(user, 'name', '')

    chain = TestTaskChain(
        plan_id=plan_id,
        name=data['name'],
        description=data.get('description', ''),
        priority=data.get('priority', 'P1'),
        total_steps=len(data['steps']),
        status='draft',
        created_by=created_by,
    )
    db.session.add(chain)
    db.session.flush()

    for idx, step_data in enumerate(data['steps'], 1):
        step = TestTaskChainStep(
            chain_id=chain.id,
            step_order=idx,
            name=step_data.get('name', f'步骤 {idx}'),
            description=step_data.get('description', ''),
            task_type=step_data.get('task_type', 'other'),
            assignee_claw_id=_resolve_assignee_claw_id(step_data),
            status='waiting',
        )
        db.session.add(step)

    db.session.commit()
    return jsonify(chain.to_dict(with_steps=True)), 201


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>', methods=['GET'])
def get_task_chain(plan_id, chain_id):
    """获取任务链详情"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()
    return jsonify(chain.to_dict(with_steps=True))


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>', methods=['PUT'])
def update_task_chain(plan_id, chain_id):
    """更新任务链基本信息"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()
    data = request.get_json()

    for field in ('name', 'description', 'priority', 'status'):
        if field in data:
            setattr(chain, field, data[field])

    db.session.commit()
    return jsonify(chain.to_dict(with_steps=True))


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>/conclusion', methods=['PUT', 'POST'])
def save_task_chain_conclusion(plan_id, chain_id):
    """保存任务链执行结论（支持 markdown/html 富文本）"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()

    user = _get_current_user()
    can_edit = _can_edit_plan(user, chain.plan)
    if not can_edit and user:
        uname = getattr(user, 'username', '') or ''
        claw_name = getattr(user, '_claw_name', '') or ''
        assignees = TestTaskChainStep.query.filter_by(chain_id=chain_id).all()
        can_edit = any(
            ((s.assignee and s.assignee.owner == uname) or
             (claw_name and s.assignee and s.assignee.name == claw_name))
            for s in assignees
        )
    if not can_edit:
        return jsonify({'error': '无权更新该任务链结论'}), 403

    data = request.get_json() or {}
    content = data.get('execution_conclusion')
    if not isinstance(content, str):
        return jsonify({'error': 'execution_conclusion 为必填字符串'}), 400

    fmt = (data.get('conclusion_format') or 'markdown').lower()
    if fmt not in ('markdown', 'html'):
        return jsonify({'error': 'conclusion_format 仅支持 markdown/html'}), 400

    chain.execution_conclusion = content
    chain.conclusion_format = fmt
    chain.conclusion_updated_at = datetime.now()
    chain.conclusion_updated_by = (
        getattr(user, 'username', '') or
        getattr(user, 'name', '') or
        getattr(user, '_claw_name', '') or
        'agent'
    )
    db.session.commit()
    return jsonify(chain.to_dict(with_steps=True))


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>', methods=['DELETE'])
def delete_task_chain(plan_id, chain_id):
    """删除任务链"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()
    db.session.delete(chain)
    db.session.commit()
    return jsonify({'id': chain_id, 'deleted': True})


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>/start', methods=['POST'])
def start_task_chain(plan_id, chain_id):
    """启动任务链：将状态改为 active，激活第一步"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()

    if chain.status not in ('draft', 'paused'):
        return jsonify({'error': f'当前状态 {chain.status} 不允许启动'}), 400

    first_step = TestTaskChainStep.query.filter_by(
        chain_id=chain.id, step_order=1).first()
    if not first_step:
        return jsonify({'error': '任务链没有步骤'}), 400

    chain.status = 'active'
    chain.current_step = 1
    first_step.status = 'pending'
    first_step.started_at = datetime.now()

    # Notify the first step's assignee
    _notify_chain_step(chain, first_step, action='started')

    db.session.commit()
    if first_step.assignee_claw_id:
        from app.api.agent_client import notify_claw, notify_claw_todo
        notify_claw(first_step.assignee_claw_id)
        notify_claw_todo(first_step.assignee_claw_id)
    return jsonify(chain.to_dict(with_steps=True))


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>/steps/<int:step_id>/submit',
              methods=['POST'])
def submit_chain_step(plan_id, chain_id, step_id):
    """提交步骤结果，自动推进到下一步

    请求体：
    {
        "result_summary": "分析完成，发现3个关键模块...",
        "output_data": {"key_modules": [...], "risk_points": [...]}
    }
    """
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()
    step = TestTaskChainStep.query.filter_by(id=step_id, chain_id=chain_id).first_or_404()

    if step.status not in ('pending', 'in_progress'):
        return jsonify({'error': f'步骤状态为 {step.status}，不允许提交'}), 400

    data = request.get_json() or {}

    # Mark current step as completed
    step.status = 'completed'
    step.result_summary = data.get('result_summary', '')
    step.output_data = json.dumps(data['output_data'], ensure_ascii=False) if data.get('output_data') else None
    step.completed_at = datetime.now()

    # Auto-advance to next step
    next_step = TestTaskChainStep.query.filter_by(
        chain_id=chain_id, step_order=step.step_order + 1).first()

    if next_step:
        # Activate next step
        next_step.status = 'pending'
        next_step.started_at = datetime.now()
        chain.current_step = next_step.step_order

        # Notify next step's assignee with context from previous step
        _notify_chain_step(chain, next_step, action='your_turn',
                           prev_step=step)
    else:
        # All steps completed
        chain.status = 'completed'
        chain.completed_at = datetime.now()

    db.session.commit()
    if next_step and next_step.assignee_claw_id:
        from app.api.agent_client import notify_claw, notify_claw_todo
        notify_claw(next_step.assignee_claw_id)
        notify_claw_todo(next_step.assignee_claw_id)

    return jsonify({
        'chain': chain.to_dict(with_steps=True),
        'completed_step': step.to_dict(),
        'next_step': next_step.to_dict() if next_step else None,
        'chain_completed': chain.status == 'completed',
    })


@api_bp.route('/test-plans/<int:plan_id>/task-chains/<int:chain_id>/steps/<int:step_id>',
              methods=['PUT'])
def update_chain_step(plan_id, chain_id, step_id):
    """更新步骤状态（手动干预）"""
    chain = TestTaskChain.query.filter_by(plan_id=plan_id, id=chain_id).first_or_404()
    step = TestTaskChainStep.query.filter_by(id=step_id, chain_id=chain_id).first_or_404()
    data = request.get_json() or {}

    for field in ('name', 'description', 'task_type', 'status', 'result_summary'):
        if field in data:
            setattr(step, field, data[field])

    if any(k in data for k in ('assignee_claw_id', 'assignee_owner', 'assignee_username')):
        step.assignee_claw_id = _resolve_assignee_claw_id(data)

    if data.get('status') == 'in_progress' and not step.started_at:
        step.started_at = datetime.now()

    db.session.commit()
    return jsonify(step.to_dict())


def _notify_chain_step(chain, step, action='your_turn', prev_step=None):
    """Create task-chain todo and message notifications for the assignee."""
    if not step.assignee_claw_id:
        return None

    if action == 'started':
        title = f'[任务链] {chain.name} - 第{step.step_order}步开始'
        message = (f'任务链「{chain.name}」已启动，你负责第 {step.step_order} 步：\n'
                   f'📌 {step.name}\n\n'
                   f'{step.description or "(无详细描述)"}\n\n'
                   f'完成后请提交结果：POST /api/v1/test-plans/{chain.plan_id}'
                   f'/task-chains/{chain.id}/steps/{step.id}/submit')
    else:
        prev_info = ''
        if prev_step:
            prev_owner = prev_step.assignee.owner if prev_step.assignee else '未知'
            prev_info = (f'\n\n📋 上一步「{prev_step.name}」(by {prev_owner}) 已完成：\n'
                         f'{prev_step.result_summary or "(无摘要)"}')
        title = f'[任务链] {chain.name} - 轮到你了(第{step.step_order}/{chain.total_steps}步)'
        message = (f'任务链「{chain.name}」前置步骤已完成，现在轮到你：\n'
                   f'📌 第 {step.step_order} 步：{step.name}\n\n'
                   f'{step.description or "(无详细描述)"}'
                   f'{prev_info}\n\n'
                   f'完成后请提交结果：POST /api/v1/test-plans/{chain.plan_id}'
                   f'/task-chains/{chain.id}/steps/{step.id}/submit')

    todo = ClawTodo(
        openclaw_id=step.assignee_claw_id,
        title=title,
        description=message,
        schedule_type='once',
        urgency_level='flexible',
        priority=chain.priority or 'P1',
        task_category='test_task',
        enabled=True,
        created_by='system:task_chain',
    )
    db.session.add(todo)
    msg = ClawMessage(
        claw_id=step.assignee_claw_id,
        sender_name='Hub任务链',
        content=message,
        msg_type='task_delegate',
        direction='to_claw',
        status='pending',
    )
    db.session.add(msg)
    return {'todo': todo, 'message': msg}
