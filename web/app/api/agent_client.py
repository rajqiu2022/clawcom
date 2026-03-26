"""
OpenClaw 子agent插件 - Manager端
部署在 openclaw-manager 服务器上

功能：
1. 接收子agent的SSE长连接
2. 下发任务给子agent（修改配置、执行命令、更新文件等）
3. 接收子agent的任务执行结果

API设计：
- GET  /api/openclaws/<id>/events     - SSE端点，子agent连接接收任务
- POST /api/openclaws/<id>/report    - 子agent上报任务结果
- POST /api/openclaws/<id>/dispatch  - 派发任务给指定claw (Manager管理接口)
"""

from datetime import datetime
from flask import request, jsonify, Response, stream_with_context, Blueprint
from app import db
from app.models import OpenClawInstance
from functools import wraps
import json
import time
import hashlib

# 创建蓝图（注意：这里不再使用 api_bp 前缀，因为已在 __init__.py 中单独注册）
agent_bp = Blueprint('agent_client', __name__, url_prefix='/api/openclaws')


def require_claw_token(f):
    """OpenClaw API Token 认证装饰器"""
    @wraps(f)
    def decorated(claw_id, *args, **kwargs):
        claw = OpenClawInstance.query.get_or_404(claw_id)
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401
        token = auth_header[7:]
        if not claw.verify_token(token):
            return jsonify({'error': 'Token 无效'}), 403
        return f(claw_id, claw=claw, *args, **kwargs)
    return decorated


class AgentTask(db.Model):
    """子agent任务队列"""
    __tablename__ = 'agent_tasks'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    claw_id = db.Column(db.Integer, db.ForeignKey('openclaw_instances.id'), nullable=False)
    task_id = db.Column(db.String(64), unique=True, nullable=False, comment='全局唯一任务ID')
    task_type = db.Column(db.String(50), nullable=False, comment='任务类型')
    command = db.Column(db.String(255), comment='操作命令')
    target_path = db.Column(db.String(500), comment='目标文件路径')
    payload = db.Column(db.Text, comment='操作内容(JSON)')  # JSON字符串
    status = db.Column(db.String(20), default='pending', comment='pending/running/completed/failed')
    result = db.Column(db.Text, comment='执行结果')
    error = db.Column(db.Text, comment='错误信息')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    assigned_at = db.Column(db.DateTime, comment='分配时间')
    completed_at = db.Column(db.DateTime, comment='完成时间')

    claw = db.relationship('OpenClawInstance', backref='tasks')

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'claw_id': self.claw_id,
            'task_type': self.task_type,
            'command': self.command,
            'target_path': self.target_path,
            'payload': json.loads(self.payload) if self.payload else None,
            'status': self.status,
            'result': self.result,
            'error': self.error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'assigned_at': self.assigned_at.isoformat() if self.assigned_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


@agent_bp.route('/<int:claw_id>/events', methods=['GET'])
@require_claw_token
def claw_sse_events(claw_id, claw=None):
    """
    SSE 端点 - 子agent 长连接接收任务

    事件格式：
    - event: connected\ndata: {claw_id, name}\n\n
    - event: heartbeat\ndata: {server_time}\n\n
    - event: task\ndata: {task_id, task_type, command, target_path, payload}\n\n
    - event: ping\ndata: \n\n
    """
    def generate():
        # 发送连接成功事件
        yield f"event: connected\ndata: {json.dumps({'claw_id': claw.id, 'name': claw.name, 'server_time': datetime.utcnow().isoformat()})}\n\n"

        last_task_check = time.time()

        while True:
            try:
                # 每5秒发送一次心跳，并检查新任务
                tasks = AgentTask.query.filter(
                    AgentTask.claw_id == claw_id,
                    AgentTask.status == 'pending'
                ).order_by(AgentTask.created_at.asc()).limit(10).all()

                now = time.time()
                if now - last_task_check > 5:
                    # 发送心跳
                    yield f"event: heartbeat\ndata: {json.dumps({'server_time': datetime.utcnow().isoformat()})}\n\n"

                    # 发送待处理任务
                    for task in tasks:
                        task.status = 'running'
                        task.assigned_at = datetime.utcnow()
                        db.session.commit()

                        yield f"event: task\ndata: {json.dumps(task.to_dict())}\n\n"

                    last_task_check = now

                # 发送 ping 保持连接
                yield f"event: ping\ndata: \n\n"

                time.sleep(3)

            except GeneratorExit:
                # 客户端断开连接
                break
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@agent_bp.route('/<int:claw_id>/report', methods=['POST'])
@require_claw_token
def claw_task_report(claw_id, claw=None):
    """
    子agent上报任务执行结果

    请求体：
    {
        "task_id": "xxx",
        "status": "completed/failed",
        "result": "执行结果",
        "error": "错误信息（可选）"
    }
    """
    data = request.get_json()
    if not data or not data.get('task_id'):
        return jsonify({'error': 'task_id 为必填项'}), 400

    task = AgentTask.query.filter_by(task_id=data['task_id'], claw_id=claw_id).first()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    task.status = data.get('status', 'completed')
    task.result = data.get('result')
    task.error = data.get('error')
    task.completed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'status': 'ok', 'task': task.to_dict()})


@agent_bp.route('/<int:claw_id>/dispatch', methods=['POST'])
def dispatch_task_to_claw(claw_id):
    """
    派发任务给指定子agent（Manager管理接口）

    请求体：
    {
        "task_type": "modify_config|execute_command|update_file|read_file",
        "command": "具体命令",
        "target_path": "/path/to/file",
        "payload": {...}  # JSON格式的操作内容
    }
    """
    claw = OpenClawInstance.query.get_or_404(claw_id)
    data = request.get_json()

    if not data or not data.get('task_type'):
        return jsonify({'error': 'task_type 为必填项'}), 400

    # 生成唯一任务ID
    import secrets
    task_id = f"task_{int(time.time())}_{secrets.token_hex(8)}"

    task = AgentTask(
        claw_id=claw_id,
        task_id=task_id,
        task_type=data['task_type'],
        command=data.get('command'),
        target_path=data.get('target_path'),
        payload=json.dumps(data.get('payload', {})),
        status='pending',
    )
    db.session.add(task)
    db.session.commit()

    return jsonify({
        'status': 'dispatched',
        'task': task.to_dict()
    }), 201


@agent_bp.route('/<int:claw_id>/tasks', methods=['GET'])
def list_claw_tasks(claw_id):
    """查询指定claw的任务列表"""
    claw = OpenClawInstance.query.get_or_404(claw_id)

    status = request.args.get('status')  # pending/running/completed/failed
    limit = request.args.get('limit', 50, type=int)

    query = AgentTask.query.filter_by(claw_id=claw_id)
    if status:
        query = query.filter_by(status=status)

    tasks = query.order_by(AgentTask.created_at.desc()).limit(limit).all()

    return jsonify([t.to_dict() for t in tasks])


@agent_bp.route('/<int:claw_id>/tasks/<task_id>', methods=['GET'])
def get_claw_task(claw_id, task_id):
    """查询指定claw的指定任务"""
    claw = OpenClawInstance.query.get_or_404(claw_id)
    task = AgentTask.query.filter_by(claw_id=claw_id, task_id=task_id).first_or_404()

    return jsonify(task.to_dict())
