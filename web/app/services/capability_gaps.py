"""Capability Gap lifecycle normalization helpers."""

import hashlib
import json

from app.services.automation_candidates import (
    capability_gap_key,
    normalize_string_list,
)


CAPABILITY_GAP_STATUSES = {'open', 'in_progress', 'resolved'}


def canonical_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def normalize_requirement(value):
    if value in (None, ''):
        return {'ref': '', 'url': '', 'status': ''}
    if not isinstance(value, dict):
        raise ValueError('development_requirement must be an object')
    ref = str(value.get('ref') or value.get('id') or '').strip()
    url = str(value.get('url') or '').strip()
    status = str(value.get('status') or '').strip().lower()
    if len(ref) > 500:
        raise ValueError('development_requirement.ref must not exceed 500 characters')
    if len(url) > 1000:
        raise ValueError('development_requirement.url must not exceed 1000 characters')
    if url and '://' not in url:
        raise ValueError('development_requirement.url must be an absolute URL')
    if len(status) > 64:
        raise ValueError('development_requirement.status must not exceed 64 characters')
    return {'ref': ref, 'url': url, 'status': status}


def normalize_evidence(value):
    if value in (None, ''):
        return {}
    if not isinstance(value, dict):
        raise ValueError('evidence must be an object')
    size = len(json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), default=str
    ).encode('utf-8'))
    if size > 65536:
        raise ValueError('evidence must not exceed 64 KiB')
    return value


def normalize_gap_payload(data, project_id, existing=None):
    missing = normalize_string_list(
        data.get('missing_capabilities')
        if 'missing_capabilities' in data
        else getattr(existing, 'missing_capabilities_json', None),
        'missing_capabilities')
    operations = normalize_string_list(
        data.get('required_operations')
        if 'required_operations' in data
        else getattr(existing, 'required_operations_json', None),
        'required_operations')
    observables = normalize_string_list(
        data.get('required_observables')
        if 'required_observables' in data
        else getattr(existing, 'required_observables_json', None),
        'required_observables')
    reset_hooks = normalize_string_list(
        data.get('required_reset_hooks')
        if 'required_reset_hooks' in data
        else getattr(existing, 'required_reset_hooks_json', None),
        'required_reset_hooks')
    if not any((missing, operations, observables, reset_hooks)):
        raise ValueError(
            'at least one missing capability, operation, observable, or reset hook '
            'is required')
    key_material = missing + [
        f'operation:{value}' for value in operations
    ] + [
        f'observable:{value}' for value in observables
    ] + [
        f'reset_hook:{value}' for value in reset_hooks
    ]
    gap_key = str(data.get('gap_key') or getattr(existing, 'gap_key', '') or '').strip()
    if not gap_key:
        gap_key = capability_gap_key(project_id, key_material)
    if len(gap_key) > 255:
        raise ValueError('gap_key must not exceed 255 characters')
    title = str(data.get('title') or getattr(existing, 'title', '') or '').strip()
    if not title:
        title = 'Automation capability gap: ' + ', '.join(key_material[:3])
    owner = str(data.get('owner') if 'owner' in data
                else getattr(existing, 'owner', '') or '').strip()
    if len(owner) > 160:
        raise ValueError('owner must not exceed 160 characters')
    requirement = normalize_requirement(
        data.get('development_requirement')
        if 'development_requirement' in data else {
            'ref': getattr(existing, 'development_requirement_ref', ''),
            'url': getattr(existing, 'development_requirement_url', ''),
            'status': getattr(existing, 'development_requirement_status', ''),
        })
    evidence = normalize_evidence(
        data.get('evidence') if 'evidence' in data
        else getattr(existing, 'evidence_json', None))
    return {
        'gap_key': gap_key,
        'title': title[:300],
        'missing_capabilities_json': missing,
        'required_operations_json': operations,
        'required_observables_json': observables,
        'required_reset_hooks_json': reset_hooks,
        'evidence_json': evidence,
        'owner': owner,
        'development_requirement_ref': requirement['ref'],
        'development_requirement_url': requirement['url'],
        'development_requirement_status': requirement['status'],
    }


def gap_request_hash(operation, data):
    return canonical_hash({'operation': operation, 'body': data})
