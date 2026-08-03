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
