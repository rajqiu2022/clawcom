"""
Agent Hub 通信中心 API
供 OpenClaw 实例之间通信使用
"""
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
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
    from app.api.skills import _get_current_user
    user = _get_current_user()
    claw_query = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if not user or user.role not in ('super_admin', 'admin'):
        claw_query = claw_query.filter(OpenClawInstance.role != 'admin')
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
    from app.api.skills import _get_current_user
    user = _get_current_user()
    claw_query = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if not user or user.role not in ('super_admin', 'admin'):
        claw_query = claw_query.filter(OpenClawInstance.role != 'admin')
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

    if not content:
        return jsonify({'error': '内容不能为空'}), 400

    # 识别发送者：Web session 或 Bearer Token
    sender_name = 'Web Admin'
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        from app.models import OpenClawInstance as _OCI
        for _claw in _OCI.query.filter(_OCI.status != 'deleted').all():
            if _claw.verify_token(token):
                sender_name = _claw.name
                break

    target_agent_ids = data.get('target_agent_ids', [])
    target_claw_ids = data.get('target_claw_ids', [])
    msg_type = data.get('msg_type', 'broadcast')

    # 确保 target_claw_ids 是整数列表（JSON 反序列化后可能是字符串）
    if target_claw_ids:
        target_claw_ids = [int(x) for x in target_claw_ids]

    sent_agents = 0
    sent_claws = 0

    # 发送给 OpenClaw（通信中心主要使用场景）
    offline_claws = []
    chat_claws = []  # 需要AI自动回复的claw列表
    notified_claw_ids = []  # 需要通知 SSE 即时推送的 claw_id 列表
    if target_claw_ids:
        # 指定发送给某些 OpenClaw（必须在线才能接收）
        claws = OpenClawInstance.query.filter(OpenClawInstance.id.in_(target_claw_ids)).all()
        for claw in claws:
            if claw.status not in ('工作', '学习', '摸鱼', 'online'):
                offline_claws.append(claw.name)
                continue
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
            # 聊天类消息需要AI自动回复
            if msg_type in ('chat', 'text', 'broadcast'):
                chat_claws.append(claw)
        if offline_claws:
            current_app.logger.warning(f"以下 OpenClaw 不在线，消息已跳过: {offline_claws}")
    elif not target_agent_ids:
        # 全员通知（默认发送给所有在线 OpenClaw）
        online_claws = OpenClawInstance.query.filter(
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