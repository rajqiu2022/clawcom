"""OpenClaw 待办系统 API"""
from datetime import datetime, date
from flask import request, jsonify
from app import db
from app.models import ClawTodo, ClawTodoLog, OpenClawInstance
from app.api import api_bp


@api_bp.route('/openclaws/<int:claw_id>/todos', methods=['GET'])
def list_todos(claw_id):
    """获取待办列表（含今日状态）

    查询参数：
      category: 按任务类别筛选（routine/init/onboard）
      enabled_only: 是否只返回启用的（默认 true）
    """
    OpenClawInstance.query.get_or_404(claw_id)
    enabled_only = request.args.get('enabled_only', 'true').lower() == 'true'
    category = request.args.get('category')
    q = ClawTodo.query.filter_by(openclaw_id=claw_id)
    if enabled_only:
        q = q.filter_by(enabled=True)
    if category:
        q = q.filter_by(task_category=category)
    todos = q.order_by(ClawTodo.priority, ClawTodo.schedule_time).all()
    return jsonify([t.to_dict(with_today_status=True) for t in todos])


@api_bp.route('/openclaws/<int:claw_id>/todos', methods=['POST'])
def create_todo(claw_id):
    """创建待办"""
    OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()
    if not data or not data.get('title'):
        return jsonify({'error': 'title 必填'}), 400
    todo = ClawTodo(
        openclaw_id=claw_id, title=data['title'],
        description=data.get('description'),
        schedule_type=data.get('schedule_type', 'daily'),
        schedule_time=data.get('schedule_time'),
        schedule_day=data.get('schedule_day'),
        priority=data.get('priority', 'P1'),
        enabled=data.get('enabled', True),
        task_category=data.get('task_category', 'routine'),
        verification_target=data.get('verification_target'),
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
              'schedule_day', 'priority', 'created_by']:
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
    return jsonify({'message': f'待办已删除'})


@api_bp.route('/openclaws/<int:claw_id>/todos/<int:todo_id>/complete', methods=['POST'])
def complete_todo(claw_id, todo_id):
    """标记待办完成（记录当天日志）"""
    todo = ClawTodo.query.filter_by(id=todo_id, openclaw_id=claw_id).first_or_404()
    data = request.get_json() or {}
    today = date.today()
    log = ClawTodoLog.query.filter_by(todo_id=todo_id, log_date=today).first()
    if log:
        log.completed_at = datetime.utcnow()
        log.result_summary = data.get('result_summary', log.result_summary)
        log.status = 'completed'
    else:
        log = ClawTodoLog(
            todo_id=todo_id, openclaw_id=claw_id, log_date=today,
            completed_at=datetime.utcnow(),
            result_summary=data.get('result_summary'), status='completed',
        )
        db.session.add(log)

    # once 类型完成后自动关闭
    if todo.schedule_type == 'once':
        todo.enabled = False

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
        if t.schedule_type == 'daily':
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
            })

    total = len(items)
    done = sum(1 for i in items if i['status'] == 'completed')
    return jsonify({
        'date': target_date,
        'total': total, 'completed': done, 'pending': total - done,
        'rate': round(done / total * 100, 1) if total else 100,
        'items': items,
    })
