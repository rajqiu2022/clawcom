from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "web" / "templates" / "testcases.html"
)


def _mindmap_source() -> str:
    source = TEMPLATE.read_text(encoding="utf-8")
    return source.split("function renderCaseMindmap(c)", 1)[1].split(
        "// ==================== 用例 CRUD", 1
    )[0]


def test_mindmap_layout_accounts_for_wrapped_leaf_height():
    source = _mindmap_source()

    assert "estimateMindmapLeafHeight" in source
    assert "h: leafH" in source
    assert "leafY += leafH + V_GAP" in source


def test_mindmap_nodes_are_individually_draggable_and_keep_edges_connected():
    source = _mindmap_source()

    assert 'data-mm-node-id="${n.id}"' in source
    assert 'onmousedown="mmNodeDragStart(event,this)"' in source
    assert 'data-mm-from="${e.from}"' in source
    assert 'data-mm-to="${e.to}"' in source
    assert "updateMindmapEdges" in source
    assert "e.stopPropagation()" in source
