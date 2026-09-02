#!/usr/bin/env python3
"""Patch Flow #38 so an empty qualification queue closes deterministically."""

import copy
import hashlib
import json
import os
import sys
from datetime import datetime


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ROOT = next((candidate for candidate in (
    REPO_ROOT,
    os.path.join(REPO_ROOT, 'web'),
) if os.path.isfile(os.path.join(candidate, 'app', '__init__.py'))), None)
if not ROOT:
    raise RuntimeError('cannot locate Hub app package')
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import db  # noqa: E402
from app.models import AuditLog, WorkflowDefinition  # noqa: E402
from app.services.workflows import normalize_workflow_definition  # noqa: E402


FLOW_ID = 38
FLOW_KEY = 'racinggo_candidate_editor_qualification'
EMPTY_QUEUE_CODE = 'QUALIFICATION_EMPTY_QUEUE'
TEMPLATE_REVISION = 'closedloop_qualification_v6_empty_queue'
DOWNSTREAM_STEPS = [
    'run_bounded_editor_canary',
    'classify_and_release',
    'promote_qualified_candidates',
]


def patch_definition(raw):
    definition = copy.deepcopy(raw or {})
    steps = {step.get('id'): step for step in definition.get('steps') or []}
    acquire = steps.get('acquire_qualification_resources')
    if not acquire:
        raise RuntimeError('Flow #38 acquire_qualification_resources is missing')
    existing_branches = acquire.get('branches') or []
    already_applied = (
        ((definition.get('context') or {}).get('template_revision')
         == TEMPLATE_REVISION)
        and any(
            branch.get('if') == 'metrics.ready_candidate_count == 0'
            for branch in existing_branches if isinstance(branch, dict))
        and acquire.get('contract_on_fail') == 'warn'
        and isinstance(acquire.get('required_outputs'), list)
    )

    acquire['contract_on_fail'] = 'warn'
    acquire['required_outputs'] = [
        'validation_allowed',
        'defer_reason',
        'step_result',
        'candidate_manifest_uri',
        'candidate_manifest_sha256',
        'ready_candidate_keys',
        'resource_lease_ids',
    ]
    allow_empty = acquire.setdefault('allow_empty_contract_fields', {})
    output_fields = list(allow_empty.get('outputs') or [])
    for field in ('lease_ids', 'ready_candidate_keys', 'resource_lease_ids'):
        if field not in output_fields:
            output_fields.append(field)
    allow_empty['outputs'] = output_fields
    acquire['branches'] = [{
        'if': 'metrics.ready_candidate_count == 0',
        'then': [],
        'else': list(DOWNSTREAM_STEPS),
    }]
    guidance = (
        '\nREADY_FOR_CANARY=0 是正常空队列，不是业务或自动化失败：回写 '
        f'{EMPTY_QUEUE_CODE}、空数组、validation_allowed=false，并将 '
        'case_result/ui_snapshot/console/screenshots 标为 not_applicable。'
        'Hub 将跳过后续资格验证、分类和晋级节点并正常结束 Run。')
    if EMPTY_QUEUE_CODE not in str(acquire.get('prompt') or ''):
        acquire['prompt'] = str(acquire.get('prompt') or '').rstrip() + guidance
    inputs = acquire.get('inputs') if isinstance(acquire.get('inputs'), dict) else {}
    node_prompt = str(inputs.get('node_prompt') or '')
    if EMPTY_QUEUE_CODE not in node_prompt:
        inputs['node_prompt'] = node_prompt.rstrip() + guidance
    acquire['inputs'] = inputs

    current_version = int(definition.get('version') or 1)
    definition['version'] = current_version if already_applied else current_version + 1
    context = definition.setdefault('context', {})
    context['template_revision'] = TEMPLATE_REVISION
    profile = context.setdefault('evidence_profile', {})
    profile['not_applicable_is_complete'] = True
    profile['empty_queue_code'] = EMPTY_QUEUE_CODE
    normalized = normalize_workflow_definition(definition)
    normalized['key'] = str(definition.get('key') or FLOW_KEY)
    return normalized


def sha256(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def apply_patch(actor='codex-deploy'):
    row = db.session.get(WorkflowDefinition, FLOW_ID)
    if not row or row.workflow_key != FLOW_KEY:
        raise RuntimeError('Flow #38 identity mismatch')
    before = copy.deepcopy(row.definition_json or {})
    after = patch_definition(before)
    if sha256(before) == sha256(after):
        return {'changed': False, 'version': row.version, 'sha256': sha256(after)}
    row.definition_json = after
    row.version = int(after.get('version') or row.version or 1)
    row.updated_at = datetime.now()
    db.session.add(AuditLog(
        action='update',
        resource_type='workflow_definition_content',
        resource_id=row.id,
        resource_name=row.name,
        operator=actor,
        detail=json.dumps({
            'reason': 'flow38_empty_queue_contract',
            'before_version': before.get('version'),
            'after_version': after.get('version'),
            'before_definition_sha256': sha256(before),
            'after_definition_sha256': sha256(after),
        }, ensure_ascii=False, sort_keys=True),
    ))
    db.session.commit()
    return {'changed': True, 'version': row.version, 'sha256': sha256(after)}


if __name__ == '__main__':
    from run import app
    with app.app_context():
        print(json.dumps(apply_patch(), ensure_ascii=False, sort_keys=True))
