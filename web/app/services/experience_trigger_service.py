"""Hub 侧任务前经验触发（桥接 tools/collective_memory）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

def _resolve_repo_root() -> Path:
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[3]):
        if (base / 'tools' / 'collective_memory').is_dir():
            return base
    return here.parents[3]


_REPO_ROOT = _resolve_repo_root()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.collective_memory.experience_trigger import (  # noqa: E402
    append_auto_pitfall_notice,
    extract_trigger_terms,
    infer_primary_skill,
    rank_pitfalls_by_terms,
    strip_auto_pitfall_notice,
    trigger_for_task,
)
from tools.collective_memory.pitfall_registry import (  # noqa: E402
    search_local_pitfalls,
    load_seed_entries,
    _extract_solution_from_content,
)


DEFAULT_PROJECT = 'RacingGO'


def _resolve_project_name(claw) -> str:
    if claw and getattr(claw, 'project_name', None):
        return claw.project_name
    return DEFAULT_PROJECT


def _hub_pitfall_source(project: str, module: str | None,
                        terms: list[str], limit: int) -> list[dict[str, Any]]:
    """从线上知识库（KnowledgeEntry, category=pitfall）检索公共经验。

    受保护导入 app.models：无 app context 或任何异常时返回 []，
    由调用方回退到本地 seed，保证纯单测不依赖 Flask。
    """
    if not terms:
        return []
    try:
        from app.models import KnowledgeEntry  # noqa: E402
    except Exception:
        return []
    try:
        query = KnowledgeEntry.query.filter(
            KnowledgeEntry.category == 'pitfall',
            KnowledgeEntry.status == 'approved',
        )
        if project:
            query = query.filter(KnowledgeEntry.project_name == project)
        if module:
            query = query.filter(KnowledgeEntry.module_name == module)
        rows = query.order_by(KnowledgeEntry.created_at.desc()).limit(200).all()
    except Exception:
        return []

    candidates: list[dict[str, Any]] = []
    for row in rows:
        content = row.content or ''
        candidates.append({
            'id': row.id,
            'title': (row.title or '').replace('[踩坑] ', '', 1),
            'content': content,
            'keywords': [],
            'solution': _extract_solution_from_content(content) or content[:120],
            'status': 'active',
            'module': row.module_name or '',
        })
    return rank_pitfalls_by_terms(candidates, terms, limit=limit)


def build_task_operating_context(
    title: str,
    description: str | None = None,
    *,
    project: str | None = None,
    claw=None,
    pitfall_source=None,
) -> dict[str, Any]:
    project_name = project or _resolve_project_name(claw)
    clean_description = strip_auto_pitfall_notice(description)
    task_text = '\n'.join(part for part in [title, clean_description] if part)
    terms = extract_trigger_terms(task_text)

    matched: list[dict[str, Any]] = []
    if terms:
        source = pitfall_source or _hub_pitfall_source
        try:
            matched = source(project_name, None, terms, 5) or []
        except Exception:
            matched = []
        if not matched:
            matched = search_local_pitfalls(
                load_seed_entries(),
                project=project_name,
                keywords=terms,
                limit=5,
            )
    from tools.collective_memory.experience_trigger import format_trigger_notice  # noqa: E402
    pitfall_notice = format_trigger_notice(matched)
    primary_skill = infer_primary_skill(task_text)
    return {
        'project': project_name,
        'pitfall_notice': pitfall_notice,
        'primary_skill': primary_skill,
        'trigger_terms': terms,
        'matched_pitfall_ids': [item.get('id') for item in matched if item.get('id')],
        'matched_pitfalls': matched,
        'operating_protocol_skill': 'agent-operating-protocol',
    }


def enrich_todo_description(
    title: str,
    description: str | None,
    *,
    project: str | None = None,
    claw=None,
) -> str:
    ctx = build_task_operating_context(title, description, project=project, claw=claw)
    return append_auto_pitfall_notice(description, ctx['pitfall_notice'])
