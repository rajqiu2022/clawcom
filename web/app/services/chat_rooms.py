"""主题聊天室领域服务。

聊天室的身份、消息、投递和外部临时会话均独立于 ClawMessage，避免改变现有
点对点通信语义。这里不执行 Agent；只生成可审计的定向投递记录。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app, g
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import (
    ChatRoom,
    ChatRoomAudit,
    ChatRoomDelivery,
    ChatRoomEvent,
    ChatRoomGuestIdentity,
    ChatRoomGuestSession,
    ChatRoomInvite,
    ChatRoomMember,
    ChatRoomMention,
    ChatRoomMessage,
    OpenClawInstance,
    User,
)


def now_cst_naive():
    # 与 models._now 保持同一种 naive CST 存储方式。
    return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)


def secret_hash(value):
    return hashlib.sha256(str(value or '').encode('utf-8')).hexdigest()


def new_secret(prefix):
    return prefix + secrets.token_urlsafe(32)


def guest_ttl_minutes(requested=None):
    default = int(current_app.config.get('CHAT_ROOM_GUEST_TOKEN_MINUTES', 2880))
    maximum = int(current_app.config.get('CHAT_ROOM_GUEST_TOKEN_MAX_MINUTES', 10080))
    try:
        value = int(requested if requested is not None else default)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 60), maximum)


def audit(room_id, action, actor_member_id=None, target_type=None,
          target_id=None, detail=None):
    db.session.add(ChatRoomAudit(
        room_id=room_id,
        actor_member_id=actor_member_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail_json=detail or {},
    ))


def emit_room_event(room_id, event_type, actor_member_id=None, payload=None):
    event = ChatRoomEvent(
        room_id=room_id, event_type=event_type,
        actor_member_id=actor_member_id, payload_json=payload or {})
    db.session.add(event)
    db.session.flush()
    return event


def project_allowed(user=None, claw=None, project_id=None):
    """聊天室项目边界校验；全局房间只允许管理员创建。"""
    if claw is not None:
        if claw.role == 'admin' and claw.project_id is None:
            return True
        return bool(project_id and int(claw.project_id or 0) == int(project_id))
    if user is None:
        return False
    if (getattr(user, 'role', None) == 'super_admin'
            or getattr(user, 'is_global', False)):
        return True
    ids = set()
    for raw in (getattr(user, 'managed_projects', None) or []):
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            pass
    bound_id = getattr(user, 'bound_claw_id', None)
    if bound_id:
        bound = db.session.get(OpenClawInstance, bound_id)
        if bound and bound.project_id:
            ids.add(int(bound.project_id))
    return bool(project_id and int(project_id) in ids)


def member_for_actor(room_id, user=None, claw=None, guest_session=None,
                     active_only=True):
    query = ChatRoomMember.query.filter(ChatRoomMember.room_id == int(room_id))
    if guest_session is not None:
        query = query.filter(ChatRoomMember.id == guest_session.member_id)
    elif claw is not None:
        query = query.filter(ChatRoomMember.claw_id == claw.id)
    elif user is not None:
        query = query.filter(ChatRoomMember.user_id == user.id)
    else:
        return None
    if active_only:
        query = query.filter(ChatRoomMember.status == 'active')
    return query.first()


def actor_display_name(user=None, claw=None):
    if claw is not None:
        return claw.name
    return (getattr(user, 'display_name', None)
            or getattr(user, 'username', None) or 'Hub 用户')


def create_room(title, description, project_id, user=None, claw=None,
                agent_policy='mention_only'):
    title = str(title or '').strip()
    if not title:
        raise ValueError('ROOM_TITLE_REQUIRED')
    if len(title) > 160:
        raise ValueError('ROOM_TITLE_TOO_LONG')
    if not project_allowed(user=user, claw=claw, project_id=project_id):
        raise PermissionError('PROJECT_ACCESS_DENIED')

    actor_type = 'agent' if claw is not None else 'user'
    actor_id = claw.id if claw is not None else user.id
    room = ChatRoom(
        project_id=int(project_id) if project_id else None,
        title=title,
        description=str(description or '').strip(),
        agent_policy=(agent_policy if agent_policy in ('mention_only', 'all_messages')
                      else 'mention_only'),
        created_by_type=actor_type,
        created_by_id=actor_id,
    )
    db.session.add(room)
    db.session.flush()
    owner = ChatRoomMember(
        room_id=room.id,
        member_type=actor_type,
        user_id=user.id if user is not None and claw is None else None,
        claw_id=claw.id if claw is not None else None,
        display_name=actor_display_name(user=user, claw=claw),
        role='owner',
    )
    db.session.add(owner)
    db.session.flush()
    room.owner_member_id = owner.id
    audit(room.id, 'room_created', owner.id, 'room', room.id,
          {'project_id': room.project_id, 'agent_policy': room.agent_policy})
    emit_room_event(room.id, 'room_member_changed', owner.id, {
        'action': 'member_added', 'member': owner.to_dict()})
    db.session.commit()
    return room, owner


def add_registered_member(room, actor_member, member_type, subject_id, role='member'):
    if actor_member.role not in ('owner', 'admin'):
        raise PermissionError('ROOM_MEMBER_MANAGE_DENIED')
    if member_type == 'user':
        subject = db.session.get(User, int(subject_id))
        if not subject:
            raise LookupError('USER_NOT_FOUND')
        if room.project_id and not project_allowed(user=subject, project_id=room.project_id):
            raise PermissionError('USER_PROJECT_MISMATCH')
        existing = ChatRoomMember.query.filter_by(room_id=room.id, user_id=subject.id).first()
        kwargs = {'user_id': subject.id, 'claw_id': None}
        display_name = subject.display_name or subject.username
    elif member_type == 'agent':
        subject = db.session.get(OpenClawInstance, int(subject_id))
        if not subject or subject.status == 'deleted':
            raise LookupError('AGENT_NOT_FOUND')
        if room.project_id and subject.project_id and int(subject.project_id) != int(room.project_id):
            raise PermissionError('AGENT_PROJECT_MISMATCH')
        existing = ChatRoomMember.query.filter_by(room_id=room.id, claw_id=subject.id).first()
        kwargs = {'user_id': None, 'claw_id': subject.id}
        display_name = subject.name
    else:
        raise ValueError('MEMBER_TYPE_INVALID')

    if existing:
        if existing.status != 'active':
            existing.status = 'active'
            existing.removed_at = None
            existing.invitation_notified_at = None
            emit_room_event(room.id, 'room_member_changed', actor_member.id, {
                'action': 'member_reactivated', 'member': existing.to_dict()})
        return existing, False
    member = ChatRoomMember(
        room_id=room.id, member_type=member_type, display_name=display_name,
        role=role if role in ('admin', 'member') else 'member', **kwargs)
    db.session.add(member)
    db.session.flush()
    audit(room.id, 'member_added', actor_member.id, 'member', member.id,
          {'member_type': member_type, 'subject_id': int(subject_id)})
    emit_room_event(room.id, 'room_member_changed', actor_member.id, {
        'action': 'member_added', 'member': member.to_dict()})
    return member, True


def room_mentions(message_id):
    rows = ChatRoomMention.query.filter_by(message_id=message_id).order_by(
        ChatRoomMention.id.asc()).all()
    return [
        {'type': row.mention_type, 'member_id': row.member_id}
        for row in rows
    ]


def serialize_message(message):
    return message.to_dict(mentions=room_mentions(message.id))


def post_message(room, sender, content, client_message_id, mentions=None,
                 reply_to_message_id=None, origin_delivery_id=None):
    room_id = int(room.id)
    sender_id = int(sender.id)
    content = str(content or '').strip()
    client_message_id = str(client_message_id or '').strip()
    if not content:
        raise ValueError('MESSAGE_CONTENT_REQUIRED')
    if len(content) > 20000:
        raise ValueError('MESSAGE_CONTENT_TOO_LONG')
    if not client_message_id:
        raise ValueError('IDEMPOTENCY_KEY_REQUIRED')

    existing = ChatRoomMessage.query.filter_by(
        room_id=room_id, sender_member_id=sender_id,
        client_message_id=client_message_id).first()
    if existing:
        return existing, False

    if reply_to_message_id:
        replied = ChatRoomMessage.query.filter_by(
            id=int(reply_to_message_id), room_id=room_id, status='active').first()
        if not replied:
            raise ValueError('REPLY_MESSAGE_NOT_FOUND')

    automation_depth = 0
    if origin_delivery_id is not None:
        origin = ChatRoomDelivery.query.filter_by(
            id=int(origin_delivery_id), room_id=room_id, member_id=sender_id).first()
        if not origin:
            raise ValueError('ORIGIN_DELIVERY_NOT_FOUND')
        parent = db.session.get(ChatRoomMessage, origin.message_id)
        automation_depth = int(parent.automation_depth or 0) + 1 if parent else 1

    normalized = []
    direct_ids = set()
    mention_all = False
    for item in (mentions or []):
        if isinstance(item, int):
            direct_ids.add(int(item))
            continue
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'all':
            mention_all = True
        elif item.get('type') == 'member' and item.get('member_id') is not None:
            direct_ids.add(int(item['member_id']))

    active_members = ChatRoomMember.query.filter_by(
        room_id=room_id, status='active').all()
    active_by_id = {member.id: member for member in active_members}
    invalid = sorted(mid for mid in direct_ids if mid not in active_by_id)
    if invalid:
        raise ValueError('MENTION_MEMBER_NOT_FOUND')

    message = ChatRoomMessage(
        room_id=room_id,
        sender_member_id=sender_id,
        client_message_id=client_message_id,
        content=content,
        reply_to_message_id=int(reply_to_message_id) if reply_to_message_id else None,
        origin_delivery_id=int(origin_delivery_id) if origin_delivery_id is not None else None,
        automation_depth=automation_depth,
    )
    db.session.add(message)
    db.session.flush()

    if mention_all:
        db.session.add(ChatRoomMention(message_id=message.id, mention_type='all'))
        normalized.append({'type': 'all', 'member_id': None})
    for member_id in sorted(direct_ids):
        db.session.add(ChatRoomMention(
            message_id=message.id, mention_type='member', member_id=member_id))
        normalized.append({'type': 'member', 'member_id': member_id})

    max_depth = max(1, int(current_app.config.get('CHAT_ROOM_AGENT_MAX_DEPTH', 5)))
    allow_agent_notifications = automation_depth < max_depth
    notify_ids = set(direct_ids) if allow_agent_notifications else set()
    if mention_all:
        if allow_agent_notifications:
            notify_ids.update(active_by_id)
    if room.agent_policy == 'all_messages' and allow_agent_notifications:
        notify_ids.update(
            member.id for member in active_members if member.member_type == 'agent')
    notify_ids.discard(sender.id)

    for member in active_members:
        if member.id == sender.id:
            continue
        db.session.add(ChatRoomDelivery(
            room_id=room_id,
            message_id=message.id,
            member_id=member.id,
            notify_agent=(member.member_type == 'agent' and member.id in notify_ids),
        ))
    sender.last_read_message_id = message.id
    room.updated_at = now_cst_naive()
    audit(room.id, 'message_created', sender.id, 'message', message.id,
          {'mentions': normalized, 'automation_depth': automation_depth,
           'agent_notification_suppressed': not allow_agent_notifications})
    try:
        db.session.commit()
        return message, True
    except IntegrityError:
        # 并发重放也必须返回第一次落库的消息，而不是把唯一键冲突暴露成 500/400。
        db.session.rollback()
        existing = ChatRoomMessage.query.filter_by(
            room_id=room_id, sender_member_id=sender_id,
            client_message_id=client_message_id).first()
        if existing:
            return existing, False
        raise


def create_invite(room, actor_member, expires_at=None, max_joins=None, label=''):
    if actor_member.role not in ('owner', 'admin'):
        raise PermissionError('ROOM_INVITE_MANAGE_DENIED')
    raw = new_secret('room_ci_')
    invite = ChatRoomInvite(
        room_id=room.id,
        token_hash=secret_hash(raw),
        label=str(label or '').strip()[:100],
        max_joins=int(max_joins) if max_joins else None,
        expires_at=expires_at,
        created_by_member_id=actor_member.id,
    )
    db.session.add(invite)
    db.session.flush()
    audit(room.id, 'invite_created', actor_member.id, 'invite', invite.id,
          {'expires_at': str(expires_at) if expires_at else None,
           'max_joins': invite.max_joins})
    db.session.commit()
    return invite, raw


def exchange_invite(raw_invite, display_name, ttl_minutes=None):
    now = now_cst_naive()
    invite = (ChatRoomInvite.query.filter_by(
        token_hash=secret_hash(raw_invite), status='active')
        .with_for_update().first())
    if not invite:
        raise PermissionError('ROOM_INVITE_INVALID')
    # Serialize exchanges at room scope as well as invite scope.  A room may
    # have multiple live invite links, but it must not gain two active members
    # with the same visible identity through concurrent exchanges.
    room = (ChatRoom.query.filter_by(id=invite.room_id)
            .with_for_update().first())
    if not room or room.status != 'active':
        raise PermissionError('ROOM_NOT_ACTIVE')
    if invite.expires_at and invite.expires_at <= now:
        raise PermissionError('ROOM_INVITE_EXPIRED')
    if invite.max_joins is not None and invite.join_count >= invite.max_joins:
        raise PermissionError('ROOM_INVITE_EXHAUSTED')
    display_name = ' '.join(str(display_name or '').split())
    if not display_name:
        raise ValueError('DISPLAY_NAME_REQUIRED')
    display_name = display_name[:80]
    normalized_name = display_name.casefold()
    active_names = ChatRoomMember.query.filter_by(
        room_id=room.id, status='active').with_entities(
            ChatRoomMember.display_name).all()
    if any(' '.join(str(name or '').split()).casefold() == normalized_name
           for name, in active_names):
        raise ValueError('ROOM_DISPLAY_NAME_ALREADY_ACTIVE')

    resume_secret = new_secret('room_rs_')
    identity = ChatRoomGuestIdentity(
        display_name=display_name, resume_secret_hash=secret_hash(resume_secret))
    db.session.add(identity)
    db.session.flush()
    member = ChatRoomMember(
        room_id=room.id, member_type='guest', guest_identity_id=identity.id,
        display_name=identity.display_name, role='member')
    db.session.add(member)
    db.session.flush()
    invite.join_count += 1
    access_token, session = issue_guest_session(room, member, ttl_minutes, commit=False)
    audit(room.id, 'guest_joined', member.id, 'member', member.id,
          {'invite_id': invite.id})
    emit_room_event(room.id, 'room_member_changed', member.id, {
        'action': 'guest_joined', 'member': member.to_dict()})
    db.session.commit()
    return room, member, session, access_token, resume_secret


def issue_guest_session(room, member, ttl_minutes=None, commit=True):
    raw = new_secret('room_cs_')
    ttl = guest_ttl_minutes(ttl_minutes)
    session = ChatRoomGuestSession(
        room_id=room.id,
        member_id=member.id,
        access_token_hash=secret_hash(raw),
        expires_at=now_cst_naive() + timedelta(minutes=ttl),
    )
    db.session.add(session)
    if commit:
        audit(room.id, 'guest_session_issued', member.id, 'session', None,
              {'ttl_minutes': ttl})
        db.session.commit()
    return raw, session


def renew_guest_session(room_id, member_id, resume_secret, ttl_minutes=None):
    room = db.session.get(ChatRoom, int(room_id))
    member = db.session.get(ChatRoomMember, int(member_id))
    if (not room or room.status != 'active' or not member
            or member.room_id != room.id or member.status != 'active'
            or member.member_type != 'guest' or not member.guest_identity):
        raise PermissionError('ROOM_GUEST_IDENTITY_INVALID')
    identity = member.guest_identity
    if (identity.status != 'active'
            or identity.resume_secret_hash != secret_hash(resume_secret)):
        raise PermissionError('ROOM_RESUME_SECRET_INVALID')
    token, session = issue_guest_session(room, member, ttl_minutes, commit=False)
    audit(room.id, 'guest_session_renewed', member.id, 'session', None,
          {'expires_at': str(session.expires_at)})
    db.session.commit()
    return room, member, session, token


def find_active_guest_session(raw_token):
    now = now_cst_naive()
    session = ChatRoomGuestSession.query.filter_by(
        access_token_hash=secret_hash(raw_token), status='active').first()
    if not session or session.expires_at <= now:
        return None
    room = db.session.get(ChatRoom, session.room_id)
    member = db.session.get(ChatRoomMember, session.member_id)
    if (not room or room.status != 'active' or not member
            or member.status != 'active' or member.room_id != room.id):
        return None
    session.last_activity_at = now
    db.session.commit()
    return session


def guest_path_allowed(path, method):
    """外部 Token 只能读写自己所在聊天室，room_id 仍由 API 二次核验。"""
    method = method.upper()
    parts = [part for part in str(path or '').split('/') if part]
    # api/v1/chat-rooms/<room_id>[/members|messages|read|unread]
    if len(parts) < 4 or parts[:3] != ['api', 'v1', 'chat-rooms']:
        return False
    if not parts[3].isdigit():
        return False
    if len(parts) == 4:
        return method == 'GET'
    return len(parts) == 5 and parts[4] in ('members', 'messages', 'read', 'unread') and (
        (parts[4] in ('members', 'messages', 'unread') and method == 'GET')
        or (parts[4] in ('messages', 'read') and method == 'POST'))


def pending_agent_events(claw_id, limit=50):
    """领取一个 Agent 的聊天室事件；事件与旧 message 事件分离。"""
    events = []
    now = now_cst_naive()
    members = ChatRoomMember.query.join(ChatRoom).filter(
        ChatRoomMember.claw_id == int(claw_id),
        ChatRoomMember.status == 'active',
        ChatRoom.status == 'active',
    ).all()
    for member in members:
        if len(events) >= limit:
            break
        if member.invitation_notified_at is None:
            events.append(('room_invited', {
                'room': member.room.to_dict(), 'member': member.to_dict()}))
            member.invitation_notified_at = now
            if len(events) >= limit:
                break
        remaining_events = max(0, limit - len(events))
        if remaining_events:
            room_events = (ChatRoomEvent.query
                           .filter(
                               ChatRoomEvent.room_id == member.room_id,
                               ChatRoomEvent.id > int(member.last_event_id or 0),
                           )
                           .order_by(ChatRoomEvent.id.asc())
                           .limit(remaining_events).all())
            for room_event in room_events:
                events.append((room_event.event_type, {
                    'event_id': room_event.id,
                    'room_id': room_event.room_id,
                    'actor_member_id': room_event.actor_member_id,
                    **(room_event.payload_json or {}),
                }))
                member.last_event_id = room_event.id
                if len(events) >= limit:
                    break
    remaining = max(0, limit - len(events))
    if remaining:
        deliveries = (ChatRoomDelivery.query
                      .join(ChatRoomMember, ChatRoomDelivery.member_id == ChatRoomMember.id)
                      .join(ChatRoomMessage, ChatRoomDelivery.message_id == ChatRoomMessage.id)
                      .join(ChatRoom, ChatRoomDelivery.room_id == ChatRoom.id)
                      .filter(
                          ChatRoomMember.claw_id == int(claw_id),
                          ChatRoomDelivery.notify_agent.is_(True),
                          ChatRoomDelivery.status == 'unread',
                          ChatRoom.status == 'active',
                          ChatRoomMessage.status == 'active',
                      )
                      .order_by(ChatRoomDelivery.id.asc()).limit(remaining).all())
        for delivery in deliveries:
            message = db.session.get(ChatRoomMessage, delivery.message_id)
            events.append(('room_message', {
                'delivery_id': delivery.id,
                'room': db.session.get(ChatRoom, delivery.room_id).to_dict(),
                'message': serialize_message(message),
            }))
            delivery.status = 'delivered'
            delivery.delivered_at = now
    if events:
        db.session.commit()
    return events


def current_guest_session():
    return getattr(g, '_chat_guest_session', None)
