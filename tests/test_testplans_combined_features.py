from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "web" / "templates" / "testplans.html"
)


def test_iteration_tabs_are_present():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        "loadIterationTabs",
        "renderIterTabBar",
        "builtInIterationTabs",
        "flow_progress",
        "/test-iterations/${iterId}/tabs",
    ):
        assert marker in source


def test_task_case_folder_tree_is_present():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        "buildCaseTree",
        "renderCaseTree",
        "folderStatsBadges",
        "tp-folder-head",
        "taskFolderState",
    ):
        assert marker in source


def test_iteration_modal_keeps_save_actions_clear_of_tapd_dropdown():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        "modal tp-iteration-modal",
        "tp-iteration-modal-actions",
        "position: sticky",
        "bottom: calc(100% + 6px)",
        ".tp-multi-panel[hidden]",
    ):
        assert marker in source


def test_shift_left_tab_is_feature_gated_and_api_driven():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        "SHIFT_LEFT_ENABLED",
        "tab_key: 'code_analysis'",
        "renderCodeAnalysisTab",
        "/shift-left/findings?",
        "addShiftLeftComment",
        "transitionShiftLeftFinding",
    ):
        assert marker in source


def test_task_modal_has_fullscreen_mindmap_selection_and_library_sync():
    source = TEMPLATE.read_text(encoding="utf-8")
    for marker in (
        "openTaskMindmapPicker",
        "tp-mindmap-overlay",
        "CaseMindmap.render",
        "selectable: true",
        "同步最新用例库",
        "sync-library-preview",
        "sync-library-restore",
        "还原同步前数据",
        "taskCasePickerSummary",
        "覆盖用例总数",
        "选中节点",
        "compactTaskModulePaths",
        "select_all",
        "覆盖用例总数 <strong>0</strong>",
    ):
        assert marker in source


def test_shared_mindmap_renderer_supports_checkbox_selection():
    source = (
        TEMPLATE.parents[1] / "static" / "js" / "case_mindmap.js"
    ).read_text(encoding="utf-8")
    for marker in (
        "options.selectable",
        "data-select=\"1\"",
        "onSelectionChange",
        "cmm-check",
    ):
        assert marker in source
