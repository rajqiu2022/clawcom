"""Evidence-manifest normalization and hard post-run conclusion rules."""

import json
import re


COVERAGE_STATES = {
    'complete',
    'partial',
    'missing',
    'not_applicable',
    'not_requested',
}

POST_RUN_CLASSIFICATIONS = {
    'CONFIRMED_ANOMALY',
    'PRODUCT_BUG_CANDIDATE',
    'AUTOMATION_RISK',
    'PERFORMANCE_RISK',
    'ANALYSIS_INCOMPLETE',
    'NO_RISK_FOUND',
}

DEFAULT_REQUIRED_EVIDENCE = (
    'case_result',
    'ui_snapshot',
    'console',
    'screenshots',
)

_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
_INLINE_ARTIFACT_KEYS = {'content', 'data', 'base64', 'blob', 'raw'}


def _json_size(value):
    return len(json.dumps(
        value, ensure_ascii=False, separators=(',', ':'), default=str
    ).encode('utf-8'))


def normalize_coverage(value):
    if not isinstance(value, dict):
        raise ValueError('coverage must be an object')
    if len(value) > 50:
        raise ValueError('coverage must not contain more than 50 entries')
    result = {}
    for raw_key, raw_status in value.items():
        key = str(raw_key or '').strip().lower()
        status = str(raw_status or '').strip().lower()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(
                'coverage keys must be lowercase identifiers up to 64 characters')
        if status not in COVERAGE_STATES:
            raise ValueError(
                f'coverage.{key} must be one of: '
                f'{", ".join(sorted(COVERAGE_STATES))}')
        result[key] = status
    return result


def normalize_required_evidence(value):
    if value is None:
        return list(DEFAULT_REQUIRED_EVIDENCE)
    if not isinstance(value, list) or not value:
        raise ValueError('required_evidence must be a non-empty array')
    if len(value) > 50:
        raise ValueError('required_evidence must not contain more than 50 items')
    # Callers may add platform-specific requirements, but may not remove the
    # AT-10 core set to make an incomplete run look safe.
    result = list(DEFAULT_REQUIRED_EVIDENCE)
    seen = set(result)
    for raw_key in value:
        key = str(raw_key or '').strip().lower()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(
                'required_evidence values must be lowercase identifiers')
        if key not in seen:
            seen.add(key)
            result.append(key)
    if len(result) > 50:
        raise ValueError(
            'required_evidence including the AT-10 core must not exceed 50 items')
    return result


def normalize_artifacts(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError('artifacts must be an array')
    if len(value) > 500:
        raise ValueError('artifacts must not contain more than 500 items')
    result = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f'artifacts[{index}] must be an object')
        forbidden = sorted(_INLINE_ARTIFACT_KEYS & set(raw))
        if forbidden:
            raise ValueError(
                f'artifacts[{index}] must use uri instead of inline '
                f'{", ".join(forbidden)}')
        artifact_type = str(raw.get('type') or '').strip().lower()
        if not _KEY_RE.fullmatch(artifact_type):
            raise ValueError(f'artifacts[{index}].type is invalid')
        uri = str(raw.get('uri') or '').strip()
        if not uri or len(uri) > 2048 or '://' not in uri:
            raise ValueError(
                f'artifacts[{index}].uri must be an artifact link up to 2048 characters')
        sha256 = str(raw.get('sha256') or '').strip().lower()
        if sha256 and not _SHA256_RE.fullmatch(sha256):
            raise ValueError(f'artifacts[{index}].sha256 must be 64 hex characters')
        size = raw.get('size')
        if size is not None:
            if isinstance(size, bool):
                raise ValueError(f'artifacts[{index}].size must be a non-negative integer')
            try:
                size = int(size)
            except (TypeError, ValueError):
                raise ValueError(
                    f'artifacts[{index}].size must be a non-negative integer') from None
            if size < 0:
                raise ValueError(f'artifacts[{index}].size must be non-negative')
        metadata = raw.get('metadata') or {}
        if not isinstance(metadata, dict) or _json_size(metadata) > 16384:
            raise ValueError(
                f'artifacts[{index}].metadata must be an object up to 16 KiB')
        item = {'type': artifact_type, 'uri': uri}
        if sha256:
            item['sha256'] = sha256
        if size is not None:
            item['size'] = size
        for field in ('case_id', 'step_id'):
            if raw.get(field) is not None:
                item[field] = str(raw[field])[:500]
        if metadata:
            item['metadata'] = metadata
        result.append(item)
    return result


def evidence_completeness(
        coverage, required_evidence, not_applicable_is_complete=False):
    missing = []
    for key in required_evidence:
        status = coverage.get(key, 'missing')
        # A field listed as required must be complete. Optional platform data
        # such as logcat may legitimately be not_applicable by simply staying
        # outside required_evidence.
        if status != 'complete' and not (
                not_applicable_is_complete and status == 'not_applicable'):
            missing.append({'type': key, 'status': status})
    return ('complete' if not missing else 'incomplete'), missing


def normalize_manifest(data, not_applicable_is_complete=False):
    coverage = normalize_coverage(data.get('coverage'))
    required = normalize_required_evidence(data.get('required_evidence'))
    artifacts = normalize_artifacts(data.get('artifacts'))
    completeness, missing = evidence_completeness(
        coverage, required,
        not_applicable_is_complete=not_applicable_is_complete)
    return {
        'coverage': coverage,
        'required_evidence': required,
        'artifacts': artifacts,
        'completeness_status': completeness,
        'missing_required': missing,
    }


def normalize_classification(value):
    classification = str(value or '').strip().upper()
    if classification not in POST_RUN_CLASSIFICATIONS:
        raise ValueError(
            'classification must be one of: '
            + ', '.join(sorted(POST_RUN_CLASSIFICATIONS)))
    return classification


def normalize_analysis_summary(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('summary must be an object')
    if _json_size(value) > 65536:
        raise ValueError('summary must not exceed 64 KiB')
    return value
