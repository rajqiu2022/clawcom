"""Hub 主题聊天室 API。"""

import os
import hashlib
import uuid
from datetime import datetime

from flask import current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user
from app.models import (
    ChatRoom,
    ChatRoomDelivery,
    ChatRoomGuestSession,
    ChatRoomInvite,
    ChatRoomImage,
    ChatRoomMember,
    ChatRoomMessage,
    OpenClawInstance,
    User,
)
from app.services.chat_rooms import (
    add_registered_member,
    audit,
    create_invite,
    create_room,
    current_guest_session,
    exchange_invite,
    emit_room_event,
    member_for_actor,
    now_cst_naive,
    post_message,
    project_allowed,
    renew_guest_session,
    serialize_message,
)


def _disabled():
    if current_app.config.get('CHAT_ROOM_ENABLED', False):
        return None
    return jsonify({'error': '聊天室能力未开启', 'code': 'CHAT_ROOM_DISABLED'}), 404


def _error(exc, status=None):
    code = str(exc) or exc.__class__.__name__
    if status is None:
        status = 403 if isinstance(exc, PermissionError) else (
            404 if isinstance(exc, LookupError) else 400)
    return jsonify({'error': code, 'code': code}), status


def _actor():
    guest = current_guest_session()
    if guest:
        return None, None, guest
    return get_current_user(), get_current_claw(), None


def _room_and_member(room_id, active_room=True):
    room = db.session.get(ChatRoom, int(room_id))
    if not room or (active_room and room.status != 'active'):
        return None, None, _error(LookupError('ROOM_NOT_FOUND'))
    user, claw, guest = _actor()
    if guest and int(guest.room_id) != int(room.id):
        return None, None, _error(PermissionError('ROOM_ACCESS_DENIED'))
    member = member_for_actor(room.id, user=user, claw=claw, guest_session=guest)
    if not member:
        return None, None, _error(PermissionError('ROOM_ACCESS_DENIED'))
    return room, member, None


def _image_kind(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', '.gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    raise ValueError('ROOM_IMAGE_TYPE_NOT_ALLOWED')


def _image_root():
    configured = str(current_app.config.get('CHAT_ROOM_IMAGE_DIR') or '').strip()
    root = configured or os.path.join(current_app.instance_path, 'chat-room-images')
    return os.path.realpath(root)


def _image_path(image):
    root = _image_root()
    target = os.path.realpath(os.path.join(root, image.storage_key))
    if os.path.commonpath((root, target)) != root:
        raise ValueError('ROOM_IMAGE_PATH_INVALID')
    return target


def _room_payload(room, member):
    data = room.to_dict()
    data['my_member'] = member.to_dict()
    data['member_count'] = ChatRoomMember.query.filter_by(
        room_id=room.id, status='active').count()
    data['last_message'] = None
    latest = ChatRoomMessage.query.filter_by(
        room_id=room.id, status='active').order_by(ChatRoomMessage.id.desc()).first()
    if latest:
        data['last_message'] = serialize_message(latest)
    last_read = int(member.last_read_message_id or 0)
    data['unread_count'] = ChatRoomMessage.query.filter(
        ChatRoomMessage.room_id == room.id,
        ChatRoomMessage.status == 'active',
        ChatRoomMessage.id > last_read,
        ChatRoomMessage.sender_member_id != member.id,
    ).count()
    return data


@api_bp.route('/chat-rooms', methods=['GET'])
def list_chat_rooms():
    blocked = _disabled()
    if blocked:
        return blocked
    user, claw, guest = _actor()
    query = ChatRoom.query.join(ChatRoomMember).filter(
        ChatRoom.status == 'active', ChatRoomMember.status == 'active')
    if guest:
        query = query.filter(ChatRoomMember.id == guest.member_id)
    elif claw:
        query = query.filter(ChatRoomMember.claw_id == claw.id)
    elif user:
        query = query.filter(ChatRoomMember.user_id == user.id)
    else:
        return _error(PermissionError('AUTH_REQUIRED'), 401)
    rooms = query.order_by(ChatRoom.updated_at.desc(), ChatRoom.id.desc()).all()
    payload = []
    for room in rooms:
        member = member_for_actor(room.id, user=user, claw=claw, guest_session=guest)
        if member:
            payload.append(_room_payload(room, member))
    return jsonify({'items': payload, 'count': len(payload)})


@api_bp.route('/chat-rooms', methods=['POST'])
def create_chat_room():
    blocked = _disabled()
    if blocked:
        return blocked
    if current_guest_session():
        return _error(PermissionError('GUEST_CANNOT_CREATE_ROOM'))
    data = request.get_json(silent=True) or {}
    user, claw, _ = _actor()
    try:
        room, owner = create_room(
            data.get('title'), data.get('description'), data.get('project_id'),
            user=user, claw=claw, agent_policy=data.get('agent_policy'))
        for spec in data.get('members') or []:
            add_registered_member(
                room, owner, spec.get('type'), spec.get('id'), spec.get('role', 'member'))
        db.session.commit()
        return jsonify(_room_payload(room, owner)), 201
    except Exception as exc:
        db.session.rollback()
        return _error(exc)


@api_bp.route('/chat-rooms/candidates', methods=['GET'])
def chat_room_candidates():
    blocked = _disabled()
    if blocked:
        return blocked
    if current_guest_session():
        return _error(PermissionError('ROOM_ACCESS_DENIED'))
    user, claw, _ = _actor()
    project_id = request.args.get('project_id', type=int)
    if not project_allowed(user=user, claw=claw, project_id=project_id):
        return _error(PermissionError('PROJECT_ACCESS_DENIED'))
    users = [item for item in User.query.order_by(
        User.display_name.asc(), User.username.asc()).limit(300).all()
             if project_allowed(user=item, project_id=project_id)]
    agents_q = OpenClawInstance.query.filter(OpenClawInstance.status != 'deleted')
    if project_id:
        agents_q = agents_q.filter(
            (OpenClawInstance.project_id == project_id)
            | (OpenClawInstance.project_id.is_(None)))
    agents = agents_q.order_by(OpenClawInstance.name.asc()).all()
    return jsonify({
        'users': [{'id': item.id, 'name': item.display_name or item.username,
                   'username': item.username} for item in users],
        'agents': [{'id': item.id, 'name': item.name, 'status': item.status,
                    'project_id': item.project_id} for item in agents],
    })


@api_bp.route('/chat-rooms/<int:room_id>', methods=['GET'])
def get_chat_room(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    return error or jsonify(_room_payload(room, member))


@api_bp.route('/chat-rooms/<int:room_id>', methods=['PATCH'])
def update_chat_room(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    if member.role not in ('owner', 'admin'):
        return _error(PermissionError('ROOM_MANAGE_DENIED'))
    data = request.get_json(silent=True) or {}
    if 'title' in data:
        title = str(data.get('title') or '').strip()
        if not title:
            return _error(ValueError('ROOM_TITLE_REQUIRED'))
        room.title = title[:160]
    if 'description' in data:
        room.description = str(data.get('description') or '').strip()
    if data.get('agent_policy') in ('mention_only', 'all_messages'):
        room.agent_policy = data['agent_policy']
    room.updated_at = now_cst_naive()
    audit(room.id, 'room_updated', member.id, 'room', room.id,
          {'fields': sorted(data.keys())})
    db.session.commit()
    return jsonify(_room_payload(room, member))


@api_bp.route('/chat-rooms/<int:room_id>', methods=['DELETE'])
def delete_chat_room(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    if member.role != 'owner':
        return _error(PermissionError('ROOM_DELETE_DENIED'))
    if room.team_id:
        return _error(PermissionError('TEAM_ROOM_DELETE_DENIED'))
    now = now_cst_naive()
    room.status = 'deleted'
    room.deleted_at = now
    ChatRoomGuestSession.query.filter_by(room_id=room.id, status='active').update({
        ChatRoomGuestSession.status: 'revoked', ChatRoomGuestSession.revoked_at: now,
    }, synchronize_session=False)
    audit(room.id, 'room_deleted', member.id, 'room', room.id)
    db.session.commit()
    return jsonify({'ok': True, 'room_id': room.id})


@api_bp.route('/chat-rooms/<int:room_id>/members', methods=['GET'])
def list_chat_room_members(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, _, error = _room_and_member(room_id)
    if error:
        return error
    members = ChatRoomMember.query.filter_by(
        room_id=room.id, status='active').order_by(ChatRoomMember.joined_at.asc()).all()
    return jsonify({'items': [item.to_dict() for item in members], 'count': len(members)})


@api_bp.route('/chat-rooms/<int:room_id>/members', methods=['POST'])
def add_chat_room_member(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, actor, error = _room_and_member(room_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        member, created = add_registered_member(
            room, actor, data.get('type'), data.get('id'), data.get('role', 'member'))
        db.session.commit()
        return jsonify(member.to_dict()), 201 if created else 200
    except Exception as exc:
        db.session.rollback()
        return _error(exc)


@api_bp.route('/chat-rooms/<int:room_id>/members/<int:member_id>', methods=['DELETE'])
def remove_chat_room_member(room_id, member_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, actor, error = _room_and_member(room_id)
    if error:
        return error
    if actor.role not in ('owner', 'admin'):
        return _error(PermissionError('ROOM_MEMBER_MANAGE_DENIED'))
    target = ChatRoomMember.query.filter_by(id=member_id, room_id=room.id, status='active').first()
    if not target:
        return _error(LookupError('ROOM_MEMBER_NOT_FOUND'))
    if target.role == 'owner':
        return _error(PermissionError('ROOM_OWNER_CANNOT_REMOVE'))
    if room.team_id and target.member_type == 'agent':
        return _error(PermissionError('TEAM_ROOM_MEMBER_MANAGED_BY_TEAM'))
    now = now_cst_naive()
    target.status = 'removed'
    target.removed_at = now
    if target.member_type == 'guest':
        ChatRoomGuestSession.query.filter_by(
            room_id=room.id, member_id=target.id, status='active').update({
                ChatRoomGuestSession.status: 'revoked',
                ChatRoomGuestSession.revoked_at: now,
            }, synchronize_session=False)
    audit(room.id, 'member_removed', actor.id, 'member', target.id)
    emit_room_event(room.id, 'room_member_changed', actor.id, {
        'action': 'member_removed', 'member': target.to_dict()})
    db.session.commit()
    return jsonify({'ok': True})


@api_bp.route('/chat-rooms/<int:room_id>/messages', methods=['GET'])
def list_chat_room_messages(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, _, error = _room_and_member(room_id)
    if error:
        return error
    limit = min(max(request.args.get('limit', default=80, type=int), 1), 200)
    query = ChatRoomMessage.query.filter_by(room_id=room.id, status='active')
    before_id = request.args.get('before_id', type=int)
    after_id = request.args.get('after_id', type=int)
    if before_id:
        query = query.filter(ChatRoomMessage.id < before_id)
    if after_id:
        query = query.filter(ChatRoomMessage.id > after_id)
    rows = query.order_by(ChatRoomMessage.id.desc()).limit(limit).all()
    rows.reverse()
    return jsonify({'items': [serialize_message(item) for item in rows],
                    'count': len(rows)})


@api_bp.route('/chat-rooms/<int:room_id>/messages', methods=['POST'])
def create_chat_room_message(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, sender, error = _room_and_member(room_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    key = request.headers.get('Idempotency-Key') or data.get('client_message_id')
    try:
        message, created = post_message(
            room, sender, data.get('content'), key, data.get('mentions'),
            data.get('reply_to_message_id'), data.get('origin_delivery_id'),
            image_ids=data.get('image_ids') or [])
        return jsonify(serialize_message(message)), 201 if created else 200
    except Exception as exc:
        db.session.rollback()
        return _error(exc)


@api_bp.route('/chat-rooms/<int:room_id>/images', methods=['POST'])
def upload_chat_room_image(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, sender, error = _room_and_member(room_id)
    if error:
        return error
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _error(ValueError('ROOM_IMAGE_FILE_REQUIRED'))
    upload_key = str(request.headers.get('Idempotency-Key') or '').strip()
    if len(upload_key) > 100:
        return _error(ValueError('ROOM_IMAGE_IDEMPOTENCY_KEY_INVALID'))
    if upload_key:
        existing = ChatRoomImage.query.filter_by(
            room_id=room.id, uploaded_by_member_id=sender.id,
            client_upload_id=upload_key).first()
        if existing:
            return jsonify(existing.to_dict()), 200
    max_bytes = max(1024, int(current_app.config.get(
        'CHAT_ROOM_IMAGE_MAX_BYTES', 8 * 1024 * 1024)))
    data = upload.stream.read(max_bytes + 1)
    if not data:
        return _error(ValueError('ROOM_IMAGE_EMPTY'))
    if len(data) > max_bytes:
        return _error(ValueError('ROOM_IMAGE_TOO_LARGE'), 413)
    try:
        content_type, extension = _image_kind(data)
        original = secure_filename(upload.filename) or ('image' + extension)
        storage_key = '%s/%s%s' % (room.id, uuid.uuid4().hex, extension)
        image = ChatRoomImage(
            room_id=room.id, uploaded_by_member_id=sender.id,
            client_upload_id=upload_key or None,
            original_name=original[:255], content_type=content_type,
            file_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
            storage_key=storage_key, status='pending')
        target = _image_path(image)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'xb') as handle:
            handle.write(data)
        db.session.add(image)
        db.session.flush()
        audit(room.id, 'image_uploaded', sender.id, 'image', image.id, {
            'content_type': content_type, 'file_size': len(data),
            'sha256': image.sha256})
        db.session.commit()
        return jsonify(image.to_dict()), 201
    except Exception as exc:
        db.session.rollback()
        if 'target' in locals():
            try:
                os.remove(target)
            except OSError:
                pass
        return _error(exc)


@api_bp.route('/chat-rooms/<int:room_id>/images/<int:image_id>', methods=['GET'])
def get_chat_room_image(room_id, image_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    image = ChatRoomImage.query.filter_by(id=image_id, room_id=room.id).first()
    if not image or image.status not in ('pending', 'active'):
        return _error(LookupError('ROOM_IMAGE_NOT_FOUND'))
    if image.status == 'pending' and image.uploaded_by_member_id != member.id:
        return _error(PermissionError('ROOM_IMAGE_ACCESS_DENIED'))
    try:
        path = _image_path(image)
        if not os.path.isfile(path):
            return _error(LookupError('ROOM_IMAGE_FILE_NOT_FOUND'))
        response = send_file(
            path, mimetype=image.content_type, as_attachment=False,
            download_name=image.original_name, conditional=True, max_age=0)
        response.headers['Cache-Control'] = 'private, max-age=300'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'"
        return response
    except Exception as exc:
        return _error(exc)


@api_bp.route('/chat-rooms/<int:room_id>/read', methods=['POST'])
def mark_chat_room_read(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    message_id = data.get('message_id')
    message = ChatRoomMessage.query.filter_by(
        id=message_id, room_id=room.id, status='active').first()
    if not message:
        return _error(LookupError('MESSAGE_NOT_FOUND'))
    if int(message.id) > int(member.last_read_message_id or 0):
        member.last_read_message_id = message.id
    now = now_cst_naive()
    ChatRoomDelivery.query.filter(
        ChatRoomDelivery.member_id == member.id,
        ChatRoomDelivery.message_id <= message.id,
        ChatRoomDelivery.status.in_(('unread', 'delivered')),
    ).update({ChatRoomDelivery.status: 'read', ChatRoomDelivery.read_at: now},
             synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True, 'last_read_message_id': member.last_read_message_id})


@api_bp.route('/chat-rooms/<int:room_id>/unread', methods=['GET'])
def get_chat_room_unread(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    count = ChatRoomMessage.query.filter(
        ChatRoomMessage.room_id == room.id,
        ChatRoomMessage.id > int(member.last_read_message_id or 0),
        ChatRoomMessage.sender_member_id != member.id,
        ChatRoomMessage.status == 'active',
    ).count()
    return jsonify({'count': count, 'last_read_message_id': member.last_read_message_id})


def _parse_datetime(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        raise ValueError('EXPIRES_AT_INVALID')


@api_bp.route('/chat-rooms/<int:room_id>/invites', methods=['GET'])
def list_chat_room_invites(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, actor, error = _room_and_member(room_id)
    if error:
        return error
    if actor.role not in ('owner', 'admin'):
        return _error(PermissionError('ROOM_INVITE_MANAGE_DENIED'))
    rows = ChatRoomInvite.query.filter_by(room_id=room.id).order_by(
        ChatRoomInvite.id.desc()).all()
    return jsonify({'items': [{
        'id': item.id, 'label': item.label or '', 'status': item.status,
        'join_count': item.join_count, 'max_joins': item.max_joins,
        'expires_at': str(item.expires_at) if item.expires_at else None,
        'created_at': str(item.created_at) if item.created_at else None,
    } for item in rows]})


@api_bp.route('/chat-rooms/<int:room_id>/invites', methods=['POST'])
def create_chat_room_invite(room_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, actor, error = _room_and_member(room_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    try:
        invite, raw = create_invite(
            room, actor, _parse_datetime(data.get('expires_at')),
            data.get('max_joins'), data.get('label'))
        agent_base = (current_app.config.get('HUB_PUBLIC_URL')
                      or os.getenv('HUB_PUBLIC_URL')
                      or request.host_url.rstrip('/'))
        join_url = f"{agent_base.rstrip('/')}/chat/join#invite={raw}"
        response = jsonify({
            'id': invite.id,
            'room_id': room.id,
            'invite_code': raw,
            # code 放 fragment，不进入反向代理访问日志。
            # 邀请链接定位为 Agent 接入入口，固定使用 HUB_PUBLIC_URL（18800）。
            'join_url': join_url,
            'expires_at': str(invite.expires_at) if invite.expires_at else None,
        })
        # 邀请凭据属于一次性敏感响应，任何中间层和浏览器都不应缓存重放。
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['Pragma'] = 'no-cache'
        return response, 201
    except Exception as exc:
        db.session.rollback()
        return _error(exc)


@api_bp.route('/chat-rooms/<int:room_id>/invites/<int:invite_id>/revoke', methods=['POST'])
def revoke_chat_room_invite(room_id, invite_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, actor, error = _room_and_member(room_id)
    if error:
        return error
    if actor.role not in ('owner', 'admin'):
        return _error(PermissionError('ROOM_INVITE_MANAGE_DENIED'))
    invite = ChatRoomInvite.query.filter_by(id=invite_id, room_id=room.id).first()
    if not invite:
        return _error(LookupError('ROOM_INVITE_NOT_FOUND'))
    invite.status = 'revoked'
    invite.revoked_at = now_cst_naive()
    audit(room.id, 'invite_revoked', actor.id, 'invite', invite.id)
    db.session.commit()
    return jsonify({'ok': True})


@api_bp.route('/chat-room-invites/exchange', methods=['POST'])
def exchange_chat_room_invite():
    blocked = _disabled()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        room, member, guest_session, token, resume_secret = exchange_invite(
            data.get('invite_code'), data.get('display_name'), data.get('ttl_minutes'))
        return jsonify({
            'room': room.to_dict(), 'member': member.to_dict(),
            'access_token': token, 'resume_secret': resume_secret,
            'expires_at': str(guest_session.expires_at),
        }), 201
    except Exception as exc:
        db.session.rollback()
        return _error(exc, 401 if isinstance(exc, PermissionError) else None)


@api_bp.route('/chat-room-sessions/renew', methods=['POST'])
def renew_chat_room_session():
    blocked = _disabled()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    try:
        room, member, guest_session, token = renew_guest_session(
            data.get('room_id'), data.get('member_id'), data.get('resume_secret'),
            data.get('ttl_minutes'))
        return jsonify({
            'room': room.to_dict(), 'member': member.to_dict(),
            'access_token': token, 'expires_at': str(guest_session.expires_at),
        }), 201
    except Exception as exc:
        db.session.rollback()
        return _error(exc, 401 if isinstance(exc, PermissionError) else None)


def _delivery_payload(delivery):
    response = (ChatRoomMessage.query.filter_by(
        origin_delivery_id=delivery.id, status='active')
        .order_by(ChatRoomMessage.id.asc()).first())
    return {
        'id': delivery.id, 'room_id': delivery.room_id,
        'message_id': delivery.message_id, 'member_id': delivery.member_id,
        'status': delivery.status,
        'processing_at': (str(delivery.processing_at)
                          if delivery.processing_at else None),
        'done_at': str(delivery.done_at) if delivery.done_at else None,
        'failed_reason': delivery.failed_reason,
        'response_message_id': response.id if response else None,
    }


@api_bp.route('/chat-rooms/<int:room_id>/deliveries/<int:delivery_id>', methods=['GET', 'PATCH'])
def update_chat_room_delivery(room_id, delivery_id):
    blocked = _disabled()
    if blocked:
        return blocked
    room, member, error = _room_and_member(room_id)
    if error:
        return error
    if member.member_type != 'agent':
        return _error(PermissionError('DELIVERY_STATUS_AGENT_ONLY'))
    delivery = ChatRoomDelivery.query.filter_by(
        id=delivery_id, room_id=room.id, member_id=member.id).first()
    if not delivery:
        return _error(LookupError('DELIVERY_NOT_FOUND'))
    if request.method == 'GET':
        return jsonify(_delivery_payload(delivery))
    data = request.get_json(silent=True) or {}
    target = data.get('status')
    if target == delivery.status:
        return jsonify(_delivery_payload(delivery))
    allowed = {
        # A Worker may finish after losing the processing ACK.  Terminal
        # transitions stay bounded to the same authenticated Agent/member.
        'delivered': ('processing', 'read', 'done', 'failed'),
        'processing': ('done', 'failed'),
        'unread': ('delivered', 'processing', 'done', 'failed'),
    }
    if target not in allowed.get(delivery.status, ()):
        return _error(ValueError('DELIVERY_TRANSITION_INVALID'))
    delivery.status = target
    now = now_cst_naive()
    if target == 'processing':
        delivery.processing_at = now
    elif target == 'done':
        delivery.done_at = now
    elif target == 'failed':
        delivery.failed_reason = str(data.get('failed_reason') or '')[:2000]
    elif target == 'read':
        delivery.read_at = now
    db.session.commit()
    return jsonify(_delivery_payload(delivery))
