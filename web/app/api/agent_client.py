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
from app.models import OpenClawInstance, ClawMessage
from functools import wraps
import json
import time
import hashlib
import logging

logger = logging.getLogger(__name__)

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
            'created_at': str(self.created_at) if self.created_at else None,
            'assigned_at': str(self.assigned_at) if self.assigned_at else None,
            'completed_at': str(self.completed_at) if self.completed_at else None,
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
    # 连接时更新状态为 online
    claw.status = 'online'
    claw.last_heartbeat = datetime.utcnow()
    db.session.commit()
    logger.info(f"SSE连接建立，OpenClaw {claw_id} ({claw.name}) 状态已设为 online")

    def generate():
        # 发送连接成功事件
        yield f"event: connected\ndata: {json.dumps({'claw_id': claw.id, 'name': claw.name, 'server_time': datetime.utcnow().isoformat()})}\n\n"

        last_task_check = time.time()

        while True:
            try:
                # 每5秒发送一次心跳，并检查新任务和新消息
                tasks = AgentTask.query.filter(
                    AgentTask.claw_id == claw_id,
                    AgentTask.status == 'pending'
                ).order_by(AgentTask.created_at.asc()).limit(10).all()

                # 检查待发送的消息
                messages = ClawMessage.query.filter(
                    ClawMessage.claw_id == claw_id,
                    ClawMessage.status == 'pending'
                ).order_by(ClawMessage.created_at.asc()).limit(10).all()

                now = time.time()
                if now - last_task_check > 5:
                    # 更新心跳时间
                    claw.last_heartbeat = datetime.utcnow()
                    db.session.commit()

                    # 发送心跳
                    yield f"event: heartbeat\ndata: {json.dumps({'server_time': datetime.utcnow().isoformat()})}\n\n"

                    # 发送待处理任务
                    for task in tasks:
                        task.status = 'running'
                        task.assigned_at = datetime.utcnow()
                        db.session.commit()

                        yield f"event: task\ndata: {json.dumps(task.to_dict())}\n\n"

                    # 发送待处理消息
                    for msg in messages:
                        msg.status = 'delivered'
                        msg.delivered_at = datetime.utcnow()
                        db.session.commit()

                        yield f"event: message\ndata: {json.dumps(msg.to_dict())}\n\n"

                    last_task_check = now

                # 发送 ping 保持连接
                yield f"event: ping\ndata: \n\n"

                time.sleep(3)

            except GeneratorExit:
                # 客户端断开连接，更新状态为 offline
                try:
                    claw.status = 'offline'
                    db.session.commit()
                    logger.info(f"SSE断开，OpenClaw {claw_id} ({claw.name}) 状态已设为 offline")
                except Exception as e:
                    logger.error(f"更新 claw 状态失败: {e}")
                    db.session.rollback()
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


# ==================== OpenClaw 消息 API ====================

@agent_bp.route('/<int:claw_id>/messages', methods=['POST'])
@require_claw_token
def claw_send_message(claw_id, claw=None):
    """
    OpenClaw 发送消息给 Web 管理端

    请求体：
    {
        "content": "消息内容",
        "msg_type": "text|task_delegate|knowledge_share|request_help",
        "reply_to": 123  // 可选，回复某条消息的ID
    }
    """
    data = request.get_json()
    if not data or not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    msg = ClawMessage(
        claw_id=claw_id,
        sender_name=claw.name,
        content=data['content'],
        msg_type=data.get('msg_type', 'text'),
        direction='from_claw',
        reply_to=data.get('reply_to'),
        status='delivered',
        delivered_at=datetime.utcnow(),
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({'status': 'ok', 'message': msg.to_dict()}), 201


@agent_bp.route('/<int:claw_id>/messages', methods=['GET'])
@require_claw_token
def claw_get_messages(claw_id, claw=None):
    """
    OpenClaw 获取消息历史（双向）

    参数：
    - limit: 返回条数（默认50）
    - unread: true 只返回未读消息
    - direction: to_claw/from_claw 过滤方向
    """
    limit = request.args.get('limit', 50, type=int)
    unread = request.args.get('unread', 'false').lower() == 'true'
    direction = request.args.get('direction')

    query = ClawMessage.query.filter_by(claw_id=claw_id)

    if unread:
        query = query.filter(
            ClawMessage.direction == 'to_claw',
            ClawMessage.status.in_(['pending', 'delivered'])
        )
    if direction:
        query = query.filter_by(direction=direction)

    messages = query.order_by(ClawMessage.created_at.desc()).limit(limit).all()

    return jsonify({
        'messages': [m.to_dict() for m in messages],
        'count': len(messages),
    })


@agent_bp.route('/<int:claw_id>/messages/<int:msg_id>/read', methods=['PUT'])
@require_claw_token
def claw_mark_read(claw_id, msg_id, claw=None):
    """OpenClaw 标记消息已读"""
    msg = ClawMessage.query.filter_by(id=msg_id, claw_id=claw_id).first()
    if not msg:
        return jsonify({'error': '消息不存在'}), 404

    msg.status = 'read'
    msg.read_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'status': 'ok'})


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


# ==================== 轮询模式接口 ====================

@agent_bp.route('/<int:claw_id>/pending-tasks', methods=['GET'])
@require_claw_token
def get_pending_tasks_poll(claw_id, claw=None):
    """
    轮询模式专用接口 - OpenClaw 客户端定期轮询获取待处理任务

    请求方式：GET /api/openclaws/<claw_id>/pending-tasks
    认证：Authorization: Bearer <token>

    返回：
    {
        "has_tasks": true/false,
        "tasks": [...],  // 最多10条
        "server_time": "..."
    }
    """
    # 获取待处理任务
    tasks = AgentTask.query.filter(
        AgentTask.claw_id == claw_id,
        AgentTask.status == 'pending'
    ).order_by(AgentTask.created_at.asc()).limit(10).all()

    # 标记为 running
    for task in tasks:
        task.status = 'running'
        task.assigned_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'has_tasks': len(tasks) > 0,
        'tasks': [t.to_dict() for t in tasks],
        'server_time': datetime.utcnow().isoformat(),
    })


@agent_bp.route('/<int:claw_id>/poll', methods=['POST'])
@require_claw_token
def poll_task_result(claw_id, claw=None):
    """
    轮询模式 - 上报任务执行结果（兼容 MCP 模式）

    请求体：
    {
        "task_id": "xxx",
        "status": "completed/failed",
        "result": "执行结果",
        "error": "错误信息"
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
