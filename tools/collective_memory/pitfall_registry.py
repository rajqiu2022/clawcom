from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.collective_memory.knowledge_client import HubKnowledgeClient, KnowledgeClient, KnowledgeQuery

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / 'knowledge' / 'schemas' / 'pitfall_entry.schema.json'
DEFAULT_SEEDS_PATH = REPO_ROOT / 'knowledge' / 'seeds' / 'racinggo-pitfalls.initial.json'

REQUIRED_FIELDS = [
    'id', 'title', 'project', 'module', 'type', 'symptom', 'solution', 'keywords', 'status', 'updated_at',
]
VALID_TYPES = {'pitfall', 'workaround', 'fixed', 'api', 'deployment', 'wecom', 'tapd', 'hub'}
VALID_STATUSES = {'active', 'workaround', 'fixed', 'expired'}
TRIGGERABLE_STATUSES = {'active', 'workaround'}


def load_json(path: Path) -> Any:
    with path.open(encoding='utf-8') as f:
        return json.load(f)


def validate_pitfall_entry(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in entry or entry[field] in (None, ''):
            errors.append(f'missing required field: {field}')
    if entry.get('type') not in VALID_TYPES:
        errors.append(f"invalid type: {entry.get('type')}")
    if entry.get('status') not in VALID_STATUSES:
        errors.append(f"invalid status: {entry.get('status')}")
    keywords = entry.get('keywords')
    if not isinstance(keywords, list) or not keywords:
        errors.append('keywords must be a non-empty list')
    verified_steps = entry.get('verified_steps', [])
    if not verified_steps:
        errors.append('verified_steps must contain at least one step')
    return errors


def format_pitfall_markdown(entry: dict[str, Any]) -> str:
    verified = entry.get('verified_steps') or []
    verified_lines = '\n'.join(f'{i}. {step}' for i, step in enumerate(verified, 1))
    keywords = ', '.join(entry.get('keywords') or [])
    evidence = entry.get('evidence') or {}
    discussion_url = evidence.get('discussion_url', '')
    root_cause = entry.get('root_cause') or '未确认'

    return (
        f"<!-- pitfall_id: {entry['id']} -->\n\n"
        f"# 踩坑登记：{entry['title']}\n\n"
        f"**ID:** `{entry['id']}`\n"
        f"**Project:** {entry['project']}\n"
        f"**Module:** {entry['module']}\n"
        f"**Type:** {entry['type']}\n"
        f"**Status:** {entry['status']}\n"
        f"**Owner:** {entry.get('owner', 'unknown')}\n"
        f"**Updated At:** {entry['updated_at']}\n\n"
        f"## 现象\n\n{entry['symptom']}\n\n"
        f"## 根因\n\n{root_cause}\n\n"
        f"## 解决方式\n\n{entry['solution']}\n\n"
        f"## 验证步骤\n\n{verified_lines}\n\n"
        f"## 关键词\n\n{keywords}\n\n"
        f"## 相关链接\n\n"
        f"- 讨论帖：{discussion_url}\n"
    )


def pitfall_to_knowledge_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        'title': f"[踩坑] {entry['title']}",
        'content': format_pitfall_markdown(entry),
        'category': 'pitfall',
        'scope': 'project',
        'project_name': entry['project'],
        'module_name': entry['module'],
        'status': 'approved',
        'source_type': 'manual',
    }


_SEED_CACHE: dict[str, list[dict[str, Any]]] = {}


def load_seed_entries(path: Path | None = None) -> list[dict[str, Any]]:
    seed_path = path or DEFAULT_SEEDS_PATH
    cache_key = str(seed_path.resolve())
    if cache_key in _SEED_CACHE:
        return _SEED_CACHE[cache_key]
    entries = load_json(seed_path)
    if not isinstance(entries, list):
        raise ValueError(f'seed file must be a JSON array: {seed_path}')
    _SEED_CACHE[cache_key] = entries
    return entries


def validate_seed_file(path: Path | None = None) -> list[str]:
    errors: list[str] = []
    for entry in load_seed_entries(path):
        entry_errors = validate_pitfall_entry(entry)
        for err in entry_errors:
            errors.append(f"{entry.get('id', '?')}: {err}")
    return errors


def score_pitfall_match(entry: dict[str, Any], terms: list[str]) -> int:
    if entry.get('status') not in TRIGGERABLE_STATUSES:
        return 0
    haystack = ' '.join([
        entry.get('title', ''),
        entry.get('symptom', ''),
        entry.get('solution', ''),
        ' '.join(entry.get('keywords') or []),
        entry.get('module', ''),
    ]).lower()
    score = 0
    for term in terms:
        if term.lower() in haystack:
            score += 1
    return score


def search_local_pitfalls(
    entries: list[dict[str, Any]],
    *,
    project: str,
    keywords: list[str],
    modules: list[str] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        if entry.get('project') != project:
            continue
        if modules and entry.get('module') not in modules:
            continue
        score = score_pitfall_match(entry, keywords)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], item[1].get('id', '')))
    return [entry for _, entry in scored[:limit]]


class PitfallRegistry:
    def __init__(self, client: KnowledgeClient | None = None,
                 seeds_path: Path | None = None):
        self.client = client or HubKnowledgeClient()
        self.seeds_path = seeds_path or DEFAULT_SEEDS_PATH
        self._local_entries = load_seed_entries(self.seeds_path)

    def list_local(self) -> list[dict[str, Any]]:
        return list(self._local_entries)

    def search(self, query: KnowledgeQuery, *, prefer_remote: bool = False) -> list[dict[str, Any]]:
        if prefer_remote and getattr(self.client, 'base_url', ''):
            try:
                remote = self.client.search_entries('pitfall', query)
                if remote:
                    return self._normalize_remote(remote, query.limit)
            except Exception:
                pass
        local = search_local_pitfalls(
            self._local_entries,
            project=query.project,
            keywords=query.keywords,
            modules=query.modules or None,
            limit=query.limit,
        )
        return local

    def _normalize_remote(self, entries: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in entries[:limit]:
            normalized.append({
                'id': item.get('id'),
                'title': item.get('title', '').replace('[踩坑] ', '', 1),
                'solution': _extract_solution_from_content(item.get('content', '')),
                'source': 'hub',
            })
        return normalized

    def import_seeds_to_hub(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        validation_errors = validate_seed_file(self.seeds_path)
        if validation_errors:
            raise ValueError('seed validation failed:\n' + '\n'.join(validation_errors))

        results: list[dict[str, Any]] = []
        for entry in self._local_entries:
            payload = pitfall_to_knowledge_payload(entry)
            if dry_run:
                results.append({'dry_run': True, 'pitfall_id': entry['id'], 'payload': payload})
            else:
                created = self.client.create_entry('pitfall', payload)
                results.append({'pitfall_id': entry['id'], 'knowledge_id': created.get('id')})
        return results


def _extract_solution_from_content(content: str) -> str:
    marker = '## 解决方式'
    if marker not in content:
        return ''
    section = content.split(marker, 1)[1]
    for stop in ('## 验证步骤', '## 关键词', '## 相关链接'):
        if stop in section:
            section = section.split(stop, 1)[0]
    return section.strip()
