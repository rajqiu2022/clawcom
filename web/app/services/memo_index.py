"""笔记索引（三层记忆·记全层）

把某个 claw 存在 Memos 里的「当前任务上下文(taskctx)」与「关键决策/配置(decision)」
压成轻量索引，随 sidecar-config 下发。sidecar 每次构 prompt 前注入该索引，让 agent
「知道自己记过什么」，需要全文时再 GET /api/v1/memos/memo/{id} 取——省 token、跨 session。

约定标签（写入侧用 /memos/upsert）：
  - 任务上下文：#openclaw/taskctx/{ref}   ref 如 todo-2298 / agent_task-123 / workflow_step-45
  - 关键决策：  #openclaw/decision/{scope}
均带 #claw-{name} 做归属隔离。已归档(ARCHIVED)的 memo 不会出现在 search，故索引天然只含"进行中/常驻"。
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

_KINDS = (
    ('taskctx', '#openclaw/taskctx/'),
    ('decision', '#openclaw/decision/'),
)


def _parse_memo(content: str):
    """从 memo markdown 解析 (title, summary)。

    结构：`# 标题` / 空行 / 若干 `#标签` 行 / 空行 / 正文。
    heading 以 `# `(井号+空格) 开头；标签行以 `#`+非空格开头。
    """
    title = ''
    body: List[str] = []
    for line in (content or '').split('\n'):
        s = line.strip()
        if not s:
            continue
        if s.startswith('# '):
            if not title:
                title = s[2:].strip()
            continue
        if s.startswith('#'):  # 标签行
            continue
        body.append(s)
    summary = ' '.join(body).strip()
    return title, summary


def build_memo_index(claw, limit: int = 20) -> List[dict]:
    """构建该 claw 的笔记索引，taskctx 优先，其次 decision，按更新时间倒序。"""
    name = getattr(claw, 'name', None)
    if not name:
        return []
    try:
        from app import memos_client
    except Exception:
        return []
    try:
        memos = memos_client.search_memos(claw_name=name, limit=max(limit * 3, 40))
    except Exception as e:
        logger.warning('build_memo_index 拉取失败 claw=%s: %s', name, e)
        return []

    items: List[dict] = []
    for m in memos:
        content = m.get('content') or ''
        kind = None
        for k, needle in _KINDS:
            if needle in content:
                kind = k
                break
        if not kind:
            continue
        title, summary = _parse_memo(content)
        ref = ''
        mt = re.search(r'#openclaw/(?:taskctx|decision)/([^\s#]+)', content)
        if mt:
            ref = mt.group(1)
        items.append({
            'id': (m.get('name') or '').split('/')[-1],
            'kind': kind,
            'ref': ref,
            'title': (title or summary[:30] or '(无标题)')[:80],
            'summary': summary[:80],
            'updated': (m.get('updateTime') or '')[:16],
        })

    # taskctx 优先；同类按更新时间倒序（search 默认已是近更新在前，稳定排序即可）
    items.sort(key=lambda x: 0 if x['kind'] == 'taskctx' else 1)
    return items[:limit]
