"""OpenClaw 待办任务 API

紧急度分 5 级：
  interrupt  — 定时中断，到点立即执行
  flexible   — 当天弹性，当天完成即可
  background — 后台任务，空闲时做
  periodic   — 周期容错-跳过，错过就下次
  retry      — 周期容错-重试，错过延后重试
"""
from datetime import datetime, date
from flask import request, jsonify
from app import db
from app.models import ClawTodo, ClawTodoLog, OpenClawInstance
from app.api import api_bp


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

    todos = q.order_by(ClawTodo.priority, ClawTodo.schedule_time).all()
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
    OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'title 必填'}), 400

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
        enabled=data.get('enabled', True),
        created_by=data.get('created_by', 'system'),
    )
    db.session.add(todo)
    db.session.commit()
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
    return jsonify(todo.to_dict(with_today_status=True))


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>', methods=['DELETE'])
def delete_todo(claw_id, todo_id):
    """删除待办"""
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    db.session.delete(todo)
    db.session.commit()
    return jsonify({'message': '待办已删除'})


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>/complete', methods=['POST'])
def complete_todo(claw_id, todo_id):
    """标记待办完成（上报执行结果）

    请求体：
    {
      "result_summary": "执行结果描述",
      "status": "completed"           // completed/skipped/retry_failed（可选，默认 completed）
    }
    """
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    data = request.get_json() or {}
    today = date.today()
    status = data.get('status', 'completed')

    log = ClawTodoLog.query.filter_by(todo_id=todo_id, log_date=today).first()
    if log:
        log.completed_at = datetime.utcnow()
        log.result_summary = data.get('result_summary', log.result_summary)
        log.status = status
        if status == 'retry_failed':
            log.retry_count = (log.retry_count or 0) + 1
    else:
        log = ClawTodoLog(
            todo_id=todo_id, openclaw_id=claw_id, log_date=today,
            completed_at=datetime.utcnow(),
            result_summary=data.get('result_summary'),
            status=status,
            retry_count=1 if status == 'retry_failed' else 0,
        )
        db.session.add(log)

    # once 类型完成后自动关闭
    if todo.schedule_type == 'once' and status == 'completed':
        todo.enabled = False

    db.session.commit()
    return jsonify(log.to_dict())


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
            # once 类型只在创建当天需要
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
    done = sum(1 for i in items if i['status'] == 'completed')
    return jsonify({
        'date': target_date,
        'total': total, 'completed': done, 'pending': total - done,
        'rate': round(done / total * 100, 1) if total else 100,
        'items': items,
    })
