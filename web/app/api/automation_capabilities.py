"""Project-scoped, evidence-gated automation capability catalog API."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from uuid import uuid4

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import db
from app.api import api_bp
from app.api.auth_utils import (
    get_current_claw,
    get_current_user,
    is_admin_user,
    user_project_ids,
)
from app.models import (
    AutomationCapability,
    AutomationCapabilityEvent,
    AutomationCaseCandidate,
    AutomationCaseCandidateEvent,
    CapabilityGap,
    CapabilityGapEvent,
    EntityRelation,
    OpenClawInstance,
)
from app.services.entity_relations import upsert_entity_relations


STATUSES = {'planned', 'available', 'degraded', 'unavailable', 'retired'}
IMPLEMENTATION_STATUSES = {'declared', 'implemented'}
VERIFICATION_STATUSES = {'unverified', 'verified', 'failed'}
_CST = timezone(timedelta(hours=8))
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_COMMIT_RE = re.compile(r'^[0-9a-f]{7,64}$')
_MAX_HEALTH_LEASE = timedelta(hours=24)


def _now():
    return datetime.now(_CST).replace(tzinfo=None)


def _request_id():
    return str(request.headers.get('X-Request-ID') or uuid4().hex)[:128]


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
            'name': claw.name or f'claw:{claw.id}', 'claw': claw,
        }
    user = get_current_user()
    if user:
        return {
            'type': 'user', 'id': int(user.id),
            'name': user.username or f'user:{user.id}', 'user': user,
        }
    return None


def _can_access_project(actor, project_id):
    if is_admin_user():
        return True
    if not actor:
        return False
    if actor['type'] == 'claw':
        project = actor['claw'].project_id
        return project is None or int(project) == int(project_id)
    return int(project_id) in user_project_ids(actor['user'])


def _string_list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{field} must be an array')
    result = []
    for item in value:
        normalized = str(item or '').strip()
        if not normalized:
            raise ValueError(f'{field} must not contain empty values')
        if normalized not in result:
            result.append(normalized)
    return result


def _datetime_value(value, field):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError(f'{field} must be an ISO-8601 datetime') from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_CST).replace(tzinfo=None)
    return parsed


def _json_object(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    return value


def _idempotency_key(data):
    body_key = str(data.get('idempotency_key') or '').strip()
    header_key = str(request.headers.get('Idempotency-Key') or '').strip()
    if body_key and header_key and body_key != header_key:
        raise ValueError('Body and header idempotency keys must match')
    key = body_key or header_key
    if not key:
        raise ValueError('idempotency_key or Idempotency-Key header is required')
    if len(key) > 128:
        raise ValueError('idempotency_key must not exceed 128 characters')
    return key


def _request_hash(data):
    payload = dict(data)
    payload.pop('idempotency_key', None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)


def _capability_event(row, event_type, actor, from_status, version_before,
                      idempotency_key=None, request_hash=None, payload=None):
    event = AutomationCapabilityEvent(
        capability_id=row.id,
        event_type=event_type,
        from_status=from_status,
        to_status=row.status,
        version_before=version_before,
        version_after=row.version,
        payload_json=payload or {},
        actor_type=actor['type'],
        actor_id=actor['id'],
        actor_name=actor['name'],
        request_id=_request_id(),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    db.session.add(event)
    return event


def _idempotent_replay_response(row, actor, idempotency_key, fingerprint):
    replay = AutomationCapabilityEvent.query.filter_by(
        capability_id=row.id,
        actor_type=actor['type'],
        actor_id=actor['id'],
        idempotency_key=idempotency_key,
    ).first()
    if not replay:
        return None
    if replay.request_hash != fingerprint:
        return _error(
            'IDEMPOTENCY_KEY_REUSED',
            'The same idempotency key was used for another payload', 409)
    payload = dict((replay.payload_json or {}).get('response') or
                   _capability_payload(row))
    payload['idempotent_replay'] = True
    return jsonify(payload), 200


def _commit_publish_or_recover(project_id, capability_key, producer_claw_id, actor,
                               idempotency_key, fingerprint):
    """Recover an exactly-once response after a concurrent unique-key race."""
    try:
        db.session.commit()
        return None
    except IntegrityError:
        db.session.rollback()
    row = AutomationCapability.query.filter_by(
        project_id=project_id, capability_key=capability_key,
        producer_claw_id=producer_claw_id).first()
    if row:
        replay = _idempotent_replay_response(
            row, actor, idempotency_key, fingerprint)
        if replay:
            return replay
        return _error(
            'AUTOMATION_CAPABILITY_CONCURRENT_UPDATE',
            'Automation capability changed concurrently; read the current '
            'version and retry', 409, {'current_version': row.version})
    return _error(
        'AUTOMATION_CAPABILITY_WRITE_CONFLICT',
        'Automation capability publication conflicted with another write', 409)


def _capability_payload(row, with_events=False):
    payload = row.to_dict()
    eligible, reason = _capability_eligibility(row)
    payload['eligible_for_gap_resolution'] = eligible
    payload['eligibility_reason'] = reason
    if with_events:
        payload['events'] = [event.to_dict() for event in row.events]
    return payload


def _capability_eligibility(row, now=None):
    now = now or _now()
    checks = (
        (row.status == 'available', 'status_not_available'),
        (row.implementation_status == 'implemented', 'not_implemented'),
        (row.verification_status == 'verified', 'not_verified'),
        (bool(row.implementation_version), 'implementation_version_missing'),
        (bool(row.producer_claw_id), 'producer_claw_missing'),
        (bool(row.release_id), 'release_id_missing'),
        (bool(_COMMIT_RE.fullmatch(str(row.source_commit or '').lower())),
         'source_commit_invalid'),
        (bool(_SHA256_RE.fullmatch(str(row.manifest_sha256 or '').lower())),
         'manifest_sha256_invalid'),
        (bool(row.platforms_json), 'platforms_missing'),
        (bool(row.health_checked_at), 'health_check_missing'),
        (bool(row.health_expires_at), 'health_lease_missing'),
        (bool(row.health_expires_at and row.health_expires_at > now),
         'health_lease_expired'),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    verification = row.verification_json or {}
    for check_name in ('release_check', 'contract_check', 'health_probe'):
        check = verification.get(check_name)
        if not isinstance(check, dict):
            return False, f'{check_name}_missing'
        if str(check.get('status') or '').lower() not in ('passed', 'healthy'):
            return False, f'{check_name}_failed'
    evidence_refs = verification.get('evidence_refs')
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return False, 'verification_evidence_missing'
    return True, 'verified_available'


def _available_capability_snapshot(project_id, now=None):
    """Map exact contract tokens to verified, leased capability sources."""
    now = now or _now()
    token_sources = {}
    rows = AutomationCapability.query.filter_by(project_id=project_id).all()
    for row in rows:
        eligible, _ = _capability_eligibility(row, now)
        if not eligible:
            continue
        source = {
            'capability_id': row.id,
            'key': row.capability_key,
            'catalog_version': row.version,
            'implementation_version': row.implementation_version,
            'producer_claw_id': row.producer_claw_id,
            'release_id': row.release_id,
            'source_commit': row.source_commit,
            'manifest_sha256': row.manifest_sha256,
            'platforms': row.platforms_json or [],
            'health_checked_at': str(row.health_checked_at),
            'health_expires_at': str(row.health_expires_at),
            'verification': row.verification_json or {},
        }
        tokens = {
            row.capability_key,
            *(row.operations_json or []),
            *(row.observables_json or []),
            *(row.reset_hooks_json or []),
        }
        for token in tokens:
            normalized = str(token or '').strip()
            if normalized:
                token_sources.setdefault(normalized, []).append(source)
    return token_sources


def _gap_required_tokens(gap):
    required = set()
    for values in (
            gap.missing_capabilities_json,
            gap.required_operations_json,
            gap.required_observables_json,
            gap.required_reset_hooks_json):
        required.update(
            str(item).strip() for item in (values or []) if str(item).strip())
    return required


def _gap_required_platforms(gap, candidates):
    platforms = set()
    evidence = gap.evidence_json if isinstance(gap.evidence_json, dict) else {}
    raw_values = evidence.get('platforms')
    if raw_values is None and evidence.get('platform'):
        raw_values = [evidence['platform']]
    for value in raw_values if isinstance(raw_values, list) else []:
        normalized = str(value or '').strip()
        if normalized:
            platforms.add(normalized)
    for candidate in candidates:
        draft = candidate.case_draft_json or {}
        automation = draft.get('automation') if isinstance(draft, dict) else {}
        if not isinstance(automation, dict):
            continue
        raw_values = automation.get('platforms')
        if raw_values is None and automation.get('platform'):
            raw_values = [automation['platform']]
        for value in raw_values if isinstance(raw_values, list) else []:
            normalized = str(value or '').strip()
            if normalized:
                platforms.add(normalized)
    return platforms


def _sources_for_gap(required, required_platforms, token_sources):
    if not required or not required.issubset(set(token_sources)):
        return []
    by_id = {}
    for token in sorted(required):
        # Select one deterministic source for every token/platform pair.
        # Adding a redundant Worker is harmless; failover changes the source
        # signature and therefore requires a fresh canary.
        platform_targets = sorted(required_platforms) or [None]
        for platform in platform_targets:
            choices = [
                item for item in token_sources[token]
                if platform is None or platform in item['platforms']]
            if not choices:
                return []
            raw = sorted(choices, key=lambda item: item['capability_id'])[0]
            source = by_id.setdefault(raw['capability_id'], dict(raw))
            source.setdefault('matched_tokens', []).append(token)
            if platform:
                source.setdefault('matched_platforms', []).append(platform)
    for source in by_id.values():
        source['matched_tokens'] = sorted(set(source['matched_tokens']))
        source['matched_platforms'] = sorted(set(
            source.get('matched_platforms') or []))
    return [by_id[key] for key in sorted(by_id)]


def _source_signature(sources):
    stable = [{
        'capability_id': source['capability_id'],
        'implementation_version': source['implementation_version'],
        'producer_claw_id': source['producer_claw_id'],
        'release_id': source['release_id'],
        'source_commit': source['source_commit'],
        'manifest_sha256': source['manifest_sha256'],
        'matched_tokens': source['matched_tokens'],
        'matched_platforms': source['matched_platforms'],
    } for source in sources]
    return hashlib.sha256(_canonical(stable).encode('utf-8')).hexdigest()


def _sync_capability_gap_relations(project_id, gap, sources, actor_name, now):
    selected_ids = {str(source['capability_id']) for source in sources}
    existing = EntityRelation.query.filter_by(
        project_id=project_id,
        from_type='automation_capability',
        relation_type='satisfies',
        to_type='capability_gap',
        to_id=str(gap.id),
    ).all()
    for relation in existing:
        if relation.from_id in selected_ids:
            continue
        metadata = dict(relation.metadata_json or {})
        if metadata.get('active') is not False:
            metadata.update({
                'active': False,
                'invalidated_at': str(now),
            })
            relation.metadata_json = metadata
            relation.updated_by = actor_name
            relation.updated_at = now
    relations = []
    for source in sources:
        relations.append({
            'from_type': 'automation_capability',
            'from_id': str(source['capability_id']),
            'relation_type': 'satisfies',
            'to_type': 'capability_gap',
            'to_id': str(gap.id),
            'metadata': {
                'active': True,
                'matched_tokens': source['matched_tokens'],
                'matched_platforms': source['matched_platforms'],
                'catalog_version': source['catalog_version'],
                'implementation_version': source['implementation_version'],
                'producer_claw_id': source['producer_claw_id'],
                'release_id': source['release_id'],
                'source_commit': source['source_commit'],
                'manifest_sha256': source['manifest_sha256'],
                'health_expires_at': source['health_expires_at'],
            },
        })
    if relations:
        upsert_entity_relations(project_id, relations, actor_name)


def _candidate_transition(candidate, target_state, event_type, gap, sources,
                          actor, now, idempotency_key=None):
    if candidate.state == target_state:
        return False
    previous_state = candidate.state
    version_before = int(candidate.version or 1)
    previous_qualification = {
        'outcome': candidate.qualification_outcome,
        'run_id': candidate.qualification_run_id,
        'evidence': candidate.qualification_evidence_json or {},
    }
    candidate.state = target_state
    candidate.qualification_outcome = None
    candidate.qualification_run_id = None
    candidate.qualification_evidence_json = {}
    candidate.version = version_before + 1
    candidate.updated_by = actor['name']
    candidate.updated_at = now
    db.session.add(AutomationCaseCandidateEvent(
        candidate_id=candidate.id,
        event_type=event_type,
        from_state=previous_state,
        to_state=target_state,
        version_before=version_before,
        version_after=candidate.version,
        payload_json={
            'capability_gap_id': gap.id,
            'capability_sources': sources,
            'previous_qualification': previous_qualification,
        },
        actor_type=actor['type'], actor_id=actor['id'],
        actor_name=actor['name'], request_id=_request_id(),
        idempotency_key=idempotency_key,
    ))
    return True


def _reconcile_gaps_and_candidates(project_id, actor, idempotency_key=None):
    """Resolve, invalidate and requalify Gaps from exact capability tokens."""
    now = _now()
    token_sources = _available_capability_snapshot(project_id, now)
    result = {
        'resolved_gap_ids': [],
        'reopened_gap_ids': [],
        'requalified_gap_ids': [],
        'requeued_candidate_ids': [],
        'waiting_candidate_ids': [],
        'quarantined_candidate_ids': [],
    }
    gaps = (CapabilityGap.query.filter_by(project_id=project_id)
            .filter(CapabilityGap.status.in_(('open', 'in_progress', 'resolved')))
            .order_by(CapabilityGap.id.asc()).with_for_update().all())
    for gap in gaps:
        required = _gap_required_tokens(gap)
        candidates = (AutomationCaseCandidate.query.filter_by(
            capability_gap_id=gap.id).order_by(
                AutomationCaseCandidate.id.asc()).with_for_update().all())
        required_platforms = _gap_required_platforms(gap, candidates)
        sources = _sources_for_gap(
            required, required_platforms, token_sources)
        _sync_capability_gap_relations(
            project_id, gap, sources, actor['name'], now)
        previous_resolution = gap.resolution_json or {}
        managed = previous_resolution.get('source') == 'capability_catalog'
        signature = _source_signature(sources) if sources else ''
        old_signature = str(previous_resolution.get('source_signature') or '')

        if sources and gap.status in ('open', 'in_progress'):
            gap_before = int(gap.version or 1)
            previous_status = gap.status
            candidate_relations = []
            for candidate in candidates:
                if candidate.state not in ('WAITING_CAPABILITY', 'QUARANTINED'):
                    continue
                if _candidate_transition(
                        candidate, 'READY_FOR_CANARY',
                        'capability_catalog_auto_requeued', gap, sources,
                        actor, now, idempotency_key):
                    result['requeued_candidate_ids'].append(candidate.id)
                    candidate_relations.append({
                        'from_type': 'automation_case_candidate',
                        'from_id': str(candidate.id),
                        'relation_type': 'unblocked_by',
                        'to_type': 'capability_gap',
                        'to_id': str(gap.id),
                        'metadata': {
                            'gap_version': gap_before + 1,
                            'resolution': 'capability_catalog_auto_resolved',
                            'source_signature': signature,
                        },
                    })
            if candidate_relations:
                upsert_entity_relations(
                    project_id, candidate_relations, actor['name'])
            gap.status = 'resolved'
            gap.resolution_json = {
                'source': 'capability_catalog',
                'summary': 'Verified capability sources satisfy every exact token',
                'required_tokens': sorted(required),
                'required_platforms': sorted(required_platforms),
                'source_signature': signature,
                'capability_sources': sources,
                'auto_requeued_candidate_ids': [
                    row.id for row in candidates
                    if row.state == 'READY_FOR_CANARY'],
            }
            gap.resolved_by = actor['name']
            gap.resolved_at = now
            gap.candidate_requeue_pending = False
            gap.version = gap_before + 1
            gap.updated_by = actor['name']
            gap.updated_at = now
            db.session.add(CapabilityGapEvent(
                capability_gap_id=gap.id,
                event_type='capability_catalog_auto_resolved',
                from_status=previous_status, to_status='resolved',
                version_before=gap_before, version_after=gap.version,
                payload_json=gap.resolution_json,
                actor_type=actor['type'], actor_id=actor['id'],
                actor_name=actor['name'], request_id=_request_id(),
                idempotency_key=idempotency_key,
            ))
            result['resolved_gap_ids'].append(gap.id)
            continue

        if sources and gap.status == 'resolved' and managed:
            if signature == old_signature:
                continue
            gap_before = int(gap.version or 1)
            changed_candidates = []
            for candidate in candidates:
                if candidate.state in ('QUALIFIED', 'PENDING_PUBLISH'):
                    if _candidate_transition(
                            candidate, 'READY_FOR_CANARY',
                            'capability_catalog_requalification_required',
                            gap, sources, actor, now, idempotency_key):
                        changed_candidates.append(candidate.id)
                        result['requeued_candidate_ids'].append(candidate.id)
                elif candidate.state == 'ACTIVE':
                    if _candidate_transition(
                            candidate, 'READY_FOR_CANARY',
                            'capability_catalog_requalification_required',
                            gap, sources, actor, now, idempotency_key):
                        changed_candidates.append(candidate.id)
                        result['requeued_candidate_ids'].append(candidate.id)
            gap.resolution_json = {
                'source': 'capability_catalog',
                'summary': 'Capability source version changed; qualification refreshed',
                'required_tokens': sorted(required),
                'required_platforms': sorted(required_platforms),
                'source_signature': signature,
                'capability_sources': sources,
                'requalification_candidate_ids': changed_candidates,
                'previous_resolution': previous_resolution,
            }
            gap.version = gap_before + 1
            gap.updated_by = actor['name']
            gap.updated_at = now
            db.session.add(CapabilityGapEvent(
                capability_gap_id=gap.id,
                event_type='capability_catalog_sources_changed',
                from_status='resolved', to_status='resolved',
                version_before=gap_before, version_after=gap.version,
                payload_json=gap.resolution_json,
                actor_type=actor['type'], actor_id=actor['id'],
                actor_name=actor['name'], request_id=_request_id(),
                idempotency_key=idempotency_key,
            ))
            result['requalified_gap_ids'].append(gap.id)
            continue

        if not sources and gap.status == 'resolved' and managed:
            gap_before = int(gap.version or 1)
            for candidate in candidates:
                if candidate.state in (
                        'READY_FOR_CANARY', 'QUALIFIED', 'PENDING_PUBLISH'):
                    if _candidate_transition(
                            candidate, 'WAITING_CAPABILITY',
                            'capability_catalog_invalidated', gap, [], actor,
                            now, idempotency_key):
                        result['waiting_candidate_ids'].append(candidate.id)
                elif candidate.state == 'ACTIVE':
                    if _candidate_transition(
                            candidate, 'QUARANTINED',
                            'capability_catalog_active_candidate_quarantined',
                            gap, [], actor, now, idempotency_key):
                        result['quarantined_candidate_ids'].append(candidate.id)
            gap.status = 'open'
            gap.resolution_json = {
                'source': 'capability_catalog',
                'summary': 'Capability coverage expired or became unavailable',
                'required_tokens': sorted(required),
                'required_platforms': sorted(required_platforms),
                'source_signature': '',
                'capability_sources': [],
                'invalidated_resolution': previous_resolution,
            }
            gap.resolved_by = ''
            gap.resolved_at = None
            gap.candidate_requeue_pending = any(
                row.state == 'WAITING_CAPABILITY' for row in candidates)
            gap.version = gap_before + 1
            gap.updated_by = actor['name']
            gap.updated_at = now
            db.session.add(CapabilityGapEvent(
                capability_gap_id=gap.id,
                event_type='capability_catalog_resolution_invalidated',
                from_status='resolved', to_status='open',
                version_before=gap_before, version_after=gap.version,
                payload_json=gap.resolution_json,
                actor_type=actor['type'], actor_id=actor['id'],
                actor_name=actor['name'], request_id=_request_id(),
                idempotency_key=idempotency_key,
            ))
            result['reopened_gap_ids'].append(gap.id)
    return result


def _expire_stale_capabilities(project_id):
    now = _now()
    actor = {'type': 'system', 'id': 0,
             'name': 'hub-capability-health-reconciler'}
    expired = []
    rows = (AutomationCapability.query.filter_by(
        project_id=project_id, status='available').filter(
            AutomationCapability.health_expires_at.isnot(None),
            AutomationCapability.health_expires_at <= now,
        ).with_for_update().all())
    for row in rows:
        version_before = int(row.version or 1)
        row.status = 'unavailable'
        row.verification_status = 'failed'
        row.version = version_before + 1
        row.updated_by = actor['name']
        row.updated_at = now
        _capability_event(
            row, 'capability_health_lease_expired', actor, 'available',
            version_before, payload={
                'health_expires_at': str(row.health_expires_at),
                'reason': 'health_lease_expired',
            })
        expired.append(row.id)
    reconciliation = _reconcile_gaps_and_candidates(project_id, actor)
    reconciliation['expired_capability_ids'] = expired
    return reconciliation


@api_bp.route('/automation-capabilities', methods=['GET'])
def list_automation_capabilities():
    actor = _actor()
    project_id = request.args.get('project_id', type=int)
    if not project_id:
        return _error('PROJECT_ID_REQUIRED', 'project_id is required')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', 'Project access denied', 403)
    reconciliation = _expire_stale_capabilities(project_id)
    db.session.commit()
    query = AutomationCapability.query.filter_by(project_id=project_id)
    producer_filter = request.args.get('producer_claw_id')
    if producer_filter not in (None, ''):
        try:
            query = query.filter_by(producer_claw_id=int(producer_filter))
        except (TypeError, ValueError):
            return _error('INVALID_PRODUCER_CLAW_ID',
                          'producer_claw_id must be an integer')
    if request.args.get('status'):
        query = query.filter_by(status=request.args['status'])
    if request.args.get('platform'):
        # JSON containment differs between SQLite/MariaDB; keep catalog sizes
        # bounded and apply the portable filter after the project query.
        platform = request.args['platform']
    else:
        platform = None
    page = max(request.args.get('page', 1, type=int), 1)
    page_size = min(max(request.args.get('page_size', 50, type=int), 1), 200)
    rows = query.order_by(
        AutomationCapability.capability_key.asc(),
        AutomationCapability.producer_claw_id.asc()).all()
    if platform:
        rows = [row for row in rows if platform in (row.platforms_json or [])]
    total = len(rows)
    start = (page - 1) * page_size
    include_events = str(request.args.get('include_events') or '').lower() in {
        '1', 'true', 'yes'}
    all_rows = AutomationCapability.query.filter_by(project_id=project_id).all()
    eligible_count = sum(
        1 for row in all_rows if _capability_eligibility(row)[0])
    return jsonify({
        'items': [
            _capability_payload(row, include_events)
            for row in rows[start:start + page_size]],
        'total': total,
        'page': page,
        'page_size': page_size,
        'diagnostics': {
            'catalog_total': len(all_rows),
            'eligible_for_gap_resolution': eligible_count,
            'ineligible': len(all_rows) - eligible_count,
            'expired_capability_ids': reconciliation[
                'expired_capability_ids'],
        },
    })


def _publish_values(data, row, actor):
    def text_value(key, current='', limit=None):
        value = data[key] if key in data else current
        normalized = str(value or '').strip()
        return normalized[:limit] if limit else normalized

    def list_value(key, field, current=None):
        if key not in data:
            return list(current or [])
        return _string_list(data.get(key), field)

    status = text_value('status', row.status if row else 'planned').lower()
    implementation_status = text_value(
        'implementation_status',
        row.implementation_status if row else 'declared').lower()
    verification_status = text_value(
        'verification_status',
        row.verification_status if row else 'unverified').lower()
    if status not in STATUSES:
        raise ValueError(f'status must be one of {sorted(STATUSES)}')
    if implementation_status not in IMPLEMENTATION_STATUSES:
        raise ValueError(
            'implementation_status must be one of '
            f'{sorted(IMPLEMENTATION_STATUSES)}')
    if verification_status not in VERIFICATION_STATUSES:
        raise ValueError(
            'verification_status must be one of '
            f'{sorted(VERIFICATION_STATUSES)}')

    producer = data.get(
        'producer_claw_id', row.producer_claw_id if row else None)
    if row and producer in (None, ''):
        producer = row.producer_claw_id
    if actor['type'] == 'claw':
        if producer not in (None, '', actor['id'], str(actor['id'])):
            raise PermissionError(
                'A Worker may only publish its own capability health')
        producer = actor['id']
    if producer not in (None, ''):
        try:
            producer = int(producer)
        except (TypeError, ValueError):
            raise ValueError('producer_claw_id must be an integer') from None

    health_checked = (
        _datetime_value(data.get('health_checked_at'), 'health_checked_at')
        if 'health_checked_at' in data else
        (row.health_checked_at if row else None))
    health_expires = (
        _datetime_value(data.get('health_expires_at'), 'health_expires_at')
        if 'health_expires_at' in data else
        (row.health_expires_at if row else None))
    verification = (
        _json_object(data.get('verification'), 'verification')
        if 'verification' in data else
        dict(row.verification_json or {}) if row else {})
    return {
        'name': text_value('name', row.name if row else '', 300),
        'status': status,
        'implementation_status': implementation_status,
        'verification_status': verification_status,
        'implementation_version': text_value(
            'implementation_version',
            row.implementation_version if row else '', 80),
        'producer_claw_id': producer,
        'release_id': text_value(
            'release_id', row.release_id if row else '', 255),
        'source_commit': text_value(
            'source_commit', row.source_commit if row else '', 64).lower(),
        'manifest_sha256': text_value(
            'manifest_sha256', row.manifest_sha256 if row else '', 64).lower(),
        'operations_json': list_value(
            'operations', 'operations', row.operations_json if row else []),
        'observables_json': list_value(
            'observables', 'observables', row.observables_json if row else []),
        'reset_hooks_json': list_value(
            'reset_hooks', 'reset_hooks', row.reset_hooks_json if row else []),
        'platforms_json': list_value(
            'platforms', 'platforms', row.platforms_json if row else []),
        'verification_json': verification,
        'health_checked_at': health_checked,
        'health_expires_at': health_expires,
    }


def _validate_available_values(project_id, values):
    if values['status'] != 'available':
        return
    required = {
        'implementation_status': 'implemented',
        'verification_status': 'verified',
    }
    for field, expected in required.items():
        if values[field] != expected:
            raise ValueError(
                f'available capability requires {field}={expected}')
    for field in ('implementation_version', 'release_id'):
        if not values[field]:
            raise ValueError(f'available capability requires {field}')
    if not _COMMIT_RE.fullmatch(values['source_commit']):
        raise ValueError(
            'available capability requires a 7-64 character hex source_commit')
    if not _SHA256_RE.fullmatch(values['manifest_sha256']):
        raise ValueError(
            'available capability requires a lowercase SHA-256 manifest_sha256')
    if not values['platforms_json']:
        raise ValueError('available capability requires at least one platform')
    producer_id = values['producer_claw_id']
    producer = db.session.get(OpenClawInstance, producer_id) if producer_id else None
    if not producer or producer.deleted_at:
        raise ValueError('available capability requires an active producer_claw_id')
    if producer.project_id not in (None, project_id):
        raise ValueError('producer_claw_id belongs to another project')
    checked = values['health_checked_at']
    expires = values['health_expires_at']
    if not checked or not expires or expires <= checked:
        raise ValueError(
            'available capability requires a health lease ending after its check')
    if expires - checked > _MAX_HEALTH_LEASE:
        raise ValueError('available capability health lease must not exceed 24 hours')
    if checked > _now() + timedelta(minutes=5):
        raise ValueError('health_checked_at is too far in the future')
    if expires <= _now():
        raise ValueError('available capability health lease has already expired')
    verification = values['verification_json']
    for check_name in ('release_check', 'contract_check', 'health_probe'):
        check = verification.get(check_name)
        if (not isinstance(check, dict) or
                str(check.get('status') or '').lower()
                not in ('passed', 'healthy')):
            raise ValueError(
                'available capability requires a passed '
                f'verification.{check_name}')
    evidence_refs = verification.get('evidence_refs')
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ValueError(
            'available capability requires verification.evidence_refs')
    if not all(
            (isinstance(ref, str) and bool(ref.strip())) or
            (isinstance(ref, dict) and bool(
                str(ref.get('type') or '').strip()) and bool(
                str(ref.get('id') or ref.get('value') or '').strip()))
            for ref in evidence_refs):
        raise ValueError(
            'verification.evidence_refs must contain typed references')


def _same_publish_values(row, values):
    for field, value in values.items():
        current = getattr(row, field)
        if field.endswith('_json'):
            if _canonical(current or ([] if isinstance(value, list) else {})) != _canonical(value):
                return False
        elif current != value:
            return False
    return True


@api_bp.route('/automation-capabilities', methods=['POST'])
def upsert_automation_capability():
    actor = _actor()
    data = request.get_json(silent=True) or {}
    try:
        project_id = int(data.get('project_id'))
    except (TypeError, ValueError):
        return _error('PROJECT_ID_REQUIRED', 'project_id must be an integer')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', 'Project access denied', 403)
    key = str(data.get('key') or '').strip()
    if not key:
        return _error(
            'AUTOMATION_CAPABILITY_INVALID', 'key is required')
    try:
        idem_key = _idempotency_key(data)
    except ValueError as exc:
        return _error('IDEMPOTENCY_KEY_REQUIRED', str(exc))
    fingerprint = _request_hash(data)
    producer = data.get('producer_claw_id')
    if actor['type'] == 'claw':
        producer = actor['id']
    elif producer not in (None, ''):
        try:
            producer = int(producer)
        except (TypeError, ValueError):
            return _error('INVALID_PRODUCER_CLAW_ID',
                          'producer_claw_id must be an integer')
    else:
        producer = None
    matching = (AutomationCapability.query.filter_by(
        project_id=project_id, capability_key=key)
        .with_for_update())
    if producer is not None:
        row = matching.filter_by(producer_claw_id=producer).first()
    else:
        rows = matching.limit(2).all()
        if len(rows) > 1:
            return _error('PRODUCER_CLAW_ID_REQUIRED',
                          'producer_claw_id is required when multiple Workers '
                          'publish the same capability')
        row = rows[0] if rows else None
    created = row is None
    if row:
        replay = _idempotent_replay_response(
            row, actor, idem_key, fingerprint)
        if replay:
            return replay
    try:
        values = _publish_values(data, row, actor)
        if not values['name']:
            raise ValueError('name is required')
        _validate_available_values(project_id, values)
    except PermissionError as exc:
        return _error('CAPABILITY_PRODUCER_MISMATCH', str(exc), 403)
    except ValueError as exc:
        return _error('AUTOMATION_CAPABILITY_INVALID', str(exc))
    if row:
        try:
            expected_version = int(data.get('expected_version'))
        except (TypeError, ValueError):
            return _error(
                'EXPECTED_VERSION_REQUIRED',
                'expected_version is required when updating')
        if expected_version != row.version:
            return _error(
                'AUTOMATION_CAPABILITY_VERSION_CONFLICT',
                'Automation capability has been updated by another writer',
                409, {'expected_version': expected_version,
                      'current_version': row.version})
        if _same_publish_values(row, values):
            payload = _capability_payload(row)
            payload['unchanged'] = True
            payload['auto_requalification'] = {
                'resolved_gap_ids': [], 'reopened_gap_ids': [],
                'requalified_gap_ids': [], 'requeued_candidate_ids': [],
                'waiting_candidate_ids': [], 'quarantined_candidate_ids': [],
            }
            event = _capability_event(
                row, 'capability_publication_noop', actor, row.status,
                row.version, idem_key, fingerprint)
            event.payload_json = {'response': payload}
            recovered = _commit_publish_or_recover(
                project_id, key, values['producer_claw_id'], actor,
                idem_key, fingerprint)
            if recovered:
                return recovered
            return jsonify(payload), 200
        version_before = int(row.version or 1)
        previous_status = row.status
        row.version = version_before + 1
    else:
        row = AutomationCapability(
            project_id=project_id,
            capability_key=key,
            version=1,
            created_by=actor['name'],
        )
        db.session.add(row)
        version_before = 0
        previous_status = None
    for field, value in values.items():
        setattr(row, field, value)
    row.updated_by = actor['name']
    row.updated_at = _now()
    db.session.flush()
    auto_requalification = _reconcile_gaps_and_candidates(
        project_id, actor, idem_key)
    payload = _capability_payload(row)
    payload['auto_requalification'] = auto_requalification
    event = _capability_event(
        row,
        'capability_registered' if created else 'capability_published',
        actor, previous_status, version_before, idem_key, fingerprint)
    event.payload_json = {
        'response': payload,
        'verification': row.verification_json or {},
    }
    recovered = _commit_publish_or_recover(
        project_id, key, values['producer_claw_id'], actor,
        idem_key, fingerprint)
    if recovered:
        return recovered
    return jsonify(payload), 201 if created else 200


@api_bp.route('/automation-capabilities/reconcile', methods=['POST'])
def reconcile_automation_capabilities():
    actor = _actor()
    data = request.get_json(silent=True) or {}
    try:
        project_id = int(data.get('project_id'))
    except (TypeError, ValueError):
        return _error('PROJECT_ID_REQUIRED', 'project_id must be an integer')
    if not _can_access_project(actor, project_id):
        return _error('PROJECT_ACCESS_DENIED', 'Project access denied', 403)
    result = _expire_stale_capabilities(project_id)
    db.session.commit()
    return jsonify(result)
