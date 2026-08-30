"""Immutable analysis-rule versions and deterministic replay gates."""

import hashlib
import json


RULE_VERSION_STATUSES = {'draft', 'replay_passed', 'active', 'retired'}

DEFAULT_THRESHOLDS = {
    'min_sample_count': 20,
    'min_precision': 0.8,
    'min_recall': 0.8,
    'max_false_positive_rate': 0.2,
    'max_false_negative_rate': 0.2,
}


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)


def content_hash(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _json_object(value, field, max_bytes=65536):
    if not isinstance(value, dict):
        raise ValueError(f'{field} must be an object')
    if len(canonical_json(value).encode('utf-8')) > max_bytes:
        raise ValueError(f'{field} must not exceed {max_bytes // 1024} KiB')
    return value


def normalize_definition(value):
    value = _json_object(value, 'definition')
    if not value:
        raise ValueError('definition must not be empty')
    return value


def normalize_thresholds(value):
    if value is None:
        return dict(DEFAULT_THRESHOLDS)
    value = _json_object(value, 'thresholds', 8192)
    unknown = sorted(set(value) - set(DEFAULT_THRESHOLDS))
    if unknown:
        raise ValueError('unknown thresholds: ' + ', '.join(unknown))
    result = dict(DEFAULT_THRESHOLDS)
    result.update(value)
    try:
        result['min_sample_count'] = int(result['min_sample_count'])
        for field in (
                'min_precision', 'min_recall', 'max_false_positive_rate',
                'max_false_negative_rate'):
            result[field] = float(result[field])
    except (TypeError, ValueError):
        raise ValueError('threshold values must be numeric') from None
    if result['min_sample_count'] < 1:
        raise ValueError('min_sample_count must be at least 1')
    for field in (
            'min_precision', 'min_recall', 'max_false_positive_rate',
            'max_false_negative_rate'):
        if not 0 <= result[field] <= 1:
            raise ValueError(f'{field} must be between 0 and 1')
    return result


def normalize_finding_ids(value):
    if value in (None, ''):
        return []
    if not isinstance(value, list) or len(value) > 500:
        raise ValueError('source_finding_ids must be an array up to 500 items')
    result = []
    for raw in value:
        try:
            finding_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError('source_finding_ids must contain integers') from None
        if finding_id <= 0:
            raise ValueError('source_finding_ids must contain positive integers')
        if finding_id not in result:
            result.append(finding_id)
    return result


def normalize_metrics(value):
    value = _json_object(value, 'metrics', 16384)
    fields = (
        'sample_count', 'hit_count', 'true_positive_count',
        'false_positive_count', 'false_negative_count')
    result = {}
    for field in fields:
        try:
            parsed = int(value.get(field))
        except (TypeError, ValueError):
            raise ValueError(f'metrics.{field} must be an integer') from None
        if parsed < 0:
            raise ValueError(f'metrics.{field} must not be negative')
        result[field] = parsed
    if result['hit_count'] != (
            result['true_positive_count'] + result['false_positive_count']):
        raise ValueError(
            'metrics.hit_count must equal true_positive_count + '
            'false_positive_count')
    classified = (result['true_positive_count']
                  + result['false_positive_count']
                  + result['false_negative_count'])
    if result['sample_count'] < classified:
        raise ValueError(
            'metrics.sample_count must cover true/false positives and false negatives')
    hit_count = result['hit_count']
    positive_truth = (
        result['true_positive_count'] + result['false_negative_count'])
    result['precision'] = (
        result['true_positive_count'] / hit_count if hit_count else 0.0)
    result['recall'] = (
        result['true_positive_count'] / positive_truth if positive_truth else 1.0)
    result['false_positive_rate'] = (
        result['false_positive_count'] / hit_count if hit_count else 0.0)
    result['false_negative_rate'] = (
        result['false_negative_count'] / positive_truth
        if positive_truth else 0.0)
    result['true_negative_count'] = result['sample_count'] - classified
    return result


def evaluate_replay_gate(metrics, thresholds):
    reasons = []
    checks = (
        ('sample_count', '>=', thresholds['min_sample_count']),
        ('precision', '>=', thresholds['min_precision']),
        ('recall', '>=', thresholds['min_recall']),
        ('false_positive_rate', '<=', thresholds['max_false_positive_rate']),
        ('false_negative_rate', '<=', thresholds['max_false_negative_rate']),
    )
    for field, operation, limit in checks:
        actual = metrics[field]
        passed = actual >= limit if operation == '>=' else actual <= limit
        if not passed:
            reasons.append({
                'metric': field,
                'actual': actual,
                'operator': operation,
                'threshold': limit,
            })
    return not reasons, reasons


def normalize_impact(value):
    value = _json_object(value, 'impact', 32768)
    if not value:
        raise ValueError('impact must describe the replay change scope')
    return value


def normalize_artifact_refs(value):
    if value in (None, ''):
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('artifact_refs must be an array up to 100 items')
    result = []
    for raw in value:
        text = str(raw or '').strip()
        if not text or len(text) > 1000:
            raise ValueError('artifact_refs must contain non-empty links up to 1000 chars')
        result.append(text)
    return result
