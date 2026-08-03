from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.collective_memory.knowledge_client import KnowledgeQuery
from tools.collective_memory.pitfall_registry import PitfallRegistry, search_local_pitfalls, load_seed_entries, DEFAULT_SEEDS_PATH

TASK_SKILL_MAP: list[tuple[list[str], str]] = [
    (['hub', 'openclaws', 'secrets', '密钥'], 'hub-connect'),
    (['tapd', '缺陷', '需求单'], 'tapd-integration'),
    (['企业微信', 'wecom', '企微', 'websocket'], 'hub-sse-sidecar'),
    (['需求', 'iwiki', 'story', '用户故事'], 'requirement-analysis'),
    (['用例', 'testcase', '测试用例'], 'testcase-manager'),
    (['工程', '代码分析', 'commit', '全景', 'panorama'], 'game-module-panorama'),
    (['知识', '经验', 'memos', '踩坑'], 'knowledge-manager'),
    (['待办', 'todo', '日报'], 'todo-manager'),
    (['课题', '讨论', 'topic'], 'topic-discuss'),
    (['评审', 'review'], 'case-review'),
    (['报告', '测试报告'], 'test-report-manager'),
]

TRIGGER_DICTIONARY = [
    'Hub',
    'TAPD',
    '企业微信',
    'WeCom',
    '图片',
    '截图',
    'media_id',
    'RacingGO',
    'iWiki',
    '需求',
    'WebSocket',
    'Secrets',
    'git-lfs',
    'trpc-cli',
    '用例',
]


AUTO_PITFALL_BEGIN = '<!-- hub:auto-pitfall-notice begin -->'
AUTO_PITFALL_END = '<!-- hub:auto-pitfall-notice end -->'


def infer_primary_skill(task_text: str) -> str:
    lowered = task_text.lower()
    for keys, skill_name in TASK_SKILL_MAP:
        if any(k.lower() in lowered for k in keys):
            return skill_name
    return 'agent-operating-protocol'


def strip_auto_pitfall_notice(text: str | None) -> str:
    if not text:
        return ''
    if AUTO_PITFALL_BEGIN not in text:
        return text
    before = text.split(AUTO_PITFALL_BEGIN, 1)[0]
    return before.rstrip()


def append_auto_pitfall_notice(base_description: str | None, notice: str) -> str:
    clean = strip_auto_pitfall_notice(base_description)
    if not notice:
        return clean
    block = (
        f'\n\n{AUTO_PITFALL_BEGIN}\n'
        f'## 任务前公共经验（Hub 自动附加）\n\n{notice}\n'
        f'{AUTO_PITFALL_END}'
    )
    return (clean + block) if clean else block.lstrip()


def extract_trigger_terms(task_text: str) -> list[str]:
    lowered = task_text.lower()
    return [term for term in TRIGGER_DICTIONARY if term.lower() in lowered]


MAX_NOTICE_ENTRIES = 3
MAX_SOLUTION_CHARS = 120


def format_trigger_notice(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ''
    lines = ['任务开始前命中以下公共经验，请先检查：']
    for entry in entries[:MAX_NOTICE_ENTRIES]:
        title = entry.get('title', '')
        solution = entry.get('solution', '')
        if len(solution) > MAX_SOLUTION_CHARS:
            solution = solution[:MAX_SOLUTION_CHARS].rstrip() + '…'
        status = entry.get('status')
        suffix = f' [{status}]' if status == 'workaround' else ''
        lines.append(f'- {title}{suffix}：{solution}')
    if len(entries) > MAX_NOTICE_ENTRIES:
        lines.append(f'- （另有 {len(entries) - MAX_NOTICE_ENTRIES} 条，请 GET /knowledge?category=pitfall 查看）')
    return '\n'.join(lines)


def rank_pitfalls_by_terms(
    entries: list[dict[str, Any]],
    terms: list[str],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按命中词数对通用条目打分排序（纯函数，不依赖具体来源）。

    统计 terms 在 title + content + keywords 中的命中数；命中>0 才保留，
    按 (命中数 desc, id asc) 排序，截断到 limit。返回原始 entry。
    """
    lowered_terms = [t.lower() for t in terms if t]
    scored: list[tuple[int, Any, dict[str, Any]]] = []
    for entry in entries:
        haystack = ' '.join([
            str(entry.get('title', '')),
            str(entry.get('content', '')),
            ' '.join(entry.get('keywords') or []),
        ]).lower()
        score = sum(1 for term in lowered_terms if term in haystack)
        if score > 0:
            scored.append((score, entry.get('id'), entry))
    scored.sort(key=lambda item: (-item[0], _sort_key(item[1])))
    return [entry for _, _, entry in scored[:limit]]


def _sort_key(value: Any):
    """id 可能是 int 或 str，统一成可比较的元组。"""
    if isinstance(value, int):
        return (0, value)
    return (1, str(value))


def build_trigger_query(task_text: str, project: str, *, limit: int = 5) -> KnowledgeQuery | None:
    terms = extract_trigger_terms(task_text)
    if not terms:
        return None
    modules = _infer_modules(terms)
    return KnowledgeQuery(project=project, keywords=terms, modules=modules, limit=limit)


def trigger_for_task(
    task_text: str,
    project: str = 'RacingGO',
    *,
    registry: PitfallRegistry | None = None,
    seeds_path: Path | None = None,
    limit: int = 5,
) -> str:
    query = build_trigger_query(task_text, project, limit=limit)
    if not query:
        return ''

    if registry is None:
        entries = load_seed_entries(seeds_path or DEFAULT_SEEDS_PATH)
        matched = search_local_pitfalls(
            entries,
            project=query.project,
            keywords=query.keywords,
            modules=query.modules or None,
            limit=query.limit,
        )
    else:
        matched = registry.search(query)

    return format_trigger_notice(matched)


def _infer_modules(terms: list[str]) -> list[str]:
    modules: list[str] = []
    mapping = {
        'hub': ['Hub', 'Secrets', '用例'],
        'tapd': ['TAPD', '截图'],
        'wecom': ['企业微信', 'WeCom', 'WebSocket', 'media_id', '图片'],
        'requirement': ['需求', 'iWiki', 'RacingGO'],
        'deployment': ['git-lfs', 'trpc-cli'],
    }
    lowered = {t.lower() for t in terms}
    for module, keys in mapping.items():
        if any(k.lower() in lowered for k in keys):
            modules.append(module)
    return modules
