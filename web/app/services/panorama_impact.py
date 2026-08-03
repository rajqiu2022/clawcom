"""Pure helpers for panorama change impact analysis."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


RISK_WEIGHT = {
    "low": 20,
    "normal": 40,
    "high": 70,
    "critical": 90,
}


def _norm_path(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _path_matches(changed_file: str, candidate: str) -> bool:
    changed = _norm_path(changed_file).lower()
    target = _norm_path(candidate).lower()
    if not changed or not target:
        return False
    if changed == target:
        return True
    if target.endswith("/"):
        return changed.startswith(target)
    return changed.startswith(target.rstrip("/") + "/")


def _risk_level(score: int) -> str:
    if score >= 85:
        return "critical"
    if score >= 65:
        return "high"
    if score >= 35:
        return "normal"
    return "low"


def _unique_case_libraries(modules: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    libraries: Dict[Any, Dict[str, Any]] = {}
    for module in modules:
        for lib in module.get("related_case_libraries") or []:
            if not isinstance(lib, dict):
                continue
            key = lib.get("library_id") or lib.get("id") or lib.get("library_name")
            if key is not None and key not in libraries:
                libraries[key] = dict(lib)
    return sorted(libraries.values(), key=lambda lib: str(lib.get("library_id") or lib.get("id") or lib.get("library_name") or ""))


def _module_result(module: Dict[str, Any], impact_type: str, reason: str, score: int) -> Dict[str, Any]:
    return {
        "module_id": module.get("id"),
        "module_name": module.get("name"),
        "module_path": module.get("path"),
        "risk_level": module.get("risk_level") or "normal",
        "impact_type": impact_type,
        "reason": reason,
        "score": score,
        "test_focus": module.get("test_focus"),
        "related_case_libraries": module.get("related_case_libraries") or [],
    }


def analyze_changed_files(
    changed_files: Iterable[str],
    *,
    modules: Iterable[Dict[str, Any]],
    code_entities: Optional[Iterable[Dict[str, Any]]] = None,
    module_code_links: Optional[Iterable[Dict[str, Any]]] = None,
    relations: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    files = [_norm_path(f) for f in changed_files or [] if _norm_path(f)]
    module_list = [dict(m) for m in modules or []]
    entity_list = [dict(e) for e in code_entities or []]
    link_list = [dict(l) for l in module_code_links or []]
    relation_list = [dict(r) for r in relations or []]

    modules_by_id = {m.get("id"): m for m in module_list}
    entity_ids_by_file = set()
    direct_module_ids = set()
    reasons: Dict[Any, str] = {}

    for entity in entity_list:
        file_path = entity.get("file_path")
        if any(_path_matches(changed, file_path) for changed in files):
            entity_ids_by_file.add(entity.get("id"))

    for link in link_list:
        if link.get("entity_id") in entity_ids_by_file and link.get("module_id") in modules_by_id:
            module_id = link.get("module_id")
            direct_module_ids.add(module_id)
            reasons[module_id] = "matched_code_entity"

    for module in module_list:
        for pattern in module.get("code_paths") or []:
            if any(_path_matches(changed, pattern) for changed in files):
                direct_module_ids.add(module.get("id"))
                reasons[module.get("id")] = "matched_module_code_path"

    related_module_ids = set()
    relation_reasons: Dict[Any, str] = {}
    affected_relations = []
    for relation in relation_list:
        source_id = relation.get("source_module_id")
        target_id = relation.get("target_module_id")
        if source_id in direct_module_ids and target_id in modules_by_id:
            related_module_ids.add(target_id)
            relation_reasons[target_id] = f"relation:{relation.get('relation_type') or 'depends_on'}"
            affected_relations.append(dict(relation))
        if target_id in direct_module_ids and source_id in modules_by_id:
            related_module_ids.add(source_id)
            relation_reasons[source_id] = f"reverse_relation:{relation.get('relation_type') or 'depends_on'}"
            affected_relations.append(dict(relation))

    affected = []
    for module_id in direct_module_ids:
        module = modules_by_id.get(module_id)
        if not module:
            continue
        base = RISK_WEIGHT.get(module.get("risk_level"), 40)
        affected.append(_module_result(module, "direct", reasons.get(module_id, "matched_file"), min(100, base + 10)))

    for module_id in related_module_ids - direct_module_ids:
        module = modules_by_id.get(module_id)
        if not module:
            continue
        base = RISK_WEIGHT.get(module.get("risk_level"), 40)
        affected.append(_module_result(module, "related", relation_reasons.get(module_id, "related_module"), min(100, base)))

    affected.sort(key=lambda item: (item["score"], item["module_name"] or ""), reverse=True)
    affected_modules = [modules_by_id[item["module_id"]] for item in affected if item["module_id"] in modules_by_id]
    max_score = max([item["score"] for item in affected], default=0)
    selected_files = set(files)
    for entity in entity_list:
        if entity.get("id") in entity_ids_by_file:
            selected_files.add(_norm_path(entity.get("file_path")))
    total_known_files = len({_norm_path(e.get("file_path")) for e in entity_list if e.get("file_path")})
    full_context_files = max(total_known_files, len(files), len(selected_files), 1)
    selected_context_files = max(len(selected_files), 1 if affected else 0)

    return {
        "affected_modules": affected,
        "affected_relations": affected_relations,
        "recommended_case_libraries": _unique_case_libraries(affected_modules),
        "risk_score": max_score,
        "risk_level": _risk_level(max_score),
        "test_context": {
            "changed_files": files,
            "matched_entity_ids": sorted([eid for eid in entity_ids_by_file if eid is not None]),
            "test_focus": [
                {"module_id": item["module_id"], "test_focus": item.get("test_focus")}
                for item in affected
                if item.get("test_focus")
            ],
        },
        "token_savings": {
            "full_context_files": full_context_files,
            "selected_context_files": selected_context_files,
            "saved_ratio": round(max(0, full_context_files - selected_context_files) / full_context_files, 4),
        },
        "summary": f"命中 {len(direct_module_ids)} 个直接模块，扩散影响 {len(related_module_ids - direct_module_ids)} 个模块",
    }
