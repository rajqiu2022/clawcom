"""OpenClaw 待办任务 API

紧急度分 5 级：
  interrupt  — 定时中断，到点立即执行
  flexible   — 当天弹性，当天完成即可
  background — 后台任务，空闲时做
  periodic   — 周期容错-跳过，错过就下次
  retry      — 周期容错-重试，错过延后重试
"""
from datetime import datetime, date
import json
import urllib.request
from flask import request, jsonify, session as flask_session
from sqlalchemy import func
from app import db
from app.models import ClawTodo, ClawTodoLog, OpenClawInstance, User, SystemConfig
from app.api import api_bp


def _is_admin_user():
    """检查当前请求是否来自超级管理员（Web session 的 super_admin 或 admin 角色的 OpenClaw Token）"""
    # Web session 认证 — 仅 super_admin
    uid = flask_session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user and user.role == 'super_admin':
            return True

    # Bearer Token 认证 — admin 角色的 OpenClaw（龙虾王）Token 映射为超级管理员
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        token = auth[7:]
        for claw in OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all():
            if claw.verify_token(token):
                if claw.role == 'admin':
                    return True
                break
    return False


def _compute_init_gate(claw_id: int) -> dict:
    """计算某 claw 的 init 验收门状态（仅 approved/completed 视为通过）。"""
    from app.api.registration import _get_init_tasks_config, _required_verification_targets

    required_targets = _required_verification_targets(_get_init_tasks_config())
    todos = ClawTodo.query.filter_by(openclaw_id=claw_id, task_category='init').all()
    todo_by_target = {}
    for t in todos:
        vt = (t.verification_target or '').strip()
        if vt and vt not in todo_by_target:
            todo_by_target[vt] = t.id

    latest_logs = {}
    if todo_by_target:
        todo_ids = list(todo_by_target.values())
        logs = (ClawTodoLog.query
                .filter(ClawTodoLog.todo_id.in_(todo_ids))
                .order_by(ClawTodoLog.todo_id.asc(),
                          ClawTodoLog.log_date.desc(),
                          ClawTodoLog.created_at.desc())
                .all())
        for log in logs:
            if log.todo_id not in latest_logs:
                latest_logs[log.todo_id] = log

    pending_targets = []
    target_status = {}
    for vt in required_targets:
        tid = todo_by_target.get(vt)
        log = latest_logs.get(tid) if tid else None
        status = log.status if log else 'pending'
        target_status[vt] = status
        if status not in ('approved', 'completed'):
            pending_targets.append(vt)

    return {
        'required_targets': required_targets,
        'target_status': target_status,
        'pending_required_targets': pending_targets,
        'passed': len(pending_targets) == 0,
    }


def _notify_registration_pass(claw_id: int, gate: dict) -> None:
    """注册验收通过后，发送可选 webhook 通知（预留给企微接口）。"""
    cfg = SystemConfig.query.filter_by(config_key='registration_pass_webhook').first()
    webhook = (cfg.value or '').strip() if cfg and cfg.value else ''
    if not webhook:
        return

    claw = OpenClawInstance.query.get(claw_id)
    payload = {
        'event': 'registration_passed',
        'claw_id': claw_id,
        'claw_name': claw.name if claw else f'OpenClaw#{claw_id}',
        'role': claw.role if claw else None,
        'project_name': claw.project_name if claw else None,
        'gate': gate,
        'approved_at': datetime.now().isoformat(),
    }
    try:
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        # 通知失败不影响审核结果
        pass


@api_bp.route('/openclaws/<int:claw_id>/todos', methods=['GET'])
def list_todos(claw_id):
    """获取待办列表

    查询参数：
      category:     按类别筛选（routine/init/onboard）
      urgency:      按紧急度筛选（interrupt/flexible/background/periodic/retry）
      enabled_only: 是否只返回启用的（默认 true）
    """
    OpenClawInstance.query.get_or_404(claw_id)
    enabled_only = request.args.get('enabled_only', 'true').lower() == 'true'
    category = request.args.get('category')
    urgency = request.args.get('urgency')

    q = ClawTodo.query.filter_by(openclaw_id=claw_id)
    if enabled_only:
        q = q.filter_by(enabled=True)
    if category:
        q = q.filter_by(task_category=category)
    if urgency:
        q = q.filter_by(urgency_level=urgency)

    todos = q.order_by(ClawTodo.created_at.desc()).all()
    return jsonify([t.to_dict(with_today_status=True) for t in todos])


@api_bp.route('/openclaws/<int:claw_id>/todos', methods=['POST'])
def create_todo(claw_id):
    """创建待办任务

    请求体：
    {
      "title": "发送日报",
      "description": "汇总今日工作...",
      "schedule_type": "daily",
      "schedule_time": "21:00",
      "urgency_level": "interrupt",  // interrupt/flexible/background/periodic/retry
      "retry_delay": 5,             // 仅 retry 级别
      "retry_max": 1,               // 仅 retry 级别
      "priority": "P0",
      "task_category": "routine",    // routine/init/onboard
      "verification_target": null
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'title 必填'}), 400

    # created_by 优先取请求体参数，其次取 OpenClaw 名称，兜底 system
    created_by = data.get('created_by') or claw.name or 'system'

    todo = ClawTodo(
        openclaw_id=claw_id,
        title=data['title'],
        description=data.get('description'),
        schedule_type=data.get('schedule_type', 'daily'),
        schedule_time=data.get('schedule_time'),
        schedule_day=data.get('schedule_day'),
        urgency_level=data.get('urgency_level', 'flexible'),
        retry_delay=data.get('retry_delay', 5),
        retry_max=data.get('retry_max', 1),
        priority=data.get('priority', 'P1'),
        task_category=data.get('task_category', 'routine'),
        verification_target=data.get('verification_target'),
        enabled=True,  # 创建时始终启用，禁用只能通过 PUT 更新
        created_by=created_by,
    )
    db.session.add(todo)
    db.session.commit()
    # 通知 SSE 长连接立即推送（待办变更通过 SSE 事件通知，不再发聊天消息）
    from app.api.agent_client import notify_claw_todo
    notify_claw_todo(claw_id)
    return jsonify(todo.to_dict(with_today_status=True)), 201


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>', methods=['PUT'])
def update_todo(claw_id, todo_id):
    """更新待办"""
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    data = request.get_json()
    for f in ['title', 'description', 'schedule_type', 'schedule_time',
              'schedule_day', 'urgency_level', 'retry_delay', 'retry_max',
              'priority', 'task_category', 'verification_target', 'created_by']:
        if f in data:
            setattr(todo, f, data[f])
    if 'enabled' in data:
        todo.enabled = bool(data['enabled'])
    db.session.commit()
    # 通知 SSE 长连接立即推送（待办变更通过 SSE 事件通知，不再发聊天消息）
    from app.api.agent_client import notify_claw_todo
    notify_claw_todo(claw_id)
    return jsonify(todo.to_dict(with_today_status=True))


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>', methods=['DELETE'])
def delete_todo(claw_id, todo_id):
    """删除待办"""
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    todo_title = todo.title
    db.session.delete(todo)
    db.session.commit()
    # 通知 SSE 长连接立即推送（待办变更通过 SSE 事件通知，不再发聊天消息）
    from app.api.agent_client import notify_claw_todo
    notify_claw_todo(claw_id)
    return jsonify({'message': '待办已删除'})


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>/complete', methods=['POST'])
def complete_todo(claw_id, todo_id):
    """标记待办为已提交（上报执行结果）

    请求体：
    {
      "result_summary": "执行结果描述",
      "status": "submitted"            // submitted(默认)/skipped/retry_failed
    }
    提交后状态变为 submitted，需管理员审核通过后才算完成(approved)。
    """
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    data = request.get_json() or {}
    today = date.today()
    status = data.get('status', 'submitted')

    log = ClawTodoLog.query.filter_by(todo_id=todo_id, log_date=today).first()
    if log:
        log.completed_at = datetime.now()
        log.result_summary = data.get('result_summary', log.result_summary)
        log.status = status
        if status == 'retry_failed':
            log.retry_count = (log.retry_count or 0) + 1
    else:
        log = ClawTodoLog(
            todo_id=todo_id, openclaw_id=claw_id, log_date=today,
            completed_at=datetime.now(),
            result_summary=data.get('result_summary'),
            status=status,
            retry_count=1 if status == 'retry_failed' else 0,
        )
        db.session.add(log)

    # once 类型提交后自动关闭
    if todo.schedule_type == 'once' and status == 'submitted':
        todo.enabled = False

    db.session.commit()
    return jsonify(log.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>/approve', methods=['POST'])
def approve_todo(claw_id, todo_id):
    """审核通过待办（管理员操作，将 submitted 改为 approved）

    请求体（可选）：
    {
      "log_date": "2026-04-13"   // 指定审核哪天的记录，默认今天
    }
    """
    if not _is_admin_user():
        return jsonify({'error': '仅管理员可审核待办'}), 403

    log_date_str = (request.get_json() or {}).get('log_date')
    target_date = date.fromisoformat(log_date_str) if log_date_str else date.today()

    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    log = ClawTodoLog.query.filter_by(
        todo_id=todo_id, openclaw_id=claw_id, log_date=target_date
    ).first()
    if not log:
        return jsonify({'error': '未找到该日期的提交记录'}), 404
    if log.status != 'submitted':
        return jsonify({'error': f'当前状态为 {log.status}，只能审核 submitted 状态的记录'}), 400

    log.status = 'approved'
    db.session.commit()
    gate = None
    if todo.task_category == 'init':
        gate = _compute_init_gate(claw_id)
        if gate.get('passed'):
            _notify_registration_pass(claw_id, gate)
    return jsonify({'log': log.to_dict(), 'registration_gate': gate})


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>/skip', methods=['POST'])
def skip_todo(claw_id, todo_id):
    """上报跳过（periodic 级别错过时调用）

    请求体：
    {
      "result_summary": "跳过原因"
    }
    """
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    data = request.get_json() or {}
    today = date.today()

    log = ClawTodoLog.query.filter_by(todo_id=todo_id, log_date=today).first()
    if log:
        log.status = 'skipped'
        log.result_summary = data.get('result_summary', log.result_summary)
    else:
        log = ClawTodoLog(
            todo_id=todo_id, openclaw_id=claw_id, log_date=today,
            result_summary=data.get('result_summary', '已跳过'),
            status='skipped',
        )
        db.session.add(log)

    db.session.commit()
    return jsonify(log.to_dict())


@api_bp.route('/openclaws/<int:claw_id>/todo-summary', methods=['GET'])
def todo_summary(claw_id):
    """待办完成汇总（支持日期筛选）"""
    OpenClawInstance.query.get_or_404(claw_id)
    target_date = request.args.get('date', date.today().isoformat())
    target = date.fromisoformat(target_date)
    todos = ClawTodo.query.filter_by(openclaw_id=claw_id, enabled=True).all()
    logs = {l.todo_id: l for l in ClawTodoLog.query.filter_by(
        openclaw_id=claw_id, log_date=target).all()}

    items = []
    for t in todos:
        need = False
        if t.schedule_type == 'once':
            need = t.created_at and t.created_at.date() == target
        elif t.schedule_type == 'daily':
            need = True
        elif t.schedule_type == 'weekly' and t.schedule_day:
            need = target.isoweekday() == t.schedule_day
        elif t.schedule_type == 'monthly' and t.schedule_day:
            need = target.day == t.schedule_day
        if need:
            log = logs.get(t.id)
            items.append({
                'todo': t.to_dict(),
                'status': log.status if log else 'pending',
                'completed_at': str(log.completed_at) if log and log.completed_at else None,
                'result_summary': log.result_summary if log else None,
                'retry_count': log.retry_count if log else 0,
            })

    total = len(items)
    submitted = sum(1 for i in items if i['status'] == 'submitted')
    approved = sum(1 for i in items if i['status'] in ('approved', 'completed'))
    pending = sum(1 for i in items if i['status'] == 'pending')
    return jsonify({
        'date': target_date,
        'total': total,
        'pending': pending,
        'submitted': submitted,
        'approved': approved,
        'rate': round(approved / total * 100, 1) if total else 100,
        'items': items,
    })


@api_bp.route('/openclaws/<int:claw_id>/todos/completed', methods=['GET'])
def list_completed_todos(claw_id):
    """待办执行记录列表（最近 3 天或最近 50 条，取较小集合）

    查询参数：
      days:  回溯天数（默认 3）
      limit: 最大条数（默认 50）

    返回按完成时间倒序的执行记录列表，每条包含待办详情和执行结果。
    包括 submitted/approved/completed/skipped/overdue/retry_failed 状态。
    """
    from datetime import timedelta

    OpenClawInstance.query.get_or_404(claw_id)
    days = request.args.get('days', 3, type=int)
    limit = request.args.get('limit', 50, type=int)

    since = date.today() - timedelta(days=max(days, 1) - 1)

    # 查询指定天数内的执行记录
    logs = (ClawTodoLog.query
            .filter(ClawTodoLog.openclaw_id == claw_id,
                    ClawTodoLog.log_date >= since)
            .order_by(func.coalesce(ClawTodoLog.completed_at, ClawTodoLog.created_at).desc(),
                      ClawTodoLog.created_at.desc())
            .limit(limit)
            .all())

    # 批量获取关联的待办信息
    todo_ids = list({l.todo_id for l in logs})
    todos_map = {t.id: t for t in ClawTodo.query.filter(
        ClawTodo.id.in_(todo_ids)).all()} if todo_ids else {}

    items = []
    for log in logs:
        todo = todos_map.get(log.todo_id)
        items.append({
            'log': log.to_dict(),
            'todo': todo.to_dict() if todo else {'id': log.todo_id, 'title': '(已删除)'},
        })

    return jsonify({
        'since': since.isoformat(),
        'count': len(items),
        'items': items,
    })
