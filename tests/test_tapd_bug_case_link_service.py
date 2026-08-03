import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "app"
    / "services"
    / "tapd_bug_case_link.py"
)
SPEC = importlib.util.spec_from_file_location("tapd_bug_case_link", MODULE_PATH)
tapd_bug_case_link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tapd_bug_case_link)


def test_parse_tapd_bug_url_accepts_bugtrace_link():
    parsed = tapd_bug_case_link.parse_tapd_bug_url(
        "https://www.tapd.cn/70202650/bugtrace/bugs/view/1170202650001000001"
    )

    assert parsed["workspace_id"] == "70202650"
    assert parsed["bug_id"] == "1170202650001000001"
    assert parsed["url"] == (
        "https://www.tapd.cn/70202650/bugtrace/bugs/view/1170202650001000001"
    )


def test_parse_tapd_bug_url_accepts_internal_tapd_fe_link():
    parsed = tapd_bug_case_link.parse_tapd_bug_url(
        "https://tapd.woa.com/tapd_fe/70202650/bug/detail/1170202650001000001"
        "?hidden_left_side=true"
    )

    assert parsed["workspace_id"] == "70202650"
    assert parsed["bug_id"] == "1170202650001000001"


def test_parse_tapd_bug_url_extracts_link_from_title_and_link():
    parsed = tapd_bug_case_link.parse_tapd_bug_url(
        "【车辆技能】局内触发异常 "
        "https://tapd.woa.com/tapd_fe/70202650/bug/detail/1170202650001000001"
    )

    assert parsed["workspace_id"] == "70202650"
    assert parsed["bug_id"] == "1170202650001000001"
    assert parsed["url"] == (
        "https://tapd.woa.com/tapd_fe/70202650/bug/detail/1170202650001000001"
    )


def test_parse_tapd_bug_url_extracts_longest_id_from_title_and_id():
    parsed = tapd_bug_case_link.parse_tapd_bug_url(
        "20260727 车辆技能触发失败 1170202650001000001"
    )

    assert parsed["workspace_id"] == ""
    assert parsed["bug_id"] == "1170202650001000001"
    assert parsed["url"] == ""


def test_build_tapd_bug_url_uses_internal_detail_page():
    assert tapd_bug_case_link.build_tapd_bug_url(
        "70202650", "1170202650001000001"
    ) == (
        "https://tapd.woa.com/tapd_fe/70202650/bug/detail/1170202650001000001"
    )


def test_resolve_case_info_field_accepts_cached_field_map():
    field = tapd_bug_case_link.resolve_case_info_field(
        {"custom_field_42": "用例信息"}
    )

    assert field == "custom_field_42"


def test_extract_field_map_supports_custom_field_config_response():
    mapping = tapd_bug_case_link._extract_field_map_from_settings([
        {"CustomFieldConfig": {
            "custom_field": "custom_field_88",
            "name": "用例信息",
        }}
    ])

    assert mapping == {"custom_field_88": "用例信息"}
    assert tapd_bug_case_link.resolve_case_info_field(mapping) == "custom_field_88"


def test_build_case_info_snapshot_contains_stable_path_and_case_identity():
    library = SimpleNamespace(id=29, name="RacingGO M2 3C车辆被动技能用例库")
    case = SimpleNamespace(
        id=501,
        case_id="TC_010",
        title="01 基础配置与车库装配",
        module_path="3C/车辆被动技能通用规则（局内事件触发）",
        library_id=29,
        library=library,
    )
    task_case = SimpleNamespace(id=77, case_id=501, case=case)
    task = SimpleNamespace(id=88, name="M2 冒烟", library_id=29, library=library)
    plan = SimpleNamespace(id=99, name="M2 计划", project=SimpleNamespace(name="RacingGO"))

    snapshot = tapd_bug_case_link.build_case_info_snapshot(task_case, task, plan)

    assert snapshot["schema_version"] == "hub.test_case_link/v1"
    assert snapshot["library_id"] == 29
    assert snapshot["case_pk"] == 501
    assert snapshot["case_id"] == "TC_010"
    assert snapshot["module_path_parts"] == ["3C", "车辆被动技能通用规则（局内事件触发）"]
    assert snapshot["display_path"] == (
        "用例库29，RacingGO M2 3C车辆被动技能用例库 › 3C › "
        "车辆被动技能通用规则（局内事件触发） › 01 基础配置与车库装配"
    )


def test_merge_case_info_json_upserts_by_library_and_case_pk_and_keeps_legacy():
    old_value = "人工填写的旧内容"
    first = {
        "library_id": 29,
        "case_pk": 501,
        "case_id": "TC_010",
        "display_path": "old",
    }
    second = {
        "library_id": 29,
        "case_pk": 501,
        "case_id": "TC_010",
        "display_path": "new",
    }
    other = {
        "library_id": 30,
        "case_pk": 777,
        "case_id": "TC_011",
        "display_path": "other",
    }

    merged = tapd_bug_case_link.merge_case_info_value(
        tapd_bug_case_link.merge_case_info_value(old_value, first),
        other,
    )
    merged = tapd_bug_case_link.merge_case_info_value(merged, second)
    parsed = json.loads(merged)

    assert parsed["schema"] == "hub.test_case_links/v1"
    assert parsed["legacy_text"] == old_value
    assert len(parsed["cases"]) == 2
    assert parsed["cases"][0]["display_path"] == "new"
    assert parsed["cases"][1]["display_path"] == "other"

