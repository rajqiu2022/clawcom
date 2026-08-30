"""Strict v1 validators for the three initial Agent Artifact contracts."""

import re

from app.services.agent_artifacts import canonical_json


STRICT_ARTIFACT_TYPES = frozenset({
    'requirement_analysis', 'engineering_analysis', 'execution_record',
})
_SHA256_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_TOKEN_PATTERNS = (
    re.compile(r'\boc_tk_[A-Za-z0-9_-]{16,}\b'),
    re.compile(r'\b(?:Bearer|ApiKey)\s+[A-Za-z0-9._~+/=-]{12,}', re.I),
    re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b'),
)
_SENSITIVE_KEYS = frozenset({
    'access_token', 'api_token', 'api_key', 'password', 'passwd', 'secret',
    'client_secret', 'resume_secret', 'private_key', 'authorization',
})
_CONFIDENCE = frozenset({'high', 'medium', 'low'})


class ArtifactContractError(ValueError):
    def __init__(self, code, message, errors=None):
        super().__init__(message)
        self.code = code
        self.errors = errors or []


def _issue(errors, code, path, message):
    errors.append({'code': code, 'path': path, 'message': message})


def _walk_sensitive(value, path='$'):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace('-', '_')
            child_path = f'{path}.{key}'
            if normalized in _SENSITIVE_KEYS:
                return child_path
            found = _walk_sensitive(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _walk_sensitive(child, f'{path}[{index}]')
            if found:
                return found
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in _TOKEN_PATTERNS):
            return path
    return None


def _is_sha256(value):
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value.lower()))


def _nonempty_text(value, maximum=20000):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _list(value, maximum=500):
    return isinstance(value, list) and len(value) <= maximum


def _unknown_fields(content, allowed, errors):
    for key in sorted(set(content) - set(allowed)):
        _issue(errors, 'UNKNOWN_FIELD', f'$.{key}', 'field is not in schema v1')


def _required(content, fields, errors):
    for field in fields:
        if field not in content:
            _issue(errors, 'REQUIRED_FIELD_MISSING', f'$.{field}', 'field is required')


def _validate_common(content, artifact_type, errors):
    if not isinstance(content, dict):
        _issue(errors, 'CONTENT_NOT_OBJECT', '$', 'content must be an object')
        return False
    if content.get('schema') != 1:
        _issue(errors, 'SCHEMA_MISMATCH', '$.schema', 'schema must equal 1')
    if content.get('artifact_type') != artifact_type:
        _issue(
            errors, 'ARTIFACT_TYPE_MISMATCH', '$.artifact_type',
            f'artifact_type must equal {artifact_type}')
    return True


def _mission_context(mission):
    value = getattr(mission, 'context_json', None)
    return value if isinstance(value, dict) else {}


def _mission_baseline(mission):
    context = _mission_context(mission)
    for key in ('input_baseline_sha256', 'baseline_sha256'):
        value = context.get(key)
        if _is_sha256(value):
            return value.lower()
    baseline = context.get('baseline')
    if isinstance(baseline, dict):
        for key in ('sha256', 'digest', 'content_sha256'):
            value = baseline.get(key)
            if _is_sha256(value):
                return value.lower()
    return ''


def _remote_source_ids(mission):
    context = _mission_context(mission)
    result = set()

    def add(value):
        if isinstance(value, str) and value.strip():
            result.add(value.strip())
        elif isinstance(value, list):
            result.update(
                item.strip() for item in value
                if isinstance(item, str) and item.strip())

    add(context.get('remote_source_id'))
    add(context.get('remote_source_ids'))
    system_context = context.get('system_context')
    if isinstance(system_context, dict):
        policy = system_context.get('policy')
        if isinstance(policy, dict):
            add(policy.get('remote_source_id'))
            add(policy.get('remote_source_ids'))
    return result


def _validate_source_refs(value, path, errors, required=False):
    if not _list(value, 100):
        _issue(errors, 'SOURCE_REFS_INVALID', path, 'source refs must be an array')
        return
    if required and not value:
        _issue(errors, 'AC_SOURCE_REQUIRED', path, 'at least one source ref is required')
    for index, ref in enumerate(value):
        if not isinstance(ref, (dict, str)) or ref in ({}, ''):
            _issue(
                errors, 'SOURCE_REF_INVALID', f'{path}[{index}]',
                'source ref must be a non-empty object or string')
        if isinstance(ref, dict) and ref.get('verified') is True:
            _issue(
                errors, 'AGENT_VERIFICATION_FORBIDDEN',
                f'{path}[{index}].verified',
                'verified can only be set by Hub or Runner')


def _validate_baseline(value, envelope_baseline, mission, path, errors):
    if not _is_sha256(value):
        _issue(errors, 'BASELINE_HASH_INVALID', path, 'baseline must be sha256:<64 hex>')
        return
    value = value.lower()
    if envelope_baseline and value != envelope_baseline.lower():
        _issue(
            errors, 'ENVELOPE_BASELINE_MISMATCH', path,
            'content baseline differs from Artifact envelope')
    frozen = _mission_baseline(mission)
    if frozen and value != frozen:
        _issue(
            errors, 'MISSION_BASELINE_MISMATCH', path,
            'content baseline differs from Mission frozen baseline')


def _validate_requirement(content, envelope_baseline, mission):
    errors = []
    allowed = {
        'schema', 'artifact_type', 'input_baseline_sha256', 'summary',
        'actors', 'functional_flows', 'acceptance_criteria', 'risks',
        'ambiguities', 'open_questions', 'out_of_scope', 'evidence_refs',
        'confidence',
    }
    required = allowed
    if not _validate_common(content, 'requirement_analysis', errors):
        return errors
    _unknown_fields(content, allowed, errors)
    _required(content, required, errors)
    _validate_baseline(
        content.get('input_baseline_sha256'), envelope_baseline, mission,
        '$.input_baseline_sha256', errors)
    if not _nonempty_text(content.get('summary')):
        _issue(errors, 'SUMMARY_REQUIRED', '$.summary', 'summary must not be empty')
    for field in ('actors', 'functional_flows', 'ambiguities', 'open_questions',
                  'out_of_scope', 'evidence_refs'):
        if not _list(content.get(field), 500):
            _issue(errors, 'FIELD_NOT_ARRAY', f'$.{field}', 'field must be an array')
    criteria = content.get('acceptance_criteria')
    if not _list(criteria, 500) or not criteria:
        _issue(
            errors, 'AC_REQUIRED', '$.acceptance_criteria',
            'at least one acceptance criterion is required')
        criteria = []
    criterion_ids = set()
    unresolved_text = canonical_json({
        'ambiguities': content.get('ambiguities', []),
        'open_questions': content.get('open_questions', []),
    }).lower()
    for index, item in enumerate(criteria):
        path = f'$.acceptance_criteria[{index}]'
        if not isinstance(item, dict):
            _issue(errors, 'AC_INVALID', path, 'criterion must be an object')
            continue
        criterion_id = str(item.get('id') or '').strip()
        if not criterion_id or criterion_id in criterion_ids:
            _issue(errors, 'AC_ID_INVALID', f'{path}.id', 'criterion id must be unique')
        criterion_ids.add(criterion_id)
        if not _nonempty_text(item.get('statement'), 4000):
            _issue(errors, 'AC_STATEMENT_REQUIRED', f'{path}.statement', 'statement is required')
        if item.get('priority') not in ('P0', 'P1', 'P2'):
            _issue(errors, 'AC_PRIORITY_INVALID', f'{path}.priority', 'priority is invalid')
        _validate_source_refs(item.get('source_refs'), f'{path}.source_refs', errors, True)
        if not isinstance(item.get('testable'), bool):
            _issue(errors, 'AC_TESTABLE_INVALID', f'{path}.testable', 'testable must be boolean')
        elif item.get('testable') is False:
            statement = str(item.get('statement') or '').strip().lower()
            if not ((criterion_id and criterion_id.lower() in unresolved_text)
                    or (statement and statement in unresolved_text)):
                _issue(
                    errors, 'UNTESTABLE_CRITERION_UNTRACKED', path,
                    'untestable criterion must appear in ambiguity or open question')
    risks = content.get('risks')
    if not _list(risks, 500):
        _issue(errors, 'RISKS_INVALID', '$.risks', 'risks must be an array')
        risks = []
    risk_ids = set()
    for index, item in enumerate(risks):
        path = f'$.risks[{index}]'
        if not isinstance(item, dict):
            _issue(errors, 'RISK_INVALID', path, 'risk must be an object')
            continue
        risk_id = str(item.get('id') or '').strip()
        if not risk_id or risk_id in risk_ids:
            _issue(errors, 'RISK_ID_INVALID', f'{path}.id', 'risk id must be unique')
        risk_ids.add(risk_id)
        if not _nonempty_text(item.get('description'), 4000):
            _issue(errors, 'RISK_DESCRIPTION_REQUIRED', f'{path}.description', 'description is required')
        for field in ('impact', 'probability'):
            if item.get(field) not in _CONFIDENCE:
                _issue(errors, 'RISK_LEVEL_INVALID', f'{path}.{field}', 'level is invalid')
        _validate_source_refs(item.get('source_refs'), f'{path}.source_refs', errors)
        if not _list(item.get('recommended_coverage'), 100):
            _issue(errors, 'RISK_COVERAGE_INVALID', f'{path}.recommended_coverage', 'must be an array')
    _validate_source_refs(content.get('evidence_refs'), '$.evidence_refs', errors)
    if content.get('confidence') not in _CONFIDENCE:
        _issue(errors, 'CONFIDENCE_INVALID', '$.confidence', 'confidence is invalid')
    return errors


def _module_name(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get('module') or value.get('name') or '').strip()
    return ''


def _validate_engineering(content, envelope_baseline, mission):
    errors = []
    allowed = {
        'schema', 'artifact_type', 'input_baseline_sha256', 'repositories',
        'affected_modules', 'changed_paths', 'dependency_edges',
        'runtime_entrypoints', 'protocols', 'resource_dependencies',
        'testability_findings', 'code_requirement_links',
        'test_recommendations', 'inferences', 'unknowns', 'evidence_refs',
        'confidence',
    }
    if not _validate_common(content, 'engineering_analysis', errors):
        return errors
    _unknown_fields(content, allowed, errors)
    _required(content, allowed, errors)
    _validate_baseline(
        content.get('input_baseline_sha256'), envelope_baseline, mission,
        '$.input_baseline_sha256', errors)
    approved = _remote_source_ids(mission)
    repositories = content.get('repositories')
    if not _list(repositories, 50) or not repositories:
        _issue(errors, 'REPOSITORY_REQUIRED', '$.repositories', 'repository snapshot is required')
        repositories = []
    for index, repo in enumerate(repositories):
        path = f'$.repositories[{index}]'
        if not isinstance(repo, dict):
            _issue(errors, 'REPOSITORY_INVALID', path, 'repository must be an object')
            continue
        source_id = str(repo.get('source_id') or '').strip()
        if not source_id or source_id not in approved:
            _issue(
                errors, 'REMOTE_SOURCE_NOT_ALLOWED', f'{path}.source_id',
                'source_id is not approved by Hub Mission policy')
        if not _nonempty_text(repo.get('commit'), 200):
            _issue(errors, 'REPOSITORY_COMMIT_REQUIRED', f'{path}.commit', 'commit is required')
        if not _is_sha256(repo.get('tree_digest')):
            _issue(errors, 'TREE_DIGEST_INVALID', f'{path}.tree_digest', 'tree digest is invalid')
    modules = content.get('affected_modules')
    if not _list(modules, 500):
        _issue(errors, 'AFFECTED_MODULES_INVALID', '$.affected_modules', 'must be an array')
        modules = []
    module_names = [_module_name(item) for item in modules]
    for index, name in enumerate(module_names):
        if not name:
            _issue(errors, 'MODULE_NAME_REQUIRED', f'$.affected_modules[{index}]', 'module name is required')
    changed_paths = content.get('changed_paths')
    if not _list(changed_paths, 2000):
        _issue(errors, 'CHANGED_PATHS_INVALID', '$.changed_paths', 'must be an array')
        changed_paths = []
    links = content.get('code_requirement_links')
    if not _list(links, 2000):
        _issue(errors, 'CODE_LINKS_INVALID', '$.code_requirement_links', 'must be an array')
        links = []
    evidence_texts = []
    for item in changed_paths:
        if isinstance(item, str):
            evidence_texts.append(item.lower())
        elif isinstance(item, dict):
            text = ' '.join(str(item.get(key) or '') for key in ('module', 'path', 'symbol'))
            if item.get('evidence_refs'):
                evidence_texts.append(text.lower())
    for index, item in enumerate(links):
        path = f'$.code_requirement_links[{index}]'
        if not isinstance(item, dict):
            _issue(errors, 'CODE_LINK_INVALID', path, 'code link must be an object')
            continue
        if item.get('relation') not in ('implements', 'guards', 'configures', 'unknown'):
            _issue(errors, 'CODE_LINK_RELATION_INVALID', f'{path}.relation', 'relation is invalid')
        if not _nonempty_text(item.get('acceptance_id'), 120):
            _issue(errors, 'ACCEPTANCE_LINK_REQUIRED', f'{path}.acceptance_id', 'acceptance id is required')
        if not (str(item.get('path') or '').strip() or str(item.get('symbol') or '').strip()):
            _issue(errors, 'CODE_LOCATION_REQUIRED', path, 'path or symbol is required')
        _validate_source_refs(item.get('evidence_refs'), f'{path}.evidence_refs', errors, True)
        evidence_texts.append(
            ' '.join(str(item.get(key) or '') for key in ('path', 'symbol')).lower())
    for name in module_names:
        if name and not any(name.lower() in text for text in evidence_texts):
            _issue(
                errors, 'MODULE_EVIDENCE_REQUIRED', '$.affected_modules',
                f'module {name} has no path/symbol evidence')
    for field in (
            'dependency_edges', 'runtime_entrypoints', 'protocols',
            'resource_dependencies', 'testability_findings',
            'test_recommendations', 'inferences', 'unknowns', 'evidence_refs'):
        if not _list(content.get(field), 2000):
            _issue(errors, 'FIELD_NOT_ARRAY', f'$.{field}', 'field must be an array')
    inference_set = {
        canonical_json(item) for item in content.get('inferences', [])}
    unknown_set = {canonical_json(item) for item in content.get('unknowns', [])}
    if inference_set & unknown_set:
        _issue(
            errors, 'INFERENCE_UNKNOWN_OVERLAP', '$.inferences',
            'inference and unknown entries must be distinct')
    _validate_source_refs(content.get('evidence_refs'), '$.evidence_refs', errors)
    if content.get('confidence') not in _CONFIDENCE:
        _issue(errors, 'CONFIDENCE_INVALID', '$.confidence', 'confidence is invalid')
    return errors


def _selected_case_ids(mission):
    context = _mission_context(mission)
    for key in ('selected_case_ids', 'case_ids'):
        value = context.get(key)
        if isinstance(value, list):
            return {item for item in value if type(item) is int and item > 0}
    return None


def _validate_execution(content, mission):
    errors = []
    allowed = {
        'schema', 'artifact_type', 'environment_baseline', 'case_results',
        'environment_failures', 'tool_failures', 'business_failures',
        'summary', 'evidence_manifest_id',
    }
    if not _validate_common(content, 'execution_record', errors):
        return errors
    _unknown_fields(content, allowed, errors)
    _required(content, allowed, errors)
    baseline = content.get('environment_baseline')
    baseline_fields = (
        'code_commit', 'build_id', 'runner_release', 'adapter_release',
        'device_generation')
    if not isinstance(baseline, dict):
        _issue(errors, 'ENVIRONMENT_BASELINE_REQUIRED', '$.environment_baseline', 'must be an object')
        baseline = {}
    for field in baseline_fields:
        if not _nonempty_text(baseline.get(field), 200):
            _issue(errors, 'ENVIRONMENT_BASELINE_FIELD_REQUIRED', f'$.environment_baseline.{field}', 'field is required')
    expected_baseline = _mission_context(mission).get('environment_baseline')
    if isinstance(expected_baseline, dict):
        for field in baseline_fields:
            expected = expected_baseline.get(field)
            if expected not in (None, '') and baseline.get(field) != expected:
                _issue(
                    errors, 'ENVIRONMENT_BASELINE_MISMATCH',
                    f'$.environment_baseline.{field}',
                    'value differs from Mission confirmed baseline')
    results = content.get('case_results')
    if not _list(results, 5000) or not results:
        _issue(errors, 'CASE_RESULTS_REQUIRED', '$.case_results', 'case results must not be empty')
        results = []
    seen = set()
    blocked_ids = set()
    for index, item in enumerate(results):
        path = f'$.case_results[{index}]'
        if not isinstance(item, dict):
            _issue(errors, 'CASE_RESULT_INVALID', path, 'case result must be an object')
            continue
        case_id = item.get('case_id')
        if type(case_id) is not int or case_id <= 0 or case_id in seen:
            _issue(errors, 'CASE_ID_INVALID', f'{path}.case_id', 'case id must be unique positive integer')
        seen.add(case_id)
        status = item.get('status')
        classification = item.get('classification')
        if status not in ('passed', 'failed', 'blocked', 'skipped'):
            _issue(errors, 'CASE_STATUS_INVALID', f'{path}.status', 'status is invalid')
        if classification not in ('business', 'environment', 'tool', 'case'):
            _issue(errors, 'CASE_CLASSIFICATION_INVALID', f'{path}.classification', 'classification is invalid')
        if not _nonempty_text(item.get('started_at'), 100) or not _nonempty_text(item.get('finished_at'), 100):
            _issue(errors, 'CASE_TIMING_REQUIRED', path, 'started_at and finished_at are required')
        receipts = item.get('action_receipts')
        observations = item.get('observation_refs')
        evidence = item.get('evidence_refs')
        for field, value in (
                ('action_receipts', receipts), ('observation_refs', observations),
                ('evidence_refs', evidence)):
            if not _list(value, 1000):
                _issue(errors, 'CASE_REFS_INVALID', f'{path}.{field}', 'must be an array')
        if status == 'passed':
            if not receipts:
                _issue(errors, 'PASSED_RECEIPT_REQUIRED', f'{path}.action_receipts', 'passed requires Runner receipt')
            if not observations:
                _issue(errors, 'PASSED_OBSERVATION_REQUIRED', f'{path}.observation_refs', 'passed requires observation')
        if status == 'failed':
            if not evidence:
                _issue(errors, 'FAILED_EVIDENCE_REQUIRED', f'{path}.evidence_refs', 'failed requires evidence')
            if not _nonempty_text(item.get('failure_fingerprint'), 500):
                _issue(errors, 'FAILURE_FINGERPRINT_REQUIRED', f'{path}.failure_fingerprint', 'failed requires fingerprint')
        if status == 'blocked':
            blocked_ids.add(case_id)
            if classification == 'business':
                _issue(errors, 'BLOCKED_BUSINESS_FORBIDDEN', f'{path}.classification', 'blocked is not a business failure')
        retry_count = item.get('retry_count')
        if type(retry_count) is not int or retry_count < 0:
            _issue(errors, 'RETRY_COUNT_INVALID', f'{path}.retry_count', 'retry_count must be non-negative integer')
        for receipt_index, receipt in enumerate(receipts or []):
            if not isinstance(receipt, dict):
                _issue(errors, 'RECEIPT_INVALID', f'{path}.action_receipts[{receipt_index}]', 'receipt must be an object')
                continue
            if not isinstance(receipt.get('side_effect'), bool):
                _issue(
                    errors, 'RECEIPT_EFFECT_SCOPE_REQUIRED',
                    f'{path}.action_receipts[{receipt_index}].side_effect',
                    'receipt must explicitly declare side_effect')
            if receipt.get('side_effect') is True:
                if (not _nonempty_text(receipt.get('idempotency_key'), 128)
                        or not _nonempty_text(receipt.get('lease_id'), 160)
                        or type(receipt.get('fencing_token')) is not int
                        or receipt.get('fencing_token') <= 0):
                    _issue(
                        errors, 'SIDE_EFFECT_FENCE_REQUIRED',
                        f'{path}.action_receipts[{receipt_index}]',
                        'side effect requires idempotency key, lease and fencing token')
    selected = _selected_case_ids(mission)
    if selected is None:
        _issue(errors, 'MISSION_CASE_SELECTION_MISSING', '$.case_results', 'Mission has no selected case ids')
    elif seen != selected:
        _issue(
            errors, 'CASE_COVERAGE_MISMATCH', '$.case_results',
            'case results must exactly cover Mission selected cases')
    for field in ('environment_failures', 'tool_failures', 'business_failures'):
        if not _list(content.get(field), 5000):
            _issue(errors, 'FAILURE_BUCKET_INVALID', f'$.{field}', 'must be an array')
    business_ids = set()
    for item in content.get('business_failures', []):
        if type(item) is int:
            business_ids.add(item)
        elif isinstance(item, dict) and type(item.get('case_id')) is int:
            business_ids.add(item['case_id'])
    if blocked_ids & business_ids:
        _issue(errors, 'BLOCKED_IN_BUSINESS_FAILURES', '$.business_failures', 'blocked cases cannot be business failures')
    if not _nonempty_text(content.get('summary')):
        _issue(errors, 'SUMMARY_REQUIRED', '$.summary', 'summary must not be empty')
    manifest_id = content.get('evidence_manifest_id')
    if type(manifest_id) is not int or manifest_id <= 0:
        _issue(errors, 'EVIDENCE_MANIFEST_REQUIRED', '$.evidence_manifest_id', 'positive manifest id is required')
    return errors


def validate_artifact_contract(
        artifact_type, schema_version, content, envelope_baseline, mission):
    """Return persisted validation facts or raise a fail-closed contract error."""
    sensitive_path = _walk_sensitive(content)
    if sensitive_path:
        raise ArtifactContractError(
            'SENSITIVE_DATA_DETECTED',
            f'sensitive value or field detected at {sensitive_path}',
            [{'code': 'SENSITIVE_DATA_DETECTED', 'path': sensitive_path,
              'message': 'secrets must be stored in the secret vault'}])
    if artifact_type not in STRICT_ARTIFACT_TYPES:
        return {
            'mode': 'legacy_generic', 'valid': True,
            'artifact_type': artifact_type,
            'schema_version': int(schema_version or 1), 'errors': [],
        }
    if int(schema_version or 0) != 1:
        raise ArtifactContractError(
            'ARTIFACT_SCHEMA_UNSUPPORTED',
            f'{artifact_type} schema_version {schema_version} is unsupported',
            [{'code': 'ARTIFACT_SCHEMA_UNSUPPORTED', 'path': '$.schema',
              'message': 'only schema version 1 is supported'}])
    if artifact_type == 'requirement_analysis':
        errors = _validate_requirement(content, envelope_baseline, mission)
    elif artifact_type == 'engineering_analysis':
        errors = _validate_engineering(content, envelope_baseline, mission)
    else:
        errors = _validate_execution(content, mission)
    if errors:
        raise ArtifactContractError(
            'ARTIFACT_CONTRACT_INVALID',
            f'{artifact_type} v1 contract validation failed', errors)
    return {
        'mode': 'strict', 'valid': True, 'artifact_type': artifact_type,
        'schema_version': 1, 'errors': [],
    }
