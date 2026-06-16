"""Helpers for custom test report categories."""
from datetime import datetime


MAX_CUSTOM_CATEGORY_KEY_LENGTH = 120


def normalize_custom_category_key(raw):
    """Use the trimmed category title as the unique key."""
    key = (raw or '').strip()
    if not key:
        raise ValueError('custom category title cannot be empty')
    if len(key) > MAX_CUSTOM_CATEGORY_KEY_LENGTH:
        raise ValueError(f'custom category title cannot exceed {MAX_CUSTOM_CATEGORY_KEY_LENGTH} chars')
    return key


def parse_report_datetime(raw):
    if not raw:
        return None
    value = str(raw).strip()
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f'invalid datetime: {raw}')


def parse_report_time_range(since_raw, until_raw):
    return parse_report_datetime(since_raw), parse_report_datetime(until_raw)


def build_report_link(hub_base, report_id):
    base = (hub_base or '').strip().rstrip('/')
    return f'{base}/test-reports/{int(report_id)}'
