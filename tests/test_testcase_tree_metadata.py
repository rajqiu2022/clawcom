from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "web" / "templates" / "testcases.html"
)


def _render_tree_source() -> str:
    source = TEMPLATE.read_text(encoding="utf-8")
    return source.split("function renderTree()", 1)[1].split(
        "function toggleDir(", 1
    )[0]


def test_tree_hides_library_metadata_badges():
    source = _render_tree_source()
    assert "libReviewBadgeHtml(lib)" not in source
    assert "libSharedBadgeHtml(lib)" not in source
    assert "tc-tree-creator" not in source


def test_tree_keeps_information_and_action_entries():
    source = _render_tree_source()
    assert "openTestcaseInfoModal(" in source
    assert "openSubdirModal(" in source
    assert "openLibShareDialog(" in source
    assert "openLibReviewDialog(" in source
    assert "tc-tree-cnt" in source
