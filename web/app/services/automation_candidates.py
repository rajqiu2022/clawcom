"""Pure lifecycle rules for automation case candidates."""

import hashlib
import json


CANDIDATE_STATES = {
    'DISCOVERED',
    'DESIGNED',
    'WAITING_CAPABILITY',
    'READY_FOR_CANARY',
    'QUALIFIED',
    'PENDING_PUBLISH',
    'ACTIVE',
    'CASE_DESIGN_ERROR',
    'PRODUCT_BUG_CANDIDATE',
    'ENV_BLOCKED',
    'QUARANTINED',
    'RETIRED',
}

QUALIFICATION_OUTCOMES = {
    'QUALIFIED',
    'AUTOMATION_CAPABILITY_GAP',
    'CASE_DESIGN_ERROR',
    'PRODUCT_BUG_CANDIDATE',
}

QUALIFICATION_TARGET_STATES = {
    'QUALIFIED': 'QUALIFIED',
    'AUTOMATION_CAPABILITY_GAP': 'WAITING_CAPABILITY',
    'CASE_DESIGN_ERROR': 'CASE_DESIGN_ERROR',
    'PRODUCT_BUG_CANDIDATE': 'PRODUCT_BUG_CANDIDATE',
}

# ACTIVE is deliberately excluded from client-driven transitions. Promotion is
# the only service allowed to activate a candidate and bind a production case.
CANDIDATE_TRANSITIONS = {
    'DISCOVERED': {'DESIGNED', 'RETIRED'},
    'DESIGNED': {
        'WAITING_CAPABILITY', 'READY_FOR_CANARY', 'CASE_DESIGN_ERROR',
        'RETIRED',
    },
    'WAITING_CAPABILITY': {'READY_FOR_CANARY', 'RETIRED'},
    'READY_FOR_CANARY': {
        'QUALIFIED', 'WAITING_CAPABILITY', 'CASE_DESIGN_ERROR',
        'PRODUCT_BUG_CANDIDATE', 'ENV_BLOCKED', 'RETIRED',
    },
    'ENV_BLOCKED': {'READY_FOR_CANARY', 'RETIRED'},
    'QUALIFIED': {'PENDING_PUBLISH', 'QUARANTINED', 'RETIRED'},
    'PENDING_PUBLISH': {'QUALIFIED', 'QUARANTINED', 'RETIRED'},
    'ACTIVE': {'QUARANTINED', 'RETIRED'},
    'QUARANTINED': {'READY_FOR_CANARY', 'RETIRED'},
    'CASE_DESIGN_ERROR': {'DESIGNED', 'RETIRED'},
    'PRODUCT_BUG_CANDIDATE': {'DESIGNED', 'RETIRED'},
    'RETIRED': set(),
}


def normalize_string_list(value, field_name, max_items=100):
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise ValueError(f'{field_name}: expected an array')
    result = []
    for index, item in enumerate(value[:max_items]):
        text = str(item or '').strip()
        if not text:
            raise ValueError(f'{field_name}[{index}]: expected a non-empty string')
        if text not in result:
            result.append(text[:200])
    return result


def normalize_source_refs(value):
    if value in (None, ''):
        return []
    if not isinstance(value, list):
        raise ValueError('source_refs: expected an array')
    refs = []
    for index, item in enumerate(value[:200]):
        if not isinstance(item, dict):
            raise ValueError(f'source_refs[{index}]: expected an object')
        ref_type = str(item.get('type') or '').strip()
        ref_value = str(item.get('value') or item.get('id') or '').strip()
        if not ref_type or not ref_value:
            raise ValueError(
                f'source_refs[{index}]: type and value are required')
        ref = {'type': ref_type[:64], 'value': ref_value[:500]}
        if item.get('metadata') is not None:
            if not isinstance(item.get('metadata'), dict):
                raise ValueError(
                    f'source_refs[{index}].metadata: expected an object')
            ref['metadata'] = item.get('metadata')
        refs.append(ref)
    return refs


def normalize_case_draft(value, require_runnable=False):
    if value in (None, ''):
        value = {}
    if not isinstance(value, dict):
        raise ValueError('case_draft: expected an object')
    draft = dict(value)
    for field in ('preconditions', 'steps', 'expected_results'):
        raw = draft.get(field, [])
        if not isinstance(raw, list):
            raise ValueError(f'case_draft.{field}: expected an array')
        draft[field] = raw
    automation = draft.get('automation', {})
    if not isinstance(automation, dict):
        raise ValueError('case_draft.automation: expected an object')
    draft['automation'] = automation
    if require_runnable:
        if not draft['steps']:
            raise ValueError(
                'case_draft.steps: DESIGNED candidates require at least one action')
        if not draft['expected_results']:
            raise ValueError(
                'case_draft.expected_results: DESIGNED candidates require '
                'business-state verification')
    return draft


def default_dedupe_key(project_id, title, module_key, source_type, source_refs):
    payload = {
        'project_id': int(project_id),
        'title': str(title or '').strip(),
        'module_key': str(module_key or '').strip(),
        'source_type': str(source_type or 'manual').strip(),
        'source_refs': source_refs or [],
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def validate_candidate_transition(current_state, target_state):
    current = str(current_state or '').strip().upper()
    target = str(target_state or '').strip().upper()
    if current not in CANDIDATE_STATES:
        raise ValueError(f'current state {current!r} is invalid')
    if target not in CANDIDATE_STATES:
        raise ValueError(f'target state {target!r} is invalid')
    if target == current:
        return target
    if target not in CANDIDATE_TRANSITIONS.get(current, set()):
        raise ValueError(f'candidate state cannot transition from {current} to {target}')
    return target


def qualification_target_state(current_state, outcome):
    current = str(current_state or '').strip().upper()
    normalized_outcome = str(outcome or '').strip().upper()
    if normalized_outcome not in QUALIFICATION_OUTCOMES:
        allowed = ', '.join(sorted(QUALIFICATION_OUTCOMES))
        raise ValueError(f'qualification_outcome must be one of: {allowed}')
    if current != 'READY_FOR_CANARY':
        raise ValueError(
            'qualification results are only accepted from READY_FOR_CANARY')
    target = QUALIFICATION_TARGET_STATES[normalized_outcome]
    validate_candidate_transition(current, target)
    return normalized_outcome, target


def capability_gap_key(project_id, missing_capabilities):
    normalized = sorted(set(missing_capabilities or []))
    digest = hashlib.sha256(json.dumps(
        {'project_id': int(project_id), 'missing': normalized},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()
    return f'gap:{digest}'
