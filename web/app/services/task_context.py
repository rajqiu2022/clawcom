"""统一任务上下文包：todo / agent_task / workflow_step 共用同一注入结构。

设计文档：docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md
"""
from __future__ import annotations

from typing import Any

try:  # Flask 运行时
    from app.services.experience_trigger_service import build_task_operating_context
except Exception:  # 独立单测（模块名直接可用）
    from experience_trigger_service import build_task_operating_context  # type: ignore


# 通用壳 Skill：所有任务恒定要求加载
GENERAL_SHELL_SKILL = 'basic-operations-preflight'

# 5 条通用铁律的执行前自检项（对应设计文档）
PREFLIGHT_CHECKLIST = [
    '身份校验：确认当前 claw_id 与 token 前缀一致',
    '读写分离：验证只用 GET，禁止用 POST/PATCH/DELETE 做验证',
    '提交后回读：写操作后用独立 GET 确认真实落库',
    '查证再填：枚举/ID 字段必须从 API options 取，禁止凭记忆手打',
    '配置校验：配置变更后比对 hash，防止配置漂移',
]

MAX_TOP_PITFALLS = 3
MAX_SOLUTION_CHARS = 120


def build_task_context_payload(
    title: str,
    description: str | None = None,
    *,
    project: str | None = None,
    claw=None,
    pitfall_source=None,
) -> dict[str, Any]:
    """构建任务上下文包。字段全部给安全默认值。"""
    ctx = build_task_operating_context(
        title or '', description, project=project, claw=claw,
        pitfall_source=pitfall_source,
    )

    primary_skill = ctx.get('primary_skill') or 'agent-operating-protocol'
    required_skills: list[str] = []
    for skill in (primary_skill, GENERAL_SHELL_SKILL):
        if skill and skill not in required_skills:
            required_skills.append(skill)

    matched = ctx.get('matched_pitfalls') or []
    top_pitfalls: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    for item in matched[:MAX_TOP_PITFALLS]:
        solution = str(item.get('solution') or '')
        if len(solution) > MAX_SOLUTION_CHARS:
            solution = solution[:MAX_SOLUTION_CHARS - 1].rstrip() + '…'
        top_pitfalls.append({
            'id': item.get('id'),
            'title': item.get('title') or '',
            'solution': solution,
            'status': item.get('status') or 'active',
        })
        if item.get('id') is not None:
            references.append({
                'type': 'knowledge',
                'id': item.get('id'),
                'title': item.get('title') or '',
            })

    return {
        'project': ctx.get('project') or project or '',
        'required_skills': required_skills,
        'primary_skill': primary_skill,
        'operating_protocol_skill': ctx.get('operating_protocol_skill') or 'agent-operating-protocol',
        'trigger_terms': ctx.get('trigger_terms') or [],
        'pitfall_notice': ctx.get('pitfall_notice') or '',
        'matched_pitfall_ids': ctx.get('matched_pitfall_ids') or [],
        'top_pitfalls': top_pitfalls,
        'preflight_checklist': list(PREFLIGHT_CHECKLIST),
        'references': references,
    }


def build_heartbeat_memory_inject(
    titles,
    *,
    project=None,
    claw=None,
    pitfall_source=None,
    limit=3,
):
    """心跳分层记忆注入：按今日待办标题聚合命中的公共经验（去重，限流）。

    返回 {'pitfall_alerts': [{id,title,solution}], 'unread_pitfall_count': N}。
    对应 #41 建议3/5：心跳按任务上下文返回匹配片段 + 未读 pitfall 数。
    """
    seen: dict = {}
    for title in (titles or []):
        if not title:
            continue
        ctx = build_task_context_payload(
            title, None, project=project, claw=claw, pitfall_source=pitfall_source)
        for p in ctx.get('top_pitfalls') or []:
            pid = p.get('id')
            key = pid if pid is not None else p.get('title')
            if key in seen:
                continue
            seen[key] = {
                'id': pid,
                'title': p.get('title') or '',
                'solution': p.get('solution') or '',
            }
    alerts = list(seen.values())
    return {
        'pitfall_alerts': alerts[:limit],
        'unread_pitfall_count': len(alerts),
    }
