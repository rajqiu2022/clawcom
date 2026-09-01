"""Test-task testcase selection, library reconciliation and one-step restore."""

import hashlib
import json
import re
from datetime import datetime

from app import db
from app.models import TestCase, TestTaskCase


def _json_copy(value):
    return json.loads(json.dumps(value or {}, ensure_ascii=False, default=str))


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).lower()


def case_snapshot(case):
    semantic = {
        'case_key': str(case.case_id or '').strip(),
        'mindmap_node_id': str(case.mindmap_node_id or '').strip(),
        'module_path': str(case.module_path or '').strip().strip('/'),
        'title': str(case.title or '').strip(),
        'priority': case.priority or '',
        'type': case.type or '',
        'content': _json_copy(case.content),
        'tags': list(case.tags or []),
    }
    digest = hashlib.sha256(json.dumps(
        semantic, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    # The task only needs a deterministic fingerprint and matching keys. Do
    # not duplicate full steps/expected-results into every task row/backup.
    compact = dict(semantic)
    compact.pop('content', None)
    return dict({'source_case_id': case.id, 'sha256': digest}, **compact)


def case_reference(case):
    snap = case_snapshot(case)
    return {key: snap[key] for key in (
        'source_case_id', 'case_key', 'mindmap_node_id',
        'module_path', 'title')}


def _match_keys(snapshot):
    keys = []
    if snapshot.get('case_key'):
        keys.append('case-key:' + _norm(snapshot['case_key']))
    if snapshot.get('mindmap_node_id'):
        keys.append('mindmap:' + _norm(snapshot['mindmap_node_id']))
    path_title = '%s\x1f%s' % (
        _norm(snapshot.get('module_path')), _norm(snapshot.get('title')))
    if path_title.strip('\x1f'):
        keys.append('path-title:' + path_title)
    if snapshot.get('title'):
        keys.append('title:' + _norm(snapshot['title']))
    return keys


def enrich_case_filter(library_id, raw_filter):
    result = _json_copy(raw_filter)
    raw_ids = result.get('case_ids') or []
    case_ids = []
    for value in raw_ids:
        try:
            cid = int(value)
        except (TypeError, ValueError):
            continue
        if cid not in case_ids:
            case_ids.append(cid)
    result['case_ids'] = case_ids
    existing_refs = result.get('selected_case_refs') or []
    if library_id and existing_refs:
        candidates = TestCase.query.filter(
            TestCase.library_id == int(library_id),
            TestCase.is_placeholder != True,  # noqa: E712
        ).all()
        matched = _match_references(candidates, existing_refs)
        result['case_ids'] = [case.id for case in matched]
        result['selected_case_refs'] = [case_reference(case) for case in matched]
    elif library_id and case_ids:
        rows = TestCase.query.filter(
            TestCase.library_id == int(library_id),
            TestCase.id.in_(case_ids),
            TestCase.is_placeholder != True,  # noqa: E712
        ).all()
        by_id = {row.id: row for row in rows}
        result['selected_case_refs'] = [
            case_reference(by_id[cid]) for cid in case_ids if cid in by_id]
    elif not case_ids:
        result.pop('selected_case_refs', None)
    return result or None


def _matches_module(case, paths):
    case_path = str(case.module_path or '').strip().strip('/')
    return any(
        not str(path or '').strip().strip('/')
        or case_path == str(path).strip().strip('/')
        or case_path.startswith(str(path).strip().strip('/') + '/')
        for path in paths)


def _match_references(cases, references):
    index = {}
    for case in cases:
        snap = case_snapshot(case)
        for key in _match_keys(snap):
            index.setdefault(key, []).append(case)
    selected = []
    selected_ids = set()
    for ref in references or []:
        hit = None
        for key in _match_keys(ref):
            candidates = [c for c in index.get(key, [])
                          if c.id not in selected_ids]
            if len(candidates) == 1:
                hit = candidates[0]
                break
        if hit:
            selected.append(hit)
            selected_ids.add(hit.id)
    return selected


def select_library_cases(library_id, raw_filter):
    if not library_id:
        return []
    case_filter = raw_filter or {}
    # 关联了用例库不等于选择整个库。空筛选必须是 0 条，只有显式
    # select_all 才允许把全库导入任务，避免新建/同步时意外扩张范围。
    if not case_filter:
        return []
    query = TestCase.query.filter(
        TestCase.library_id == int(library_id),
        TestCase.is_placeholder != True,  # noqa: E712
    )
    priorities = case_filter.get('priorities') or []
    types = case_filter.get('types') or []
    if priorities:
        query = query.filter(TestCase.priority.in_(priorities))
    if types:
        query = query.filter(TestCase.type.in_(types))
    candidates = query.order_by(TestCase.id.asc()).all()

    module_paths = case_filter.get('module_paths') or []
    case_ids = {int(v) for v in (case_filter.get('case_ids') or [])
                if str(v).isdigit()}
    references = case_filter.get('selected_case_refs') or []
    reference_cases = _match_references(candidates, references)
    reference_ids = {case.id for case in reference_cases}

    if case_filter.get('selection_mode') == 'nodes':
        if case_filter.get('select_none') is True:
            return []
        if case_filter.get('select_all') is True:
            return candidates
        if not module_paths and not case_ids and not references:
            return []
        return [case for case in candidates if (
            (module_paths and _matches_module(case, module_paths))
            or case.id in case_ids or case.id in reference_ids)]

    if module_paths:
        candidates = [case for case in candidates
                      if _matches_module(case, module_paths)]
    if references:
        allowed = reference_ids
        candidates = [case for case in candidates if case.id in allowed]
    elif case_ids:
        candidates = [case for case in candidates if case.id in case_ids]
    return candidates


def build_sync_plan(task):
    desired = select_library_cases(task.library_id, task.case_filter)
    old_rows = task.task_cases.order_by(TestTaskCase.id.asc()).all()
    old_by_id = {row.case_id: row for row in old_rows}
    old_index = {}
    for row in old_rows:
        snap = row.case_source_snapshot or (
            case_snapshot(row.case) if row.case else {})
        for key in _match_keys(snap):
            old_index.setdefault(key, []).append(row)

    used_old = set()
    matches = []
    added = []
    for case in desired:
        row = old_by_id.get(case.id)
        if row and row.id in used_old:
            row = None
        if not row:
            for key in _match_keys(case_snapshot(case)):
                candidates = [candidate for candidate in old_index.get(key, [])
                              if candidate.id not in used_old]
                if len(candidates) == 1:
                    row = candidates[0]
                    break
        if not row:
            added.append(case)
            continue
        used_old.add(row.id)
        old_snapshot = row.case_source_snapshot or (
            case_snapshot(row.case) if row.case else {})
        new_snapshot = case_snapshot(case)
        matches.append({
            'row': row,
            'case': case,
            'changed': bool(old_snapshot.get('sha256')
                            and old_snapshot.get('sha256') != new_snapshot['sha256']),
            'relinked': row.case_id != case.id,
            'new_snapshot': new_snapshot,
        })
    removed = [row for row in old_rows if row.id not in used_old]
    affected_executed = [
        item['row'].id for item in matches
        if item['changed'] and item['row'].status != 'pending'
    ] + [row.id for row in removed if row.status != 'pending']
    return {
        'desired': desired,
        'matches': matches,
        'added': added,
        'removed': removed,
        'summary': {
            'current_total': len(old_rows),
            'latest_total': len(desired),
            'preserved': len(matches),
            'changed': sum(1 for item in matches if item['changed']),
            'relinked': sum(1 for item in matches if item['relinked']),
            'added': len(added),
            'removed': len(removed),
            'executed_results_affected': len(set(affected_executed)),
        },
    }


def serialize_backup(task):
    rows = task.task_cases.order_by(TestTaskCase.id.asc()).all()
    return {
        'schema_version': 1,
        'task_id': task.id,
        'library_id': task.library_id,
        'case_filter': _json_copy(task.case_filter),
        'task_cases': [{
            'case_id': row.case_id,
            'status': row.status,
            'executed_at': str(row.executed_at) if row.executed_at else None,
            'executed_by': row.executed_by,
            'note': row.note,
            'tapd_bug_id': row.tapd_bug_id,
            'tapd_bug_url': row.tapd_bug_url,
            'case_info_snapshot': _json_copy(row.case_info_snapshot),
            'case_source_snapshot': _json_copy(
                row.case_source_snapshot or (
                    case_snapshot(row.case) if row.case else {})),
            'bug_sync_status': row.bug_sync_status,
            'bug_sync_error': row.bug_sync_error,
            'bug_synced_at': str(row.bug_synced_at) if row.bug_synced_at else None,
        } for row in rows],
    }


def apply_sync(task, plan):
    sync_plan = build_sync_plan(task)
    task.case_sync_backup_json = serialize_backup(task)
    task.case_sync_backup_created_at = datetime.now()
    task.case_sync_backup_restored_at = None

    for row in sync_plan['removed']:
        db.session.delete(row)
    for item in sync_plan['matches']:
        item['row'].case_id = item['case'].id
        item['row'].case_source_snapshot = item['new_snapshot']
    for case in sync_plan['added']:
        db.session.add(TestTaskCase(
            task_id=task.id,
            case_id=case.id,
            status='pending',
            case_source_snapshot=case_snapshot(case),
        ))
    task.case_filter = enrich_case_filter(task.library_id, task.case_filter)
    db.session.flush()
    return sync_plan


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def restore_backup(task):
    backup = task.case_sync_backup_json or {}
    if not backup or task.case_sync_backup_restored_at:
        raise ValueError('没有可用的同步备份，或本次还原机会已使用')
    rows = backup.get('task_cases') or []
    case_ids = [int(row['case_id']) for row in rows if row.get('case_id')]
    existing = {row[0] for row in db.session.query(TestCase.id).filter(
        TestCase.id.in_(case_ids or [-1])).all()}
    missing = sorted(set(case_ids) - existing)
    if missing:
        raise ValueError('备份中的 %d 条源用例已不存在，无法完整还原' % len(missing))
    for current in TestTaskCase.query.filter_by(task_id=task.id).all():
        db.session.delete(current)
    db.session.flush()
    for item in rows:
        db.session.add(TestTaskCase(
            task_id=task.id,
            case_id=int(item['case_id']),
            status=item.get('status') or 'pending',
            executed_at=_parse_datetime(item.get('executed_at')),
            executed_by=item.get('executed_by'),
            note=item.get('note'),
            tapd_bug_id=item.get('tapd_bug_id'),
            tapd_bug_url=item.get('tapd_bug_url'),
            case_info_snapshot=item.get('case_info_snapshot') or {},
            case_source_snapshot=item.get('case_source_snapshot') or {},
            bug_sync_status=item.get('bug_sync_status') or 'none',
            bug_sync_error=item.get('bug_sync_error'),
            bug_synced_at=_parse_datetime(item.get('bug_synced_at')),
        ))
    task.library_id = backup.get('library_id')
    task.case_filter = backup.get('case_filter')
    task.case_sync_backup_restored_at = datetime.now()
    db.session.flush()
    return {'restored_cases': len(rows), 'backup_created_at': str(
        task.case_sync_backup_created_at) if task.case_sync_backup_created_at else None}
