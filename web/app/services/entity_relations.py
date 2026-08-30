"""Idempotent generic entity lineage and best-effort workflow integration."""

from datetime import datetime
import hashlib
import json
import logging
import re

from app import db
from app.models import EntityRelation


logger = logging.getLogger(__name__)

ENTITY_TYPES = {
    'commit',
    'requirement',
    'panorama_node',
    'module',
    'finding',
    'automation_case_candidate',
    'workflow_run',
    'testcase',
    'testcase_library_revision',
    'capability_gap',
    'bug',
    'learned_rule',
    'test_report',
    'evidence_manifest',
}

_SLUG_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


def normalize_entity_type(value, field_name):
    normalized = str(value or '').strip().lower()
    if normalized not in ENTITY_TYPES:
        raise ValueError(
            f'{field_name} must be one of: {", ".join(sorted(ENTITY_TYPES))}')
    return normalized


def normalize_entity_id(value, field_name):
    normalized = str(value if value is not None else '').strip()
    if not normalized:
        raise ValueError(f'{field_name} is required')
    if len(normalized) > 500:
        raise ValueError(f'{field_name} must not exceed 500 characters')
    return normalized


def normalize_relation_type(value):
    normalized = str(value or '').strip().lower()
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(
            'relation_type must be a lowercase identifier up to 64 characters')
    return normalized


def normalize_relation(item):
    if not isinstance(item, dict):
        raise ValueError('each relation must be an object')
    metadata = item.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise ValueError('metadata must be an object')
    normalized = {
        'from_type': normalize_entity_type(item.get('from_type'), 'from_type'),
        'from_id': normalize_entity_id(item.get('from_id'), 'from_id'),
        'relation_type': normalize_relation_type(item.get('relation_type')),
        'to_type': normalize_entity_type(item.get('to_type'), 'to_type'),
        'to_id': normalize_entity_id(item.get('to_id'), 'to_id'),
        'metadata': metadata,
    }
    if (normalized['from_type'], normalized['from_id']) == (
            normalized['to_type'], normalized['to_id']):
        raise ValueError('self-referential entity relations are not allowed')
    return normalized


def relation_key(project_id, relation):
    payload = {
        'project_id': int(project_id),
        'from_type': relation['from_type'],
        'from_id': relation['from_id'],
        'relation_type': relation['relation_type'],
        'to_type': relation['to_type'],
        'to_id': relation['to_id'],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return 'sha256:' + hashlib.sha256(encoded).hexdigest()


def _json_equal(left, right):
    return json.dumps(
        left or {}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ) == json.dumps(
        right or {}, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    )


def upsert_entity_relations(project_id, relations, actor_name='system'):
    """Upsert natural-key edges without committing the caller's transaction."""
    normalized = [normalize_relation(item) for item in relations]
    # De-duplicate inside one batch; the last metadata payload wins.
    by_key = {}
    for item in normalized:
        by_key[relation_key(project_id, item)] = item
    keys = sorted(by_key)
    existing = {
        row.relation_key: row
        for row in EntityRelation.query.filter(
            EntityRelation.relation_key.in_(keys)).all()
    } if keys else {}
    created = []
    updated = []
    unchanged = []
    now = datetime.now()
    for key in keys:
        item = by_key[key]
        row = existing.get(key)
        if row is None:
            row = EntityRelation(
                project_id=int(project_id),
                from_type=item['from_type'],
                from_id=item['from_id'],
                relation_type=item['relation_type'],
                to_type=item['to_type'],
                to_id=item['to_id'],
                relation_key=key,
                metadata_json=item['metadata'],
                created_by=actor_name,
                updated_by=actor_name,
            )
            db.session.add(row)
            created.append(row)
        elif not _json_equal(row.metadata_json, item['metadata']):
            row.metadata_json = item['metadata']
            row.updated_by = actor_name
            row.updated_at = now
            updated.append(row)
        else:
            unchanged.append(row)
    db.session.flush()
    return {
        'items': created + updated + unchanged,
        'created': created,
        'updated': updated,
        'unchanged': unchanged,
    }


def best_effort_upsert_relations(project_id, relations, actor_name='system'):
    """Write optional lineage in a SAVEPOINT and never poison core business work."""
    if not relations:
        return {'created': 0, 'updated': 0, 'unchanged': 0}, None
    try:
        with db.session.begin_nested():
            result = upsert_entity_relations(project_id, relations, actor_name)
        return {
            'created': len(result['created']),
            'updated': len(result['updated']),
            'unchanged': len(result['unchanged']),
        }, None
    except Exception as exc:
        logger.warning(
            'Entity lineage write failed for project %s: %s',
            project_id, exc, exc_info=True)
        return None, {
            'code': 'ENTITY_RELATION_WRITE_FAILED',
            'message': 'Lineage recording failed; core operation continued',
            'details': {'error_type': type(exc).__name__},
        }


def candidate_source_relations(candidate):
    """Build source commit/finding/module edges pointing to one candidate."""
    target_id = str(candidate.id)
    relations = []
    for ref in candidate.source_refs_json or []:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get('type') or '').strip().lower()
        if ref_type not in ENTITY_TYPES:
            continue
        ref_value = str(ref.get('value') or ref.get('id') or '').strip()
        if not ref_value:
            continue
        relations.append({
            'from_type': ref_type,
            'from_id': ref_value,
            'relation_type': 'source_of',
            'to_type': 'automation_case_candidate',
            'to_id': target_id,
            'metadata': ref.get('metadata') or {},
        })
    if candidate.module_key:
        relations.append({
            'from_type': 'module',
            'from_id': candidate.module_key,
            'relation_type': 'source_of',
            'to_type': 'automation_case_candidate',
            'to_id': target_id,
            'metadata': {'source_type': candidate.source_type or 'manual'},
        })
    return relations
