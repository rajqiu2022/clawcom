"""Pure helpers for testcase-library to panorama-module links.

These helpers intentionally accept dictionaries/lists so they can be tested
without Flask or SQLAlchemy. API handlers convert ORM rows to dicts before
calling them.
"""


def _value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _is_placeholder(case):
    return bool(_value(case, 'is_placeholder', False))


def snapshot_case(case):
    """Return a compact audit snapshot for a testcase."""
    if not case:
        return {}
    return {
        'id': _value(case, 'id'),
        'library_id': _value(case, 'library_id'),
        'case_id': _value(case, 'case_id') or '',
        'title': _value(case, 'title') or '',
        'priority': _value(case, 'priority') or '',
        'type': _value(case, 'type') or '',
        'content': _value(case, 'content') or {},
        'tags': _value(case, 'tags') or [],
        'module_path': _value(case, 'module_path') or '',
    }


def changed_fields(old_snapshot, new_snapshot):
    fields = []
    for key in ('case_id', 'title', 'priority', 'type', 'content', 'tags', 'module_path'):
        if (old_snapshot or {}).get(key) != (new_snapshot or {}).get(key):
            fields.append(key)
    return fields


def collect_descendant_module_ids(modules, root_id):
    """Return root_id and every descendant module id."""
    children_by_parent = {}
    for module in modules:
        parent_id = _value(module, 'parent_id')
        children_by_parent.setdefault(parent_id, []).append(_value(module, 'id'))

    seen = set()
    stack = [root_id]
    while stack:
        mid = stack.pop()
        if mid in seen:
            continue
        seen.add(mid)
        stack.extend(children_by_parent.get(mid, []))
    return seen


def _directory_matches(case_path, link_path):
    case_path = (case_path or '').strip()
    link_path = (link_path or '').strip()
    if not link_path:
        return case_path == ''
    return case_path == link_path or case_path.startswith(link_path + '/')


def link_case_count(link, cases):
    """Count real cases covered by a library/directory/case link.

    Counts are intentionally scoped to the link and do not dedupe across links.
    """
    library_id = _value(link, 'library_id')
    link_level = _value(link, 'link_level') or 'library'
    module_path = _value(link, 'module_path') or ''
    case_pk = _value(link, 'case_pk')
    total = 0

    for case in cases:
        if _is_placeholder(case):
            continue
        if _value(case, 'library_id') != library_id:
            continue
        if link_level == 'case':
            if _value(case, 'id') == case_pk:
                total += 1
            continue
        if link_level == 'directory':
            if _directory_matches(_value(case, 'module_path') or '', module_path):
                total += 1
            continue
        total += 1
    return total


def aggregate_module_test_metrics(modules, links):
    """Aggregate direct and subtree testcase coverage metrics per module."""
    modules_by_id = {_value(module, 'id'): module for module in modules}
    metrics = {}

    for module_id, module in modules_by_id.items():
        direct_links = [link for link in links if _value(link, 'module_id') == module_id]
        direct_case_count = sum(int(_value(link, 'case_count', 0) or 0) for link in direct_links)
        descendant_ids = collect_descendant_module_ids(modules, module_id)
        subtree_links = [link for link in links if _value(link, 'module_id') in descendant_ids]
        library_ids = {
            _value(link, 'library_id')
            for link in subtree_links
            if _value(link, 'library_id') is not None
        }
        directory_links = [
            link for link in subtree_links
            if (_value(link, 'link_level') or 'library') == 'directory'
        ]
        metrics[module_id] = {
            'module_id': module_id,
            'project_id': _value(module, 'project_id'),
            'workspace_id': _value(module, 'workspace_id'),
            'direct_case_count': direct_case_count,
            'subtree_case_count': sum(int(_value(link, 'case_count', 0) or 0) for link in subtree_links),
            'linked_library_count': len(library_ids),
            'linked_directory_count': len(directory_links),
        }
    return metrics


def agent_test_metric_payload(existing, payload):
    """Keep system-derived testcase counts while accepting agent risk metrics."""
    result = dict(payload or {})
    current = existing or {}
    for key in (
            'direct_case_count', 'subtree_case_count',
            'linked_library_count', 'linked_directory_count'):
        result[key] = int(_value(current, key, 0) or 0)
    metadata = dict(_value(current, 'metrics_payload', {}) or {})
    if result.get('metrics_payload') is not None and not isinstance(result['metrics_payload'], dict):
        raise ValueError('metrics_payload must be an object')
    metadata.update(result.get('metrics_payload') or {})
    # A sync-created zero is not a risk assessment. Explicit zero is valid.
    metadata['risk_assessed'] = bool(
        (_value(current, 'metrics_payload', {}) or {}).get('risk_assessed'))
    metadata['bug_count_recorded'] = bool(
        (_value(current, 'metrics_payload', {}) or {}).get('bug_count_recorded'))
    if (payload or {}).get('bug_count') is not None:
        count = payload['bug_count']
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError('bug_count must be a non-negative integer')
        metadata['bug_count_recorded'] = True
    if (payload or {}).get('bug_risk_score') is not None:
        score = payload['bug_risk_score']
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise ValueError('bug_risk_score must be an integer from 0 to 100')
        metadata['risk_assessed'] = True
    result['metrics_payload'] = metadata
    return result


def detect_orphan_links(links, module_ids, library_ids, cases):
    """Return links whose module/library/directory/case target is stale."""
    orphans = []
    for link in links:
        reason = None
        if _value(link, 'module_id') not in module_ids:
            reason = 'module_deleted'
        elif _value(link, 'library_id') not in library_ids:
            reason = 'library_deleted'
        else:
            link_level = _value(link, 'link_level') or 'library'
            if link_level == 'directory' and link_case_count(link, cases) == 0:
                reason = 'directory_empty'
            elif link_level == 'case' and link_case_count(link, cases) == 0:
                reason = 'case_deleted'
        if reason:
            item = dict(link) if isinstance(link, dict) else {
                'id': getattr(link, 'id', None),
                'module_id': getattr(link, 'module_id', None),
                'library_id': getattr(link, 'library_id', None),
            }
            item['reason'] = reason
            orphans.append(item)
    return orphans
