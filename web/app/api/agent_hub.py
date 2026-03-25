"""
Agent Hub 通信中心 API
供 OpenClaw 实例之间通信使用
"""
from flask import Blueprint, jsonify, request
from datetime import datetime
from app import db
from app.models import Agent, Message, Conversation, hash_token, generate_agent_token

agent_hub_bp = Blueprint('agent_hub', __name__)

# ============== 认证装饰器 ==============

def require_agent_token(f):
    """从 Authorization Header 获取 Token，验证后添加 agent 到 kwargs"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': '缺少认证 Token'}), 401
        token = auth_header[7:]
        agent = None
        for a in Agent.query.all():
            if a.verify_token(token):
                agent = a
                break
        if not agent:
            return jsonify({'error': 'Token 无效'}), 403
        kwargs['agent'] = agent
        return f(*args, **kwargs)
    return decorated


from functools import wraps


# ============== Agent 管理 ==============

@agent_hub_bp.route('/agents', methods=['POST'])
def register_agent():
    """注册新 Agent（需要 admin 角色）"""
    data = request.get_json()

    # 检查必填字段
    required = ['name', 'agent_key']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'缺少必填字段: {field}'}), 400

    # 检查是否已有同名或同 key
    if Agent.query.filter_by(name=data['name']).first():
        return jsonify({'error': 'Agent 名称已存在'}), 409
    if Agent.query.filter_by(agent_key=data['agent_key']).first():
        return jsonify({'error': 'Agent Key 已存在'}), 409

    # 只有 admin 可以注册
    if data.get('role') == 'admin':
        return jsonify({'error': '只有管理员才能注册 admin 角色'}), 403

    # 生成 Token
    token = generate_agent_token()
    token_hash = hash_token(token)

    agent = Agent(
        name=data['name'],
        agent_key=data['agent_key'],
        role=data.get('role', 'test_member'),
        project_name=data.get('project_name'),
        module_name=data.get('module_name'),
        hub_url=data.get('hub_url'),
        api_token_hash=token_hash,
        status='online',
    )

    db.session.add(agent)
    db.session.commit()

    return jsonify({
        'message': '注册成功',
        'agent': agent.to_dict(),
        'token': token,  # 只在注册时返回一次
    }), 201


@agent_hub_bp.route('/agents/<int:agent_id>/heartbeat', methods=['POST'])
@require_agent_token
def heartbeat(agent_id, agent=None):
    """更新心跳"""
    if agent.id != agent_id:
        return jsonify({'error': '无权操作'}), 403

    agent.last_heartbeat = datetime.utcnow()
    agent.status = 'online'
    db.session.commit()

    return jsonify({'message': '心跳更新成功'})


@agent_hub_bp.route('/agents/online', methods=['GET'])
@require_agent_token
def online_agents(agent=None):
    """获取在线 Agent 列表"""
    online = Agent.query.filter_by(status='online').all()
    return jsonify({
        'agents': [a.to_dict() for a in online],
        'count': len(online),
    })


@agent_hub_bp.route('/agents/<int:agent_id>', methods=['GET'])
@require_agent_token
def get_agent(agent_id, agent=None):
    """获取 Agent 信息"""
    target = Agent.query.get_or_404(agent_id)
    return jsonify({'agent': target.to_dict()})


# ============== 消息功能 ==============

@agent_hub_bp.route('/messages', methods=['POST'])
@require_agent_token
def send_message(agent=None):
    """发送消息"""
    data = request.get_json()

    receiver_id = data.get('receiver_id')
    content = data.get('content')

    if not receiver_id or not content:
        return jsonify({'error': '缺少 receiver_id 或 content'}), 400

    receiver = Agent.query.get(receiver_id)
    if not receiver:
        return jsonify({'error': '接收者不存在'}), 404

    msg = Message(
        sender_id=agent.id,
        receiver_id=receiver_id,
        content=content,
        msg_type=data.get('msg_type', 'text'),
        extra_data=data.get('metadata'),
    )

    # 更新会话时间
    conv = Conversation.query.filter(
        ((Conversation.agent_a_id == agent.id) & (Conversation.agent_b_id == receiver_id)) |
        ((Conversation.agent_a_id == receiver_id) & (Conversation.agent_b_id == agent.id))
    ).first()

    if conv:
        conv.last_message_at = datetime.utcnow()
    else:
        conv = Conversation(
            agent_a_id=agent.id,
            agent_b_id=receiver_id,
            last_message_at=datetime.utcnow(),
        )
        db.session.add(conv)

    db.session.add(msg)
    db.session.commit()

    return jsonify({
        'message': '发送成功',
        'msg': msg.to_dict(),
    }), 201


@agent_hub_bp.route('/messages/inbox', methods=['GET'])
@require_agent_token
def get_inbox(agent=None):
    """获取收件箱"""
    unread_only = request.args.get('unread', 'false').lower() == 'true'

    query = Message.query.filter_by(receiver_id=agent.id)
    if unread_only:
        query = query.filter_by(status='unread')

    messages = query.order_by(Message.created_at.desc()).all()

    return jsonify({
        'messages': [m.to_dict() for m in messages],
        'count': len(messages),
        'unread_count': Message.query.filter_by(receiver_id=agent.id, status='unread').count(),
    })


@agent_hub_bp.route('/messages/<int:msg_id>/read', methods=['PUT'])
@require_agent_token
def mark_read(msg_id, agent=None):
    """标记消息为已读"""
    msg = Message.query.get_or_404(msg_id)

    if msg.receiver_id != agent.id:
        return jsonify({'error': '无权操作'}), 403

    msg.status = 'read'
    msg.read_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': '已标记为已读'})


@agent_hub_bp.route('/messages/conversation/<int:other_id>', methods=['GET'])
@require_agent_token
def get_conversation(other_id, agent=None):
    """获取与某个 Agent 的对话历史"""
    messages = Message.query.filter(
        ((Message.sender_id == agent.id) & (Message.receiver_id == other_id)) |
        ((Message.sender_id == other_id) & (Message.receiver_id == agent.id))
    ).order_by(Message.created_at.asc()).all()

    return jsonify({
        'messages': [m.to_dict() for m in messages],
        'count': len(messages),
    })


@agent_hub_bp.route('/messages/broadcast', methods=['POST'])
@require_agent_token
def broadcast(agent=None):
    """广播消息（仅 admin）"""
    if agent.role != 'admin':
        return jsonify({'error': '只有管理员可以广播'}), 403

    data = request.get_json()
    content = data.get('content')

    if not content:
        return jsonify({'error': '内容不能为空'}), 400

    online_agents = Agent.query.filter_by(status='online').all()
    sent = 0

    for receiver in online_agents:
        if receiver.id == agent.id:
            continue
        msg = Message(
            sender_id=agent.id,
            receiver_id=receiver.id,
            content=content,
            msg_type='broadcast',
            extra_data=data.get('metadata'),
        )
        db.session.add(msg)
        sent += 1

    db.session.commit()

    return jsonify({
        'message': f'广播成功，已发送给 {sent} 个在线 Agent',
        'sent_count': sent,
    })


# ============== 统计 ==============

@agent_hub_bp.route('/stats', methods=['GET'])
@require_agent_token
def get_stats(agent=None):
    """获取通信中心统计"""
    total_agents = Agent.query.count()
    online_agents = Agent.query.filter_by(status='online').count()
    total_messages = Message.query.count()
    unread_messages = Message.query.filter_by(receiver_id=agent.id, status='unread').count()

    return jsonify({
        'total_agents': total_agents,
        'online_agents': online_agents,
        'total_messages': total_messages,
        'my_unread': unread_messages,
    })