"""
Agent Hub 通信中心 API
供 OpenClaw 实例之间通信使用
"""
from functools import wraps
from flask import Blueprint, jsonify, request
from datetime import datetime
from app import db
from app.models import Agent, Message, Conversation, OpenClawInstance, ClawMessage, hash_token, generate_agent_token

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


# ============== Web 管理端点（管理员面板用，免 Agent Token） ==============

@agent_hub_bp.route('/web/stats', methods=['GET'])
def web_stats():
    """Web 管理端 - 通信中心统计概览"""
    total_agents = Agent.query.count()
    online_agents = Agent.query.filter_by(status='online').count()
    total_messages = Message.query.count()
    total_conversations = Conversation.query.count()
    unread_messages = Message.query.filter_by(status='unread').count()

    return jsonify({
        'total_agents': total_agents,
        'online_agents': online_agents,
        'total_messages': total_messages,
        'total_conversations': total_conversations,
        'unread_messages': unread_messages,
    })


@agent_hub_bp.route('/web/agents', methods=['GET'])
def web_list_agents():
    """Web 管理端 - Agent 列表（含在线状态）"""
    agents = Agent.query.order_by(Agent.status.desc(), Agent.name).all()
    return jsonify({
        'agents': [a.to_dict() for a in agents],
        'total': len(agents),
        'online': sum(1 for a in agents if a.status == 'online'),
    })


@agent_hub_bp.route('/web/agents', methods=['POST'])
def web_register_agent():
    """Web 管理端 - 注册新 Agent"""
    data = request.get_json()

    required = ['name', 'agent_key']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'缺少必填字段: {field}'}), 400

    if Agent.query.filter_by(name=data['name']).first():
        return jsonify({'error': 'Agent 名称已存在'}), 409
    if Agent.query.filter_by(agent_key=data['agent_key']).first():
        return jsonify({'error': 'Agent Key 已存在'}), 409

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
        'token': token,
    }), 201


@agent_hub_bp.route('/web/agents/<int:agent_id>', methods=['PUT'])
def web_update_agent(agent_id):
    """Web 管理端 - 更新 Agent"""
    agent = Agent.query.get_or_404(agent_id)
    data = request.get_json()

    for field in ['name', 'role', 'project_name', 'module_name',
                  'hub_url', 'status']:
        if field in data:
            setattr(agent, field, data[field])

    db.session.commit()
    return jsonify({'agent': agent.to_dict()})


@agent_hub_bp.route('/web/agents/<int:agent_id>', methods=['DELETE'])
def web_delete_agent(agent_id):
    """Web 管理端 - 删除 Agent"""
    agent = Agent.query.get_or_404(agent_id)

    # 删除关联消息
    Message.query.filter(
        (Message.sender_id == agent_id) | (Message.receiver_id == agent_id)
    ).delete()
    Conversation.query.filter(
        (Conversation.agent_a_id == agent_id) | (Conversation.agent_b_id == agent_id)
    ).delete()

    db.session.delete(agent)
    db.session.commit()
    return jsonify({'message': f'已删除 Agent: {agent.name}'})


@agent_hub_bp.route('/web/conversations', methods=['GET'])
def web_list_conversations():
    """Web 管理端 - 会话列表"""
    convs = Conversation.query.order_by(
        Conversation.last_message_at.desc()
    ).limit(50).all()

    result = []
    for conv in convs:
        d = conv.to_dict()
        # 获取最近一条消息
        last_msg = Message.query.filter(
            ((Message.sender_id == conv.agent_a_id) & (Message.receiver_id == conv.agent_b_id)) |
            ((Message.sender_id == conv.agent_b_id) & (Message.receiver_id == conv.agent_a_id))
        ).order_by(Message.created_at.desc()).first()
        if last_msg:
            d['last_message'] = {
                'content': last_msg.content[:80],
                'sender_name': last_msg.sender.name if last_msg.sender else None,
                'created_at': str(last_msg.created_at) if last_msg.created_at else None,
            }
        # 未读数
        d['unread_count'] = Message.query.filter(
            ((Message.sender_id == conv.agent_a_id) & (Message.receiver_id == conv.agent_b_id)) |
            ((Message.sender_id == conv.agent_b_id) & (Message.receiver_id == conv.agent_a_id))
        ).filter_by(status='unread').count()
        result.append(d)

    return jsonify({'conversations': result})


@agent_hub_bp.route('/web/messages', methods=['GET'])
def web_list_messages():
    """Web 管理端 - 消息列表（支持 agent_id 筛选）"""
    agent_id = request.args.get('agent_id', type=int)
    query = Message.query

    if agent_id:
        query = query.filter(
            (Message.sender_id == agent_id) | (Message.receiver_id == agent_id)
        )

    messages = query.order_by(Message.created_at.desc()).limit(100).all()
    return jsonify({
        'messages': [m.to_dict() for m in messages],
        'count': len(messages),
    })


@agent_hub_bp.route('/web/messages/<int:msg_id>/read', methods=['PUT'])
def web_mark_read(msg_id):
    """Web 管理端 - 标记消息已读"""
    msg = Message.query.get_or_404(msg_id)
    msg.status = 'read'
    msg.read_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': '已标记为已读'})


@agent_hub_bp.route('/web/claw-messages', methods=['GET'])
def web_list_claw_messages():
    """Web 管理端 - OpenClaw 消息列表（通信中心记录）

    支持筛选：
    - claw_id: 指定某个 OpenClaw
    - status: pending/delivered/read
    - msg_type: 消息类型
    - limit: 返回条数
    """
    claw_id = request.args.get('claw_id', type=int)
    status = request.args.get('status')
    msg_type = request.args.get('msg_type')
    limit = request.args.get('limit', 100, type=int)

    query = ClawMessage.query

    if claw_id:
        query = query.filter_by(claw_id=claw_id)
    if status:
        query = query.filter_by(status=status)
    if msg_type:
        query = query.filter_by(msg_type=msg_type)

    messages = query.order_by(ClawMessage.created_at.desc()).limit(limit).all()

    # 附加 OpenClaw 名称
    result = []
    for m in messages:
        d = m.to_dict()
        claw = OpenClawInstance.query.get(m.claw_id)
        d['claw_name'] = claw.name if claw else '未知'
        d['claw_status'] = claw.status if claw else 'unknown'
        result.append(d)

    return jsonify({
        'messages': result,
        'count': len(result),
    })


@agent_hub_bp.route('/web/broadcast', methods=['POST'])
def web_broadcast():
    """Web 管理端 - 广播消息（支持 Agent 和 OpenClaw）

    通信中心主要给 OpenClaw 用：
    - target_claw_ids: 指定发送给哪些 OpenClaw
    - target_agent_ids: 指定发送给哪些 Agent
    - 如果都为空，默认发送给所有在线 OpenClaw
    """
    data = request.get_json()
    content = data.get('content')

    if not content:
        return jsonify({'error': '内容不能为空'}), 400

    target_agent_ids = data.get('target_agent_ids', [])
    target_claw_ids = data.get('target_claw_ids', [])
    msg_type = data.get('msg_type', 'broadcast')

    sent_agents = 0
    sent_claws = 0

    # 发送给 OpenClaw（通信中心主要使用场景）
    if target_claw_ids:
        # 指定发送给某些 OpenClaw
        claws = OpenClawInstance.query.filter(OpenClawInstance.id.in_(target_claw_ids)).all()
        for claw in claws:
            claw_msg = ClawMessage(
                claw_id=claw.id,
                sender_name='Web Admin',
                content=content,
                msg_type=msg_type,
            )
            db.session.add(claw_msg)
            sent_claws += 1
    elif not target_agent_ids:
        # 全员通知（默认发送给所有在线 OpenClaw）
        online_claws = OpenClawInstance.query.filter_by(status='online').all()
        for claw in online_claws:
            claw_msg = ClawMessage(
                claw_id=claw.id,
                sender_name='Web Admin',
                content=content,
                msg_type=msg_type,
            )
            db.session.add(claw_msg)
            sent_claws += 1

    # 发送给 Agent（如果有指定）
    if target_agent_ids:
        agents = Agent.query.filter(Agent.id.in_(target_agent_ids)).all()
        for receiver in agents:
            msg = Message(
                sender_id=0,  # 0 = Web 管理员
                receiver_id=receiver.id,
                content=content,
                msg_type=msg_type,
                extra_data={'from': 'web_admin'},
            )
            db.session.add(msg)
            sent_agents += 1

    db.session.commit()

    parts = []
    if sent_claws > 0:
        parts.append(f'{sent_claws} 个 OpenClaw')
    if sent_agents > 0:
        parts.append(f'{sent_agents} 个 Agent')

    if parts:
        return jsonify({
            'message': f'广播成功，已发送给 {", ".join(parts)}',
            'sent_agents': sent_agents,
            'sent_claws': sent_claws,
        })
    else:
        return jsonify({
            'message': '没有在线的接收者',
            'sent_agents': 0,
            'sent_claws': 0,
        })