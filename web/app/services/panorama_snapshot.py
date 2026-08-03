"""Pure helpers for panorama snapshot build and diff."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


MODULE_COMPARE_FIELDS = (
    'name',
    'path',
    'risk_level',
    'status',
    'parent_path',
    'test_focus',
    'code_paths',
    'resource_paths',
)


def _module_path(module: Dict[str, Any], module_path_by_id: Optional[Dict[Any, str]] = None) -> str:
    path = module.get('path') or module.get('name') or ''
    if path:
        return str(path)
    module_id = module.get('id')
    if module_path_by_id and module_id in module_path_by_id:
        return str(module_path_by_id[module_id])
    return str(module_id or '')


def _module_payload(module: Dict[str, Any], module_path_by_id: Optional[Dict[Any, str]] = None) -> Dict[str, Any]:
    parent_id = module.get('parent_id')
    parent_path = module.get('parent_path')
    if not parent_path and parent_id and module_path_by_id:
        parent_path = module_path_by_id.get(parent_id)
    return {
        'id': module.get('id'),
        'name': module.get('name'),
        'path': _module_path(module, module_path_by_id),
        'risk_level': module.get('risk_level') or 'normal',
        'status': module.get('status') or 'active',
        'parent_path': parent_path,
        'test_focus': module.get('test_focus'),
        'code_paths': module.get('code_paths') or [],
        'resource_paths': module.get('resource_paths') or [],
    }


def _relation_key(
    relation: Dict[str, Any],
    module_path_by_id: Dict[Any, str],
) -> Optional[str]:
    source_path = module_path_by_id.get(relation.get('source_module_id'))
    target_path = module_path_by_id.get(relation.get('target_module_id'))
    relation_type = relation.get('relation_type') or 'depends_on'
    if not source_path or not target_path:
        return None
    return f'relation:{source_path}->{target_path}:{relation_type}'


def build_snapshot_items(
    *,
    modules: Iterable[Dict[str, Any]],
    relations: Optional[Iterable[Dict[str, Any]]] = None,
    code_entities: Optional[Iterable[Dict[str, Any]]] = None,
    module_code_links: Optional[Iterable[Dict[str, Any]]] = None,
    module_path_by_id: Optional[Dict[Any, str]] = None,
) -> List[Dict[str, Any]]:
    module_list = [dict(m) for m in modules or []]
    path_by_id = dict(module_path_by_id or {})
    for module in module_list:
        if module.get('id') is not None:
            path_by_id[module['id']] = _module_path(module, path_by_id)

    items: List[Dict[str, Any]] = []
    for module in module_list:
        payload = _module_payload(module, path_by_id)
        path = payload['path']
        if not path:
            continue
        items.append({
            'item_type': 'module',
            'item_key': f'module:{path}',
            'payload': payload,
        })

    for relation in relations or []:
        key = _relation_key(dict(relation), path_by_id)
        if not key:
            continue
        items.append({
            'item_type': 'relation',
            'item_key': key,
            'payload': {
                'source_module_id': relation.get('source_module_id'),
                'target_module_id': relation.get('target_module_id'),
                'source_path': path_by_id.get(relation.get('source_module_id')),
                'target_path': path_by_id.get(relation.get('target_module_id')),
                'relation_type': relation.get('relation_type') or 'depends_on',
                'confidence': float(relation.get('confidence') if relation.get('confidence') is not None else 1.0),
                'evidence': relation.get('evidence') or {},
            },
        })
    entity_key_by_id = {}
    for entity in code_entities or []:
        payload = {
            'id': entity.get('id'),
            'repo_key': entity.get('repo_key') or 'default',
            'file_path': entity.get('file_path') or '',
            'entity_type': entity.get('entity_type') or 'file',
            'symbol_name': entity.get('symbol_name') or '',
            'language': entity.get('language') or '',
            'start_line': int(entity.get('start_line') or 0),
            'end_line': int(entity.get('end_line') or 0),
            'content_hash': entity.get('content_hash') or '',
            'extra': entity.get('extra') or {},
        }
        if not payload['file_path']:
            continue
        key = (
            f"code:{payload['repo_key']}:{payload['file_path']}:"
            f"{payload['entity_type']}:{payload['symbol_name']}:{payload['start_line']}"
        )
        entity_key_by_id[entity.get('id')] = key
        items.append({'item_type': 'code_entity', 'item_key': key, 'payload': payload})

    for link in module_code_links or []:
        module_path = path_by_id.get(link.get('module_id'))
        entity_key = entity_key_by_id.get(link.get('entity_id'))
        link_type = link.get('link_type') or 'owns'
        if not module_path or not entity_key:
            continue
        items.append({
            'item_type': 'code_link',
            'item_key': f'code_link:{module_path}->{entity_key}:{link_type}',
            'payload': {
                'module_path': module_path,
                'entity_key': entity_key,
                'link_type': link_type,
                'confidence': float(link.get('confidence') if link.get('confidence') is not None else 1.0),
                'evidence': link.get('evidence') or {},
                'source': link.get('source') or 'agent',
            },
        })
    return items


def _index_items(items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {item['item_key']: item for item in items or [] if item.get('item_key')}


def _field_changes(before: Dict[str, Any], after: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    changes = {}
    for field in fields:
        old = before.get(field)
        new = after.get(field)
        if old != new:
            changes[field] = {'from': old, 'to': new}
    return changes


def _risk_weight(level: Any) -> int:
    return {'low': 1, 'normal': 2, 'high': 3, 'critical': 4}.get(level or 'normal', 2)


def diff_snapshot_items(
    from_items: Iterable[Dict[str, Any]],
    to_items: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    before = _index_items(from_items)
    after = _index_items(to_items)

    added_modules = []
    removed_modules = []
    changed_modules = []
    added_relations = []
    removed_relations = []
    changed_relations = []
    risk_escalations = 0
    risk_deescalations = 0

    for key, item in after.items():
        payload = item.get('payload') or {}
        if item.get('item_type') == 'module':
            if key not in before:
                added_modules.append(payload)
                continue
            changes = _field_changes(before[key].get('payload') or {}, payload, MODULE_COMPARE_FIELDS)
            if changes:
                changed_modules.append({'path': payload.get('path'), 'changes': changes})
                if 'risk_level' in changes:
                    if _risk_weight(changes['risk_level']['to']) > _risk_weight(changes['risk_level']['from']):
                        risk_escalations += 1
                    elif _risk_weight(changes['risk_level']['to']) < _risk_weight(changes['risk_level']['from']):
                        risk_deescalations += 1
        elif item.get('item_type') == 'relation':
            if key not in before:
                added_relations.append(payload)
                continue
            changes = _field_changes(before[key].get('payload') or {}, payload, ('confidence', 'evidence'))
            if changes:
                changed_relations.append({
                    'source_path': payload.get('source_path'),
                    'target_path': payload.get('target_path'),
                    'relation_type': payload.get('relation_type'),
                    'changes': changes,
                })

    for key, item in before.items():
        payload = item.get('payload') or {}
        if key in after:
            continue
        if item.get('item_type') == 'module':
            removed_modules.append(payload)
        elif item.get('item_type') == 'relation':
            removed_relations.append(payload)

    added_modules.sort(key=lambda m: m.get('path') or '')
    removed_modules.sort(key=lambda m: m.get('path') or '')
    changed_modules.sort(key=lambda m: m.get('path') or '')

    return {
        'added_modules': added_modules,
        'removed_modules': removed_modules,
        'changed_modules': changed_modules,
        'added_relations': added_relations,
        'removed_relations': removed_relations,
        'changed_relations': changed_relations,
        'summary': {
            'module_added': len(added_modules),
            'module_removed': len(removed_modules),
            'module_changed': len(changed_modules),
            'relation_added': len(added_relations),
            'relation_removed': len(removed_relations),
            'relation_changed': len(changed_relations),
            'risk_escalations': risk_escalations,
            'risk_deescalations': risk_deescalations,
        },
    }


def snapshot_ids_to_prune(snapshots: Iterable[Dict[str, Any]], keep: int = 10) -> List[int]:
    """Return snapshot ids older than the most recent ``keep`` entries."""
    if keep < 0:
        keep = 0
    rows = sorted(
        list(snapshots or []),
        key=lambda s: (str(s.get('created_at') or ''), int(s.get('id') or 0)),
        reverse=True,
    )
    return [int(s['id']) for s in rows[keep:] if s.get('id') is not None]


def plan_restore_items(items: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Normalize snapshot items into module and relation restore plans.

    Modules are sorted parents first by path depth, so the API layer can recreate
    the tree and then resolve relation endpoints by ``source_path``/``target_path``.
    """
    modules: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    code_entities: List[Dict[str, Any]] = []
    code_links: List[Dict[str, Any]] = []
    seen_paths = set()
    seen_relations = set()
    seen_entities = set()
    seen_links = set()

    for item in items or []:
        payload = dict(item.get('payload') or {})
        item_type = item.get('item_type')
        if item_type == 'module':
            path = str(payload.get('path') or '').strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            modules.append({
                'name': payload.get('name') or path.rsplit('/', 1)[-1],
                'path': path,
                'parent_path': payload.get('parent_path'),
                'description': payload.get('description'),
                'code_paths': payload.get('code_paths') or [],
                'resource_paths': payload.get('resource_paths') or [],
                'test_focus': payload.get('test_focus'),
                'related_case_libraries': payload.get('related_case_libraries') or [],
                'status': payload.get('status') or 'active',
                'risk_level': payload.get('risk_level') or 'normal',
                'extra': payload.get('extra'),
            })
        elif item_type == 'relation':
            source_path = str(payload.get('source_path') or '').strip()
            target_path = str(payload.get('target_path') or '').strip()
            relation_type = payload.get('relation_type') or 'depends_on'
            key = (source_path, target_path, relation_type)
            if not source_path or not target_path or key in seen_relations:
                continue
            seen_relations.add(key)
            relations.append({
                'source_path': source_path,
                'target_path': target_path,
                'relation_type': relation_type,
                'confidence': float(payload.get('confidence') if payload.get('confidence') is not None else 1.0),
                'evidence': payload.get('evidence') or {},
            })
        elif item_type == 'code_entity':
            key = item.get('item_key') or ''
            if not key or key in seen_entities:
                continue
            seen_entities.add(key)
            payload['entity_key'] = key
            code_entities.append({
                'entity_key': key,
                'repo_key': payload.get('repo_key') or 'default',
                'file_path': payload.get('file_path') or '',
                'entity_type': payload.get('entity_type') or 'file',
                'symbol_name': payload.get('symbol_name') or '',
                'language': payload.get('language') or '',
                'start_line': int(payload.get('start_line') or 0),
                'end_line': int(payload.get('end_line') or 0),
                'content_hash': payload.get('content_hash') or '',
                'extra': payload.get('extra') or {},
            })
        elif item_type == 'code_link':
            module_path = str(payload.get('module_path') or '').strip()
            entity_key = str(payload.get('entity_key') or '').strip()
            link_type = payload.get('link_type') or 'owns'
            key = (module_path, entity_key, link_type)
            if not module_path or not entity_key or key in seen_links:
                continue
            seen_links.add(key)
            code_links.append({
                'module_path': module_path,
                'entity_key': entity_key,
                'link_type': link_type,
                'confidence': float(payload.get('confidence') if payload.get('confidence') is not None else 1.0),
                'evidence': payload.get('evidence') or {},
                'source': payload.get('source') or 'agent',
            })

    modules.sort(key=lambda m: (str(m.get('path') or '').count('/'), str(m.get('path') or '')))
    relations.sort(key=lambda r: (r['source_path'], r['target_path'], r['relation_type']))
    code_entities.sort(key=lambda e: e['entity_key'])
    code_links.sort(key=lambda l: (l['module_path'], l['entity_key'], l['link_type']))
    return {
        'modules': modules,
        'relations': relations,
        'code_entities': code_entities,
        'code_links': code_links,
    }
