"""Pure helpers for building the panorama graph payload.

This module intentionally has no Flask/SQLAlchemy dependency so it can be
tested locally and reused by API handlers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


RISK_WEIGHT = {
    "low": 1,
    "normal": 2,
    "high": 3,
    "critical": 4,
}


def _module_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        match = re.search(r"\((\d+)\)", stripped)
        if match:
            return int(match.group(1))
        return None
    if isinstance(value, dict):
        for key in ("id", "module_id", "target_module_id"):
            if key in value:
                return _module_id(value.get(key))
    return None


def _module_label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("path") or "")
    if isinstance(value, str):
        return value
    return ""


def _module_reason(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("reason") or value.get("summary") or "")
    if isinstance(value, str) and ":" in value:
        return value.split(":", 1)[1].strip()
    return ""


def _dedupe_key(edge: Dict[str, Any]) -> Tuple[int, int, str]:
    return (
        int(edge["source_module_id"]),
        int(edge["target_module_id"]),
        str(edge["relation_type"]),
    )


def _edge(
    source_id: int,
    target_id: int,
    relation_type: str,
    *,
    confidence: float = 1.0,
    evidence: Optional[Dict[str, Any]] = None,
    source: str = "derived",
    relation_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "id": relation_id,
        "source_module_id": int(source_id),
        "target_module_id": int(target_id),
        "relation_type": relation_type,
        "confidence": float(confidence if confidence is not None else 1.0),
        "evidence": evidence or {},
        "source": source,
    }


def _legacy_edges(modules: List[Dict[str, Any]], module_ids: set) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for module in modules:
        module_id = _module_id(module.get("id"))
        if module_id is None:
            continue

        parent_id = _module_id(module.get("parent_id"))
        if parent_id in module_ids:
            edges.append(_edge(parent_id, module_id, "child", source="parent_id"))

        extra = module.get("extra") or {}
        if not isinstance(extra, dict):
            continue

        for dep in extra.get("dependencies") or []:
            dep_id = _module_id(dep)
            if dep_id in module_ids:
                edges.append(_edge(
                    module_id,
                    dep_id,
                    "depends_on",
                    evidence={"legacy": "extra.dependencies"},
                    source="legacy_extra",
                ))

        cross = extra.get("cross_module") or {}
        if not isinstance(cross, dict):
            continue
        for dep in cross.get("depends_on") or []:
            dep_id = _module_id(dep)
            if dep_id in module_ids:
                edges.append(_edge(
                    module_id,
                    dep_id,
                    "depends_on",
                    evidence={
                        "legacy": "extra.cross_module.depends_on",
                        "label": _module_label(dep),
                        "reason": _module_reason(dep),
                    },
                    source="legacy_extra",
                ))
        for affected in cross.get("affects") or []:
            affected_id = _module_id(affected)
            if affected_id in module_ids:
                edges.append(_edge(
                    module_id,
                    affected_id,
                    "affects",
                    evidence={
                        "legacy": "extra.cross_module.affects",
                        "label": _module_label(affected),
                        "reason": _module_reason(affected),
                    },
                    source="legacy_extra",
                ))
    return edges


def _explicit_edges(relations: Iterable[Dict[str, Any]], module_ids: set) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for relation in relations or []:
        source_id = _module_id(relation.get("source_module_id"))
        target_id = _module_id(relation.get("target_module_id"))
        if source_id not in module_ids or target_id not in module_ids:
            continue
        relation_type = relation.get("relation_type") or "depends_on"
        edges.append(_edge(
            source_id,
            target_id,
            relation_type,
            confidence=relation.get("confidence", 1.0),
            evidence=relation.get("evidence") or {},
            source=relation.get("source") or "relation",
            relation_id=relation.get("id"),
        ))
    return edges


def _merge_edges(edges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[int, int, str], Dict[str, Any]] = {}
    priority = {
        "relation": 4,
        "agent": 4,
        "manual": 4,
        "code_index": 4,
        "migration": 3,
        "parent_id": 2,
        "legacy_extra": 1,
        "derived": 0,
    }
    for edge in edges:
        key = _dedupe_key(edge)
        current = merged.get(key)
        if not current:
            merged[key] = edge
            continue
        current_score = priority.get(str(current.get("source")), 0)
        next_score = priority.get(str(edge.get("source")), 0)
        if next_score > current_score:
            merged[key] = edge
    return list(merged.values())


def _node(module: Dict[str, Any], degree: int) -> Dict[str, Any]:
    node = dict(module)
    node["degree"] = degree
    return node


def _metrics(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    degree = defaultdict(int)
    incoming = defaultdict(int)
    outgoing = defaultdict(int)
    for edge in edges:
        s = edge["source_module_id"]
        t = edge["target_module_id"]
        degree[s] += 1
        degree[t] += 1
        outgoing[s] += 1
        incoming[t] += 1

    by_id = {int(n["id"]): n for n in nodes}
    ranked = sorted(
        (_node(n, degree[int(n["id"])]) for n in nodes),
        key=lambda n: (n["degree"], RISK_WEIGHT.get(n.get("risk_level"), 2), n.get("name") or ""),
        reverse=True,
    )
    hub_modules = [n for n in ranked if n["degree"] > 0][:10]
    bridge_modules = [
        _node(n, degree[int(n["id"])])
        for n in nodes
        if incoming[int(n["id"])] > 0 and outgoing[int(n["id"])] > 0
    ]
    bridge_modules.sort(key=lambda n: (n["degree"], n.get("name") or ""), reverse=True)
    isolated_modules = [
        _node(n, 0)
        for n in nodes
        if degree[int(n["id"])] == 0
    ]
    isolated_modules.sort(key=lambda n: n.get("name") or "")
    untested_high_risk_modules = [
        _node(n, degree[int(n["id"])])
        for n in nodes
        if n.get("risk_level") in ("high", "critical") and not n.get("related_case_libraries")
    ]
    untested_high_risk_modules.sort(
        key=lambda n: (RISK_WEIGHT.get(n.get("risk_level"), 2), n["degree"]),
        reverse=True,
    )
    surprising_relations = []
    for edge in edges:
        source = by_id.get(edge["source_module_id"])
        target = by_id.get(edge["target_module_id"])
        if not source or not target:
            continue
        source_root = str(source.get("path") or source.get("name") or "").split("/", 1)[0]
        target_root = str(target.get("path") or target.get("name") or "").split("/", 1)[0]
        if source_root and target_root and source_root != target_root and edge["relation_type"] != "child":
            surprising_relations.append({
                "source_module_id": edge["source_module_id"],
                "target_module_id": edge["target_module_id"],
                "relation_type": edge["relation_type"],
                "reason": "cross_root_module",
            })

    return {
        "hub_modules": hub_modules,
        "bridge_modules": bridge_modules[:10],
        "isolated_modules": isolated_modules[:20],
        "untested_high_risk_modules": untested_high_risk_modules[:20],
        "surprising_relations": surprising_relations[:20],
    }


def build_graph_payload(
    modules: Iterable[Dict[str, Any]],
    relations: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    module_list = [dict(m) for m in (modules or []) if _module_id(m.get("id")) is not None]
    module_ids = {_module_id(m.get("id")) for m in module_list}
    edges = _merge_edges(
        _legacy_edges(module_list, module_ids)
        + _explicit_edges(relations or [], module_ids)
    )
    degree = defaultdict(int)
    for edge in edges:
        degree[edge["source_module_id"]] += 1
        degree[edge["target_module_id"]] += 1
    nodes = [_node(module, degree[int(module["id"])]) for module in module_list]
    return {
        "nodes": nodes,
        "edges": edges,
        "metrics": _metrics(nodes, edges),
    }
