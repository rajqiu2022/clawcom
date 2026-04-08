# 知识库管理 (knowledge-manager)

## 简介

本 Skill 用于管理 OpenClaw 的知识经验存储体系，覆盖三层架构：

- **第 1 层 — 内部知识库（KnowledgeEntry）**：MySQL 结构化存储，带审核流程（draft → pending_review → approved）
- **第 2 层 — Memos 经验沉淀**：外部 Memos 服务，Markdown + 标签体系，7 类知识分类
- **第 3 层 — 日报知识提取**：从日报中 LLM 自动提取知识点并沉淀到 Memos

**触发词**：知识库、经验沉淀、知识搜索、Memos、知识审核、知识共享、沉淀知识

---

## 前置条件

| 变量 | 说明 |
|------|------|
| `HUB_URL` | Hub 服务地址 |
| `HUB_API_TOKEN` | API Token（认证用） |
| `CLAW_ID` | 本 OpenClaw 的 ID |

---

## 一、内部知识库 API（KnowledgeEntry）

### 1. 查询知识列表

```
GET /api/v1/knowledge

查询参数（均可选）：
  scope=global           范围：global / project / module
  category=method        分类标签
  project=QQ飞车         项目名
  module=核心单局         模块名
  status=approved        状态：draft / pending_review / approved / rejected
  source_type=openclaw   来源：openclaw / openspace / manual
  search=关键词           全文搜索（标题 + 内容）

返回：知识条目数组（最多 200 条）
```

### 2. 创建知识

```
POST /api/v1/knowledge

请求体：
{
  "title": "核心单局测试方法总结",          // 必填
  "content": "## 测试策略\n\n...",           // 必填，Markdown 格式
  "category": "method",                     // 分类标签
  "scope": "project",                       // global / project / module
  "project_name": "QQ飞车",                 // scope=project 时填
  "module_name": "核心单局",                 // scope=module 时填
  "source_openclaw_id": 4,                  // 来源 OpenClaw ID
  "source_type": "openclaw",                // openclaw / openspace / manual
  "status": "draft"                         // 初始状态，默认 draft
}

返回：创建的知识条目（201）
```

### 3. 更新知识

```
PUT /api/v1/knowledge/{id}

请求体（部分更新）：
{
  "title": "更新后标题",
  "content": "更新后内容",
  "status": "pending_review"
}
```

### 4. 删除知识

```
DELETE /api/v1/knowledge/{id}
```

### 5. 批量导入

```
POST /api/v1/knowledge/batch-import

请求体：
{
  "source_type": "openclaw",
  "entries": [
    { "title": "标题1", "content": "内容1", "category": "pitfall", "scope": "global" },
    { "title": "标题2", "content": "内容2", "category": "method", "scope": "project", "project_name": "QQ飞车" }
  ]
}

返回：{ "message": "导入 N 条知识（待审核）", "created": N }
```

### 6. 待审核列表

```
GET /api/v1/knowledge/pending

返回：所有 status=pending_review 的知识条目
```

### 7. 审核知识

```
POST /api/v1/knowledge/{id}/review

请求体：
{
  "action": "approve",          // approve 或 reject
  "notes": "审核通过，内容完善",  // 审核意见
  "reviewer": "龙虾王"
}
```

### 8. 共享/分发知识

```
POST /api/v1/knowledge/{id}/distribute

前提：知识状态必须是 approved

请求体：
{
  "target_scope": "all",           // all / project / module
  "target_project": "QQ飞车",      // target_scope=project 时填
  "target_module": "核心单局",      // target_scope=module 时填
  "distributed_by": "龙虾王"
}
```

---

## 二、Memos 经验沉淀 API

### 知识标签体系

| 标签 key | 中文名 | 说明 |
|----------|--------|------|
| `method` | 测试方法 | 新发现的测试策略或方法论 |
| `bug-standard` | Bug 标准 | Bug 判定标准、严重度定义 |
| `bug-pattern` | Bug 模式 | 高频 Bug 类型、复现规律 |
| `perf-baseline` | 性能基线 | 性能指标、阈值数据 |
| `pitfall` | 踩坑记录 | 容易犯的错、注意事项 |
| `workflow` | 流程规范 | 团队流程、规范文档 |
| `best-practice` | 最佳实践 | 值得推广的做法 |

Memo 中使用 `#openclaw/{tag}/{scope}` 格式标签，如 `#openclaw/method/core_gameplay`。

### 1. 获取标签列表

```
GET /api/v1/memos/tags

返回：[{ "key": "method", "label": "测试方法" }, ...]
```

### 2. 测试 Memos 连接

```
GET /api/v1/memos/test

返回：{ "status": "ok", "message": "Memos 连接正常" }
```

### 3. 搜索知识

```
GET /api/v1/memos/search

查询参数：
  tag=openclaw/method      按标签过滤
  keyword=性能              按关键词过滤
  limit=20                  返回条数（默认 20）

返回：{ "memos": [...], "count": N }
每条 memo 附加 knowledge_tags（中文标签名 + scope）和 title（从 # 标题行提取）
```

### 4. 列出所有 openclaw 知识

```
GET /api/v1/memos/knowledge

查询参数：
  tag=openclaw              默认查所有 openclaw 知识
  limit=50

返回包含所有 7 类标签的知识列表
```

### 5. 手动触发经验沉淀（LLM 提取）

```
POST /api/v1/memos/deposit

请求体：
{
  "claw_id": 4,
  "content": "今天测试了核心单局的匹配逻辑，发现...",
  "module": "核心单局",
  "project": "QQ飞车"
}

返回：
{
  "message": "提取并沉淀了 3 条知识",
  "deposited": [
    { "tag": "method", "tag_label": "测试方法", "title": "匹配逻辑测试策略", "action": "created", "memo_name": "memos/xxx" },
    { "tag": "pitfall", "tag_label": "踩坑记录", "title": "匹配超时边界问题", "action": "updated", "memo_name": "memos/yyy" }
  ]
}
```

**LLM 自动提取的 6 类知识**：测试方法、Bug标准、Bug模式、性能基线、踩坑记录、最佳实践。

### 6. 直接写入/更新知识 Memo

```
POST /api/v1/memos/upsert

请求体：
{
  "tag": "method",                    // 必填，7 类标签之一
  "scope_key": "core_gameplay",       // 范围标识（模块名/项目名）
  "title": "核心单局测试方法",          // 标题
  "content": "## 测试策略\n\n..."      // 必填，Markdown
}

逻辑：按 #openclaw/{tag}/{scope_key} 去重
  - 已存在 → 合并更新（保留历史记录）
  - 不存在 → 创建新 Memo
```

---

## 三、推荐工作流

### 日常知识沉淀

```
1. 完成测试工作
2. 提交日报 → 后端自动调用 LLM 提取知识 → 写入 Memos
3. 可手动 POST /memos/deposit 触发额外提取
4. 重要知识 → POST /knowledge 写入内部知识库
5. 提交审核 → PUT status=pending_review
6. 管理员审核 → POST /knowledge/{id}/review approve
7. 共享 → POST /knowledge/{id}/distribute
```

### 搜索和引用

```
1. GET /memos/search?keyword=性能&tag=openclaw/perf-baseline → 找到性能基线
2. GET /knowledge?scope=project&project=QQ飞车&status=approved → 找到项目级已审核知识
3. 引用知识写入测试报告或用例
```

---

## 四、触发词速查

| 触发词 | 对应操作 |
|--------|----------|
| 搜索知识 / 查找经验 | GET /memos/search 或 GET /knowledge |
| 沉淀知识 / 记录经验 | POST /memos/deposit 或 POST /memos/upsert |
| 创建知识 / 新增知识 | POST /knowledge |
| 审核知识 / 待审核 | GET /knowledge/pending + POST review |
| 共享知识 / 分发 | POST /knowledge/{id}/distribute |
| 知识标签 / 标签列表 | GET /memos/tags |
| 测试 Memos | GET /memos/test |
