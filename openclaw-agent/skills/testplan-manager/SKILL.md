---
name: testplan-manager
description: 管理 Hub 测试计划、周期任务、执行实例、用例进度、报告，以及 AgentTask 条件等待与自动续跑。
---

# 测试计划管理 (testplan-manager)

## 简介

本 Skill 用于管理 Hub 中心的测试计划排期和测试任务，支持完整的测试项目管理流程：

- **测试计划 CRUD**：创建/查询/编辑/删除测试计划，关联项目与 TAPD 迭代
- **测试任务 CRUD**：在计划下创建/管理测试任务，关联用例库，并按 owner 分配任务
- **用例执行追踪**：实时跟踪每个用例的执行状态（通过/失败/阻塞/跳过）
- **进度上报**：OpenClaw 通过 API 上报任务进度和用例执行结果
- **TAPD 迭代关联**：测试计划支持多选 TAPD 迭代，自动查询迭代列表
- **用例库节点筛选**：创建任务时支持按目录节点和优先级筛选用例

---

## 前置条件

- 已完成注册流程并启动 `hub-sse-sidecar`（SSE 通信链路在线）
- 拥有有效的 `HUB_API_TOKEN` 和 `CLAW_ID`

## Hub 地址

```
Hub 地址: http://clawteam.woa.com:18800
API 前缀: /api/v1
```

## 认证方式

```
Authorization: Bearer {HUB_API_TOKEN}
Content-Type: application/json
```

---

## 数据结构

### 测试计划 (TestPlan)

```json
{
  "id": 1,
  "name": "v3.2 常规版本测试计划",
  "description": "覆盖 v3.2 版本所有功能测试",
  "version_type": "regular",
  "version_name": "v3.2.1",
  "start_date": "2026-04-15",
  "end_date": "2026-04-30",
  "project_id": 1,
  "project_name": "QQ飞车",
  "tapd_iteration_ids": ["10001", "10002"],
  "tapd_iteration_names": ["Sprint 3.2-1", "Sprint 3.2-2"],
  "tapd_workspace_id": "12345678",
  "status": "active",
  "total_tasks": 5,
  "completed_tasks": 2,
  "total_bugs": 3,
  "resolved_bugs": 1,
  "progress": 40.0,
  "created_by": "龙虾王",
  "created_at": "2026-04-15 10:00:00"
}
```

**版本类型**：`regular`（常规版本）、`resource`（资源版本）、`hotfix`（紧急补丁）
**状态**：`draft`（草稿）、`active`（进行中）、`completed`（已完成）、`archived`（已归档）

### 测试任务 (TestTask)

```json
{
  "id": 1,
  "plan_id": 1,
  "name": "功能测试-主流程",
  "description": "覆盖核心主流程功能测试",
  "task_type": "functional",
  "assignee_claw_id": 3,
  "assignee_owner": "rajqiu",
  "assignee_owner_display_name": "rajqiu",
  "start_date": "2026-04-16",
  "end_date": "2026-04-20",
  "priority": "P1",
  "library_id": 2,
  "library_name": "登录模块用例库",
  "case_filter": {
    "module_paths": ["登录模块", "支付模块/退款"],
    "priorities": ["P0", "P1"]
  },
  "status": "in_progress",
  "progress": 60,
  "result_summary": "主流程功能基本正常，发现2个Bug",
  "total_cases": 20,
  "passed_cases": 12,
  "failed_cases": 2,
  "blocked_cases": 1,
  "skipped_cases": 0,
  "tapd_bug_ids": ["12345", "12346"],
  "bug_count": 2,
  "created_by": "龙虾王",
  "created_at": "2026-04-16 09:00:00"
}
```

**任务类型**：`functional`（功能）、`automation`（自动化）、`activity`（活动）、`performance`（性能）、`compatibility`（兼容性）、`security`（安全）、`interface`（接口）、`other`（其他）
**优先级**：`P0`（紧急）、`P1`（高）、`P2`（中）、`P3`（低）
**状态**：`assigned`（新分配）、`pending`（待开始）、`in_progress`（进行中）、`completed`（已完成）、`blocked`（阻塞）、`skipped`（跳过）

**状态流转**：
```
assigned（新分配，环境未就绪，不可执行）
    ↓ 管理员/系统扭转（环境就绪）
pending（待开始，可以执行了）
    ↓ Agent 开始执行
    ↓ report { "status": "in_progress" }
in_progress（进行中）
    ↓ 执行完成
completed / blocked / skipped
```

> ❗ 重要：当任务状态为 `assigned` 时，表示测试环境还未准备好，**不应开始执行**。
> 只有状态变为 `pending` 后才可以开始测试。
> 状态变化时会通过待办任务系统发送通知，请关注待办列表中 `task_category=test_task` 的任务。

### 团队主 Agent 直接派发非 Flow 任务

团队测试计划中，若任务无需启动 Workflow，测试经理作出执行决策后必须创建正式的普通
`AgentTask`，不能只创建 Todo。Todo 只负责可见性提醒，不提供领取、租约、进度或终态回执。

```
POST /api/v1/test-plans/{PLAN_ID}/supervision/agent-tasks

{
  "command_key": "plan-50-task-228-dispatch-v1",
  "test_task_id": 228,
  "instruction": "执行每日 Bug 回归并回写结构化结论与证据",
  "retry_max": 1
}
```

- 只有计划绑定团队的主测试经理 Agent 可以调用；不要求 Plan Supervisor 当前持有短期 Turn
  租约，因此某个 Flow/Stage 阻断不会误伤独立任务。
- Hub 从不可变 Stage 解析执行 Agent，调用方不能覆盖执行者。
- 相同 `command_key` 幂等重放返回原 `AgentTask`；任务被 Worker 领取后，测试任务才从
  `pending/assigned` 进入 `in_progress`。
- Worker 完成普通 `AgentTask` 后，Hub 自动同步测试任务和 Mission Stage 终态；不要让
  Agent 另行拼接进度/终态回写请求。

#### 当日目标、条件等待与自动续跑

周期任务派发的 payload 会包含 `test_task_occurrence_id`、`execution_goal`、
`checkpoint`、`resume_contract`、`resume_fencing_token`。执行 Agent 对这个当日
occurrence 持续负责，直到完成、到达 `due_at`，或进入真正需要人工决策的终态。

登录态、设备、WDA、构建、账号池、服务器或资源租约暂时未就绪时，不要返回
`blocked`。通过普通 AgentTask 结果接口返回 `waiting_condition`：

```json
{
  "status": "waiting_condition",
  "result": {
    "summary": "设备与 WDA 已就绪，等待微信登录态恢复",
    "checkpoint": {
      "completed_scope": ["device_ready", "wda_ready"],
      "remaining_steps": ["enter_minigame", "collect_performance"],
      "side_effect_receipts": []
    },
    "resume_contract": {
      "conditions": [{
        "condition_type": "login_session_ready",
        "condition_scope": {"device_id": "...", "app": "wecom"},
        "probe_operation": "ios.wecom.login_session_ready_v1"
      }],
      "probe_interval_seconds": 120,
      "resume_from_checkpoint": "enter_minigame",
      "owner_gate": true,
      "human_action": "请在测试设备上完成微信登录"
    }
  }
}
```

- `waiting_condition` 是非终态；Hub 保存 Checkpoint/ResumeContract，Worker 启动
  allowlist 内的无 LLM 轻量探针。不要用 Codex/Hermes 轮询。
- `owner_gate=true` 仅表示需要一次人工动作通知；人工完成后不需要经理重新下令。
- 探针发现条件变化后调用：

```
POST /api/v1/test-plans/{PLAN_ID}/supervision/occurrences/{OCCURRENCE_ID}/condition-events

{
  "command_key": "occ-301-login-ready-observation-17",
  "condition_type": "login_session_ready",
  "condition_ready": true,
  "expected_resume_fencing_token": 0,
  "observed_at": "2026-09-23T23:30:00+08:00",
  "facts": {"page": "home", "entry_visible": true}
}
```

条件全部满足后，Hub 会递增 `resume_fencing_token`，创建同一 occurrence 的新
AgentTask attempt，并把 Checkpoint 注入 payload。只能在新 claim/fence 下继续；旧
attempt 不得再产生副作用。到 `due_at` 仍未恢复时，Hub 以已完成范围和缺失范围收口为
`ANALYSIS_INCOMPLETE`，次日 occurrence 独立开始。

### 用例执行记录 (TestTaskCase)

```json
{
  "id": 1,
  "task_id": 1,
  "case_id": 5,
  "case_title": "验证密码错误提示",
  "case_priority": "P1",
  "status": "passed",
  "executed_at": "2026-04-16 14:30:00",
  "executed_by": "小测",
  "note": "",
  "tapd_bug_id": null
}
```

**执行状态**：`pending`（待执行）、`passed`（通过）、`failed`（失败）、`blocked`（阻塞）、`skipped`（跳过）

### 用例筛选条件 (case_filter)

创建/编辑任务时，如果关联了用例库，可通过 `case_filter` 灵活筛选要导入的用例：

```json
{
  "module_paths": ["登录模块", "支付模块/退款"],
  "priorities": ["P0", "P1"],
  "case_ids": [1, 2, 3],
  "types": ["functional", "interface"]
}
```

| 字段 | 说明 |
|------|------|
| `module_paths` | 目录节点路径列表，支持前缀匹配（包含子目录用例） |
| `priorities` | 优先级列表，如 `["P0", "P1"]` |
| `case_ids` | 指定用例 ID 列表（精确匹配） |
| `types` | 用例类型列表 |

> 不设置 `case_filter` 或留空，则关联用例库的全部用例。

---

## 一、测试计划管理

### 1. 获取测试计划列表

```
GET /api/v1/test-plans

查询参数：
  project_id     按项目ID筛选
  status         按状态筛选（draft/active/completed/archived）
  version_type   按版本类型筛选（regular/resource/hotfix）
  search         搜索计划名称
```

**响应**：返回 TestPlan 数组。

### 2. 创建测试计划

```
POST /api/v1/test-plans

{
  "name": "v3.2 常规版本测试计划",       // 必填
  "description": "覆盖所有功能测试",     // 选填
  "version_type": "regular",             // 选填，默认 regular
  "version_name": "v3.2.1",              // 选填
  "start_date": "2026-04-15",            // 必填，YYYY-MM-DD
  "end_date": "2026-04-30",              // 必填，YYYY-MM-DD
  "project_id": 1,                       // 选填，关联项目ID
  "tapd_iteration_ids": ["10001","10002"], // 选填，TAPD迭代ID列表
  "tapd_iteration_names": ["Sprint 3.2-1","Sprint 3.2-2"], // 选填，冗余展示
  "tapd_workspace_id": "12345678",       // 选填，TAPD workspace ID
  "status": "draft"                      // 选填，默认 draft
}
```

### 3. 获取测试计划详情（含任务列表）

```
GET /api/v1/test-plans/{PLAN_ID}
```

**响应**：返回 TestPlan 对象，包含 `tasks` 数组。

### 4. 更新测试计划

```
PUT /api/v1/test-plans/{PLAN_ID}

{
  "name": "新名称",
  "status": "active",
  "tapd_iteration_ids": ["10001"],
  "tapd_iteration_names": ["Sprint 3.2-1"]
}
```

### 5. 删除测试计划

```
DELETE /api/v1/test-plans/{PLAN_ID}
```

> 删除计划会级联删除其下所有测试任务和用例执行记录。

### 6. 获取测试计划统计

```
GET /api/v1/test-plans/{PLAN_ID}/stats
```

**响应**：
```json
{
  "id": 1,
  "name": "v3.2 测试计划",
  "total_tasks": 5,
  "completed_tasks": 2,
  "progress": 40.0,
  "total_bugs": 3,
  "by_type": {
    "functional": {"total": 3, "completed": 1},
    "performance": {"total": 2, "completed": 1}
  },
  "by_status": {
    "pending": 1,
    "in_progress": 2,
    "completed": 2
  }
}
```

### 7. 测试计划报告（支持多份）

测试计划报告由 Agent 编写，支持 Markdown/HTML 富文本。

#### 7.1 获取报告列表

```
GET /api/v1/test-plans/{PLAN_ID}/reports
```

响应：

```json
{
  "items": [
    {
      "id": 12,
      "plan_id": 1,
      "title": "v3.2 回归测试日报-第3天",
      "format": "markdown",
      "summary": "今日完成核心链路回归，发现2个高优先级问题...",
      "created_by": "Hermes Agent小赫",
      "created_at": "2026-05-07 14:20:00"
    }
  ],
  "total": 1
}
```

#### 7.2 获取单份报告详情

```
GET /api/v1/test-plans/{PLAN_ID}/reports/{REPORT_ID}
```

#### 7.3 创建报告

```
POST /api/v1/test-plans/{PLAN_ID}/reports

{
  "title": "v3.2 回归测试日报-第3天",
  "format": "markdown",        // markdown 或 html
  "content": "# 测试报告\n\n..."
}
```

#### 7.4 删除报告

```
DELETE /api/v1/test-plans/{PLAN_ID}/reports/{REPORT_ID}
```

#### 7.5 兼容旧接口（仍可用）

```
GET  /api/v1/test-plans/{PLAN_ID}/report
POST /api/v1/test-plans/{PLAN_ID}/report
PUT  /api/v1/test-plans/{PLAN_ID}/report
```

> 说明：旧接口会返回/写入“最新一份”报告；新接口用于完整历史管理（多份报告列表、详情、删除）。

建议 Agent 报告结构：

- 测试范围与版本信息
- 执行概况：任务数、用例通过/失败/阻塞/跳过
- 关键风险与未关闭问题
- TAPD Bug 摘要
- 发布建议与后续行动

---

## 二、TAPD 迭代关联

### 8. 查询 TAPD 迭代列表

创建计划时，选择项目后可查询该项目对应的 TAPD 迭代，支持多选关联。

```
GET /api/v1/test-plans/tapd-iterations

查询参数：
  project_id   项目ID（不传则返回所有有 workspace_id 的项目迭代）
```

**响应**：
```json
[
  {
    "project_id": 1,
    "project_name": "QQ飞车",
    "workspace_id": "12345678",
    "iterations": [
      {
        "id": "10001",
        "name": "Sprint 3.2-1",
        "status": "open",
        "startdate": "2026-04-14",
        "enddate": "2026-04-25"
      },
      {
        "id": "10002",
        "name": "Sprint 3.2-2",
        "status": "planning",
        "startdate": "2026-04-28",
        "enddate": "2026-05-09"
      }
    ]
  }
]
```

> 使用方式：选择项目 → 调此 API 获取迭代列表 → 用户多选迭代 → 将选中的 `id` 和 `name` 分别存入 `tapd_iteration_ids` 和 `tapd_iteration_names`。

---

## 三、测试任务管理

### 9. 获取计划下的任务列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks

查询参数：
  task_type          按任务类型筛选
  status             按状态筛选
  assignee_claw_id   按后台映射的执行 Agent ID 筛选
  assignee_owner     按 owner 筛选（username/display_name）
```

### 10. 创建测试任务

```
POST /api/v1/test-plans/{PLAN_ID}/tasks

{
  "name": "功能测试-主流程",              // 必填
  "description": "覆盖核心流程",          // 选填
  "task_type": "functional",             // 选填，默认 functional
  "priority": "P1",                      // 选填，默认 P2
  "assignee_claw_id": 3,                 // 选填，后台执行 Agent ID
  "assignee_owner": "rajqiu",            // 选填，不传 assignee_claw_id 时可用用户解析到对应 Claw
  "library_id": 2,                       // 选填，关联用例库
  "case_filter": {                       // 选填，用例筛选条件
    "module_paths": ["登录模块"],
    "priorities": ["P0", "P1"]
  },
  "start_date": "2026-04-16",            // 选填
  "end_date": "2026-04-20",              // 选填
  "status": "pending"                    // 选填，默认 pending
}
```

> 如果指定了 `library_id` 和 `case_filter`，系统会自动根据筛选条件导入用例到任务中，创建 TestTaskCase 记录。
> 指派语义：业务上按 owner 分配和展示，不直接显示 Agent 名；底层仍以 `assignee_claw_id` 绑定执行 Agent，保证 Agent 可以拉取和上报任务。Hub 会从 OpenClaw 的 `owner` 自动返回 `assignee_owner`、`assignee_owner_display_name`、`assignee_owner_user_id`、`assignee_owner_wecom_userid`。如果创建/更新时只知道用户，可传 `assignee_owner` 或 `assignee_username`，系统会优先找该用户绑定的 Claw，再找 `OpenClawInstance.owner` 匹配的 Claw。

### 11. 获取任务详情

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}
```

**响应**：返回 TestTask 对象，包含 `task_cases` 用例执行列表。

### 12. 更新任务

```
PUT /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}

{
  "name": "新名称",
  "assignee_owner": "rajqiu",
  "status": "in_progress",
  "progress": 75,
  "result_summary": "已完成大部分测试"
}
```

### 13. 删除任务

```
DELETE /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}
```

---

## 四、用例执行追踪

### 14. 获取任务下的用例执行列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases

查询参数：
  status   按执行状态筛选
  page     页码，从 1 开始。页面展示建议传 page=1
  page_size 每页条数，页面固定 50，最大 200
```

不传 `page/page_size` 时兼容旧版，直接返回数组。传分页参数时返回：

```json
{
  "items": [],
  "total": 125,
  "page": 1,
  "page_size": 50,
  "pages": 3
}
```

### 15. 更新单条用例执行状态

```
PUT /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases/{TC_ID}

{
  "status": "failed",              // 必填：pending/passed/failed/blocked/skipped
  "note": "断言失败：期望200，实际500",
  "tapd_bug_id": "12345"
}
```

### 16. 批量更新用例执行状态

```
POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/cases/batch

{
  "updates": [
    {"tc_id": 1, "status": "passed"},
    {"tc_id": 2, "status": "failed", "note": "超时"},
    {"tc_id": 3, "status": "blocked", "tapd_bug_id": "12346"}
  ]
}
```

---

## 五、OpenClaw 进度上报 API

以下接口专供 OpenClaw（龙虾/Agent）通过 Token 认证上报任务进度。

### 17. 获取指派给 OpenClaw 的测试任务

```
GET /api/v1/openclaws/{CLAW_ID}/test-tasks

查询参数：
  status     按状态筛选
  plan_id    按计划筛选
```

**响应**：
```json
{
  "tasks": [
    {
      "id": 1,
      "plan_id": 1,
      "plan_name": "v3.2 测试计划",
      "name": "功能测试-主流程",
      "status": "in_progress",
      "progress": 60,
      "task_cases": [...]
    }
  ],
  "count": 1
}
```

### 18. 上报任务进度

```
POST /api/v1/openclaws/{CLAW_ID}/test-tasks/{TASK_ID}/report

{
  "status": "in_progress",            // 选填：更新任务状态
  "progress": 75,                     // 选填：更新进度百分比
  "result_summary": "执行摘要...",     // 选填：执行结果摘要
  "case_updates": [                   // 选填：批量更新用例状态
    {"case_id": 5, "status": "passed"},
    {"case_id": 6, "status": "failed", "note": "超时"},
    {"case_id": 7, "status": "blocked", "tapd_bug_id": "12345"}
  ],
  "tapd_bug_ids": ["12345", "12346"]  // 选填：关联 Bug ID 列表（增量追加）
}
```

> `tapd_bug_ids` 是增量追加，不会覆盖已有 Bug 关联。

---

## 六、用例库目录树 & 筛选 API

创建测试任务时，可查看用例库的目录结构和筛选用例。

### 18. 获取用例库目录树

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/tree
```

**响应**：
```json
{
  "library_id": 2,
  "library_name": "登录模块用例库",
  "total_cases": 25,
  "directories": [
    "登录模块",
    "登录模块/手机号登录",
    "登录模块/微信登录",
    "支付模块",
    "支付模块/退款"
  ],
  "priority_counts": {
    "P0": 3,
    "P1": 8,
    "P2": 10,
    "P3": 4
  },
  "tree": [
    {
      "name": "登录模块",
      "path": "登录模块",
      "count": 15,
      "children": [
        {"name": "手机号登录", "path": "登录模块/手机号登录", "count": 8, "children": []},
        {"name": "微信登录", "path": "登录模块/微信登录", "count": 7, "children": []}
      ]
    }
  ]
}
```

### 19. 获取用例库筛选后的用例列表

```
GET /api/v1/testcase-libraries/{LIBRARY_ID}/cases

查询参数：
  module_path   目录路径（前缀匹配，包含子目录）
  priority      优先级筛选（可多个，如 priority=P0&priority=P1）
  type          类型筛选（可多个）
  case_id       用例ID筛选（可多个）
```

> 此 API 用于创建任务前预览将导入的用例范围。

---

## 七、测试任务报告

测试任务报告与测试计划报告类似，每个任务支持多份报告，由 Agent 或人工编写。

### 20. 获取任务报告列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/reports
```

**响应**：
```json
{
  "items": [
    {
      "id": 1,
      "task_id": 5,
      "title": "功能测试执行报告-第2天",
      "format": "markdown",
      "summary": "今日完成登录模块回归，发现1个P1问题...",
      "created_by": "小赫",
      "created_by_name": "Hermes Agent小赫",
      "created_at": "2026-05-10 15:30:00",
      "updated_at": "2026-05-10 15:30:00"
    }
  ],
  "total": 1
}
```

### 21. 获取单份任务报告详情

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/reports/{REPORT_ID}
```

### 22. 创建任务报告

```
POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/reports

{
  "title": "功能测试执行报告-第2天",    // 必填
  "content": "# 测试报告\n\n## 执行概况\n- 执行用例 30 条\n- 通过 28 条\n- 失败 2 条\n\n## 问题清单\n...",  // 必填
  "format": "markdown"                // 选填，默认 markdown，支持 markdown/html
}
```

### 23. 删除任务报告

```
DELETE /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/reports/{REPORT_ID}
```

建议 Agent 任务报告结构：

- 执行概况：用例通过/失败/阻塞/跳过数
- 发现问题清单（含 Bug 链接）
- 风险与阻塞项
- 下一步计划

---

## 八、测试任务 Bug 上报

每个测试任务支持多次 Bug 上报，通用参数为 `total_bugs`（总 Bug 数）+ `content`（markdown/html 自由格式详情）。

### 24. 获取任务 Bug 上报列表

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/bug-reports
```

**响应**：
```json
{
  "items": [
    {
      "id": 1,
      "task_id": 5,
      "total_bugs": 3,
      "content": "## Bug 列表\n\n| # | 标题 | 严重级别 | 状态 |\n|---|------|---------|------|\n| 1 | 登录超时无提示 | P1 | open |\n| 2 | 支付页面闪退 | P0 | open |\n| 3 | 头像加载失败 | P2 | fixed |",
      "format": "markdown",
      "created_by": "小赫",
      "created_at": "2026-05-10 16:00:00"
    }
  ],
  "total": 1,
  "total_bugs": 3
}
```

> `total_bugs` 是所有上报记录的 Bug 数汇总。

### 25. 上报任务 Bug 列表

```
POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/bug-reports

{
  "total_bugs": 3,                   // 本次上报的 Bug 总数
  "content": "## Bug 列表\n\n...",   // markdown/html 自由格式
  "format": "markdown"               // 选填，默认 markdown
}
```

> 上报后会同步更新任务的 `bug_count` 字段，并重新计算计划统计。

### 26. 删除 Bug 上报记录

```
DELETE /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/bug-reports/{REPORT_ID}
```

---

## 九、TAPD Bug 关联

### 27. 查询任务关联的 TAPD Bug 详情

```
GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/tapd-bugs
```

**响应**：
```json
{
  "bugs": [
    {
      "id": "12345",
      "title": "登录页面白屏",
      "status": "open",
      "severity": "fatal",
      "priority": "high",
      "current_owner": "开发者A"
    }
  ],
  "count": 1
}
```

---

## 推荐工作流

### 创建测试计划

```
1. 获取项目列表 → GET /api/v1/projects
2. 查询 TAPD 迭代 → GET /api/v1/test-plans/tapd-iterations?project_id=X
3. 创建计划 → POST /api/v1/test-plans（含迭代关联）
4. 查询用例库 → GET /api/v1/testcase-libraries
5. 查看用例库目录树 → GET /api/v1/testcase-libraries/{ID}/tree
6. 创建测试任务 → POST /api/v1/test-plans/{ID}/tasks（含用例库关联和筛选）
7. 指派给 owner → 优先设置 assignee_owner；Hub 会映射到 assignee_claw_id
```

### OpenClaw 执行测试任务

```
1. 查询自己的任务 → GET /api/v1/openclaws/{CLAW_ID}/test-tasks
2. 检查任务状态：
   - status=assigned → 环境未就绪，不可执行，等待通知
   - status=pending → 可以开始执行
3. 获取任务详情（含用例） → GET /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}
4. 开始执行，上报进度 → POST /api/v1/openclaws/{CLAW_ID}/test-tasks/{TASK_ID}/report
   { "status": "in_progress", "progress": 10 }
5. 批量更新用例状态 → case_updates 数组
6. 发现 Bug 时上报 → POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/bug-reports
   { "total_bugs": 3, "content": "## Bug列表\n...", "format": "markdown" }
7. 任务完成 → report { "status": "completed", "progress": 100 }
8. 编写任务报告 → POST /api/v1/test-plans/{PLAN_ID}/tasks/{TASK_ID}/reports
   { "title": "执行报告", "content": "# 报告\n...", "format": "markdown" }
```

> 📌 状态变化通知：当任务状态从 assigned 变为 pending 时，系统会自动创建一条待办任务通知你。
> 请关注待办列表中 `task_category=test_task` 的任务，处理完后记得 complete 待办。

### API 创建任务（灵活筛选）

```
1. 查询用例库目录树 → GET /api/v1/testcase-libraries/{ID}/tree
2. 预览筛选结果 → GET /api/v1/testcase-libraries/{ID}/cases?priority=P0
3. 创建任务并指定筛选 → POST /api/v1/test-plans/{ID}/tasks
   {
     "library_id": 2,
     "case_filter": {"priorities": ["P0"], "module_paths": ["登录模块"]}
   }
```

---

## 错误处理

| HTTP 状态码 | 说明 | 处理方式 |
|-------------|------|----------|
| 200 | 成功 | 正常处理 |
| 201 | 创建成功 | 正常处理 |
| 400 | 参数错误 | 检查必填字段 |
| 401 | Token 缺失 | 检查 `HUB_API_TOKEN` |
| 403 | 权限不足 / Token 无效 | 确认角色权限 |
| 404 | 资源不存在 | 检查计划/任务 ID |
| 500 | 服务器错误 | 查看 Hub 日志 |

---

## 任务链（Task Chain）

任务链是一连串有序任务，分配给多人或多 Agent。前置任务提交后自动推进到下一步，下一个 Agent 会收到待办通知。

### 适用场景

- 商业化活动模块用例设计：服务端代码分析 → 客户端分析 → 需求分析 → 用例设计
- 跨团队协作测试：环境搭建 → 数据准备 → 功能测试 → 性能测试
- 任何需要多人串行协作的流程

### 创建任务链

```
POST /api/v1/test-plans/{PLAN_ID}/task-chains

{
  "name": "商业化活动模块用例设计",
  "description": "从代码分析到用例设计的完整流程",
  "priority": "P1",
  "steps": [
    {
      "name": "服务端代码分析",
      "description": "分析商业化模块服务端代码结构和关键逻辑",
      "task_type": "other",
      "assignee_claw_id": 3
    },
    {
      "name": "客户端代码分析",
      "description": "分析商业化模块客户端代码",
      "task_type": "other",
      "assignee_claw_id": 5
    },
    {
      "name": "需求分析",
      "description": "结合代码分析结果进行需求分析",
      "task_type": "other",
      "assignee_claw_id": 6
    },
    {
      "name": "用例设计",
      "description": "基于前置分析结果设计测试用例",
      "task_type": "functional",
      "assignee_claw_id": 3
    }
  ]
}
```

> 至少需要 2 个步骤。支持 `assignee_owner` 代替 `assignee_claw_id`。

### 启动任务链

```
POST /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}/start
```

启动后第一步的 assignee 会收到待办通知。

### 提交步骤结果（自动推进）

```
POST /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}/steps/{STEP_ID}/submit

{
  "result_summary": "分析完成，发现3个关键模块...",
  "output_data": {"key_modules": ["payment", "reward", "shop"]}
}
```

> 提交后自动激活下一步，下一个 Agent 收到待办通知（包含上一步的结果摘要）。
> 所有步骤完成后，任务链状态自动变为 `completed`。

### 查看任务链

```
GET /api/v1/test-plans/{PLAN_ID}/task-chains
GET /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}
```

### 更新任务链/步骤

```
PUT /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}
PUT /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}/steps/{STEP_ID}
```

### 保存任务链执行结论（支持富文本）

```
PUT  /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}/conclusion
POST /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}/conclusion

{
  "execution_conclusion": "# 任务链执行结论\n\n总体完成度 95%，剩余风险...",
  "conclusion_format": "markdown"   // markdown 或 html
}
```

### 删除任务链

```
DELETE /api/v1/test-plans/{PLAN_ID}/task-chains/{CHAIN_ID}
```

### 任务链状态流转

```
draft → active → completed
         ↕
       paused
         ↓
      cancelled
```

### 步骤状态流转

```
waiting → pending → in_progress → completed
                                → failed
                                → skipped
```

- `waiting`：等待前置步骤完成
- `pending`：已激活，等待 Agent 开始执行
- `in_progress`：Agent 正在执行
- `completed`：已提交结果

---

## 触发词

- "创建测试计划"
- "查看测试计划"
- "测试排期"
- "创建测试任务"
- "指派测试任务"
- "查看任务进度"
- "上报测试进度"
- "更新用例状态"
- "测试计划管理"
- "测试任务管理"
- "关联 TAPD 迭代"
- "P0 用例任务"
- "查看我的任务"
- "创建任务链"
- "启动任务链"
- "提交任务链步骤"
- "编写任务报告"
- "查看任务报告"
- "上报Bug"
- "Bug列表"
- "查看Bug上报"
