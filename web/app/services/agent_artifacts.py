"""Validation and hashing primitives for the Agent Artifact contract."""

import hashlib
import json
import re


ARTIFACT_STATUSES = frozenset({
    'draft', 'submitted', 'accepted', 'rejected', 'superseded',
})
REVIEW_DECISIONS = frozenset({'accepted', 'rejected'})
MAX_CONTENT_BYTES = 512 * 1024

_CREATE_FIELDS = frozenset({
    'project_id', 'mission_id', 'stage_id', 'run_id', 'workflow_run_id',
    'artifact_type', 'schema_version', 'artifact_version',
    'input_baseline_sha256', 'content', 'idempotency_key',
})
_READ_ONLY_FIELDS = frozenset({
    'id', 'artifact_id', 'producer', 'producer_type', 'producer_id',
    'producer_name', 'producer_claw_id', 'producer_role_key',
    'producer_provider', 'producer_profile_version', 'content_sha256',
    'request_sha256', 'status', 'review', 'reviewed_by_id',
    'reviewed_by_name', 'created_at', 'updated_at', 'submitted_at',
    'reviewed_at',
})
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$')
_SHA256_RE = re.compile(r'^(?:sha256:)?([0-9a-fA-F]{64})$')


class ArtifactValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def sha256_json(value, prefix=False):
    digest = hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()
    return f'sha256:{digest}' if prefix else digest


def _positive_int(value, field, required=True):
    if value in (None, ''):
        if required:
            raise ArtifactValidationError(
                'VALIDATION_FAILED', f'{field} is required')
        return None
    if isinstance(value, bool):
        raise ArtifactValidationError(
            'VALIDATION_FAILED', f'{field} must be a positive integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            'VALIDATION_FAILED',
            f'{field} must be a positive integer') from exc
    if result <= 0:
        raise ArtifactValidationError(
            'VALIDATION_FAILED', f'{field} must be a positive integer')
    return result


def _key(value, field):
    result = str(value or '').strip()
    if not _KEY_RE.fullmatch(result):
        raise ArtifactValidationError(
            'VALIDATION_FAILED',
            f'{field} must be 1-80 URL-safe key characters')
    return result


def _baseline(value):
    value = str(value or '').strip()
    if not value:
        return ''
    matched = _SHA256_RE.fullmatch(value)
    if not matched:
        raise ArtifactValidationError(
            'VALIDATION_FAILED',
            'input_baseline_sha256 must be a SHA-256 digest')
    return f'sha256:{matched.group(1).lower()}'


def normalize_create_payload(data):
    if not isinstance(data, dict):
        raise ArtifactValidationError(
            'VALIDATION_FAILED', 'JSON object body is required')
    read_only = sorted(set(data) & _READ_ONLY_FIELDS)
    if read_only:
        raise ArtifactValidationError(
            'READ_ONLY_FIELD',
            f"server-owned fields are read-only: {', '.join(read_only)}")
    unknown = sorted(set(data) - _CREATE_FIELDS)
    if unknown:
        raise ArtifactValidationError(
            'UNKNOWN_FIELD', f"unknown fields: {', '.join(unknown)}")

    content = data.get('content')
    if not isinstance(content, dict):
        raise ArtifactValidationError(
            'VALIDATION_FAILED', 'content must be a JSON object')
    try:
        content_text = canonical_json(content)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            'VALIDATION_FAILED', 'content must be valid finite JSON') from exc
    if len(content_text.encode('utf-8')) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(
            'CONTENT_TOO_LARGE',
            f'content must not exceed {MAX_CONTENT_BYTES} bytes')

    run_id = data.get('workflow_run_id')
    alias_run_id = data.get('run_id')
    if run_id not in (None, '') and alias_run_id not in (None, ''):
        if str(run_id) != str(alias_run_id):
            raise ArtifactValidationError(
                'VALIDATION_FAILED',
                'run_id and workflow_run_id must match')
    run_id = run_id if run_id not in (None, '') else alias_run_id

    normalized = {
        'project_id': _positive_int(data.get('project_id'), 'project_id'),
        'mission_id': _positive_int(data.get('mission_id'), 'mission_id'),
        'stage_id': _key(data.get('stage_id'), 'stage_id'),
        'workflow_run_id': _positive_int(
            run_id, 'workflow_run_id', required=False),
        'artifact_type': _key(data.get('artifact_type'), 'artifact_type'),
        'schema_version': _positive_int(
            data.get('schema_version', 1), 'schema_version'),
        'artifact_version': _positive_int(
            data.get('artifact_version', 1), 'artifact_version'),
        'input_baseline_sha256': _baseline(
            data.get('input_baseline_sha256')),
        'content': content,
    }
    normalized['content_sha256'] = sha256_json(content, prefix=True)
    normalized['request_sha256'] = sha256_json(normalized)
    return normalized

