# Knowledge Favorites Sharing Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识库增加用户/Agent 收藏、匿名分享链接及单条 Markdown 下载。

**Architecture:** 收藏复用现有 `KnowledgeFavorite`，分享状态保存在 `KnowledgeEntry`。权限、公开 payload 和 Markdown 生成放入纯函数 service，以 TDD 锁住边界；Flask API 负责认证、查询和持久化；登录页与匿名页分别调用受控 API。

**Tech Stack:** Flask、SQLAlchemy、Jinja2、原生 JavaScript、pytest/unittest、MariaDB。

## Global Constraints

- 匿名分享支持 Markdown 下载。
- 创建者可分享自己能编辑的任意审核状态知识。
- 匿名响应不暴露内部 ID、Memos ID、审核意见和内部 Agent ID。
- 不修改测试报告现有分享行为。

---

### Task 1: 纯函数契约与数据模型

**Files:**
- Create: `web/app/services/knowledge_sharing.py`
- Modify: `web/app/models.py`
- Create: `tests/test_knowledge_sharing.py`

**Interfaces:**
- `knowledge_favorite_owner(user, claw) -> dict`
- `can_manage_knowledge_share(entry, user, claw, owned_claw_ids) -> bool`
- `public_knowledge_payload(entry) -> dict`
- `knowledge_markdown(entry) -> str`
- `knowledge_markdown_filename(title) -> str`

- [ ] 写权限、脱敏、文件名和 Markdown 失败测试。
- [ ] 运行 `python -m pytest tests/test_knowledge_sharing.py -q`，确认因实现缺失失败。
- [ ] 实现最小 service 和 `KnowledgeEntry` 分享字段/token 方法。
- [ ] 运行测试确认通过。

### Task 2: 收藏、分享和导出 API

**Files:**
- Modify: `web/app/api/knowledge.py`
- Modify: `web/app/api/__init__.py`
- Create: `tests/test_knowledge_features_api.py`

**Interfaces:**
- `POST/DELETE /knowledge/{id}/favorite`
- `POST/DELETE /knowledge/{id}/share`
- `GET /knowledge/shared/{token}`
- `GET /knowledge/{id}/export.md`
- `GET /knowledge/shared/{token}/export.md`

- [ ] 写 API 失败测试，覆盖认证、权限、刷新撤销、匿名脱敏和下载。
- [ ] 运行测试确认失败原因是路由/字段缺失。
- [ ] 实现 API、列表 `favorite=1` 和 `is_favorite`。
- [ ] 删除知识前清理 `KnowledgeFavorite`。
- [ ] 运行 API 测试确认通过。

### Task 3: 页面交互

**Files:**
- Modify: `web/static/js/api.js`
- Modify: `web/templates/knowledge.html`
- Modify: `web/app/views/__init__.py`
- Create: `web/templates/knowledge_share.html`
- Create: `tests/test_knowledge_frontend_contract.py`

**Interfaces:**
- `/k/{token}`
- `/knowledge/share/{token}`

- [ ] 写静态契约测试，断言收藏、分享、导出控件和公开页 API。
- [ ] 实现卡片星标、仅收藏筛选、详情分享区和导出按钮。
- [ ] 实现匿名只读页及 Markdown 下载按钮。
- [ ] 运行静态测试及 JavaScript 语法检查。

### Task 4: 迁移、回归与经验记录

**Files:**
- Modify: `web/app/__init__.py`
- Create: `ops/migrations/20260803_knowledge_share.sql`
- Modify: `docs/经验记录.md`

- [ ] 增加幂等列迁移和唯一索引。
- [ ] 运行知识库、测试报告和项目权限回归。
- [ ] 检查 lint、Python 编译及模板脚本语法。
- [ ] 记录收藏归属、匿名 payload 白名单和文件名兼容经验。
