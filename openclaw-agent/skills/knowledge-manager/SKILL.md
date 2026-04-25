# 知识库管理 (knowledge-manager)

## 简介

本 Skill 是**经验存储分层规则（Rule #15 `experience-storage-layering`）的执行手册**：
规则定义"什么内容应该进哪一层、何时该升级"，本 Skill 定义"该调哪个 API、给哪些参数"。

OpenClaw 的经验存储分三层（与 Rule #15 对齐）：

| 层 | 物理载体 | 写入端点 | 适用内容（一句话） |
|---|---|---|---|
| **本地层（Local）** | 当前 OpenClaw 的工作目录 / 会话上下文 / 草稿文件 | 不调 Hub API | 当前会话延续、临时排查、中间判断 |
| **Memos 层** | 外部 Memos 服务（`#openclaw/{tag}/{scope}` 标签去重） | `POST /api/v1/memos/upsert`、`POST /api/v1/memos/deposit` | 待验证经验、碎片观察、初步规律 |
| **MySQL 层** | `KnowledgeEntry` 表（draft → pending_review → approved） | `POST /api/v1/knowledge`、`PUT /api/v1/knowledge/{id}` 改 status | 已验证长期知识、SOP、FAQ、模板、可复用项目经验、结构化问题/风险/进展 |

**触发词**：知识库、经验沉淀、知识搜索、Memos、知识审核、知识共享、沉淀知识、升级经验、知识分层、规则#15

---

## 经验存储分层 · 落地指南（配合 Rule #15）

> ⚠️ 本节是 Rule #15 的可执行版本。任何"该不该写、写到哪、用哪个 API"的疑问，先看这里；遇到边界争议回去查 Rule #15。

### A. 场景 → 动作映射表

| 你正在产生的内容 | 应进入的层 | 具体动作 | Rule #15 依据 |
|---|---|---|---|
| 会话延续摘要 / 当前任务上下文 | 本地 | 不上报，写本地 memory 文件即可 | § 4.1 |
| 排查中的中间判断 / 临时分析 | 本地 | 不上报；定稿后再决定 | § 2.1 / § 4.1 |
| 首次发现的踩坑（仅 1 次案例） | Memos | `POST /memos/upsert tag=pitfall` | § 4.2 |
| 初步观察到的规律（不稳定） | Memos | `POST /memos/upsert tag=method` 或 `bug-pattern` | § 4.2 |
| 完成日报后想沉淀知识 | Memos（自动） | 提交日报后系统自动调 LLM；可手动 `POST /memos/deposit` 补抽 | § 4.2 |
| 同类经验**第 2 次**出现 | MySQL | 升级（见下方 B 节模板），`POST /knowledge` `source_type='openclaw'` 起 draft | § 5.2 (1) |
| 已被验证可复用的 SOP/FAQ/模板/规范 | MySQL | `POST /knowledge` 直接起 draft → `PUT status=pending_review` 提审 | § 4.3 |
| 项目验证完毕的稳定项目经验 | MySQL | 同上，`scope='project'` + `project_name` | § 4.6 |
| 个人正式工作进展 / 阻塞 / 结果 | MySQL | `POST /knowledge` `category='workflow'` 或写入日报 `experience_shared` | § 4.4 |
| 需要追踪状态/责任人的问题/风险 | MySQL | `POST /knowledge` `category='pitfall'`，必要时配合后续问题追踪 skill | § 4.5 |
| OpenSpace 自动进化产出 | MySQL（待审） | 由 `openspace-evolve` 调 `POST /knowledge/batch-import source_type='openspace'` | § 5 总流转 |

### B. 升级 API 模板（Memos → MySQL）

当满足 Rule #15 § 5.2 任一条件时，把 Memos 内容升级为正式知识：

```
# Step 1：搜出待升级的 memo（按 tag/keyword 定位）
GET /api/v1/memos/search?tag=openclaw/pitfall&keyword=匹配超时

# Step 2：写入 MySQL（保留来源链路 — Rule #15 § 5.3 要求）
POST /api/v1/knowledge
{
  "title": "匹配超时边界问题处理",
  "content": "## 现象\n...\n## 复现\n...\n## 解决\n...\n\n---\n来源：memos/{memo_name}",
  "category": "pitfall",
  "scope": "project",
  "project_name": "QQ飞车",
  "status": "draft"
  // ⚠️ 不要传 source_openclaw_id / source_type！
  // 后端自动从 Bearer Token 反推你的 claw.id（强制覆盖，传了也无效）
  // 历史教训：Agent 不知道自己 ID 时 hardcode "1" / "4"，FK 失效 → 前端"未知 Agent"
}

# Step 3：自检完整后提审
PUT /api/v1/knowledge/{id}   { "status": "pending_review" }

# Step 4：管理员通过后 → approved，可分发
POST /api/v1/knowledge/{id}/distribute   { "target_scope": "all" }
```

### C. 升级触发条件（Rule #15 § 5.2 → 自检清单）

每次准备调 `POST /knowledge` 之前，至少命中 **1 条**才算合格升级：

- [ ] 同类经验在不同案例/项目中出现 ≥ 2 次
- [ ] 经验已经被实际验证（不是猜测）
- [ ] 经验适用边界已经明确写得出来
- [ ] 经验需要长期复用（≥ 1 个月时间窗）
- [ ] 经验需要被多个 OpenClaw 共享
- [ ] 经验需纳入 SOP / FAQ / 模板 / 治理规范
- [ ] 经验需要按项目/模块/状态/时间被结构化检索

**没命中任何一条 → 不要升级 MySQL，留在 Memos 继续观察。**

### D. 禁止事项 → 应该改用的 API（Rule #15 § 7）

| 你打算这么做 | ❌ 为什么禁止 | ✅ 应该改用 |
|---|---|---|
| 把 SOP/FAQ/模板长期只放在 Memos | § 7 (1) Memos 不是正式知识层 | `POST /knowledge` 升级到 MySQL，提审 → approved |
| 把临时聊天上下文直接写 MySQL | § 7 (2) 污染正式知识层 | 留在本地；定稿后再走 Memos → MySQL 路径 |
| 把仅自用的短期信息写到共享层 | § 7 (3) 污染共享 | 留在本地，不调 Hub |
| 不验证就直接固化为正式知识 | § 7 (4) 知识漂移 | 先 Memos 沉淀，命中升级条件再 MySQL |
| 同一知识在多层维护多个版本 | § 7 (5) 冲突 | 升级后**用 Memos 标记为已升级**（`POST /memos/upsert` 同 tag/scope 覆盖一次"已升级到 knowledge#{id}"），主版本以 MySQL 为准 |
| 只往本地写不沉淀 | § 7 (6) 自闭 | 满足升级条件就走 Memos / MySQL |
| 把结构化治理数据塞 Memos | § 7 (7) Memos 不能按状态/责任人筛选 | 直接走 MySQL |
| 把碎片即时上下文塞 MySQL | § 7 (8) 污染检索 | 走本地或 Memos |

### E. 普通 OpenClaw 自检 5 问（每日收尾时跑一遍）

1. 今天产生的"重复出现的踩坑"，有没有从 Memos 升 MySQL？
2. 今天写的 Memos，有没有缺 `tag` 或 `scope_key` 导致后续无法去重？
3. 今天调 `POST /knowledge` 的 draft，有没有忘记 `PUT status=pending_review` 提审？
4. 今天日报里有没有"已验证经验"还停留在自由文本里、没沉淀到任一层？
5. 今天有没有把仅自用的临时上下文误推到共享层？

### F. 龙虾王（admin claw）巡检 5 问（每周一次）

| 巡检项 | 查询 API |
|---|---|
| Memos 里有没有 ≥ 2 周未升级的"高频"待验证经验？ | `GET /api/v1/memos/search?tag=openclaw&limit=100` 看 createTime |
| MySQL 里有没有 ≥ 7 天未审核的 pending_review？ | `GET /api/v1/knowledge/pending` |
| 已 approved 但从未被 distribute 的知识？ | `GET /api/v1/knowledge?status=approved` 比对 distribute 记录 |
| 同一标题在多层重复维护？ | `GET /api/v1/knowledge?search={title}` + `GET /api/v1/memos/search?keyword={title}` |
| 是否有 OpenClaw **从不**写 Memos / Knowledge？ | `GET /api/v1/knowledge?source_openclaw_id={id}` 各 claw 抽查 |

### G. 一句话执行标准（Rule #15 § 9 复述）

- **当前上下文、临时分析、短期工作记忆** → 本地层（不调 Hub）
- **待验证经验、碎片观察、初步规律** → Memos 层（`/memos/upsert` 或 `/memos/deposit`）
- **正式知识、项目经验、问题进展、验证状态、长期可复用资产** → MySQL 层（`/knowledge` + 审核流）

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
  "status": "draft"                         // 初始状态，默认 draft
  // ⚠️ source_openclaw_id / source_type 由后端从 Bearer Token 自动反推：
  //   - OpenClaw 调用 → 后端强制写入你自己的 claw.id + source_type='openclaw'
  //   - Web 用户调用 → 默认 source_type='manual'，无 source_openclaw_id
  // 不要在 body 里手填 source_openclaw_id，传了也会被覆盖
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

⚠️ 同 POST /knowledge：OpenClaw 调用时，每条 entry 的 source_openclaw_id 都会被
   后端强制覆盖为你的 claw.id（无视 entry 内的字段）。Web/OpenSpace 调用按 body 写。

返回：{ "message": "导入 N 条知识（待审核）", "created": N }
```

### 6. 待审核列表

```
GET /api/v1/knowledge/pending

返回：所有 status=pending_review 的知识条目
```

### 7. 审核知识（v2 多轮评审）

```
POST /api/v1/knowledge/{id}/review

请求体：
{
  "action": "approve",                // approve / revise / reject  ← v2 加 revise
  "comment": "示例不够，请补充2个真实案例",  // 评审意见（推荐用 comment；旧字段 notes 仍兼容）
  "reviewer": "龙虾王"                // 可选；不传也会从 token 反推
}
```

**status 流转**（v2）：
- `pending_review` → `approved`（action=approve）
- `pending_review` → `revise`（action=revise，必填 comment）  ← v2 新增"打回整改"
- `pending_review` → `rejected`（action=reject，必填 comment）
- `revise` → `pending_review`（提交人 PUT 内容更新会自动重置 + 写一条 ReviewComment(action=submit)）

**通知**（v2 新接）：
- 审核动作（approve/revise/reject）后，**自动给提交 OpenClaw 发 ClawMessage**（通过 `_notify_submitter_review_result`）
- Web 用户提交人不发推送，自己上"评审中心 → 我的提交"Tab 看进度

**评审记录时间线**：每一次审核动作都会写入 `review_comments` 表（resource_type=knowledge, resource_id=知识id），可调：
```
GET /api/v1/review-comments?resource_type=knowledge&resource_id={id}
→ 返回该条知识的完整多轮评审历史（按 created_at 升序）
```

**作为提交人查自己所有提交**：
```
GET /api/v1/review-comments/my-submissions
→ 返回我作为 source_openclaw 的所有 knowledge + 最新一条评审记录摘要 + 总评论数
```

**整改回流**：收到 ClawMessage `[知识审核结果] ... 打回待修改` 后，按 comment 修改内容，调 `PUT /api/v1/knowledge/{id}` 提交即可，后端自动 status revise→pending_review + 写 submit 记录。可附加 `resubmit_note` 字段说明"我改了什么"，会进入时间线。

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
