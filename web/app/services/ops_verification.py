"""执行后校验：从数据库重读真实落库状态并与期望比较。

设计文档：docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md
核心原则（#41 提单小助手）：verify 必须从 DB 重读 actual，严禁回显请求体。
"""
from __future__ import annotations

from typing import Any

# resource_type → 模型类名（延迟到函数内导入，避免模块级依赖 Flask）
RESOURCE_MODEL_NAMES = {
    'todo': 'ClawTodo',
    'knowledge': 'KnowledgeEntry',
    'test_report': 'TestReport',
    'workflow_step': 'WorkflowRunStep',
    'agent_task': 'AgentTask',
}


def _normalize(value: Any) -> str:
    """归一为字符串用于宽松比较（None → 空串，bool → '0'/'1' 兼容整型）。"""
    if value is None:
        return ''
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def compare_expected_actual(expected: dict[str, Any],
                            actual: dict[str, Any]) -> dict[str, Any]:
    """逐键比较 expected 与 actual（宽松字符串归一）。

    返回 {'verified': bool, 'mismatches': [{'field','expected','actual'}]}。
    actual 缺键按 None 处理。
    """
    mismatches: list[dict[str, Any]] = []
    for key, exp in (expected or {}).items():
        act = actual.get(key) if actual else None
        if _normalize(exp) != _normalize(act):
            mismatches.append({
                'field': key,
                'expected': exp,
                'actual': act,
            })
    return {'verified': not mismatches, 'mismatches': mismatches}


def reread_resource(resource_type: str, resource_id: int,
                    keys: list[str]) -> dict[str, Any] | None:
    """从数据库重读资源指定字段。不存在返回 None，异常返回 None。

    受保护导入 app.models，保证无 app context 的纯单测不依赖 Flask。
    """
    model_name = RESOURCE_MODEL_NAMES.get(resource_type)
    if not model_name:
        return None
    try:
        from app import models as _models
        model = getattr(_models, model_name, None)
        if model is None:
            return None
        row = model.query.get(resource_id)
    except Exception:
        return None
    if row is None:
        return None
    actual: dict[str, Any] = {}
    for key in keys or []:
        value = getattr(row, key, None)
        try:
            import json
            json.dumps(value)
            actual[key] = value
        except Exception:
            actual[key] = _normalize(value)
    return actual
