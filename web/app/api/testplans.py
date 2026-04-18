# -*- coding: utf-8 -*-
"""
测试计划排期 & 测试任务管理 API
支持 OpenClaw Token 认证操作
"""
import json
from datetime import datetime, date
from flask import request, jsonify, session
from sqlalchemy import func
from app import db
from app.models import (TestPlan, TestTask, TestTaskCase,
                        Project, OpenClawInstance, TestCaseLibrary, TestCase)
from app.api import api_bp


def _get_current_user():
    """获取当前用户（支持 Web session 和 OpenClaw Bearer Token）"""
    from app.api.skills import _get_current_user as _orig
    return _orig()


def _can_edit_plan(user, plan):
    """检查用户是否有权编辑测试计划"""
    if not user:
        return False
    if user.role in ('super_admin', 'admin'):
        return True
    return plan.created_by == user.username


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

    project_id = request.args.get('project_id', type=int)
    status = request.args.get('status')
    version_type = request.args.get('version_type')
    search = request.args.get('search')

    if project_id:
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
def create_test_plan():
    """创建测试计划"""
    data = request.get_json()
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
        name=data['name'],
        description=data.get('description', ''),
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
    plan = TestPlan.query.get_or_404(plan_id)
    data = request.get_json()

    updatable_fields = ['name', 'description', 'version_type', 'version_name',
                        'start_date', 'end_date', 'project_id',
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
    db.session.commit()
    return jsonify(plan.to_dict())


@api_bp.route('/test-plans/<int:plan_id>', methods=['DELETE'])
def delete_test_plan(plan_id):
    """删除测试计划"""
    plan = TestPlan.query.get_or_404(plan_id)
    db.session.delete(plan)
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


def _recalc_plan_stats(plan):
    """重新计算计划的统计数字"""
    tasks = TestTask.query.filter_by(plan_id=plan.id).all()
    plan.total_tasks = len(tasks)
    plan.completed_tasks = sum(1 for t in tasks if t.status == 'completed')
    plan.total_bugs = sum(t.bug_count for t in tasks)
    plan.resolved_bugs = sum(t.bug_count for t in tasks if t.status == 'completed')


# ==================== 测试任务 CRUD ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks', methods=['GET'])
def list_test_tasks(plan_id):
    """获取测试计划下的任务列表"""
    plan = TestPlan.query.get_or_404(plan_id)

    query = TestTask.query.filter_by(plan_id=plan_id)

    task_type = request.args.get('task_type')
    status = request.args.get('status')
    assignee_claw_id = request.args.get('assignee_claw_id', type=int)

    if task_type:
        query = query.filter_by(task_type=task_type)
    if status:
        query = query.filter_by(status=status)
    if assignee_claw_id:
        query = query.filter_by(assignee_claw_id=assignee_claw_id)

    tasks = query.order_by(TestTask.priority, TestTask.created_at).all()
    return jsonify([t.to_dict() for t in tasks])


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

    task = TestTask(
        plan_id=plan_id,
        name=data['name'],
        description=data.get('description', ''),
        task_type=data.get('task_type', 'functional'),
        assignee_claw_id=data.get('assignee_claw_id'),
        start_date=start_date,
        end_date=end_date,
        priority=data.get('priority', 'P2'),
        library_id=data.get('library_id'),
        case_filter=data.get('case_filter'),
        status=data.get('status', 'pending'),
        created_by=created_by,
    )
    db.session.add(task)
    db.session.flush()

    # 如果关联了用例库，根据 case_filter 筛选用例导入到 task_cases
    if task.library_id:
        query = TestCase.query.filter_by(
            library_id=task.library_id
        ).filter(TestCase.is_placeholder != True)

        cf = task.case_filter or {}
        module_paths = cf.get('module_paths', [])
        priorities = cf.get('priorities', [])
        case_ids = cf.get('case_ids', [])
        types = cf.get('types', [])

        if module_paths:
            # 支持节点前缀匹配：module_path 以指定路径开头
            import_or_filters = []
            for mp in module_paths:
                import_or_filters.append(TestCase.module_path == mp)
                import_or_filters.append(TestCase.module_path.like(mp + '/%'))
            query = query.filter(db.or_(*import_or_filters))

        if priorities:
            query = query.filter(TestCase.priority.in_(priorities))

        if case_ids:
            query = query.filter(TestCase.id.in_(case_ids))

        if types:
            query = query.filter(TestCase.type.in_(types))

        cases = query.all()
        for case in cases:
            tc = TestTaskCase(
                task_id=task.id,
                case_id=case.id,
                status='pending',
            )
            db.session.add(tc)
        task.total_cases = len(cases)

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
    data = request.get_json()

    updatable_fields = ['name', 'description', 'task_type', 'assignee_claw_id',
                        'priority', 'library_id', 'status', 'progress',
                        'result_summary', 'bug_count']
    for field in updatable_fields:
        if field in data:
            setattr(task, field, data[field])

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

    # tapd_bug_ids
    if 'tapd_bug_ids' in data:
        task.tapd_bug_ids = data['tapd_bug_ids']
        task.bug_count = len(data['tapd_bug_ids']) if data['tapd_bug_ids'] else 0

    # 如果关联了新用例库，重新导入
    if 'library_id' in data and data['library_id'] != task.library_id:
        # 清除旧关联
        TestTaskCase.query.filter_by(task_id=task.id).delete()
        if data['library_id']:
            cases = TestCase.query.filter_by(
                library_id=data['library_id']
            ).filter(TestCase.is_placeholder != True).all()
            for case in cases:
                tc = TestTaskCase(task_id=task.id, case_id=case.id, status='pending')
                db.session.add(tc)
            task.total_cases = len(cases)
        else:
            task.total_cases = 0

    # 重算用例统计
    _recalc_task_case_stats(task)

    plan = TestPlan.query.get(plan_id)
    if plan:
        _recalc_plan_stats(plan)

    db.session.commit()
    return jsonify(task.to_dict())


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


# ==================== 用例执行状态 ====================

@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases', methods=['GET'])
def list_task_cases(plan_id, task_id):
    """获取任务下的用例执行列表"""
    task = TestTask.query.filter_by(plan_id=plan_id, id=task_id).first_or_404()

    status = request.args.get('status')
    query = TestTaskCase.query.filter_by(task_id=task_id)
    if status:
        query = query.filter_by(status=status)

    cases = query.all()
    return jsonify([tc.to_dict() for tc in cases])


@api_bp.route('/test-plans/<int:plan_id>/tasks/<int:task_id>/cases/<int:tc_id>', methods=['PUT'])
def update_task_case(plan_id, task_id, tc_id):
    """更新用例执行状态（OpenClaw 核心接口）

    请求体：
    {
        "status": "passed|failed|blocked|skipped",
        "note": "失败原因等备注",
        "tapd_bug_id": "关联的 TAPD Bug ID"
    }
    """
    tc = TestTaskCase.query.filter_by(task_id=task_id, id=tc_id).first_or_404()
    data = request.get_json()

    if not data or not data.get('status'):
        return jsonify({'error': 'status 为必填项'}), 400

    valid_statuses = ('pending', 'passed', 'failed', 'blocked', 'skipped')
    if data['status'] not in valid_statuses:
        return jsonify({'error': f'status 必须是: {", ".join(valid_statuses)}'}), 400

    tc.status = data['status']
    tc.executed_at = datetime.now()

    user = _get_current_user()
    if user:
        tc.executed_by = getattr(user, 'username', '') or getattr(user, 'name', '')
    if 'note' in data:
        tc.note = data['note']
    if 'tapd_bug_id' in data:
        tc.tapd_bug_id = data['tapd_bug_id']

    # 重算任务统计
    task = TestTask.query.get(task_id)
    if task:
        _recalc_task_case_stats(task)
        plan = TestPlan.query.get(plan_id)
        if plan:
            _recalc_plan_stats(plan)

    db.session.commit()
    return jsonify(tc.to_dict())


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
    for item in data['updates']:
        tc_id = item.get('tc_id')
        if not tc_id:
            continue
        tc = TestTaskCase.query.filter_by(task_id=task_id, id=tc_id).first()
        if not tc:
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
    return jsonify({'message': f'已更新 {updated} 条用例状态', 'updated': updated})


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
    task = TestTask.query.filter_by(id=task_id, assignee_claw_id=claw_id).first_or_404()

    data = request.get_json()
    if not data:
        return jsonify({'error': '上报数据为空'}), 400

    # 更新任务状态
    if data.get('status'):
        task.status = data['status']
    if data.get('progress') is not None:
        task.progress = data['progress']
    if data.get('result_summary'):
        task.result_summary = data['result_summary']

    # 更新用例执行状态
    case_updates = data.get('case_updates', [])
    for cu in case_updates:
        case_id = cu.get('case_id')
        if not case_id:
            continue
        tc = TestTaskCase.query.filter_by(task_id=task_id, case_id=case_id).first()
        if tc and cu.get('status'):
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

    db.session.commit()
    return jsonify(task.to_dict())


# ==================== TAPD 迭代关联 ====================

@api_bp.route('/test-plans/tapd-iterations', methods=['GET'])
def get_tapd_iterations_for_plans():
    """获取可关联的 TAPD 迭代列表（根据项目绑定的 workspace_id）"""
    project_id = request.args.get('project_id', type=int)
    if not project_id:
        projects = Project.query.filter(
            Project.tapd_workspace_id.isnot(None),
            Project.tapd_workspace_id != ''
        ).all()
    else:
        project = Project.query.get(project_id)
        projects = [project] if project and project.tapd_workspace_id else []

    result = []
    for p in projects:
        try:
            from app.api.tapd import _tapd_request
            data = _tapd_request('GET', 'https://api.tapd.cn/iterations',
                                 {'workspace_id': p.tapd_workspace_id, 'limit': 10,
                                  'order': 'created desc'})
            iterations = []
            for item in data:
                it = item.get('Iteration', {})
                iterations.append({
                    'id': it.get('id'),
                    'name': it.get('name'),
                    'status': it.get('status'),
                    'startdate': it.get('startdate'),
                    'enddate': it.get('enddate'),
                })
            result.append({
                'project_id': p.id,
                'project_name': p.name,
                'workspace_id': p.tapd_workspace_id,
                'iterations': iterations,
            })
        except Exception as e:
            result.append({
                'project_id': p.id,
                'project_name': p.name,
                'workspace_id': p.tapd_workspace_id,
                'error': str(e),
                'iterations': [],
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
        from app.api.tapd import _tapd_request
        bugs = []
        for bug_id in task.tapd_bug_ids:
            try:
                data = _tapd_request('GET', 'https://api.tapd.cn/bugs',
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
