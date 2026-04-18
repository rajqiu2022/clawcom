"""注册初始化任务管理 API

管理员（龙虾王）可通过 API 查看和修改新 OpenClaw 注册时自动下发的待办任务列表。
配置存储在数据库 system_config 表中，key='init_tasks'。
"""
import json
from flask import request, jsonify
from app import db
from app.api import api_bp
from app.models import OpenClawInstance


def _get_init_tasks_config():
    """从数据库读取初始化任务配置，不存在则用 seed.py 的默认值"""
    from app.models import SystemConfig
    cfg = SystemConfig.query.filter_by(config_key='init_tasks').first()
    if cfg and cfg.value:
        try:
            return json.loads(cfg.value)
        except (json.JSONDecodeError, TypeError):
            pass
    # fallback 到 seed.py 的硬编码
    from app.seed import INIT_TASKS
    return INIT_TASKS


def _save_init_tasks_config(tasks):
    """保存初始化任务配置到数据库"""
    from app.models import SystemConfig
    cfg = SystemConfig.query.filter_by(config_key='init_tasks').first()
    if not cfg:
        cfg = SystemConfig(config_key='init_tasks', value=json.dumps(tasks, ensure_ascii=False))
        db.session.add(cfg)
    else:
        cfg.value = json.dumps(tasks, ensure_ascii=False)
    db.session.commit()


@api_bp.route('/registration/init-tasks', methods=['GET'])
def get_init_tasks():
    """查看当前初始化任务配置"""
    tasks = _get_init_tasks_config()
    return jsonify({'tasks': tasks, 'count': len(tasks)})


@api_bp.route('/registration/init-tasks', methods=['PUT'])
def update_init_tasks():
    """更新初始化任务配置（管理员权限）

    请求体：
    {
      "tasks": [
        {
          "title": "任务标题",
          "description": "任务描述",
          "priority": "P0",
          "urgency_level": "background",
          "verification_target": "target-name"
        }
      ]
    }
    """
    data = request.get_json()
    if not data or 'tasks' not in data:
        return jsonify({'error': 'tasks 为必填项'}), 400

    tasks = data['tasks']
    # 校验每个任务必须有 title
    for t in tasks:
        if not t.get('title'):
            return jsonify({'error': '每个任务必须有 title'}), 400

    _save_init_tasks_config(tasks)
    return jsonify({'message': f'已更新 {len(tasks)} 个初始化任务', 'count': len(tasks)})


@api_bp.route('/openclaws/<int:claw_id>/init-tasks', methods=['POST'])
def send_init_tasks(claw_id):
    """手动为已注册的 OpenClaw 补发初始化任务"""
    OpenClawInstance.query.get_or_404(claw_id)
    tasks = _get_init_tasks_config()

    from app.models import ClawTodo
    created = 0
    for task in tasks:
        todo = ClawTodo(
            openclaw_id=claw_id,
            title=task['title'],
            description=task.get('description', ''),
            schedule_type='once',
            priority=task.get('priority', 'P1'),
            urgency_level=task.get('urgency_level', 'background'),
            task_category='init',
            verification_target=task.get('verification_target', ''),
            enabled=True,
            created_by='system',
        )
        db.session.add(todo)
        created += 1
    db.session.commit()
    return jsonify({'message': f'已下发 {created} 个初始化任务', 'count': created}), 201


@api_bp.route('/registration/init-tasks/status', methods=['GET'])
def get_all_init_tasks_status():
    """查询系统上所有 OpenClaw 的 init 类待办任务

    认证方式（二选一）：
      1. Web session 登录（super_admin 用户）
      2. OpenClaw Token（Authorization: Bearer {TOKEN}，且该 OpenClaw 的 role=admin）

    查询参数：
      status: 筛选状态（pending/completed/all，默认 all）
    """
    from flask import session as flask_session
    from app.models import User, ClawTodo, ClawTodoLog
    from datetime import date as d

    # 权限检查：Web session super_admin 或 OpenClaw admin Token
    authorized = False

    # 方式1：Web session
    uid = flask_session.get('user_id')
    if uid:
        user = User.query.get(uid)
        if user and user.role in ('super_admin', 'admin'):
            authorized = True

    # 方式2：OpenClaw Token
    if not authorized:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            # 遍历所有 admin role 的 OpenClaw 验证 token
            admin_claws = OpenClawInstance.query.filter_by(role='admin').all()
            for claw in admin_claws:
                if claw.verify_token(token):
                    authorized = True
                    break

    if not authorized:
        return jsonify({'error': '仅超级管理员或管理员 OpenClaw 可查询'}), 403

    status_filter = request.args.get('status', 'all')

    # 查所有 init 任务
    q = ClawTodo.query.filter_by(task_category='init')
    todos = q.order_by(ClawTodo.openclaw_id, ClawTodo.created_at).all()

    # 获取今天的执行记录
    today = d.today()
    today_logs = {}
    if todos:
        todo_ids = [t.id for t in todos]
        for log in ClawTodoLog.query.filter(
            ClawTodoLog.todo_id.in_(todo_ids),
            ClawTodoLog.log_date == today
        ).all():
            today_logs[log.todo_id] = log

    # 构建 OpenClaw 名称映射
    claw_ids = list({t.openclaw_id for t in todos})
    claws_map = {}
    if claw_ids:
        for c in OpenClawInstance.query.filter(OpenClawInstance.id.in_(claw_ids)).all():
            claws_map[c.id] = c.name

    # 按 OpenClaw 分组
    grouped = {}
    for t in todos:
        log = today_logs.get(t.id)
        is_done = (log and log.status == 'completed') or (not t.enabled and t.schedule_type == 'once')
        task_status = 'completed' if is_done else 'pending'

        if status_filter != 'all' and task_status != status_filter:
            continue

        claw_name = claws_map.get(t.openclaw_id, f'OpenClaw#{t.openclaw_id}')
        if claw_name not in grouped:
            grouped[claw_name] = {'claw_id': t.openclaw_id, 'tasks': [], 'completed': 0, 'total': 0}

        grouped[claw_name]['total'] += 1
        if is_done:
            grouped[claw_name]['completed'] += 1

        grouped[claw_name]['tasks'].append({
            'id': t.id,
            'title': t.title,
            'verification_target': t.verification_target,
            'urgency_level': t.urgency_level,
            'priority': t.priority,
            'enabled': t.enabled,
            'status': task_status,
            'completed_at': str(log.completed_at) if log and log.completed_at else None,
            'result_summary': log.result_summary if log else None,
            'created_at': str(t.created_at) if t.created_at else None,
        })

    # 汇总
    summary = {
        'total_claws': len(grouped),
        'total_tasks': sum(g['total'] for g in grouped.values()),
        'total_completed': sum(g['completed'] for g in grouped.values()),
    }
    summary['total_pending'] = summary['total_tasks'] - summary['total_completed']

    return jsonify({
        'summary': summary,
        'claws': grouped,
    })
