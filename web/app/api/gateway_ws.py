"""
OpenClaw Gateway WebSocket 协议兼容模块
实现与 OpenClaw Gateway 协议兼容的 WebSocket 服务器

协议说明：
1. 握手：客户端发送 hello，服务器回复 hello-ok
2. req/res：客户端发送 req，服务器回复 res
3. event：服务器主动推送事件

事件类型：
- agent: 智能体运行输出
- presence: 在线状态更新
- tick: 心跳
- shutdown: 关闭通知
"""

import json
import uuid
import logging
from datetime import datetime
from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from threading import Thread, Lock
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)

# 全局 SocketIO 实例（延迟初始化）
socketio: Optional[SocketIO] = None

# 连接管理
class ConnectionManager:
    """管理 WebSocket 连接"""

    def __init__(self):
        self.connections: Dict[str, Dict[str, Any]] = {}  # session_id -> connection info
        self.lock = Lock()
        self._seq = 0

    def add_connection(self, session_id: str, info: Dict[str, Any]):
        with self.lock:
            self.connections[session_id] = {
                **info,
                'connected_at': datetime.utcnow(),
                'last_activity': datetime.utcnow(),
            }
        logger.info(f"连接已注册: {session_id}, 客户端: {info.get('display_name', 'unknown')}")

    def remove_connection(self, session_id: str):
        with self.lock:
            if session_id in self.connections:
                del self.connections[session_id]
        logger.info(f"连接已移除: {session_id}")

    def update_activity(self, session_id: str):
        with self.lock:
            if session_id in self.connections:
                self.connections[session_id]['last_activity'] = datetime.utcnow()

    def get_connection(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.connections.get(session_id)

    def list_connections(self) -> Dict[str, Dict[str, Any]]:
        with self.lock:
            return dict(self.connections)

    def get_next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq

    def get_all_session_ids(self) -> list:
        with self.lock:
            return list(self.connections.keys())


# 全局连接管理器
conn_manager = ConnectionManager()


def init_socketio(app):
    """初始化 SocketIO"""
    global socketio
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode='threading',
        logger=False,
        engineio_logger=False,
        message_queue=None,  # 不使用 Redis
        channel='gateway',
    )

    register_handlers(socketio)
    return socketio


def register_handlers(sio: SocketIO):
    """注册 WebSocket 事件处理器"""

    @sio.on('connect')
    def handle_connect():
        """处理连接"""
        logger.info("WebSocket 客户端已连接")

    @sio.on('disconnect')
    def handle_disconnect():
        """处理断开连接"""
        session_id = request.sid
        
        # 获取 claw_id 并更新状态为 offline
        conn_info = conn_manager.get_connection(session_id)
        if conn_info and conn_info.get('claw_id'):
            from app.models import OpenClawInstance
            from app import db
            try:
                claw = OpenClawInstance.query.get(conn_info['claw_id'])
                if claw:
                    claw.status = 'offline'
                    db.session.commit()
                    logger.info(f"OpenClaw {claw.id} ({claw.name}) 状态已设为 offline")
            except Exception as e:
                logger.error(f"更新 claw 状态失败: {e}")
                db.session.rollback()
        
        conn_manager.remove_connection(session_id)

    @sio.on('hello')
    def handle_hello(data: Dict[str, Any]):
        """
        握手协议：客户端发送 hello
        {
            "type": "hello",
            "version": "1.0",
            "auth": {
                "token": "..."
            },
            "client": {
                "type": "openclaw",      // 客户端类型
                "version": "3.13",
                "displayName": "我的OpenClaw"
            }
        }
        """
        session_id = request.sid
        version = data.get('version', '1.0')
        auth = data.get('auth', {})
        client = data.get('client', {})

        token = auth.get('token', '')
        display_name = client.get('displayName', 'Unknown')

        logger.info(f"收到 hello: version={version}, displayName={display_name}, token={token[:20]}...")

        # 验证 token（通过 OpenClawInstance 验证）
        from app.models import OpenClawInstance
        claw = OpenClawInstance.query.filter_by(api_token=token).first()

        if claw:
            # 认证成功
            conn_manager.add_connection(session_id, {
                'claw_id': claw.id,
                'display_name': display_name,
                'client_type': client.get('type', 'openclaw'),
                'version': client.get('version', 'unknown'),
                'token': token,
            })

            # 更新 claw 状态为 online
            claw.status = 'online'
            from app import db
            db.session.commit()

            # 发送 hello-ok
            response = {
                'type': 'hello-ok',
                'sessionId': session_id,
                'protocolVersion': '1.0',
                'server': {
                    'name': 'OpenClaw Hub',
                    'version': '1.0.0',
                    'capabilities': ['agent', 'file', 'command', 'config'],
                },
                'presence': {
                    'agents': list(conn_manager.list_connections().keys()),
                },
                'health': {
                    'status': 'ok',
                    'uptime': get_server_uptime(),
                    'timestamp': datetime.utcnow().isoformat(),
                }
            }

            emit('hello-ok', response)
            logger.info(f"hello-ok 已发送给 {session_id}")

        else:
            # 认证失败
            emit('hello-error', {
                'type': 'hello-error',
                'error': 'invalid_token',
                'message': 'Token 无效或已过期'
            })
            logger.warning(f"Token 验证失败: {token[:20]}...")
            request.sio.disconnect(request.sid)

    @sio.on('req')
    def handle_req(data: Dict[str, Any]):
        """
        请求/响应模式：客户端发送 req
        {
            "type": "req",
            "method": "agent|health|nodes.list|...",
            "id": "req-xxx",
            "params": {...}
        }
        """
        session_id = request.sid
        conn_manager.update_activity(session_id)

        method = data.get('method', '')
        req_id = data.get('id', '')
        params = data.get('params', {})

        logger.info(f"收到 req: method={method}, id={req_id}")

        try:
            if method == 'agent':
                result = handle_agent_method(params)
            elif method == 'health':
                result = handle_health_method(params)
            elif method == 'nodes.list':
                result = handle_nodes_list(params)
            elif method == 'nodes.status':
                result = handle_nodes_status(params)
            elif method == 'system-presence':
                result = handle_system_presence(params)
            else:
                result = {'error': f'Unknown method: {method}'}

            # 发送响应
            emit('res', {
                'type': 'res',
                'id': req_id,
                'status': 'ok' if 'error' not in result else 'error',
                'result': result,
            })

        except Exception as e:
            logger.exception(f"req 处理异常: {e}")
            emit('res', {
                'type': 'res',
                'id': req_id,
                'status': 'error',
                'error': str(e),
            })

    @sio.on('event')
    def handle_client_event(data: Dict[str, Any]):
        """
        客户端发送的事件（如 task 结果上报）
        {
            "type": "event",
            "event": "task.completed|task.failed|...",
            "payload": {...}
        }
        """
        session_id = request.sid
        conn_manager.update_activity(session_id)

        event_type = data.get('event', '')
        payload = data.get('payload', {})

        logger.info(f"收到客户端事件: event={event_type}, payload={payload}")

        if event_type == 'task.completed' or event_type == 'task.failed':
            handle_task_report(session_id, event_type, payload)


# ==================== 方法处理器 ====================

def handle_agent_method(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    agent 方法：运行智能体轮次
    OpenClaw 客户端调用此方法，Hub 转发任务给子 agent

    params: {
        "message": "用户消息",
        "sessionId": "会话ID",
        "options": {...}
    }
    """
    message = params.get('message', '')
    session_id = params.get('sessionId', '')

    if not message:
        return {'error': 'message is required'}

    # TODO: 将任务加入队列，等待子 agent 处理
    # 目前先返回 accepted，让客户端知道任务已被接收

    task_id = f"task_{uuid.uuid4().hex[:16]}"

    return {
        'runId': task_id,
        'status': 'accepted',
        'message': f'任务已接收: {task_id}',
    }


def handle_health_method(params: Dict[str, Any]) -> Dict[str, Any]:
    """health 方法：获取服务器健康状态"""
    return {
        'status': 'ok',
        'uptime': get_server_uptime(),
        'timestamp': datetime.utcnow().isoformat(),
        'connections': len(conn_manager.list_connections()),
    }


def handle_nodes_list(params: Dict[str, Any]) -> Dict[str, Any]:
    """nodes.list 方法：列出所有连接的节点"""
    connections = conn_manager.list_connections()

    nodes = []
    for sid, info in connections.items():
        nodes.append({
            'id': sid,
            'name': info.get('display_name', 'Unknown'),
            'status': 'online',
            'lastSeen': info.get('last_activity', datetime.utcnow()).isoformat() if isinstance(info.get('last_activity'), datetime) else str(info.get('last_activity', '')),
        })

    return {'nodes': nodes}


def handle_nodes_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """nodes.status 方法：获取节点状态"""
    node_id = params.get('nodeId') or params.get('node') or request.sid

    info = conn_manager.get_connection(node_id)
    if not info:
        return {'error': f'Node not found: {node_id}'}

    return {
        'nodeId': node_id,
        'status': 'online',
        'uptime': 0,  # 可以计算连接时长
        'capabilities': ['agent', 'file', 'command', 'config'],
    }


def handle_system_presence(params: Dict[str, Any]) -> Dict[str, Any]:
    """system-presence 方法：获取系统在线状态"""
    connections = conn_manager.list_connections()

    return {
        'presence': {
            sid: {
                'status': 'online',
                'displayName': info.get('display_name', 'Unknown'),
            }
            for sid, info in connections.items()
        }
    }


def handle_task_report(session_id: str, event_type: str, payload: Dict[str, Any]):
    """处理任务结果上报"""
    task_id = payload.get('task_id')
    status = 'completed' if event_type == 'task.completed' else 'failed'
    result = payload.get('result')
    error = payload.get('error')

    if task_id:
        # 更新任务状态
        from app.models import AgentTask
        from app import db

        task = AgentTask.query.filter_by(task_id=task_id).first()
        if task:
            task.status = status
            task.result = result
            task.error = error
            task.completed_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"任务 {task_id} 状态已更新: {status}")


# ==================== 辅助函数 ====================

_server_start_time = datetime.utcnow()

def get_server_uptime() -> int:
    """获取服务器运行时间（秒）"""
    delta = datetime.utcnow() - _server_start_time
    return int(delta.total_seconds())


# ==================== 事件推送 ====================

def push_event(session_id: str, event_type: str, payload: Dict[str, Any]):
    """
    向指定 session 推送事件
    用于 Hub 向 OpenClaw 推送任务
    """
    if socketio:
        seq = conn_manager.get_next_seq()
        socketio.emit('event', {
            'type': 'event',
            'event': event_type,
            'payload': payload,
            'seq': seq,
        }, room=session_id)
        logger.info(f"已推送事件 {event_type} 到 {session_id}")


def push_task_to_session(session_id: str, task_data: Dict[str, Any]):
    """
    推送任务到指定 session
    event: agent 表示智能体运行事件
    """
    push_event(session_id, 'agent', task_data)


def broadcast_to_all(event_type: str, payload: Dict[str, Any]):
    """广播事件到所有连接"""
    if socketio:
        socketio.emit('event', {
            'type': 'event',
            'event': event_type,
            'payload': payload,
        }, broadcast=True)


def send_heartbeat():
    """发送心跳到所有连接"""
    broadcast_to_all('tick', {
        'timestamp': datetime.utcnow().isoformat(),
    })
