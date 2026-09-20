"""Shared rules for atomic TTL resource leases."""

from datetime import datetime, timedelta
import hashlib
import json

from app import db
from flask import current_app
from app.models import ResourceLease, ResourceLeaseEvent, WorkflowRun


RESOURCE_PREFIXES = {'unity', 'bridge', 'device'}
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
DEFAULT_TTL_SECONDS = 900


def normalize_resource_keys(value):
    if not isinstance(value, list) or not value:
        raise ValueError('resource_keys must be a non-empty array')
    if len(value) > 50:
        raise ValueError('resource_keys must not contain more than 50 items')
    result = []
    for index, item in enumerate(value):
        key = str(item or '').strip()
        if not key or len(key) > 255 or ':' not in key:
            raise ValueError(
                f'resource_keys[{index}] must be a valid namespaced key')
        prefix, remainder = key.split(':', 1)
        if prefix == 'account':
            raise ValueError(
                'account resources must use the existing test-account-manager API')
        if prefix == 'testcase_library':
            raise ValueError(
                'testcase library writers are protected by the promotion database lock')
        if prefix not in RESOURCE_PREFIXES or not remainder.strip():
            allowed = ', '.join(f'{item}:*' for item in sorted(RESOURCE_PREFIXES))
            raise ValueError(f'unsupported resource key; allowed prefixes: {allowed}')
        if key not in result:
            result.append(key)
    return sorted(result)


def normalize_ttl_seconds(value, default=DEFAULT_TTL_SECONDS):
    try:
        parsed = int(default if value in (None, '') else value)
    except (TypeError, ValueError):
        raise ValueError('ttl_seconds must be an integer') from None
    if parsed < MIN_TTL_SECONDS or parsed > MAX_TTL_SECONDS:
        raise ValueError(
            f'ttl_seconds must be between {MIN_TTL_SECONDS} and '
            f'{MAX_TTL_SECONDS}')
    return parsed


def canonical_request_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()


def lease_event(lease, event_type, actor, payload=None):
    row = ResourceLeaseEvent(
        lease_id=lease.id,
        lease_group_id=lease.lease_group_id,
        event_type=event_type,
        actor_type=actor.get('type') or 'system',
        actor_id=actor.get('id'),
        actor_name=actor.get('name') or '',
        payload_json=payload or {},
    )
    db.session.add(row)
    return row


def expire_stale_resource_leases(now=None, resource_keys=None):
    """TTL is not proof of stop. Protected groups retain their unique slots."""
    now = now or datetime.now()
    query = ResourceLease.query.filter(
        ResourceLease.status == 'active',
        ResourceLease.expires_at <= now,
    )
    if resource_keys:
        query = query.filter(ResourceLease.resource_key.in_(resource_keys))
    stale = query.with_for_update().all()
    group_ids = sorted({row.lease_group_id for row in stale})
    if not group_ids:
        return []
    rows = (ResourceLease.query.filter(
        ResourceLease.lease_group_id.in_(group_ids),
        ResourceLease.status == 'active',
    ).with_for_update().all())
    system_actor = {'type': 'system', 'id': None, 'name': 'lease-expiry'}
    protected_groups = {row.lease_group_id for row in rows if needs_stop_confirmation(row)}
    for row in rows:
        protected = row.lease_group_id in protected_groups
        row.status = 'quarantined' if protected else 'expired'
        if not protected:
            row.active_slot = None
            row.released_at = now
        row.release_reason = 'stop_confirmation_required' if protected else 'ttl_expired'
        lease_event(row, row.status, system_actor, {
            'expired_at': str(now),
            'previous_expires_at': str(row.expires_at),
        })
    db.session.flush()
    return rows


def needs_stop_confirmation(lease):
    from app.services.agent_teams import enabled
    if enabled(current_app.config.get('RESOURCE_LEASE_RECONCILIATION_ENABLED')):
        return True
    if (lease.metadata_json or {}).get('requires_stop_confirmation') is True:
        return True
    if lease.owner_type in ('workflow_run', 'run'):
        try:
            run = db.session.get(WorkflowRun, int(lease.owner_id))
        except (ValueError, TypeError):
            run = None
        if run and (run.context_json or {}).get('mission', {}).get('control_mode') == 'team_managed':
            return True
    return False


def renew_lease_group(rows, ttl_seconds, actor, now=None):
    now = now or datetime.now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    for row in rows:
        row.ttl_seconds = ttl_seconds
        row.heartbeat_at = now
        row.expires_at = expires_at
        lease_event(row, 'renewed', actor, {
            'ttl_seconds': ttl_seconds,
            'expires_at': str(expires_at),
        })
    return expires_at


def release_lease_group(rows, actor, reason='', now=None):
    now = now or datetime.now()
    for row in rows:
        if row.status != 'active':
            continue
        row.status = 'released'
        row.active_slot = None
        row.released_at = now
        row.release_reason = str(reason or '')[:255]
        lease_event(row, 'released', actor, {
            'reason': row.release_reason,
        })
    return now
