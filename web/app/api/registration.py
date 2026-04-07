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
