"""Fixed Agent Team chat rooms and deterministic question/reply rounds."""

from datetime import timedelta

from app import db
from app.models import (
    AgentTeamChatRound,
    ChatRoom,
    ChatRoomDelivery,
    ChatRoomMember,
    ChatRoomMessage,
    OpenClawInstance,
    User,
    _now,
)
from app.services.agent_team_activity import roster
from app.services.agent_teams import TeamError
from app.services.chat_rooms import (
    audit,
    emit_room_event,
    post_message,
    room_mentions,
    serialize_message,
)


ROUND_STATUSES = frozenset({'open', 'completed', 'partial', 'failed', 'expired'})


def _member_name(claw_id):
    claw = db.session.get(OpenClawInstance, int(claw_id))
    return claw.name if claw else 'Claw #%s' % claw_id


def _upsert_agent_member(room, claw_id, role):
    claw = db.session.get(OpenClawInstance, int(claw_id))
    if (not claw or claw.status == 'deleted'
            or int(claw.project_id or 0) != int(room.project_id or 0)):
        raise TeamError('TEAM_CHAT_MEMBER_INVALID', '团队聊天室成员不存在或项目不一致', 409)
    member = ChatRoomMember.query.filter_by(
        room_id=room.id, claw_id=claw.id).first()
    created = member is None
    if member is None:
        member = ChatRoomMember(
            room_id=room.id, member_type='agent', claw_id=claw.id,
            display_name=claw.name, role=role)
        db.session.add(member)
        db.session.flush()
    else:
        member.status = 'active'
        member.removed_at = None
        member.display_name = claw.name
        member.role = role
    if created:
        audit(room.id, 'member_added', None, 'member', member.id, {
            'managed_by': 'agent_team', 'claw_id': claw.id})
        emit_room_event(room.id, 'room_member_changed', None, {
            'action': 'member_added', 'member': member.to_dict()})
    return member


def ensure_actor_member(room, actor):
    """Add the current project actor without changing the Team roster."""
    if actor['type'] == 'claw':
        member = ChatRoomMember.query.filter_by(
            room_id=room.id, claw_id=actor['id']).first()
        if not member or member.status != 'active':
            raise TeamError('TEAM_CHAT_MEMBER_REQUIRED', '仅团队 Agent 可进入固定聊天室', 403)
        return member
    user = db.session.get(User, int(actor['id']))
    if not user:
        raise TeamError('AUTH_REQUIRED', '用户不存在', 401)
    member = ChatRoomMember.query.filter_by(
        room_id=room.id, user_id=user.id).first()
    if member is None:
        member = ChatRoomMember(
            room_id=room.id, member_type='user', user_id=user.id,
            display_name=user.display_name or user.username, role='admin')
        db.session.add(member)
        db.session.flush()
        audit(room.id, 'member_added', None, 'member', member.id, {
            'managed_by': 'agent_team_access', 'user_id': user.id})
        emit_room_event(room.id, 'room_member_changed', None, {
            'action': 'member_added', 'member': member.to_dict()})
    else:
        member.status = 'active'
        member.removed_at = None
        member.display_name = user.display_name or user.username
        if member.role != 'owner':
            member.role = 'admin'
    return member


def sync_team_room(team):
    """Create exactly one durable room and mirror the registered Team roster."""
    room = ChatRoom.query.filter_by(team_id=team.id).first()
    if room is None:
        room = ChatRoom(
            team_id=team.id,
            project_id=team.project_id,
            title='%s · 团队聊天室' % team.name,
            description='Team #%s 固定协作频道；讨论留在这里，正式执行走 Stage/Run。' % team.id,
            status='active', agent_policy='mention_only',
            created_by_type='team', created_by_id=team.id)
        db.session.add(room)
        db.session.flush()
        audit(room.id, 'room_created', None, 'room', room.id, {
            'managed_by': 'agent_team', 'team_id': team.id,
            'project_id': team.project_id})
    room.title = '%s · 团队聊天室' % team.name
    room.project_id = team.project_id
    room.agent_policy = 'mention_only'
    room.status = 'active'
    room.deleted_at = None

    people = roster(team)
    active_agent_ids = set(people) if team.status == 'active' else set()
    primary = int(team.primary_manager_claw_id)
    owner = None
    for claw_id in sorted(active_agent_ids):
        role = ('owner' if claw_id == primary else
                'admin' if claw_id == team.backup_manager_claw_id else 'member')
        member = _upsert_agent_member(room, claw_id, role)
        if role == 'owner':
            owner = member
    for member in ChatRoomMember.query.filter_by(
            room_id=room.id, member_type='agent', status='active').all():
        if member.claw_id not in active_agent_ids:
            member.status = 'removed'
            member.removed_at = _now()
            emit_room_event(room.id, 'room_member_changed', None, {
                'action': 'member_removed', 'member': member.to_dict()})
    if owner is not None:
        room.owner_member_id = owner.id
    return room


def ensure_team_room(team, actor):
    room = sync_team_room(team)
    member = ensure_actor_member(room, actor)
    db.session.commit()
    return room, member


def create_team_message(team, actor, content, client_message_id,
                        mention_claw_ids=None, mention_all=False,
                        timeout_seconds=300):
    if team.status != 'active':
        raise TeamError('TEAM_NOT_ACTIVE', '暂停或归档团队的聊天室只读', 409)
    room = sync_team_room(team)
    sender = ensure_actor_member(room, actor)
    active_agents = {
        int(member.claw_id): member
        for member in ChatRoomMember.query.filter_by(
            room_id=room.id, member_type='agent', status='active').all()
    }
    requested = set()
    for value in mention_claw_ids or []:
        try:
            requested.add(int(value))
        except (TypeError, ValueError):
            raise TeamError('TEAM_CHAT_MENTION_INVALID', 'mention_claw_ids 必须为 Agent ID 数组', 400)
    if mention_all:
        requested = set(active_agents)
    requested.discard(int(sender.claw_id)) if sender.claw_id else None
    unknown = sorted(requested - set(active_agents))
    if unknown:
        raise TeamError('TEAM_CHAT_MENTION_INVALID', '被 @ 的 Agent 不属于当前团队', 400)
    mentions = ([{'type': 'all'}] if mention_all else [
        {'type': 'member', 'member_id': active_agents[claw_id].id}
        for claw_id in sorted(requested)])
    try:
        timeout = int(timeout_seconds or 300)
    except (TypeError, ValueError):
        raise TeamError('TEAM_CHAT_TIMEOUT_INVALID', '回复等待时间必须为整数', 400)
    if timeout < 30 or timeout > 86400:
        raise TeamError('TEAM_CHAT_TIMEOUT_INVALID', '回复等待时间范围为 30–86400 秒', 400)
    try:
        message, created = post_message(
            room, sender, content, client_message_id, mentions, commit=False)
    except (ValueError, PermissionError, LookupError) as exc:
        raise TeamError(str(exc), str(exc), 400)
    round_row = AgentTeamChatRound.query.filter_by(
        question_message_id=message.id).first()
    if requested and round_row is None:
        round_row = AgentTeamChatRound(
            team_id=team.id, room_id=room.id,
            question_message_id=message.id,
            created_by_member_id=sender.id,
            expected_claw_ids_json=sorted(requested),
            deadline_at=_now() + timedelta(seconds=timeout))
        db.session.add(round_row)
    db.session.commit()
    return message, round_row, created


def _round_members(round_row):
    expected = [int(value) for value in (round_row.expected_claw_ids_json or [])]
    members = {int(row.claw_id): row for row in ChatRoomMember.query.filter(
        ChatRoomMember.room_id == round_row.room_id,
        ChatRoomMember.claw_id.in_(expected)).all()} if expected else {}
    deliveries = {row.member_id: row for row in ChatRoomDelivery.query.filter_by(
        message_id=round_row.question_message_id, notify_agent=True).all()}
    delivery_ids = [row.id for row in deliveries.values()]
    responses = {row.origin_delivery_id: row for row in ChatRoomMessage.query.filter(
        ChatRoomMessage.origin_delivery_id.in_(delivery_ids),
        ChatRoomMessage.status == 'active').order_by(ChatRoomMessage.id).all()
        } if delivery_ids else {}
    items = []
    for claw_id in expected:
        member = members.get(claw_id)
        delivery = deliveries.get(member.id) if member else None
        response = responses.get(delivery.id) if delivery else None
        status = ('replied' if response else
                  delivery.status if delivery else 'missing')
        items.append({
            'claw_id': claw_id, 'name': _member_name(claw_id),
            'member_id': member.id if member else None,
            'delivery_id': delivery.id if delivery else None,
            'status': status,
            'failed_reason': delivery.failed_reason if delivery else None,
            'response_message_id': response.id if response else None,
        })
    return items


def serialize_round(round_row, reconcile=True):
    members = _round_members(round_row)
    replied = sum(1 for item in members if item['status'] == 'replied')
    failed = sum(1 for item in members if item['status'] == 'failed')
    expired = _now() >= round_row.deadline_at
    if replied == len(members):
        status = 'completed'
    elif expired and (replied or failed):
        status = 'partial'
    elif expired:
        status = 'expired'
    elif failed and replied + failed == len(members):
        status = 'failed'
    else:
        status = 'open'
    if reconcile and status != round_row.status:
        round_row.status = status
        if status != 'open':
            round_row.completed_at = _now()
    return {
        'id': round_row.id, 'team_id': round_row.team_id,
        'room_id': round_row.room_id,
        'question_message_id': round_row.question_message_id,
        'status': status, 'expected_count': len(members),
        'replied_count': replied, 'failed_count': failed,
        'deadline_at': str(round_row.deadline_at),
        'completed_at': (str(round_row.completed_at)
                         if round_row.completed_at else None),
        'members': members,
    }


def team_room_payload(team, actor, limit=120):
    room, member = ensure_team_room(team, actor)
    messages = (ChatRoomMessage.query.filter_by(
        room_id=room.id, status='active').order_by(
            ChatRoomMessage.id.desc()).limit(limit).all())
    messages.reverse()
    rounds = (AgentTeamChatRound.query.filter_by(team_id=team.id)
              .order_by(AgentTeamChatRound.id.desc()).limit(50).all())
    result = {
        'room': dict(room.to_dict(), my_member=member.to_dict()),
        'members': [row.to_dict() for row in ChatRoomMember.query.filter_by(
            room_id=room.id, status='active').order_by(
                ChatRoomMember.joined_at, ChatRoomMember.id).all()],
        'messages': [serialize_message(row) for row in messages],
        'rounds': [serialize_round(row) for row in rounds],
    }
    db.session.commit()
    return result
