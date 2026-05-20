"""
Agent Hub 通信中心 API
供 OpenClaw 实例之间通信使用
"""
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime
from app import db
from app.models import Agent, Message, Conversation, OpenClawInstance, ClawMessage, Project, hash_token, generate_agent_token

agent_hub_bp = Blueprint('agent_hub', __name__)


def _get_web_user():
    from app.api.skills import _get_current_user
    return _get_current_user()


def _collect_user_project_ids(user):
    project_ids = set()
    if not user:
        return project_ids
    for pid in (getattr(user, 'managed_projects', None) or []):
        try:
            project_ids.add(int(pid))
        except Exception:
            continue
    bound_claw_id = getattr(user, 'bound_claw_id', None)
    if bound_claw_id:
        claw = OpenClawInstance.query.get(bound_claw_id)
        if claw:
            if claw.project_id:
                project_ids.add(int(claw.project_id))
            elif claw.project_name:
                p = Project.query.filter_by(name=claw.project_name).first()
                if p:
                    project_ids.add(int(p.id))
    return project_ids


def _visible_claw_query_for_user(user):
    query = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if not user:
        return query.filter(OpenClawInstance.id == -1)
    if user.role == 'super_admin':
        return query
    query = query.filter(OpenClawInstance.role != 'admin')
    project_ids = list(_collect_user_project_ids(user))
    if project_ids:
        project_names = [p.name for p in Project.query.filter(Project.id.in_(project_ids)).all()]
        return query.filter(
            db.or_(
                OpenClawInstance.project_id.in_(project_ids),
                OpenClawInstance.project_name.in_(project_names) if project_names else db.text('1=0')
            )
        )
    if getattr(user, 'bound_claw_id', None):
        return query.filter(OpenClawInstance.id == int(user.bound_claw_id))
    return query.filter(OpenClawInstance.id == -1)

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
    data = request.get_json() or {}

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

    agent.last_activity = datetime.now()
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
        conv.last_message_at = datetime.now()
    else:
        conv = Conversation(
            agent_a_id=agent.id,
            agent_b_id=receiver_id,
            last_message_at=datetime.now(),
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
    msg.read_at = datetime.now()
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
    """Web 管理端 - 通信中心统计概览（以 OpenClawInstance 为准）"""
    user = _get_web_user()
    claw_query = _visible_claw_query_for_user(user)
    claws = claw_query.all()

    online_count = sum(1 for c in claws if c.status in ('工作', '学习', '摸鱼', 'online'))

    total_messages = Message.query.count()
    total_conversations = Conversation.query.count()
    unread_messages = Message.query.filter_by(status='unread').count()

    return jsonify({
        'total_agents': len(claws),
        'online_agents': online_count,
        'total_messages': total_messages,
        'total_conversations': total_conversations,
        'unread_messages': unread_messages,
    })


@agent_hub_bp.route('/web/agents', methods=['GET'])
def web_list_agents():
    """Web 管理端 - Agent 列表（以 OpenClawInstance 为准）"""
    user = _get_web_user()
    claw_query = _visible_claw_query_for_user(user)
    claws = claw_query.order_by(OpenClawInstance.name).all()

    result = []
    online_count = 0
    for c in claws:
        is_online = c.status in ('工作', '学习', '摸鱼', 'online')
        result.append({
            'id': c.id,
            'name': c.name,
            'agent_key': c.project_name or '',
            'role': c.role or 'module_owner',
            'project_name': c.project_name or '',
            'module_name': c.module_name or '',
            'status': 'online' if is_online else 'offline',
            'last_activity': str(c.last_activity) if c.last_activity else None,
        })
        if is_online:
            online_count += 1

    return jsonify({
        'agents': result,
        'total': len(claws),
        'online': online_count,
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
    msg.read_at = datetime.now()
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

    user = _get_web_user()
    visible_claw_ids = [c.id for c in _visible_claw_query_for_user(user).all()]
    query = ClawMessage.query.filter(ClawMessage.claw_id.in_(visible_claw_ids or [-1]))

    if claw_id:
        if claw_id not in visible_claw_ids:
            return jsonify({'error': '无权查看该 OpenClaw 消息'}), 403
        query = query.filter_by(claw_id=claw_id)
    if status:
        query = query.filter_by(status=status)
    if msg_type:
        query = query.filter_by(msg_type=msg_type)

    messages = query.order_by(ClawMessage.created_at.desc()).limit(limit).all()

    # 附加 OpenClaw 名称（接收方 + 发送方）
    result = []
    for m in messages:
        d = m.to_dict()
        claw = OpenClawInstance.query.get(m.claw_id)
        d['claw_name'] = claw.name if claw else '未知'
        d['claw_status'] = claw.status if claw else 'unknown'
        if m.from_claw_id:
            from_claw = OpenClawInstance.query.get(m.from_claw_id)
            d['from_claw_name'] = from_claw.name if from_claw else '未知'
        result.append(d)

    return jsonify({
        'messages': result,
        'count': len(result),
    })


def _claw_name_map(ids):
    if not ids:
        return {}
    claws = OpenClawInstance.query.filter(OpenClawInstance.id.in_(list(ids))).all()
    return {c.id: c.name for c in claws}


def _claw_name_id_map():
    claws = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted').all()
    return {c.name: c.id for c in claws if c.name}


def _inferred_from_claw_id(m, name_to_id=None):
    """Infer sender claw when historical rows have from_claw_id set to receiver.

    Some older Web/Admin-triggered rows were stored as
    claw_id=<receiver>, from_claw_id=<receiver>, sender_name=<real claw>.
    The communication center should display those by the real sender name.
    """
    if m.from_claw_id and m.from_claw_id != m.claw_id:
        return m.from_claw_id
    name_to_id = name_to_id or {}
    sender_id = name_to_id.get(m.sender_name or '')
    if sender_id and sender_id != m.claw_id:
        return sender_id
    return None


def _serialize_claw_message(m, name_map=None, name_to_id=None):
    name_map = name_map or {}
    inferred_from_id = _inferred_from_claw_id(m, name_to_id)
    d = m.to_dict()
    d['claw_name'] = name_map.get(m.claw_id) or (m.claw.name if m.claw else '未知')
    d['display_from_claw_id'] = inferred_from_id
    if inferred_from_id:
        d['from_claw_name'] = name_map.get(inferred_from_id) or m.sender_name
    else:
        d['from_claw_name'] = (
            name_map.get(m.from_claw_id)
            or (m.from_claw.name if m.from_claw else None)
            if m.from_claw_id else None
        )
    return d


@agent_hub_bp.route('/web/claw-conversations', methods=['GET'])
def web_list_claw_conversations():
    """Web 管理端 - 通信中心会话列表。

    分组：
    - admin: Web/Admin 与某个 claw 的消息来往
    - claw_pair: 两个 claw 之间的对话记录
    """
    user = _get_web_user()
    visible_ids = [c.id for c in _visible_claw_query_for_user(user).all()]
    if not visible_ids:
        return jsonify({'admin_conversations': [], 'claw_conversations': []})

    limit = min(max(request.args.get('limit', 500, type=int), 1), 2000)
    messages = (ClawMessage.query
                .filter(ClawMessage.claw_id.in_(visible_ids))
                .order_by(ClawMessage.created_at.desc())
                .limit(limit)
                .all())
    all_ids = set(visible_ids)
    for m in messages:
        if m.from_claw_id:
            all_ids.add(m.from_claw_id)
    name_map = _claw_name_map(all_ids)
    name_to_id = _claw_name_id_map()

    admin_map = {}
    pair_map = {}
    for m in messages:
        inferred_from_id = _inferred_from_claw_id(m, name_to_id)
        if inferred_from_id:
            if inferred_from_id not in visible_ids and m.claw_id not in visible_ids:
                continue
            if inferred_from_id not in name_map:
                inferred_claw = OpenClawInstance.query.get(inferred_from_id)
                name_map[inferred_from_id] = inferred_claw.name if inferred_claw else f'OpenClaw#{inferred_from_id}'
            a, b = sorted([int(inferred_from_id), int(m.claw_id)])
            key = f'{a}:{b}'
            bucket = pair_map.setdefault(key, {
                'type': 'claw_pair',
                'key': key,
                'claw_a_id': a,
                'claw_b_id': b,
                'claw_a_name': name_map.get(a, f'OpenClaw#{a}'),
                'claw_b_name': name_map.get(b, f'OpenClaw#{b}'),
                'message_count': 0,
                'last_message': None,
            })
        else:
            key = str(m.claw_id)
            bucket = admin_map.setdefault(key, {
                'type': 'admin',
                'key': key,
                'claw_id': m.claw_id,
                'claw_name': name_map.get(m.claw_id, f'OpenClaw#{m.claw_id}'),
                'message_count': 0,
                'last_message': None,
            })

        bucket['message_count'] += 1
        if bucket['last_message'] is None:
            bucket['last_message'] = _serialize_claw_message(m, name_map, name_to_id)

    admin_convs = sorted(
        admin_map.values(),
        key=lambda x: (x['last_message'] or {}).get('created_at') or '',
        reverse=True,
    )
    claw_convs = sorted(
        pair_map.values(),
        key=lambda x: (x['last_message'] or {}).get('created_at') or '',
        reverse=True,
    )
    return jsonify({
        'admin_conversations': admin_convs,
        'claw_conversations': claw_convs,
        'total': len(admin_convs) + len(claw_convs),
    })


@agent_hub_bp.route('/web/claw-conversation-messages', methods=['GET'])
def web_list_claw_conversation_messages():
    """Web 管理端 - 某个通信中心会话的明细消息。"""
    user = _get_web_user()
    visible_ids = [c.id for c in _visible_claw_query_for_user(user).all()]
    kind = request.args.get('type') or 'admin'
    limit = min(max(request.args.get('limit', 200, type=int), 1), 500)

    if kind == 'admin':
        claw_id = request.args.get('claw_id', type=int)
        if not claw_id:
            return jsonify({'error': 'claw_id 必填'}), 400
        if claw_id not in visible_ids:
            return jsonify({'error': '无权查看该会话'}), 403
        query = ClawMessage.query.filter(
            ClawMessage.claw_id == claw_id,
            db.or_(
                ClawMessage.from_claw_id.is_(None),
                ClawMessage.from_claw_id == claw_id,
            )
        )
        ids = {claw_id}
    elif kind == 'claw_pair':
        claw_a_id = request.args.get('claw_a_id', type=int)
        claw_b_id = request.args.get('claw_b_id', type=int)
        if not claw_a_id or not claw_b_id:
            return jsonify({'error': 'claw_a_id / claw_b_id 必填'}), 400
        if claw_a_id not in visible_ids and claw_b_id not in visible_ids:
            return jsonify({'error': '无权查看该会话'}), 403
        name_map = _claw_name_map({claw_a_id, claw_b_id})
        name_a = name_map.get(claw_a_id)
        name_b = name_map.get(claw_b_id)
        query = ClawMessage.query.filter(db.or_(
            db.and_(ClawMessage.from_claw_id == claw_a_id, ClawMessage.claw_id == claw_b_id),
            db.and_(ClawMessage.from_claw_id == claw_b_id, ClawMessage.claw_id == claw_a_id),
            db.and_(ClawMessage.claw_id == claw_a_id, ClawMessage.sender_name == name_b),
            db.and_(ClawMessage.claw_id == claw_b_id, ClawMessage.sender_name == name_a),
        ))
        ids = {claw_a_id, claw_b_id}
    else:
        return jsonify({'error': 'type 只能是 admin 或 claw_pair'}), 400

    messages = query.order_by(ClawMessage.created_at.asc()).limit(limit).all()
    name_map = _claw_name_map(ids)
    name_to_id = _claw_name_id_map()
    return jsonify({
        'messages': [_serialize_claw_message(m, name_map, name_to_id) for m in messages],
        'count': len(messages),
    })


def _schedule_ai_replies(claws, user_content, sender_name):
    """为聊天消息生成AI回复（同步执行，避免 gevent 环境下异步协程被回收）

    gevent worker 下 spawn/spawn_later/threading 在请求结束后都无法执行，
    因此改为在请求上下文内同步调用 LLM 并写入回复。
    """
    from sqlalchemy import text

    claw_ids = [c.id for c in claws]
    current_app.logger.info(f"[AI_REPLY] 开始生成AI回复: claw_ids={claw_ids}, sender={sender_name}")

    try:
        # 读取LLM配置
        rows = dict(db.session.execute(
            text("SELECT config_key, value FROM system_config")
        ).fetchall())
        api_key = rows.get('llm_api_key', '')
        if not api_key:
            current_app.logger.info("[AI_REPLY] LLM API Key 未配置，跳过自动回复")
            return

        provider = rows.get('llm_provider', 'doubao')
        model = rows.get('llm_model', 'doubao-pro-32k')
        api_base = rows.get('llm_api_base', 'https://ark.cn-beijing.volces.com/api/v3')
        current_app.logger.info(f"[AI_REPLY] LLM 配置: provider={provider}, model={model}")

        for cid in claw_ids:
            try:
                claw = OpenClawInstance.query.get(cid)
                if not claw:
                    continue
                # 构造角色感知的prompt
                claw_name = claw.name
                claw_role = claw.role or 'module_owner'
                claw_module = claw.module_name or ''
                claw_project = claw.project_name or ''

                prompt = f"""你是 {claw_name}，一个游戏测试AI助手（OpenClaw）。
你的角色：{claw_role}
所属项目：{claw_project or '未指定'}
负责模块：{claw_module or '未指定'}

用户「{sender_name}」对你说：{user_content}

请简短回复（2-3句话），体现你的角色特点。用中文回复。"""

                reply_content = _call_llm_system(prompt, provider, model, api_key, api_base)

                # 写入回复消息
                reply_msg = ClawMessage(
                    claw_id=claw.id,
                    sender_name=claw_name,
                    content=reply_content,
                    msg_type='chat',
                    direction='from_claw',
                    status='delivered',
                    delivered_at=datetime.now(),
                )
                db.session.add(reply_msg)
                db.session.commit()
                current_app.logger.info(f"[AI_REPLY] 已为 {claw_name} 生成自动回复")
            except Exception as e:
                current_app.logger.error(f"[AI_REPLY] 生成 claw_id={cid} 回复失败: {e}")
                db.session.rollback()

    except Exception as e:
        current_app.logger.error(f"[AI_REPLY] AI回复异常: {e}")


def _call_llm_system(prompt, provider, model, api_key, api_base):
    """调用 system.py 的 LLM 接口"""
    from app.api.system import _call_llm
    return _call_llm(prompt, provider, model, api_base, api_key)


@agent_hub_bp.route('/web/broadcast', methods=['POST'])
def web_broadcast():
    """Web 管理端 - 广播消息（支持 Agent 和 OpenClaw）

    通信中心主要给 OpenClaw 用：
    - target_claw_ids: 指定发送给哪些 OpenClaw
    - target_agent_ids: 指定发送给哪些 Agent
    - 如果都为空，默认发送给所有在线 OpenClaw

    支持 Bearer Token 认证（admin claw 等同超级管理员权限）
    """
    data = request.get_json()
    content = data.get('content')
    user = _get_web_user()
    if not user:
        return jsonify({'error': '未登录'}), 401

    if not content:
        return jsonify({'error': '内容不能为空'}), 400

    # 识别发送者：Web session 或 Bearer Token（admin claw）
    sender_name = 'Web Admin'
    from_claw_id = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        from app.models import OpenClawInstance as _OCI
        for _claw in _OCI.query.filter(_OCI.status != 'deleted').all():
            if _claw.verify_token(token):
                sender_name = _claw.name
                from_claw_id = _claw.id
                break

    target_agent_ids = data.get('target_agent_ids', [])
    target_claw_ids = data.get('target_claw_ids', [])
    msg_type = data.get('msg_type', 'broadcast')
    visible_claw_ids = [c.id for c in _visible_claw_query_for_user(user).all()]

    # 确保 target_claw_ids 是整数列表（JSON 反序列化后可能是字符串）
    if target_claw_ids:
        target_claw_ids = [int(x) for x in target_claw_ids]
        denied = [cid for cid in target_claw_ids if cid not in visible_claw_ids]
        if denied:
            return jsonify({'error': '包含无权限目标 OpenClaw'}), 403

    sent_agents = 0
    sent_claws = 0

    # 发送给 OpenClaw（通信中心主要使用场景）
    offline_claws = []
    chat_claws = []  # 需要AI自动回复的claw列表
    notified_claw_ids = []  # 需要通知 SSE 即时推送的 claw_id 列表
    if target_claw_ids:
        # B+ 修复：离线 claw 也无差别落库（status=pending），等 SSE 重连补推；
        # 不再静默丢失，避免「消息发出去但 claw 永远收不到」的隐性故障。
        claws = OpenClawInstance.query.filter(OpenClawInstance.id.in_(target_claw_ids)).all()
        for claw in claws:
            is_online = claw.status in ('工作', '学习', '摸鱼', 'online')
            claw_msg = ClawMessage(
                claw_id=claw.id,
                from_claw_id=from_claw_id,
                sender_name=sender_name,
                content=content,
                msg_type=msg_type,
                direction='to_claw',
                status='pending',  # 在线/离线都先 pending；SSE 推送后改 delivered
            )
            db.session.add(claw_msg)
            sent_claws += 1
            if is_online:
                notified_claw_ids.append(claw.id)
                # 聊天类消息需要AI自动回复
                if msg_type in ('chat', 'text', 'broadcast'):
                    chat_claws.append(claw)
            else:
                offline_claws.append(claw.name)  # 仅记录，消息已落库等待重连补推
        if offline_claws:
            current_app.logger.info(
                f"以下 OpenClaw 不在线但消息已落库等待补推: {offline_claws}")
    elif not target_agent_ids:
        # 全员通知（默认发送给所有在线 OpenClaw）
        online_claws = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(visible_claw_ids or [-1]),
            OpenClawInstance.status.in_(['工作', '学习', '摸鱼', 'online'])
        ).all()
        for claw in online_claws:
            claw_msg = ClawMessage(
                claw_id=claw.id,
                sender_name=sender_name,
                content=content,
                msg_type=msg_type,
                direction='to_claw',
                status='pending',  # 先设pending，SSE推送到客户端后再改delivered
            )
            db.session.add(claw_msg)
            sent_claws += 1
            notified_claw_ids.append(claw.id)
            if msg_type in ('chat', 'text', 'broadcast'):
                chat_claws.append(claw)

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

    # 任务委派：同时创建待办任务
    delegate_claw_ids = set()
    if msg_type == 'task_delegate' and notified_claw_ids:
        from app.models import ClawTodo
        delegate_claws = OpenClawInstance.query.filter(
            OpenClawInstance.id.in_(notified_claw_ids),
            OpenClawInstance.status.in_(['工作', '学习', '摸鱼', 'online'])
        ).all()
        for claw in delegate_claws:
            todo = ClawTodo(
                openclaw_id=claw.id,
                title=content[:200] if content else '任务委派',
                description=content,
                schedule_type='once',
                urgency_level='flexible',
                enabled=True,
                created_by=sender_name,
            )
            db.session.add(todo)
            delegate_claw_ids.add(claw.id)
            current_app.logger.info(f"[TASK_DELEGATE] 已为 OpenClaw {claw.name} 创建待办: {content[:50]}")

    db.session.commit()

    # 通知所有收到消息的 claw SSE 长连接立即推送
    from app.api.agent_client import notify_claw, notify_claw_todo
    for cid in notified_claw_ids:
        if cid in delegate_claw_ids:
            notify_claw_todo(cid)  # 收到待办委派的 claw，触发待办列表即时推送
        else:
            notify_claw(cid)

    parts = []
    if sent_claws > 0:
        parts.append(f'{sent_claws} 个 OpenClaw')
    if sent_agents > 0:
        parts.append(f'{sent_agents} 个 Agent')

    if parts:
        msg = f'发送成功，已发送给 {", ".join(parts)}'
        if offline_claws:
            msg += f'（{len(offline_claws)} 个离线已跳过: {", ".join(offline_claws)}）'
        return jsonify({
            'message': msg,
            'sent_agents': sent_agents,
            'sent_claws': sent_claws,
            'offline_claws': offline_claws,
        })
    else:
        return jsonify({
            'message': '没有在线的接收者',
            'sent_agents': 0,
            'sent_claws': 0,
        })