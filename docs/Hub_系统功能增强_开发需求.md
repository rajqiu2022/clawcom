# 🛠 Hub 系统功能增强 — 测试经理全流程支撑开发需求

> **提出人**: 小马（需求代码分析专员，claw_id=12）
> **目标**: 补齐 Hub 系统缺失的功能，使测试经理（小赫/claw_id=10）能在 Hub 上完成完整的测试计划编排、需求-代码-用例-Bug 全链路追溯、测试进度管控
> **日期**: 2026-04-27

---

## 一、背景

当前 Hub 系统已有：
- ✅ 需求管理（Requirement CRUD + TAPD 迭代快照 + 变更日志）
- ✅ 测试用例库管理（testcase-manager Skill，含 CRUD/批量/目录/版本快照/共享/评审）
- ✅ 测试计划管理（testplan-manager Skill，含计划CRUD/任务/进度/迭代关联）
- ✅ 用例关联字段（testcase_links，可在需求详情页建立绑定）
- ✅ 工程变更关联字段（engineering_links，基于 tapd_story_id 自动关联）
- ✅ Agent 管理（openclaws CRUD）

**但存在以下缺失，导致测试经理无法完成完整的测试全流程管理：**

---

## 二、功能缺失清单及开发需求

### 【P0】需求-代码关联管理 API

**问题**：`requirement_engineering_links` 表存在（requirement-analysis SKILL 中有提及），但**没有公开的读写 API** 供测试经理或 AI Agent 直接管理。目前仅靠 TAPD story_id 自动关联，无法手工建立需求与代码模块、函数之间的映射。

**需求**：
```
POST   /api/v1/requirements/items/<id>/engineering-links     — 建立需求与工程变更/代码模块的关联
GET    /api/v1/requirements/items/<id>/engineering-links     — 查询关联的工程变更列表
DELETE /api/v1/requirements/items/<id>/engineering-links/<link_id>  — 删除关联
```

**关联字段**（每个 link 至少包含）：
- `requirement_item_id` — 需求项 ID
- `engineering_change_id` — 工程变更项 ID（可选，用于关联到具体的提交/变更）
- `code_module` — 代码模块名称（如 "结算系统"、"无尽模式"）
- `code_file` — 代码文件路径
- `function_name` — 函数/方法名称（粒度到函数级别）
- `mapping_type` — 映射类型：`direct`（直接实现）、`partial`（部分覆盖）、`dependency`（依赖关系）
- `note` — 备注

---

### 【P0】代码模块/工程目录管理 API

**问题**：无法在 Hub 上登记项目的工程代码模块结构。测试经理做工程分析后，需要有个地方录入"这个项目的服务端分为哪些模块、客户端分为哪些模块"，并关联到需求和用例。

**需求**：
```
POST   /api/v1/projects/<id>/code-modules        — 创建代码模块
GET    /api/v1/projects/<id>/code-modules        — 查询项目代码模块树
PUT    /api/v1/projects/<id>/code-modules/<id>   — 更新模块
DELETE /api/v1/projects/<id>/code-modules/<id>   — 删除模块
```

**模块字段**：
- `name` — 模块名称（如 "结算系统"）
- `parent_id` — 父模块 ID（支持树形结构）
- `description` — 模块描述
- `code_path` — 代码目录路径
- `language` — 编程语言
- `technology` — 技术栈（如 "C#/Unity"、"Go/Kitex"）
- `owner` — 负责人
- `tags` — 标签数组

---

### 【P1】Bug 管理模块（完整 CRUD）

**问题**：测试经理需要录入、跟踪 Bug，并关联到需求、用例、测试任务。目前 Hub 上 `/api/v1/bugs` 接口返回空数据，不确定是未实现还是无数据。

**需求**：
```
GET    /api/v1/bugs?requirement_id=&testcase_id=&plan_id=&task_id=&status=&severity=&assignee=&limit=&offset=
POST   /api/v1/bugs
GET    /api/v1/bugs/<id>
PUT    /api/v1/bugs/<id>
DELETE /api/v1/bugs/<id>
```

**Bug 字段**：
- `title` — 标题
- `description` — 描述（含复现步骤/预期/实际/截图）
- `severity` — 严重级别：`P0-blocker` / `P1-critical` / `P2-major` / `P3-minor` / `P4-trivial`
- `status` — 状态：`new` / `confirmed` / `in_progress` / `resolved` / `reopened` / `closed`
- `requirement_id` — 关联需求（必填，强制追溯）
- `testcase_id` — 关联用例（可选）
- `plan_id` — 关联测试计划
- `task_id` — 关联测试任务
- `assignee_claw_id` — 指派的 Agent ID
- `module_path` — 所属模块路径
- `attachments` — 附件（截图/日志）
- `tags` — 标签（如 `weaknet` / `compatibility` / `performance`）

---

### 【P1】测试任务-需求关联 API

**问题**：`testplan-manager` Skill 中的测试任务（Task）没有字段关联需求。当前任务只有 `library_id` 关联用例库，但不知道这个任务覆盖了哪些需求。测试经理无法知道"任务 A 测了哪些需求、哪些需求还没被覆盖"。

**需求**：
- 在 Task 模型中增加 `requirement_ids: [int]` 字段
- 提供 API：
```
POST   /api/v1/test-plans/<plan_id>/tasks/<task_id>/requirements     — 批量关联需求
GET    /api/v1/test-plans/<plan_id>/tasks/<task_id>/requirements     — 查询任务覆盖的需求列表
```

---

### 【P1】测试进度看板/统计 API

**问题**：测试经理需要全局了解"哪些需求已设计用例、哪些已测、哪些有 Bug、覆盖率多少"。目前只能逐个 API 查询。

**需求**：
```
GET /api/v1/statistics/projects/<id>/test-progress
```

**返回字段**：
```json
{
  "project_id": 6,
  "total_requirements": 94,
  "requirements_with_cases": 50,
  "requirements_tested": 30,
  "requirements_blocked": 5,
  "total_cases": 469,
  "cases_executed": 200,
  "cases_passed": 180,
  "cases_failed": 15,
  "total_bugs": 20,
  "bugs_open": 8,
  "bugs_resolved": 12,
  "progress_percentage": 68.5
}
```

---

### 【P1】测试计划版本快照

**问题**：当前测试计划 `0.1MVP版本` 的结束日期是 5/9，但 M1 交付是 4/30。测试经理需要能在版本发布节点"冻结"测试状态（当时的用例覆盖、Bug 清单、执行结果），作为发布报告的依据。

**需求**：
```
POST   /api/v1/test-plans/<id>/snapshots          — 创建测试计划快照（冻结当前状态）
GET    /api/v1/test-plans/<id>/snapshots          — 查看快照列表
GET    /api/v1/test-plans/snapshots/<id>          — 查看快照详情
```

---

### 【P2】Agent 任务分配与协作状态

**问题**：测试经理需要知道"哪个 Agent 在做什么任务、状态如何"。目前 `/api/v1/openclaws` 只返回基本信息（claw_id, name, role, status），没有当前任务上下文。

**需求**：
- 在 OpenClaw 模型中增加 `current_task_id`、`current_plan_id` 字段
- 提供：
```
GET /api/v1/openclaws/<id>/tasks  — 查看某个 Agent 的待办任务列表
```

---

### 【P2】测试报告模板与自动生成

**问题**：转测周结束后需要输出测试报告（覆盖率、通过率、遗留 Bug、风险项），目前需手动编写。

**需求**：
```
POST /api/v1/test-plans/<id>/report   — 根据测试计划执行数据自动生成测试报告
GET  /api/v1/test-plans/<id>/report   — 获取最新报告
```

**报告包含**：
- 需求覆盖率统计
- 用例执行情况
- Bug 统计分析
- 遗留风险
- 建议/结论

---

## 三、开发优先级建议

| 优先级 | 功能 | 预估工作量 | 依赖关系 |
|--------|------|-----------|---------|
| **P0** | 需求-代码关联管理 API | 2人天 | 依赖已有 engineering_links 表 |
| **P0** | 代码模块/工程目录管理 API | 1人天 | 新建表 |
| **P1** | Bug 管理模块（完整 CRUD） | 3人天 | 需新建 bug 表 |
| **P1** | 测试任务-需求关联 API | 1人天 | 依赖任务已有字段扩展 |
| **P1** | 测试进度看板统计 API | 2人天 | 依赖各模块数据 |
| **P1** | 测试计划版本快照 | 2人天 | 依赖任务执行数据 |
| **P2** | Agent 任务分配与协作状态 | 1人天 | 依赖 OpenClaw 模型扩展 |
| **P2** | 测试报告模板与自动生成 | 2人天 | 依赖以上全部 |

**总计**: 约 **14 人天**

---

## 四、总体架构关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                   测试经理全流程管控 Hub                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐ │
│  │ 需求管理  │───▶│ 代码模块  │───▶│ 用例管理  │───▶│ Bug管理  │ │
│  │ (已有)    │    │ [P0新增]  │    │ (已有)    │    │ [P1新增] │ │
│  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘ │
│       │               │               │               │        │
│       ▼               ▼               ▼               ▼        │
│  ┌────────────────────────────────────────────────────────┐     │
│  │              关联追踪层 (engineering_links             │     │
│  │              + testcase_links [P0增强])                │     │
│  └──────────────────────┬─────────────────────────────────┘     │
│                         │                                      │
│                         ▼                                      │
│  ┌────────────────────────────────────────────────────────┐     │
│  │              测试计划管理 (已有 + [P1增强])              │     │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐              │     │
│  │  │ 测试任务  │  │ 版本快照  │  │ 进度统计  │              │     │
│  │  │ [P1增强]  │  │ [P1新增]  │  │ [P1新增]  │              │     │
│  │  └──────────┘  └──────────┘  └──────────┘              │     │
│  └──────────────────────┬─────────────────────────────────┘     │
│                         │                                      │
│                         ▼                                      │
│  ┌────────────────────────────────────────────────────────┐     │
│  │              输出层                                     │     │
│  │  ┌──────────────────┐  ┌──────────────────┐            │     │
│  │  │ 测试报告 [P2新增]  │  │ 追溯矩阵 [手工]   │            │     │
│  │  └──────────────────┘  └──────────────────┘            │     │
│  └────────────────────────────────────────────────────────┘     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 五、说明

1. **已有但需增强的功能**：engineering_links/testcase_links 表已存在，但需要公开 API 供手动管理
2. **全新开发的功能**：代码模块管理、Bug 管理、进度统计、版本快照、测试报告
3. **手工/文件补充**：需求-代码映射矩阵（到函数级）当前用独立 Markdown 文件输出，等 API 就绪后再迁移到系统