"""Atomic shared-resource lease API for Unity, Bridge and devices (P0-E)."""

from datetime import datetime, timedelta
import math
from uuid import uuid4

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import get_current_claw, get_current_user, is_admin_user
from app.models import ResourceLease, ResourceLeaseEvent
from app.services.resource_leases import (
    canonical_request_hash,
    expire_stale_resource_leases,
    lease_event,
    normalize_resource_keys,
    normalize_ttl_seconds,
    release_lease_group,
    renew_lease_group,
)


def _request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex).strip()[:128]


def _error(code, message, status=400, details=None):
    return jsonify({
        'error': message,
        'code': code,
        'message': message,
        'details': details or {},
        'request_id': _request_id(),
    }), status


def _actor():
    claw = get_current_claw()
    if claw:
        return {
            'type': 'claw', 'id': int(claw.id),
            'name': claw.name or f'claw:{claw.id}',
            'admin': claw.role == 'admin',
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': getattr(user, 'username', '') or f'user:{user.id}',
            'admin': is_admin_user(),
        }
    return None


def _idempotency_key(data):
    body_key = str(data.get('idempotency_key') or '').strip()
    header_key = str(request.headers.get('Idempotency-Key') or '').strip()
    if body_key and header_key and body_key != header_key:
        raise ValueError('Body and header idempotency keys must match')
    key = body_key or header_key
    if not key:
        raise ValueError('idempotency_key is required')
    if len(key) > 128:
        raise ValueError('idempotency_key must not exceed 128 characters')
    return key


def _group_payload(rows, idempotent_replay=False):
    rows = sorted(rows, key=lambda row: row.resource_key)
    return {
        'lease_group_id': rows[0].lease_group_id if rows else None,
        'owner_type': rows[0].owner_type if rows else None,
        'owner_id': rows[0].owner_id if rows else None,
        'status': (
            'active' if rows and all(row.status == 'active' for row in rows)
            else rows[0].status if rows else None),
        'resource_keys': [row.resource_key for row in rows],
        'leases': [row.to_dict() for row in rows],
        'idempotent_replay': bool(idempotent_replay),
    }


def _existing_idempotent(owner_type, owner_id, key, expected_hash):
    rows = (ResourceLease.query.filter_by(
        owner_type=owner_type, owner_id=owner_id, idempotency_key=key)
        .order_by(ResourceLease.resource_key.asc()).all())
    if not rows:
        return None
    if any(row.request_hash != expected_hash for row in rows):
        return _error(
            'IDEMPOTENCY_KEY_CONFLICT',
            'idempotency_key was already used with a different request', 409,
            {'lease_group_id': rows[0].lease_group_id})
    return jsonify(_group_payload(rows, idempotent_replay=True)), 200


def _can_manage(actor, lease):
    return bool(actor['admin'] or (
        lease.holder_actor_type == actor['type'] and
        lease.holder_actor_id == actor['id']))


def _conflict_payload(rows, now):
    result = []
    for row in sorted(rows, key=lambda item: item.resource_key):
        retry_after = max(0, math.ceil((row.expires_at - now).total_seconds()))
        result.append({
            'resource_key': row.resource_key,
            'lease_id': row.id,
            'lease_group_id': row.lease_group_id,
            'owner_type': row.owner_type,
            'owner_id': row.owner_id,
            'controller_run_id': row.controller_run_id,
            'priority': row.priority,
            'expires_at': str(row.expires_at),
            'retry_after_seconds': retry_after,
        })
    return result


@api_bp.route('/resource-leases/acquire', methods=['POST'])
def acquire_resource_leases():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    data = request.get_json(silent=True) or {}
    try:
        resource_keys = normalize_resource_keys(data.get('resource_keys'))
        ttl_seconds = normalize_ttl_seconds(data.get('ttl_seconds'))
        idem_key = _idempotency_key(data)
        priority = int(data.get('priority') or 0)
    except (TypeError, ValueError) as exc:
        message = str(exc)
        code = (
            'USE_TEST_ACCOUNT_MANAGER'
            if message.startswith('account resources')
            else 'RESOURCE_LEASE_VALIDATION_FAILED')
        return _error(code, message)
    if priority < 0 or priority > 1000:
        return _error(
            'RESOURCE_LEASE_VALIDATION_FAILED',
            'priority must be between 0 and 1000')
    owner_type = str(data.get('owner_type') or '').strip()[:40]
    owner_id = str(data.get('owner_id') or '').strip()[:160]
    if not owner_type or not owner_id:
        return _error(
            'RESOURCE_LEASE_VALIDATION_FAILED',
            'owner_type and owner_id are required')
    controller_run_id = str(data.get('controller_run_id') or '').strip()[:160]
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
    hash_payload = {
        'resource_keys': resource_keys,
        'owner_type': owner_type,
        'owner_id': owner_id,
        'controller_run_id': controller_run_id,
        'priority': priority,
        'ttl_seconds': ttl_seconds,
        'metadata': metadata,
    }
    req_hash = canonical_request_hash(hash_payload)
    replay = _existing_idempotent(owner_type, owner_id, idem_key, req_hash)
    if replay:
        return replay

    now = datetime.now()
    expire_stale_resource_leases(now, resource_keys)
    active = (ResourceLease.query.filter(
        ResourceLease.active_slot.in_(resource_keys),
        ResourceLease.status == 'active',
    ).with_for_update().all())
    if active:
        db.session.commit()  # retain expiry audit created above
        return _error(
            'RESOURCE_LEASE_CONFLICT',
            'One or more resources are already leased', 409,
            {'conflicts': _conflict_payload(active, now)})

    group_id = uuid4().hex
    expires_at = now + timedelta(seconds=ttl_seconds)
    rows = []
    for resource_key in resource_keys:
        row = ResourceLease(
            lease_group_id=group_id,
            resource_key=resource_key,
            resource_type=resource_key.split(':', 1)[0],
            active_slot=resource_key,
            owner_type=owner_type,
            owner_id=owner_id,
            controller_run_id=controller_run_id or None,
            priority=priority,
            status='active',
            ttl_seconds=ttl_seconds,
            idempotency_key=idem_key,
            request_hash=req_hash,
            holder_actor_type=actor['type'],
            holder_actor_id=actor['id'],
            holder_actor_name=actor['name'],
            metadata_json=metadata,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires_at,
        )
        db.session.add(row)
        rows.append(row)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        replay = _existing_idempotent(owner_type, owner_id, idem_key, req_hash)
        if replay:
            return replay
        conflicts = ResourceLease.query.filter(
            ResourceLease.active_slot.in_(resource_keys),
            ResourceLease.status == 'active',
        ).all()
        return _error(
            'RESOURCE_LEASE_CONFLICT',
            'One or more resources were concurrently leased', 409,
            {'conflicts': _conflict_payload(conflicts, datetime.now())})
    for row in rows:
        lease_event(row, 'acquired', actor, {
            'resource_keys': resource_keys,
            'ttl_seconds': ttl_seconds,
            'priority': priority,
        })
    db.session.commit()
    return jsonify(_group_payload(rows)), 201


@api_bp.route('/resource-leases', methods=['GET'])
def list_resource_leases():
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    now = datetime.now()
    resource_key = str(request.args.get('resource_key') or '').strip()
    expire_stale_resource_leases(now, [resource_key] if resource_key else None)
    db.session.commit()
    query = ResourceLease.query
    for field, column in (
        ('resource_key', ResourceLease.resource_key),
        ('status', ResourceLease.status),
        ('owner_type', ResourceLease.owner_type),
        ('owner_id', ResourceLease.owner_id),
        ('controller_run_id', ResourceLease.controller_run_id),
        ('lease_group_id', ResourceLease.lease_group_id),
    ):
        value = str(request.args.get(field) or '').strip()
        if value:
            query = query.filter(column == value)
    try:
        limit = min(500, max(1, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return _error('INVALID_PAGINATION', 'limit and offset must be integers')
    total = query.count()
    rows = (query.order_by(ResourceLease.created_at.desc(), ResourceLease.id.desc())
            .offset(offset).limit(limit).all())
    return jsonify({
        'items': [row.to_dict() for row in rows],
        'total': total,
        'limit': limit,
        'offset': offset,
    })


@api_bp.route('/resource-leases/<int:lease_id>/renew', methods=['POST'])
def renew_resource_lease(lease_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    lease = ResourceLease.query.filter_by(id=lease_id).with_for_update().first()
    if not lease:
        return _error('RESOURCE_LEASE_NOT_FOUND', 'Resource lease was not found', 404)
    if not _can_manage(actor, lease):
        return _error(
            'RESOURCE_LEASE_OWNER_MISMATCH',
            'Only the lease holder or an administrator may renew it', 403)
    now = datetime.now()
    if lease.status != 'active' or lease.expires_at <= now:
        expire_stale_resource_leases(now, [lease.resource_key])
        db.session.commit()
        return _error(
            'RESOURCE_LEASE_EXPIRED',
            'Expired or released leases cannot be renewed; acquire again', 409)
    data = request.get_json(silent=True) or {}
    try:
        ttl_seconds = normalize_ttl_seconds(
            data.get('ttl_seconds'), default=lease.ttl_seconds)
    except ValueError as exc:
        return _error('RESOURCE_LEASE_VALIDATION_FAILED', str(exc))
    rows = (ResourceLease.query.filter_by(
        lease_group_id=lease.lease_group_id, status='active')
        .with_for_update().all())
    renew_lease_group(rows, ttl_seconds, actor, now)
    db.session.commit()
    return jsonify(_group_payload(rows))


@api_bp.route('/resource-leases/<int:lease_id>/release', methods=['POST'])
def release_resource_lease(lease_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    lease = ResourceLease.query.filter_by(id=lease_id).with_for_update().first()
    if not lease:
        return _error('RESOURCE_LEASE_NOT_FOUND', 'Resource lease was not found', 404)
    if not _can_manage(actor, lease):
        return _error(
            'RESOURCE_LEASE_OWNER_MISMATCH',
            'Only the lease holder or an administrator may release it', 403)
    rows = (ResourceLease.query.filter_by(lease_group_id=lease.lease_group_id)
            .with_for_update().all())
    reason = str((request.get_json(silent=True) or {}).get('reason') or '').strip()
    release_lease_group(rows, actor, reason)
    db.session.commit()
    return jsonify(_group_payload(rows))


@api_bp.route('/resource-leases/<int:lease_id>/events', methods=['GET'])
def list_resource_lease_events(lease_id):
    actor = _actor()
    if not actor:
        return _error('UNAUTHENTICATED', 'Authentication is required', 401)
    lease = db.session.get(ResourceLease, lease_id)
    if not lease:
        return _error('RESOURCE_LEASE_NOT_FOUND', 'Resource lease was not found', 404)
    rows = (ResourceLeaseEvent.query.filter_by(lease_id=lease_id)
            .order_by(ResourceLeaseEvent.id.asc()).all())
    return jsonify({'items': [row.to_dict() for row in rows], 'total': len(rows)})
