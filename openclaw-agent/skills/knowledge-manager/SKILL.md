---
name: knowledge-manager
description: 管理 Hub 正式知识、Memos 经验和项目版本测试纪要；在沉淀知识、记录需求变动/用例设计问题/质量问题、查看或回退纪要版本时使用。
---

# 知识库管理 (knowledge-manager)

## 简介

本 Skill 是**经验存储分层规则（Rule #15 `experience-storage-layering`）的执行手册**：
规则定义"什么内容应该进哪一层、何时该升级"，本 Skill 定义"该调哪个 API、给哪些参数"。

OpenClaw 的经验存储分三层（与 Rule #15 对齐）。其中 MySQL 正式知识层包含两种不同载体：

- **普通知识文章**：长期 SOP、方法、FAQ，走 draft → 审核 → approved。
- **版本测试纪要**：项目版本过程事实，项目内 owner/Agent 直接协作；每次保存生成不可变 Revision，不走普通审核流。

| 层 | 物理载体 | 写入端点 | 适用内容（一句话） |
|---|---|---|---|
| **本地层（Local）** | 当前 OpenClaw 的工作目录 / 会话上下文 / 草稿文件 | 不调 Hub API | 当前会话延续、临时排查、中间判断 |
| **Memos 层** | **外部 Memos**（`http://your-hub-host:5230`），Hub 代写；每条笔记带 `#claw-{Claw名}` 隔离空间 + `#openclaw/{tag}/{scope}`；**visibility=PROTECTED**（勿用 PRIVATE，Memos 0.24 List API 不返回 PRIVATE） | `POST /api/v1/memos/upsert`、`POST /api/v1/memos/deposit`；查自己的：`GET /api/v1/memos/search?claw_only=true`；按 uid 直读：`GET /api/v1/memos/memo/{uid}` | 每日零碎、待验证经验、碎片观察（**不会出现在 Hub 知识库列表**） |
| **MySQL 层（Hub 正式知识库）** | `KnowledgeEntry` 表（draft → pending_review → approved） | `POST /api/v1/knowledge`、`PUT /api/v1/knowledge/{id}` 改 status | 已验证长期知识、SOP、FAQ、模板、可复用项目经验、结构化问题/风险/进展 |

**触发词**：知识库、经验沉淀、知识搜索、Memos、知识审核、知识共享、沉淀知识、升级经验、知识分层、版本纪要、测试纪要、研发需求变动、AI用例设计问题、质量问题、版本对比、回退纪要、规则#15、任务上下文包、preflight

## 版本测试纪要：先判断是否应使用

以下内容应写入版本测试纪要，而不是创建普通知识文章：

- 某个版本周期内的需求调整、范围变化和策划确认；
- AI 用例设计的遗漏、偏差、不可执行条件及修订结论；
- 当前版本质量问题、风险、阻塞和处理决策；
- 测试进展、环境/自动化问题、跨角色约定和待办。

跨版本长期稳定、已验证可复用的方法，仍应沉淀为普通正式知识。不要把临时聊天全文或模型思考过程写进纪要。

### 纪要 API

所有调用使用 `{HUB_URL}/api/v1`、当前 Bearer Token 和 JSON。Agent 直接请求 API，不需要打开浏览器。

```text
GET  /knowledge-notebooks?project_id={project_id}
POST /knowledge-notebooks
GET  /knowledge-notebooks/{notebook_id}
POST /knowledge-notebooks/{notebook_id}/pages
GET  /knowledge/journal-pages/{page_id}
GET  /knowledge/{page_id}/revisions
GET  /knowledge/{page_id}/revisions/{revision_no}
POST /knowledge/{page_id}/revisions
GET  /knowledge/{page_id}/compare?from={old}&to={new}
POST /knowledge/{page_id}/rollback
```

### 创建纪要本

先从测试计划读取当前项目已有迭代，禁止手写或猜测迭代 ID：

```text
GET /api/v1/test-iterations?project_id={project_id}
```

从响应中选择 `id`，再创建纪要本；`iteration_id` 必须属于同一项目，Hub 会拒绝跨项目关联。未指定 `version_name` 时，Hub 自动使用迭代的 version_name/name。

```http
POST /api/v1/knowledge-notebooks
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json

{
  "project_id": 6,
  "title": "RacingGO M3版本测试纪要",
  "iteration_id": 12
}
```

缺省模块包括：研发需求变动、AI用例设计问题、质量问题、测试进展与决策、环境与自动化问题、风险与待办。需要自定义时在创建请求中传 `modules` 数组。

### 创建模块页面

创建页面必须携带稳定的 `Idempotency-Key`。同一逻辑请求重放时复用原键；新操作生成新键。

`module_name` 必须从 `GET /knowledge-notebooks/{notebook_id}` 返回的 `modules` 数组中选择，不允许自行编造模块名。

图片必须先上传到 Hub，再把返回 URL 写入 Markdown：

```http
POST /api/v1/upload/image
Content-Type: multipart/form-data
image=@screenshot.png
```

使用返回的相对地址：`![说明](/static/uploads/...)`。禁止写 `file://`、Windows 盘符、workspace 路径或 `http://IP:18800`；Hub 会把指向自身 `/static/uploads/` 的 HTTP(S) 绝对地址自动改为相对地址，避免 HTTPS 页面混合内容裂图。

```http
POST /api/v1/knowledge-notebooks/{notebook_id}/pages
Idempotency-Key: journal-page:<notebook_id>:<stable-key>

{
  "title": "登录态方案调整",
  "module_name": "研发需求变动",
  "content": "# 登录态方案调整\n\n...",
  "change_summary": "创建需求变动记录"
}
```

### 保存新版本

先 GET 页面取得 `current_revision`，再保存。禁止猜测版本号，也禁止用旧 `PUT /knowledge/{id}` 覆盖纪要正文。

```http
POST /api/v1/knowledge/{page_id}/revisions
Idempotency-Key: journal-revision:<page_id>:<stable-key>

{
  "expected_revision": 3,
  "title": "登录态方案调整",
  "module_name": "研发需求变动",
  "content": "# 登录态方案调整\n\n更新后的结论...",
  "change_summary": "补充策划确认后的恢复规则"
}
```

- 成功后独立 GET 页面和版本列表，确认 `current_revision` 已增加。
- HTTP 409 `KNOWLEDGE_REVISION_CONFLICT` 表示其他协作者已保存；必须回读最新版本、比较差异后再决定如何合并，不得盲目重试或覆盖。
- HTTP 409 `IDEMPOTENCY_KEY_REUSED` 表示同一键被用于不同内容；停止并生成新的逻辑操作键。

### 对比与回退

```text
GET /api/v1/knowledge/{page_id}/compare?from=2&to=5
```

返回 Markdown 行级 `diff` 和 added/removed 统计。回退前必须先对比目标版本与当前版本：

```http
POST /api/v1/knowledge/{page_id}/rollback
Idempotency-Key: journal-rollback:<page_id>:<target>:<stable-key>

{
  "expected_revision": 5,
  "target_revision": 2,
  "change_summary": "恢复到 Revision 2：撤销尚未生效的方案"
}
```

回退不会删除历史，而是创建 Revision 6。纪要不允许匿名分享或物理删除；不要调用普通知识的 share/delete API。

### Wiki 直达链接

页面稳定链接为：

```text
https://clawteam.woa.com/knowledge/wiki/{page_id}
```

打开后自动选择项目、纪要本和页面。Agent 在消息、报告或任务中引用 Wiki 时应提供该链接，不要只给纪要本名称。

---

## 任务上下文包（执行前必读的检索路径）

> 设计依据：`docs/superpowers/specs/2026-07-09-agent-memory-routing-design.md`（课题 #41 落地）。
> 核心原则：**不靠"记得去查"，Hub 在任务里把该带的经验喂到嘴边**。

领到任务（todo / agent_task / workflow_step）后，**执行前**先取上下文包：

```bash
GET /api/v1/tasks/{ref_type}/{ref_id}/context
# ref_type ∈ todo | agent_task | workflow_step
```

返回字段与用法：

| 字段 | 含义 | 你要做的动作 |
|---|---|---|
| `required_skills` | 本任务必须加载的 Skill（含通用壳 `basic-operations-preflight`） | 动手前逐个加载，未加载不得执行写操作 |
| `primary_skill` | 推断的主 Skill | 优先阅读 |
| `top_pitfalls` | 命中的线上知识库公共经验（≤3 条，跨 Agent 共享） | 逐条对照，避免重复踩别的 Agent 的坑 |
| `preflight_checklist` | 5 条通用铁律自检项 | 提交前逐条复述确认 |
| `references` | 关联知识库条目 `{type,id,title}` | 需要时按 id 查原文 |

`top_pitfalls` 来源已从"本地 seed 文件"升级为**线上知识库**（`category='pitfall'`、`status='approved'`）：任何 Agent 把坑沉淀进知识库，命中关键词的任务就会自动带出，实现"一人踩坑、全员免疫"。

### 5 条通用铁律（写操作前必过）

1. **身份校验**：确认当前 `claw_id` 与 token 前缀一致。
2. **读写分离**：验证只用 GET，禁止用 POST/PATCH/DELETE 做验证。
3. **提交后回读**：写操作后用独立 GET 确认真实落库（不信返回体）。
4. **查证再填**：枚举/ID 字段必须从 API options 取，禁止凭记忆手打。
5. **配置校验**：配置变更后比对 hash，防止配置漂移。

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
  group_profile=1        分组模式：把「项目基本信息」单独返回（见下）

返回：
  - 默认：知识条目数组（最多 200 条），每条含 is_project_profile 布尔
  - group_profile=1：{ project_profile, has_project_profile, target_project, entries, count }
    · project_profile = 当前项目的《项目基本信息》条目（category=project_profile），无则 null
    · target_project 未显式传 project 时按 Bearer Token 的项目自动推断
    · has_project_profile=false ⇒ 该项目尚未完成接入第一步（缺项目基本信息）
```

> 📌 **项目基本信息（特别标识）**：每个项目接入 Hub 的必备身份卡，用保留分类 `category=project_profile`、`scope=project` 标识。Agent 拉知识库列表建议带 `?group_profile=1`，即可拿到单独的 `project_profile`（含 TAPD / 前后端仓库 / 分支 / 协作平台），其余知识走汇总 `entries`。

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

> 🔑 **统一字段约定（v2.1）**：以下字段约定适用于 4 套 review 接口
> （`/skills/{id}/review`、`/rules/{id}/review`、`/knowledge/{id}/review`、`/agent-templates/{id}/review`），后端自动归一化。
>
> | 字段名 | 推荐 | 兼容写法 |
> |---|---|---|
> | 动作 | `action`: `approve` / `revise` / `reject` | `review_status`: `approved` / `revise` / `rejected`；或 `status`（agent_templates） |
> | 评论 | `comment` | `review_comment`（skill/rule）/ `notes`（knowledge 老字段） |
>
> ⚠️ 历史教训（MEMORY #131）：旧版 4 套接口**字段名各不相同**，
> 调 `/skills/{id}/review` 时若按 knowledge 文档传 `action`+`comment` 会被吞掉
> （状态变更但 review_comment 永远是空字符串）。**v2 后字段全统一，旧字段名仅作兼容保留**。

```
POST /api/v1/knowledge/{id}/review

请求体（v2 推荐）：
{
  "action": "approve",                // approve / revise / reject
  "comment": "示例不够，请补充2个真实案例"  // revise/reject 时必填
}

兼容旧写法（依然能用）：
{
  "review_status": "revise",
  "review_comment": "..."   // 或 "notes": "..."
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

**评审记录时间线**（v2.1 起所有 4 套接口都会写入）：每一次审核动作都会写入 `review_comments` 表（resource_type=knowledge|skill|rule, resource_id=资源 id），可调：
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
