"""
Agent Hub API - 核心接口
发消息、收消息、注册、心跳
"""
import json
from datetime import datetime
from functools import wraps
from flask import Blueprint, request, jsonify
from app import db
from app.models import Agent, Message, Conversation, generate_agent_token, hash_token

api_bp = Blueprint('api', __name__)


def require_agent_token(f):
    """Agent Token 认证装饰器
    从 Authorization Header 获取 Token，验证后添加 agent 到 kwargs
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401

        token = auth_header[7:]

        # 从所有 agent 中查找匹配的 token
        agent = None
        for a in Agent.query.all():
            if a.verify_token(token):
                agent = a
                break

        if not agent:
            return jsonify({'error': 'Token 无效'}), 403

        # 把 agent 添加到 kwargs
        kwargs['agent'] = agent
        return f(*args, **kwargs)
    return decorated


# ============================================
# Agent 注册与管理
# ============================================

@api_bp.route('/agents', methods=['GET'])
def list_agents():
    """获取所有 Agent（支持按状态/项目过滤）"""
    status = request.args.get('status')
    project = request.args.get('project')

    query = Agent.query
    if status:
        query = query.filter_by(status=status)
    if project:
        query = query.filter_by(project_name=project)

    agents = query.order_by(Agent.last_heartbeat.desc()).all()
    return jsonify([a.to_dict() for a in agents])


@api_bp.route('/agents/online', methods=['GET'])
def list_online_agents():
    """获取所有在线 Agent"""
    agents = Agent.query.filter_by(status='online').order_by(Agent.name).all()
    return jsonify([a.to_dict() for a in agents])


@api_bp.route('/agents', methods=['POST'])
def register_agent():
    """注册新 Agent，返回 API Token（仅一次）"""
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': 'name 为必填项'}), 400

    name = data['name']
    agent_key = data.get('agent_key', f"agent-{name.lower().replace(' ', '-')}")

    # 检查唯一性
    if Agent.query.filter_by(agent_key=agent_key).first():
        return jsonify({'error': f'Agent Key {agent_key} 已存在'}), 409

    # 生成 Token
    raw_token = generate_agent_token()

    agent = Agent(
        name=name,
        agent_key=agent_key,
        role=data.get('role', 'test_member'),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        status='online',
        api_token_hash=hash_token(raw_token),
        hub_url=data.get('hub_url'),
    )
    db.session.add(agent)
    db.session.commit()

    result = agent.to_dict()
    result['api_token'] = raw_token  # 仅此一次返回
    result['hub_url'] = request.url_root.rstrip('/')
    return jsonify(result), 201


@api_bp.route('/agents/<int:agent_id>', methods=['GET'])
def get_agent(agent_id):
    """获取 Agent 详情"""
    agent = Agent.query.get_or_404(agent_id)
    return jsonify(agent.to_dict())


@api_bp.route('/agents/<int:agent_id>', methods=['PUT'])
@require_agent_token
def update_agent(agent_id, agent=None):
    """更新 Agent 信息"""
    data = request.get_json()
    updatable = ['name', 'role', 'project_name', 'module_name', 'hub_url']
    for field in updatable:
        if field in data:
            setattr(agent, field, data[field])
    db.session.commit()
    return jsonify(agent.to_dict())


@api_bp.route('/agents/<int:agent_id>/heartbeat', methods=['POST'])
@require_agent_token
def heartbeat(agent_id, agent=None):
    """心跳上报，保持在线状态"""
    agent.status = 'online'
    agent.last_heartbeat = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok', 'agent_id': agent.id})


@api_bp.route('/agents/<int:agent_id>/status', methods=['PUT'])
@require_agent_token
def update_status(agent_id, agent=None):
    """更新在线状态（online/offline/busy）"""
    data = request.get_json()
    new_status = data.get('status')
    if new_status not in ('online', 'offline', 'busy'):
        return jsonify({'error': '无效状态'}), 400
    agent.status = new_status
    agent.last_heartbeat = datetime.utcnow()
    db.session.commit()
    return jsonify({'status': 'ok', 'new_status': new_status})


# ============================================
# 消息发送与接收
# ============================================

@api_bp.route('/messages', methods=['POST'])
@require_agent_token
def send_message(agent=None):
    """发送消息给指定 Agent（需 Token 认证）"""
    data = request.get_json()

    receiver_id = data.get('receiver_id')
    if not receiver_id:
        return jsonify({'error': 'receiver_id 为必填项'}), 400

    receiver = Agent.query.get(receiver_id)
    if not receiver:
        return jsonify({'error': f'接收者 Agent ID {receiver_id} 不存在'}), 404

    if not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    msg_type = data.get('msg_type', 'text')
    extra_data = data.get('metadata')

    message = Message(
        sender_id=agent.id,
        receiver_id=receiver_id,
        content=data['content'],
        msg_type=msg_type,
        extra_data=extra_data,
    )
    db.session.add(message)
    update_conversation(agent.id, receiver_id)
    db.session.commit()
    return jsonify(message.to_dict()), 201


@api_bp.route('/messages/to/<int:receiver_id>', methods=['POST'])
@require_agent_token
def send_to(receiver_id, agent=None):
    """发送消息给指定 Agent（路径方式）"""
    data = request.get_json()

    receiver = Agent.query.get(receiver_id)
    if not receiver:
        return jsonify({'error': f'接收者 Agent ID {receiver_id} 不存在'}), 404

    if not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    msg_type = data.get('msg_type', 'text')
    extra_data = data.get('metadata')

    message = Message(
        sender_id=agent.id,
        receiver_id=receiver_id,
        content=data['content'],
        msg_type=msg_type,
        extra_data=extra_data,
    )
    db.session.add(message)
    update_conversation(agent.id, receiver_id)
    db.session.commit()
    return jsonify(message.to_dict()), 201


def update_conversation(agent_a_id, agent_b_id):
    """更新两个 Agent 的会话索引"""
    # 确保 agent_a_id < agent_b_id（保持一致顺序）
    if agent_a_id > agent_b_id:
        agent_a_id, agent_b_id = agent_b_id, agent_a_id

    conv = Conversation.query.filter_by(
        agent_a_id=agent_a_id,
        agent_b_id=agent_b_id
    ).first()

    if conv:
        conv.last_message_at = datetime.utcnow()
    else:
        conv = Conversation(
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
        )
        db.session.add(conv)


@api_bp.route('/messages/inbox', methods=['GET'])
@require_agent_token
def get_inbox(agent=None):
    """获取当前 Agent 的收件箱"""
    messages = Message.query.filter_by(receiver_id=agent.id)\
        .order_by(Message.created_at.desc())\
        .limit(100)\
        .all()
    return jsonify([m.to_dict() for m in messages])


@api_bp.route('/messages/inbox/unread', methods=['GET'])
@require_agent_token
def get_unread_inbox(agent=None):
    """获取未读消息"""
    messages = Message.query.filter_by(
        receiver_id=agent.id,
        status='unread'
    ).order_by(Message.created_at.desc()).all()
    return jsonify([m.to_dict() for m in messages])


@api_bp.route('/messages/outbox', methods=['GET'])
@require_agent_token
def get_outbox(agent=None):
    """获取发件箱消息"""
    messages = Message.query.filter_by(sender_id=agent.id)\
        .order_by(Message.created_at.desc())\
        .limit(100)\
        .all()
    return jsonify([m.to_dict() for m in messages])


@api_bp.route('/messages/<int:msg_id>/read', methods=['PUT'])
@require_agent_token
def mark_read(msg_id, agent=None):
    """标记消息为已读"""
    message = Message.query.get_or_404(msg_id)
    if message.receiver_id != agent.id:
        return jsonify({'error': '无权操作'}), 403
    message.status = 'read'
    message.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify(message.to_dict())


@api_bp.route('/messages/mark-all-read', methods=['PUT'])
@require_agent_token
def mark_all_read(agent=None):
    """标记所有消息为已读"""
    Message.query.filter_by(receiver_id=agent.id, status='unread')\
        .update({'status': 'read', 'read_at': datetime.utcnow()})
    db.session.commit()
    return jsonify({'status': 'ok'})


@api_bp.route('/messages/conversation/<int:other_id>', methods=['GET'])
@require_agent_token
def get_conversation(other_id, agent=None):
    """获取与另一个 Agent 的对话历史"""
    # 确保顺序一致
    id1, id2 = sorted([agent.id, other_id])

    messages = Message.query.filter(
        db.or_(
            db.and_(Message.sender_id == id1, Message.receiver_id == id2),
            db.and_(Message.sender_id == id2, Message.receiver_id == id1)
        )
    ).order_by(Message.created_at.asc()).limit(200).all()

    return jsonify([m.to_dict() for m in messages])


# ============================================
# 会话列表
# ============================================

@api_bp.route('/conversations', methods=['GET'])
@require_agent_token
def get_conversation_list(agent=None):
    """获取当前 Agent 的会话列表（按最近消息排序）"""
    convs = Conversation.query.filter(
        db.or_(
            Conversation.agent_a_id == agent.id,
            Conversation.agent_b_id == agent.id
        )
    ).order_by(Conversation.last_message_at.desc()).all()

    result = []
    for c in convs:
        conv_dict = c.to_dict()
        # 确定对方 Agent
        other_id = c.agent_b_id if c.agent_a_id == agent.id else c.agent_a_id
        other = Agent.query.get(other_id)
        if other:
            conv_dict['other_agent'] = other.to_dict()
            # 未读消息数
            conv_dict['unread_count'] = Message.query.filter_by(
                receiver_id=agent.id,
                sender_id=other_id,
                status='unread'
            ).count()
        result.append(conv_dict)

    return jsonify(result)


# ============================================
# 广播 & 群发
# ============================================

@api_bp.route('/messages/broadcast', methods=['POST'])
@require_agent_token
def broadcast(agent=None):
    """广播消息给所有在线 Agent"""
    data = request.get_json()
    if not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    online_agents = Agent.query.filter_by(status='online').all()
    messages = []
    for target in online_agents:
        if target.id == agent.id:
            continue  # 不发给自己
        msg = Message(
            sender_id=agent.id,
            receiver_id=target.id,
            content=data['content'],
            msg_type=data.get('msg_type', 'text'),
            extra_data=data.get('metadata'),
        )
        db.session.add(msg)
        messages.append(msg)

    db.session.commit()
    return jsonify({
        'status': 'ok',
        'sent_count': len(messages),
        'messages': [m.to_dict() for m in messages]
    }), 201


@api_bp.route('/messages/project/<project_name>', methods=['POST'])
@require_agent_token
def send_to_project(project_name, agent=None):
    """发送给项目中所有在线 Agent"""
    data = request.get_json()
    if not data.get('content'):
        return jsonify({'error': 'content 为必填项'}), 400

    targets = Agent.query.filter_by(
        project_name=project_name,
        status='online'
    ).all()

    messages = []
    for target in targets:
        msg = Message(
            sender_id=agent.id,
            receiver_id=target.id,
            content=data['content'],
            msg_type=data.get('msg_type', 'text'),
            extra_data=data.get('metadata'),
        )
        db.session.add(msg)
        messages.append(msg)

    db.session.commit()
    return jsonify({
        'status': 'ok',
        'sent_count': len(messages),
        'messages': [m.to_dict() for m in messages]
    }), 201


# ============================================
# 统计 & 状态
# ============================================

@api_bp.route('/stats', methods=['GET'])
def get_stats():
    """获取 Hub 统计信息"""
    total = Agent.query.count()
    online = Agent.query.filter_by(status='online').count()
    busy = Agent.query.filter_by(status='busy').count()
    offline = Agent.query.filter_by(status='offline').count()

    unread = db.session.query(db.func.count(Message.id))\
        .filter_by(status='unread').scalar()

    return jsonify({
        'total_agents': total,
        'online': online,
        'busy': busy,
        'offline': offline,
        'total_unread': unread,
    })
