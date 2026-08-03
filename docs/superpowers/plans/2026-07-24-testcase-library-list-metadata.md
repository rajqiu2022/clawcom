# 用例库列表元信息精简 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从左侧用例库树移除作者、评审状态和共享状态展示，同时保留详情信息和全部操作能力。

**Architecture:** 变更仅限前端模板树渲染。新增静态契约测试锁定列表的隐藏项与保留项，不修改接口、数据库和权限逻辑。

**Tech Stack:** Flask/Jinja HTML 模板、原生 JavaScript、pytest

## Global Constraints

- 仅移除列表展示，不删除后端字段或业务函数。
- 详情弹窗继续显示作者、评审和共享信息。
- 信息、数量、新增目录、共享和评审操作按钮必须保留。
- 未经用户明确要求，不创建 Git commit。

---

### Task 1: 精简用例库树元信息

**Files:**
- Create: `tests/test_testcase_tree_metadata.py`
- Modify: `web/templates/testcases.html:956-1010`
- Modify: `docs/经验记录.md`

**Interfaces:**
- Consumes: `renderTree()` 使用现有 `allLibraries`、`calcNodeInfo()` 和操作弹窗函数。
- Produces: 不包含作者、评审状态、共享状态徽标，但保留操作入口的用例库树 HTML。

- [ ] **Step 1: 编写失败的静态契约测试**

```python
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
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run: `python -m pytest tests/test_testcase_tree_metadata.py -v`

Expected: `test_tree_hides_library_metadata_badges` FAIL，指出当前 `renderTree()` 仍包含评审、共享或作者徽标。

- [ ] **Step 3: 实施最小前端修改**

在 `renderTree()` 中：

```javascript
const libInfo = calcNodeInfo(lib, null);
```

保留 `libInfo` 供目录继承信息计算使用，但删除：

```javascript
const reviewBadge = libReviewBadgeHtml(lib);
const sharedBadge = libSharedBadgeHtml(lib);
const libCreatorBadge = /* ... */;
```

并从用例库行模板删除：

```javascript
${reviewBadge}${sharedBadge}
${libCreatorBadge}
```

在 `renderDir()` 中删除 `showCreator`、`creatorBadge` 的构造及目录行中的 `${creatorBadge}`。保留 `calcNodeInfo()` 与 `inheritedCreator` 参数，避免改变目录信息继承和递归行为。

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `python -m pytest tests/test_testcase_tree_metadata.py -v`

Expected: `2 passed`。

- [ ] **Step 5: 运行相关回归测试**

Run: `python -m pytest tests/test_testcase_panorama_links.py tests/test_project_access.py -v`

Expected: 全部通过。

- [ ] **Step 6: 检查模板诊断**

使用 IDE lint 检查 `web/templates/testcases.html` 和新增测试，确认没有新增错误。

- [ ] **Step 7: 记录经验**

在 `docs/经验记录.md` 顶部新增简短记录：列表视觉精简应只移除渲染节点，不删除 API 字段、详情信息和操作函数；用静态契约测试同时校验“隐藏项”和“保留项”，防止误伤共享/评审能力。
