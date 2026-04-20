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


def _required_verification_targets(tasks):
    """返回配置中的必验 verification_target 列表（去空去重，保序）。"""
    seen = set()
    out = []
    for t in tasks or []:
        vt = (t.get('verification_target') or '').strip()
        if not vt or vt in seen:
            continue
        seen.add(vt)
        out.append(vt)
    return out


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
    existing_targets = {
        (t.verification_target or '').strip()
        for t in ClawTodo.query.filter_by(openclaw_id=claw_id, task_category='init').all()
    }
    created = 0
    for task in tasks:
        vt = (task.get('verification_target') or '').strip()
        if vt and vt in existing_targets:
            continue
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
        if vt:
            existing_targets.add(vt)
    db.session.commit()
    return jsonify({'message': f'已补发 {created} 个缺失初始化任务', 'count': created}), 201


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

    tasks_cfg = _get_init_tasks_config()
    required_targets = _required_verification_targets(tasks_cfg)

    # 查所有 init 任务
    q = ClawTodo.query.filter_by(task_category='init')
    todos = q.order_by(ClawTodo.openclaw_id, ClawTodo.created_at).all()

    # 获取每个 todo 的最新执行记录（不是只看今天）
    latest_logs = {}
    if todos:
        todo_ids = [t.id for t in todos]
        logs = (ClawTodoLog.query
                .filter(ClawTodoLog.todo_id.in_(todo_ids))
                .order_by(ClawTodoLog.todo_id.asc(),
                          ClawTodoLog.log_date.desc(),
                          ClawTodoLog.created_at.desc())
                .all())
        for log in logs:
            if log.todo_id not in latest_logs:
                latest_logs[log.todo_id] = log

    # 构建 OpenClaw 名称映射
    claw_ids = list({t.openclaw_id for t in todos})
    claws_map = {}
    if claw_ids:
        for c in OpenClawInstance.query.filter(OpenClawInstance.id.in_(claw_ids)).all():
            claws_map[c.id] = c.name

    # 按 OpenClaw 分组
    grouped = {}
    for t in todos:
        log = latest_logs.get(t.id)
        # 注册验收仅认 approved/completed，submitted 仍算未通过
        is_done = bool(log and log.status in ('approved', 'completed'))
        task_status = 'completed' if is_done else 'pending'

        if status_filter != 'all' and task_status != status_filter:
            continue

        claw_name = claws_map.get(t.openclaw_id, f'OpenClaw#{t.openclaw_id}')
        if claw_name not in grouped:
            grouped[claw_name] = {
                'claw_id': t.openclaw_id,
                'tasks': [],
                'completed': 0,
                'total': 0,
                'target_status': {},
                'required_targets': required_targets,
                'gate_passed': False,
            }

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
        vt = (t.verification_target or '').strip()
        if vt:
            grouped[claw_name]['target_status'][vt] = task_status

    for g in grouped.values():
        pending_required = [
            vt for vt in g['required_targets']
            if g['target_status'].get(vt) != 'completed'
        ]
        g['pending_required_targets'] = pending_required
        g['gate_passed'] = len(pending_required) == 0

    # 汇总
    summary = {
        'total_claws': len(grouped),
        'total_tasks': sum(g['total'] for g in grouped.values()),
        'total_completed': sum(g['completed'] for g in grouped.values()),
    }
    summary['total_pending'] = summary['total_tasks'] - summary['total_completed']
    summary['gate_passed_claws'] = sum(1 for g in grouped.values() if g['gate_passed'])
    summary['gate_pending_claws'] = summary['total_claws'] - summary['gate_passed_claws']

    return jsonify({
        'summary': summary,
        'claws': grouped,
    })
