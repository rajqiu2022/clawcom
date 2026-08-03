"""TAPD Bug case-link synchronization helpers."""

import json
import re
from datetime import datetime


CASE_INFO_SCHEMA = "hub.test_case_links/v1"
CASE_LINK_SCHEMA_VERSION = "hub.test_case_link/v1"
CASE_INFO_LABELS = ("用例信息", "关联用例", "测试用例信息")


def parse_tapd_bug_url(value):
    """Parse a TAPD Bug link or Bug ID embedded in user input."""
    raw = (value or "").strip()
    if not raw:
        return None

    patterns = (
        r"https?://(?:www\.)?tapd\.(?:cn|woa\.com)/(\d+)/bugtrace/bugs/view/(\d+)",
        r"https?://tapd\.woa\.com/tapd_fe/(\d+)/bug/detail/(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return {
                "workspace_id": match.group(1),
                "bug_id": match.group(2),
                "url": match.group(0),
            }

    id_candidates = re.findall(r"(?<!\d)(\d{6,})(?!\d)", raw)
    if id_candidates:
        bug_id = max(enumerate(id_candidates), key=lambda item: (len(item[1]), item[0]))[1]
        return {"workspace_id": "", "bug_id": bug_id, "url": ""}

    raise ValueError("请输入有效的 TAPD Bug 链接或 ID")


def build_tapd_bug_url(workspace_id, bug_id):
    """Build the canonical internal TAPD Bug detail URL."""
    return (
        "https://tapd.woa.com/tapd_fe/"
        f"{str(workspace_id).strip()}/bug/detail/{str(bug_id).strip()}"
    )


def resolve_case_info_field(field_map):
    """Return the TAPD custom field key labeled as case information."""
    mapping = field_map or {}
    for key, label in mapping.items():
        if key.startswith("custom_field_") and str(label).strip() in CASE_INFO_LABELS:
            return key
    for label, key in mapping.items():
        if str(label).strip() in CASE_INFO_LABELS and str(key).startswith("custom_field_"):
            return str(key)
    return None


def _split_module_path(module_path):
    return [part for part in (module_path or "").split("/") if part]


def build_case_info_snapshot(task_case, task, plan):
    """Build a stable JSON-friendly snapshot for one task case."""
    case = getattr(task_case, "case", None)
    library = getattr(case, "library", None) or getattr(task, "library", None)
    library_id = getattr(library, "id", None) or getattr(case, "library_id", None)
    library_name = getattr(library, "name", "") or ""
    title = getattr(case, "title", "") or ""
    module_parts = _split_module_path(getattr(case, "module_path", ""))

    path_parts = []
    if library_id or library_name:
        path_parts.append(f"用例库{library_id}，{library_name}")
    path_parts.extend(module_parts)
    if title:
        path_parts.append(title)

    project = getattr(plan, "project", None)
    return {
        "schema_version": CASE_LINK_SCHEMA_VERSION,
        "project_name": getattr(project, "name", "") or "",
        "plan_id": getattr(plan, "id", None),
        "plan_name": getattr(plan, "name", "") or "",
        "task_id": getattr(task, "id", None),
        "task_name": getattr(task, "name", "") or "",
        "task_case_id": getattr(task_case, "id", None),
        "library_id": library_id,
        "library_name": library_name,
        "module_path": getattr(case, "module_path", "") or "",
        "module_path_parts": module_parts,
        "case_pk": getattr(case, "id", None),
        "case_id": getattr(case, "case_id", "") or "",
        "case_title": title,
        "display_path": " › ".join(str(part) for part in path_parts if part),
    }


def _case_key(case_info):
    return (
        str(case_info.get("library_id") or ""),
        str(case_info.get("case_pk") or ""),
    )


def merge_case_info_value(existing_value, case_info):
    """Merge one case snapshot into TAPD custom field JSON."""
    legacy_text = ""
    cases = []
    if existing_value:
        try:
            parsed = json.loads(existing_value)
        except (TypeError, ValueError):
            legacy_text = str(existing_value)
        else:
            if isinstance(parsed, dict) and parsed.get("schema") == CASE_INFO_SCHEMA:
                legacy_text = parsed.get("legacy_text") or ""
                cases = list(parsed.get("cases") or [])
            elif isinstance(parsed, dict) and parsed.get("cases"):
                cases = list(parsed.get("cases") or [])
            else:
                legacy_text = str(existing_value)

    next_cases = []
    incoming_key = _case_key(case_info)
    replaced = False
    for item in cases:
        if _case_key(item) == incoming_key:
            next_cases.append(case_info)
            replaced = True
        else:
            next_cases.append(item)
    if not replaced:
        next_cases.append(case_info)

    payload = {
        "schema": CASE_INFO_SCHEMA,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "cases": next_cases,
    }
    if legacy_text:
        payload["legacy_text"] = legacy_text
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _field_map_to_dict(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_field_map_from_settings(settings):
    mapping = {}
    if isinstance(settings, dict):
        settings = settings.get("fields") or settings.get("data") or settings.get("Field") or settings
    if isinstance(settings, dict):
        iterable = settings.items()
        for key, value in iterable:
            if key.startswith("custom_field_"):
                if isinstance(value, dict):
                    mapping[key] = value.get("label") or value.get("name") or value.get("title") or ""
                else:
                    mapping[key] = str(value)
    elif isinstance(settings, list):
        for item in settings:
            item = item.get("CustomFieldConfig", item)
            field = item.get("field") or item.get("name") or item.get("key")
            field = item.get("custom_field") or field
            label = item.get("label") or item.get("title") or item.get("desc") or item.get("name")
            if field and str(field).startswith("custom_field_"):
                mapping[str(field)] = label or ""
    return mapping


def _load_bug_field_map(workspace_id):
    from app import db
    from app.models import TapdFieldMapCache, _now
    from app.api.tapd import TAPD_API_BASE_URL, _tapd_request

    row = TapdFieldMapCache.query.filter_by(
        tapd_workspace_id=str(workspace_id),
        entity_type="bug",
    ).first()
    mapping = _field_map_to_dict(row.field_map if row else None)
    field = resolve_case_info_field(mapping)
    if field:
        return mapping, field

    settings = _tapd_request(
        "GET",
        f"{TAPD_API_BASE_URL}/bugs/custom_fields_settings",
        {"workspace_id": workspace_id},
    )
    mapping = _extract_field_map_from_settings(settings)
    field = resolve_case_info_field(mapping)
    if mapping:
        if row:
            row.field_map = mapping
            row.last_synced_at = _now()
        else:
            db.session.add(TapdFieldMapCache(
                tapd_workspace_id=str(workspace_id),
                entity_type="bug",
                field_map=mapping,
                last_synced_at=_now(),
            ))
    if not field:
        raise ValueError("未找到 TAPD Bug 字段映射：用例信息")
    return mapping, field


def _bug_custom_value(workspace_id, bug_id, field_name):
    from app.api.tapd import TAPD_API_BASE_URL, _tapd_request

    data = _tapd_request(
        "GET",
        f"{TAPD_API_BASE_URL}/bugs",
        {"workspace_id": workspace_id, "id": bug_id, "limit": 1},
    )
    if isinstance(data, dict):
        data = [data]
    for item in data or []:
        bug = item.get("Bug", item) if isinstance(item, dict) else {}
        if str(bug.get("id") or "") == str(bug_id) or bug:
            return bug.get(field_name) or ""
    return ""


def sync_task_case_bug_link(task_case, task, plan, bug_url):
    """Save a task-case to TAPD Bug custom field and return sync metadata."""
    from app.api.tapd import TAPD_API_BASE_URL, _tapd_request

    parsed = parse_tapd_bug_url(bug_url)
    if not parsed:
        return None

    workspace_id = parsed["workspace_id"] or (getattr(plan, "tapd_workspace_id", "") or "")
    expected_ws = (
        getattr(plan, "tapd_workspace_id", None)
        or (getattr(getattr(plan, "project", None), "tapd_workspace_id", None))
        or ""
    )
    if parsed["workspace_id"] and expected_ws and str(parsed["workspace_id"]) != str(expected_ws):
        raise ValueError("Bug 链接所属 TAPD 项目与测试计划项目不一致")
    if not workspace_id:
        raise ValueError("无法确定 TAPD workspace_id")
    bug_page_url = parsed["url"] or build_tapd_bug_url(workspace_id, parsed["bug_id"])

    _, field = _load_bug_field_map(workspace_id)
    snapshot = build_case_info_snapshot(task_case, task, plan)
    existing = _bug_custom_value(workspace_id, parsed["bug_id"], field)
    merged = merge_case_info_value(existing, snapshot)
    _tapd_request(
        "POST",
        f"{TAPD_API_BASE_URL}/bugs",
        data={
            "workspace_id": workspace_id,
            "id": parsed["bug_id"],
            field: merged,
        },
    )
    return {
        "bug_id": parsed["bug_id"],
        "bug_url": bug_page_url,
        "case_info_snapshot": snapshot,
        "sync_status": "synced",
        "field": field,
    }

